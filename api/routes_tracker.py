"""
api/routes_tracker.py

Logovo Tracker — серверный API для мобильного приложения live-трекинга матчей
(iOS/Android). Приложение смотрит на экран FC Mobile, снимает с плашек счёт,
минуту и ключевые события и присылает их сюда; зрители видят ленту в
Веб-матч-центре (Telegram Mini App).

Авторизация двухступенчатая и не использует Telegram initData — у мобильного
приложения его просто нет:

  1. Игрок просит у бота одноразовый 4-значный ПИН (`/tracker` в личке).
     Код живёт 10 минут и привязан к telegram_id.
  2. Приложение меняет ПИН на Bearer-токен сессии (POST /api/tracker/auth/pair)
     и дальше шлёт его в заголовке `Authorization: Bearer <token>`.

Хранилище ПИН-кодов и сессий живёт в памяти процесса — ровно по той же причине,
что и лимиты в api/rate_limiter.py: бот разворачивается одним `worker`.
Перезапуск процесса гасит сессии, приложение переспрашивает ПИН и продолжает
работу; при переезде на несколько процессов эти два словаря меняются на общее
хранилище, а всё остальное остаётся прежним.

⚠️ Трекер НЕ ведёт официальный протокол. Он пишет только `live_match_states` и
ленту `live_events` (+ `matches.live_minute` для витрины). `matches.status`,
`matches.player1_score/player2_score` и очки турнира меняются исключительно
через бота, когда игрок сдаёт скриншот протокола. Поэтому здесь нет вызова
`services/live_state_machine.transition_live_match()`: он синхронизирует
`matches.status`, то есть увёл бы матч из активных матчей кабинета и из учёта
долгов.
"""

import asyncio
import base64
import binascii
import json
import logging
import secrets
import time
from typing import Any

from aiohttp import web

import config
import database
from api.auth import check_user_access
from services.live_state_machine import (
    FINISHED,
    HALFTIME,
    LIVE,
    PRE_MATCH,
    SCHEDULED,
    TERMINAL_STATES,
)

logger = logging.getLogger(__name__)

PIN_CODE_LENGTH = 4
_PIN_SPACE = 10 ** PIN_CODE_LENGTH
_PIN_GENERATION_ATTEMPTS = 64

TRACKER_PROVIDER = "tracker"

MAX_DEVICE_INFO_LEN = 120
MAX_PLAYER_NAME_LEN = 64
MAX_CLIENT_EVENT_ID_LEN = 64
MAX_MINUTE = 150

# Матчи, которые приложение вправе транслировать: протокол по ним ещё не сдан.
# Ровно три значения — запрос ниже подставляет их в литеральные `?, ?, ?`.
OPEN_MATCH_STATUSES = ("pending", "scheduled", "live")

# Период с экрана приложения -> метка периода в live_match_states.
PERIOD_ALIASES = {
    "PRE": "pre_match",
    "PRE_MATCH": "pre_match",
    "1H": "1h",
    "H1": "1h",
    "FIRST_HALF": "1h",
    "HT": "ht",
    "HALFTIME": "ht",
    "2H": "2h",
    "H2": "2h",
    "SECOND_HALF": "2h",
    "ET": "et",
    "EXTRA_TIME": "et",
    "PEN": "pen",
    "PENALTIES": "pen",
    "FT": "ft",
    "FULL_TIME": "ft",
}

# Тип события из приложения -> event_type в live_events (совпадает со словарём
# services/live_ingestion.py, чтобы лента матч-центра была однородной).
EVENT_TYPES = {
    "GOAL": "goal",
    "OWN_GOAL": "own_goal",
    "PENALTY": "penalty",
    "MISSED_PENALTY": "missed_penalty",
    "CARD": "yellow_card",  # уточняется полем card_type
    "YELLOW_CARD": "yellow_card",
    "RED_CARD": "red_card",
    "SUBSTITUTION": "substitution",
    "INJURY": "injury",
    "KICKOFF": "match_started",
    "HALFTIME": "halftime",
    "SECOND_HALF": "second_half",
}

# События, по которым фамилия игрока осмысленна и имеет смысл звать OCR.
PLAYER_EVENT_TYPES = ("goal", "own_goal", "penalty", "missed_penalty",
                      "yellow_card", "red_card", "substitution", "injury")

TEAM_SIDES = ("home", "away")

# {pin_code: {"telegram_id": int, "expires_at": float}}
_PIN_CODES: dict[str, dict[str, Any]] = {}
# {token: {"telegram_id": int, "device_info": str, "issued_at": float, "last_seen_at": float}}
_SESSIONS: dict[str, dict[str, Any]] = {}


class TrackerError(Exception):
    """Нарушение правила трекера с готовым HTTP-кодом и текстом для приложения."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"status": "error", "error": code, "message": message},
        status=status,
    )


# ─── ПИН-коды и сессии ────────────────────────────────────────────────────────

def _purge_expired(now: float) -> None:
    """Выбросить протухшие коды и остывшие сессии. Вызывается на каждом входе."""
    for code in [c for c, rec in _PIN_CODES.items() if rec["expires_at"] <= now]:
        del _PIN_CODES[code]

    ttl = config.TRACKER_SESSION_TTL_SECONDS
    if ttl > 0:
        for token in [t for t, s in _SESSIONS.items() if (now - s["last_seen_at"]) > ttl]:
            del _SESSIONS[token]


def issue_pin_code(telegram_id: int) -> tuple[str, int]:
    """
    Выдать игроку одноразовый ПИН для входа в приложение.

    Возвращает (код, время жизни в секундах). Прошлый код того же игрока
    аннулируется: одновременно живёт ровно один ПИН на пользователя, иначе
    старый код из переписки остался бы рабочим.
    """
    now = time.time()
    _purge_expired(now)

    for code in [c for c, rec in _PIN_CODES.items() if rec["telegram_id"] == int(telegram_id)]:
        del _PIN_CODES[code]

    ttl = int(config.TRACKER_PIN_TTL_SECONDS)
    for _ in range(_PIN_GENERATION_ATTEMPTS):
        code = f"{secrets.randbelow(_PIN_SPACE):0{PIN_CODE_LENGTH}d}"
        if code in _PIN_CODES:
            continue
        _PIN_CODES[code] = {"telegram_id": int(telegram_id), "expires_at": now + ttl}
        return code, ttl

    # Все 10 000 кодов заняты одновременно — такого быть не может, но молча
    # выдавать чужой код нельзя ни при каких обстоятельствах.
    raise RuntimeError("Не удалось подобрать свободный ПИН-код трекера.")


def consume_pin_code(code: str) -> int | None:
    """
    Обменять ПИН на telegram_id. Код одноразовый: любая попытка его сжигает,
    поэтому перебор не получает второй попытки по тому же коду.
    """
    now = time.time()
    _purge_expired(now)

    record = _PIN_CODES.pop(code, None)
    if not record or record["expires_at"] <= now:
        return None
    return int(record["telegram_id"])


def create_session(telegram_id: int, device_info: str | None) -> str:
    """Выдать Bearer-токен мобильной сессии."""
    now = time.time()
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {
        "telegram_id": int(telegram_id),
        "device_info": device_info or "",
        "issued_at": now,
        "last_seen_at": now,
    }
    return token


def get_session(token: str) -> dict[str, Any] | None:
    """Найти живую сессию по токену и продлить её."""
    now = time.time()
    _purge_expired(now)

    session = _SESSIONS.get(token)
    if not session:
        return None
    session["last_seen_at"] = now
    return session


def revoke_session(token: str) -> None:
    """Отозвать Bearer-токен сразу, не дожидаясь TTL. Отсутствующий токен — не ошибка:
    logout должен быть идемпотентным (повторный вызов с тем же токеном ничего не ломает)."""
    _SESSIONS.pop(token, None)


def reset_tracker_state() -> None:
    """Сбросить коды и сессии. Нужно тестам для изоляции."""
    _PIN_CODES.clear()
    _SESSIONS.clear()


def _bearer_token(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return ""
    return header[7:].strip()


def _require_session(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """
    Проверить Bearer-токен и rollout-гейт. Возвращает (session, error_response).

    Гейт доступа проверяется на КАЖДОМ запросе, а не только при спаривании:
    выданный до включения lockdown токен не должен пережить его включение.
    """
    token = _bearer_token(request)
    session = get_session(token) if token else None
    if not session:
        return None, _error(
            "unauthorized",
            "Сессия трекера недействительна. Получите новый код командой /tracker в боте.",
            401,
        )
    if not check_user_access(session["telegram_id"]):
        return None, _error("LOGOVO_LOCKDOWN", "Logovo.bet временно закрыт для пользователей.", 403)
    return session, None


# ─── Разбор и валидация тела запроса ──────────────────────────────────────────

async def _read_json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise TrackerError("invalid_json", "Тело запроса не является корректным JSON.", 400)
    if not isinstance(body, dict):
        raise TrackerError("invalid_json", "Ожидается JSON-объект.", 400)
    return body


def _coerce_int(value: Any) -> int | None:
    """int из JSON-числа или строки multipart-поля. None, если это не целое."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        raw = value.strip()
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _require_int(body: dict, key: str, minimum: int, maximum: int) -> int:
    value = _coerce_int(body.get(key))
    if value is None or value < minimum or value > maximum:
        raise TrackerError(
            f"invalid_{key}",
            f"Поле «{key}» должно быть целым числом от {minimum} до {maximum}.",
            400,
        )
    return value


def _clean_text(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def _match_id_from(body: dict) -> int:
    return _require_int(body, "match_id", 1, 2_147_483_647)


# ─── Доступ к матчу ───────────────────────────────────────────────────────────

def _match_side(row: Any, telegram_id: int, team_name: str) -> str | None:
    """
    Сторона игрока в матче: player1 — хозяева, player2 — гости.
    Матчи хранятся и по telegram_id, и по названию клуба, поэтому проверяются оба.
    """
    if row["player1_id"] is not None and int(row["player1_id"]) == int(telegram_id):
        return "home"
    if row["player2_id"] is not None and int(row["player2_id"]) == int(telegram_id):
        return "away"

    own = (team_name or "").strip().lower()
    if not own:
        return None
    if (row["player1_team"] or "").strip().lower() == own:
        return "home"
    if (row["player2_team"] or "").strip().lower() == own:
        return "away"
    return None


def _user_team(cursor, telegram_id: int) -> str:
    cursor.execute("SELECT team_name FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    return (row["team_name"] if row else None) or ""


def _load_match_for_user(cursor, match_id: int, telegram_id: int) -> tuple[dict, str]:
    """
    Достать матч и убедиться, что он принадлежит клубу авторизованного игрока.
    id матча приходит от клиента, поэтому владение проверяется здесь и всегда.
    """
    team = _user_team(cursor, telegram_id)
    cursor.execute(
        """
        SELECT id, status, round_number, player1_id, player2_id,
               player1_team, player2_team, division_id, season_id,
               player1_score, player2_score
        FROM matches WHERE id = ?
        """,
        (match_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise TrackerError("match_not_found", "Матч не найден.", 404)

    side = _match_side(row, telegram_id, team)
    if side is None:
        raise TrackerError("forbidden", "Этот матч не относится к вашему клубу.", 403)
    return dict(row), side


def _load_live_state(cursor, match_id: int) -> dict | None:
    cursor.execute("SELECT * FROM live_match_states WHERE match_id = ?", (match_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def _ensure_live_state(cursor, match: dict) -> dict:
    """Создать строку live-состояния, если трансляции по матчу ещё не было."""
    state = _load_live_state(cursor, match["id"])
    if state is not None:
        return state

    cursor.execute(
        """
        INSERT INTO live_match_states
            (match_id, season_id, division_id, status, period, minute, home_score, away_score, provider, last_updated_at)
        VALUES (?, ?, ?, ?, 'pre_match', 0, 0, 0, ?, datetime('now', '+3 hours'))
        """,
        (
            match["id"],
            match["season_id"] or 1,
            match["division_id"] or 1,
            SCHEDULED,
            TRACKER_PROVIDER,
        ),
    )
    return _load_live_state(cursor, match["id"])


def _audit(cursor, actor_id: int, action: str, match: dict, old_value: str, new_value: str) -> None:
    cursor.execute(
        """
        INSERT INTO bet_audit_log
            (actor_id, action, entity_type, entity_id, old_value, new_value, division_id, season_id, created_at)
        VALUES (?, ?, 'match', ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
        """,
        (actor_id, action, match["id"], old_value, new_value,
         match["division_id"] or 1, match["season_id"] or 1),
    )


def _live_snapshot(state: dict | None) -> dict | None:
    if not state:
        return None
    return {
        "status": state.get("status"),
        "period": state.get("period"),
        "minute": state.get("minute"),
        "score_home": state.get("home_score"),
        "score_away": state.get("away_score"),
        "version": state.get("version"),
        "last_updated_at": state.get("last_updated_at"),
    }


# ─── Синхронные операции с базой (выполняются в пуле потоков) ─────────────────

def _player_card(telegram_id: int) -> dict[str, Any]:
    """Карточка игрока для приложения: ник и клуб берутся только из базы."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT telegram_id, username, team_name, division_id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cursor.fetchone()

        division_name = None
        division_id = row["division_id"] if row else None
        if division_id:
            cursor.execute("SELECT name FROM divisions WHERE id = ?", (division_id,))
            div = cursor.fetchone()
            division_name = div["name"] if div else None

    return {
        "id": int(telegram_id),
        "nickname": (row["username"] if row else None) or f"id{telegram_id}",
        "club": (row["team_name"] if row else None),
        "division_id": division_id,
        "division_name": division_name,
        "registered": bool(row and row["team_name"]),
    }


def _fetch_open_matches(telegram_id: int) -> list[dict[str, Any]]:
    """Несыгранные матчи клуба: тур, соперник, дом/выезд, лига/кубок."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        team = _user_team(cursor, telegram_id)

        cursor.execute(
            """
            SELECT
                m.id, m.round_number, m.status, m.player1_id, m.player2_id,
                m.player1_team, m.player2_team, m.division_id, m.season_id,
                COALESCE(m.tournament_type, 'league') AS tournament_type,
                m.cup_stage, m.game_num_in_series, m.match_date, m.match_time,
                t.name AS tournament_name,
                u1.username AS player1_username, u2.username AS player2_username,
                lms.status AS live_status, lms.period AS live_period, lms.minute AS live_minute,
                lms.home_score AS live_home_score, lms.away_score AS live_away_score,
                lms.version AS live_version, lms.last_updated_at AS live_updated_at
            FROM matches m
            LEFT JOIN tournaments t ON t.id = m.tournament_id
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            LEFT JOIN live_match_states lms ON lms.match_id = m.id
            WHERE (
                    m.player1_id = ? OR m.player2_id = ?
                    OR (? <> '' AND (LOWER(m.player1_team) = LOWER(?) OR LOWER(m.player2_team) = LOWER(?)))
                  )
              AND m.status IN (?, ?, ?)
            ORDER BY m.round_number ASC, m.id ASC
            """,
            (telegram_id, telegram_id, team, team, team, *OPEN_MATCH_STATUSES),
        )
        rows = cursor.fetchall()

    if config.TRACKER_DEV_PIN_ENABLED and telegram_id == 777777 and not rows:
        return [
            {
                "id": 9991,
                "round_number": 6,
                "tour": 6,
                "tournament_name": "Премьер-Лига",
                "competition": "Премьер-Лига",
                "opponent_club": "Ливерпуль",
                "opponent_nickname": "Ахмед",
                "opponent_username": "ahmed_fc",
                "own_club": "Манчестер Сити",
                "is_home": True,
                "stage": "Тур 6",
                "live_status": "SCHEDULED",
            },
            {
                "id": 9992,
                "round_number": 7,
                "tour": None,
                "tournament_name": "Кубок Логова",
                "competition": "Кубок Логова",
                "opponent_club": "Реал Мадрид",
                "opponent_nickname": "Карим",
                "opponent_username": "karim_rm",
                "own_club": "Манчестер Сити",
                "is_home": False,
                "stage": "1/4 финала",
                "live_status": "SCHEDULED",
            },
            {
                "id": 9993,
                "round_number": 5,
                "tour": 5,
                "tournament_name": "Премьер-Лига (Долг)",
                "competition": "Премьер-Лига",
                "opponent_club": "Арсенал",
                "opponent_nickname": "Букайо",
                "opponent_username": "arsenal_gunner",
                "own_club": "Манчестер Сити",
                "is_home": True,
                "stage": "Тур 5 (Долг)",
                "live_status": "SCHEDULED",
            },
        ]

    matches = []
    for row in rows:
        side = _match_side(row, telegram_id, team)
        if side is None:
            continue
        is_home = side == "home"
        matches.append({
            "match_id": row["id"],
            "round": row["round_number"],
            "status": row["status"],
            "tournament_type": row["tournament_type"],
            "tournament_name": row["tournament_name"],
            "is_cup": (row["tournament_type"] or "league") == "cup",
            "cup_stage": row["cup_stage"],
            "game_in_series": row["game_num_in_series"],
            "is_home": is_home,
            "side": side,
            "own_team": row["player1_team"] if is_home else row["player2_team"],
            "opponent_team": row["player2_team"] if is_home else row["player1_team"],
            "opponent_nickname": row["player2_username"] if is_home else row["player1_username"],
            "home_team": row["player1_team"],
            "away_team": row["player2_team"],
            "division_id": row["division_id"],
            "season_id": row["season_id"],
            "match_date": row["match_date"],
            "match_time": row["match_time"],
            "live": _live_snapshot({
                "status": row["live_status"],
                "period": row["live_period"],
                "minute": row["live_minute"],
                "home_score": row["live_home_score"],
                "away_score": row["live_away_score"],
                "version": row["live_version"],
                "last_updated_at": row["live_updated_at"],
            }) if row["live_status"] else None,
        })
    return matches


def _start_session(telegram_id: int, match_id: int) -> dict[str, Any]:
    """Перевести матч в LIVE. Идемпотентно: повторный старт — это переподключение."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        match, side = _load_match_for_user(cursor, match_id, telegram_id)

        if match["status"] == "confirmed":
            raise TrackerError("match_already_confirmed", "Протокол этого матча уже сдан.", 409)

        state = _ensure_live_state(cursor, match)
        current = (state["status"] or SCHEDULED).upper()
        if current in TERMINAL_STATES:
            raise TrackerError("match_finished", "Трансляция этого матча уже завершена.", 409)

        # SCHEDULED -> LIVE напрямую: у трекера нет отдельной pre-match фазы,
        # старт трансляции и есть свисток. PRE_MATCH принимается для матчей,
        # которые успел завести провайдерский пайплайн.
        if current in (SCHEDULED, PRE_MATCH):
            period = "1h"
        elif current == HALFTIME:
            period = "2h"
        else:  # LIVE — приложение вернулось в уже идущую трансляцию
            period = state["period"] or "1h"

        cursor.execute(
            """
            UPDATE live_match_states
            SET status = ?, period = ?, provider = ?, version = version + 1,
                last_updated_at = datetime('now', '+3 hours')
            WHERE match_id = ?
            """,
            (LIVE, period, TRACKER_PROVIDER, match_id),
        )

        if current != LIVE:
            _audit(cursor, telegram_id, "tracker_session_start", match, current, LIVE)

        state = _load_live_state(cursor, match_id)

    logger.info("TRACKER session start: match #%s by user %s (%s -> LIVE)", match_id, telegram_id, current)
    return {
        "match_id": match_id,
        "side": side,
        "resumed": current == LIVE,
        "live": _live_snapshot(state),
    }


def _apply_tick(
    telegram_id: int,
    match_id: int,
    minute: int,
    score_home: int,
    score_away: int,
    period: str | None,
) -> dict[str, Any]:
    """
    Записать текущее состояние матча.

    Значения абсолютные: приложение — единственный источник живого счёта, оно
    же и исправляет собственную ошибку следующим тиком. Официальный счёт в
    `matches` не трогается, его владелец — протокол в боте.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        match, side = _load_match_for_user(cursor, match_id, telegram_id)

        state = _load_live_state(cursor, match_id)
        if state is None:
            raise TrackerError("session_not_started", "Трансляция не запущена.", 409)

        current = (state["status"] or SCHEDULED).upper()
        if current not in (LIVE, HALFTIME):
            raise TrackerError(
                "session_not_live",
                f"Матч не в эфире (состояние {current}).",
                409,
            )

        new_period = period or state["period"]
        cursor.execute(
            """
            UPDATE live_match_states
            SET minute = ?, home_score = ?, away_score = ?, period = ?, provider = ?,
                version = version + 1, last_updated_at = datetime('now', '+3 hours')
            WHERE match_id = ?
            """,
            (minute, score_home, score_away, new_period, TRACKER_PROVIDER, match_id),
        )
        cursor.execute("UPDATE matches SET live_minute = ? WHERE id = ?", (minute, match_id))

        version = int(state["version"] or 0) + 1

    return {
        "match_id": match_id,
        "side": side,
        "minute": minute,
        "score_home": score_home,
        "score_away": score_away,
        "period": new_period,
        "version": version,
    }


def _record_event(
    telegram_id: int,
    match_id: int,
    minute: int,
    event_code: str,
    player_name: str | None,
    team_side: str,
    provider_event_id: str,
    payload_extra: dict[str, Any],
) -> dict[str, Any]:
    """Зафиксировать событие в ленте лайва и, для HALFTIME/SECOND_HALF, сдвинуть период."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        match, side = _load_match_for_user(cursor, match_id, telegram_id)

        state = _load_live_state(cursor, match_id)
        if state is None:
            raise TrackerError("session_not_started", "Трансляция не запущена.", 409)

        current = (state["status"] or SCHEDULED).upper()
        if current in TERMINAL_STATES:
            raise TrackerError("match_finished", "Трансляция этого матча уже завершена.", 409)

        cursor.execute(
            "SELECT id FROM live_events WHERE provider = ? AND provider_event_id = ?",
            (TRACKER_PROVIDER, provider_event_id),
        )
        duplicate = cursor.fetchone()
        if duplicate:
            return {
                "match_id": match_id,
                "event_id": duplicate["id"],
                "duplicate": True,
                "live": _live_snapshot(state),
            }

        team_name = match["player1_team"] if team_side == "home" else match["player2_team"]

        # Счётом владеют тики: событие его не инкрементирует, иначе гол приехал бы
        # дважды — из плашки и из следующего тика со счётом на табло.
        new_status, new_period = current, state["period"]
        if event_code == "halftime" and current == LIVE:
            new_status, new_period = HALFTIME, "ht"
        elif event_code == "second_half" and current == HALFTIME:
            new_status, new_period = LIVE, "2h"

        cursor.execute(
            """
            UPDATE live_match_states
            SET status = ?, period = ?, minute = ?, provider = ?,
                version = version + 1, last_updated_at = datetime('now', '+3 hours')
            WHERE match_id = ?
            """,
            (new_status, new_period, minute, TRACKER_PROVIDER, match_id),
        )
        cursor.execute("UPDATE matches SET live_minute = ? WHERE id = ?", (minute, match_id))

        payload = {
            "source": TRACKER_PROVIDER,
            "side": team_side,
            "reported_by": int(telegram_id),
            "score_home": state["home_score"],
            "score_away": state["away_score"],
            **payload_extra,
        }
        cursor.execute(
            """
            INSERT INTO live_events (
                match_id, provider, provider_event_id, event_type, minute,
                team_name, player_name, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            """,
            (
                match_id, TRACKER_PROVIDER, provider_event_id, event_code, minute,
                team_name, player_name, json.dumps(payload, ensure_ascii=False),
            ),
        )
        event_id = cursor.lastrowid
        state = _load_live_state(cursor, match_id)

    logger.info(
        "TRACKER event: match #%s %s at %s' (%s, %s)",
        match_id, event_code, minute, team_side, player_name or "—"
    )
    return {
        "match_id": match_id,
        "event_id": event_id,
        "duplicate": False,
        "event_type": event_code,
        "minute": minute,
        "team_side": team_side,
        "team_name": team_name,
        "player_name": player_name,
        "live": _live_snapshot(state),
    }


def _finish_session(telegram_id: int, match_id: int) -> dict[str, Any]:
    """
    Завершить трансляцию: live_match_states -> FINISHED.

    Ни `matches.status`, ни счёт, ни очки турнира не трогаются — протокол
    сдаётся через бота, и матч обязан остаться в активных до этого момента.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        match, side = _load_match_for_user(cursor, match_id, telegram_id)

        state = _load_live_state(cursor, match_id)
        if state is None:
            raise TrackerError("session_not_started", "Трансляция не запущена.", 409)

        current = (state["status"] or SCHEDULED).upper()
        if current in TERMINAL_STATES:
            return {
                "match_id": match_id,
                "side": side,
                "already_finished": True,
                "live": _live_snapshot(state),
            }

        cursor.execute(
            """
            UPDATE live_match_states
            SET status = ?, period = 'ft', provider = ?, version = version + 1,
                last_updated_at = datetime('now', '+3 hours')
            WHERE match_id = ?
            """,
            (FINISHED, TRACKER_PROVIDER, match_id),
        )
        _audit(cursor, telegram_id, "tracker_session_finish", match, current, FINISHED)
        state = _load_live_state(cursor, match_id)

    logger.info("TRACKER session finish: match #%s by user %s", match_id, telegram_id)
    return {
        "match_id": match_id,
        "side": side,
        "already_finished": False,
        "live": _live_snapshot(state),
    }


# ─── OCR плашки события ───────────────────────────────────────────────────────

async def _recognize_player_name(image_bytes: bytes, mime_type: str, team_side: str) -> str | None:
    """
    Распознать фамилию с кадра плашки через Gemini Vision.

    Необязательный шаг: приложение зовёт его, только когда само не разобрало
    фамилию. Любая ошибка OCR гасится — событие важнее подписи к нему.
    """
    if not image_bytes or not config.TRACKER_OCR_ENABLED:
        return None

    try:
        from services.ai.ai_recognizer import clean_player_name, recognize_match_screenshot_bytes

        data = await asyncio.to_thread(
            recognize_match_screenshot_bytes,
            image_bytes,
            mime_type,
            None,
            "Это кадр игрового события FC Mobile. Нужна фамилия игрока с плашки.",
        )
    except Exception:
        logger.exception("TRACKER_OCR: распознавание плашки не удалось")
        return None

    if not isinstance(data, dict):
        return None

    primary_key = "left_goals" if team_side == "home" else "right_goals"
    names = [n for n in (data.get(primary_key) or []) if n]
    if not names:
        names = [n for n in ((data.get("left_goals") or []) + (data.get("right_goals") or [])) if n]
    if not names:
        return None

    try:
        cleaned = clean_player_name(names[-1])
    except Exception:
        logger.exception("TRACKER_OCR: очистка имени не удалась")
        return None
    return (cleaned or "").strip()[:MAX_PLAYER_NAME_LEN] or None


async def _read_event_payload(request: web.Request) -> tuple[dict[str, Any], bytes | None, str]:
    """
    Прочитать тело события: JSON (+ опциональный screenshot_base64) или multipart.
    Возвращает (поля, байты кадра или None, mime кадра).
    """
    limit = config.TRACKER_MAX_SCREENSHOT_BYTES
    if request.content_length is not None and request.content_length > limit:
        raise TrackerError("screenshot_too_large", "Кадр события слишком большой.", 413)

    content_type = (request.content_type or "").lower()

    if content_type.startswith("multipart/"):
        try:
            reader = await request.multipart()
        except Exception:
            raise TrackerError("invalid_multipart", "Некорректное multipart-тело запроса.", 400)

        fields: dict[str, Any] = {}
        image: bytes | None = None
        mime = "image/jpeg"
        while True:
            try:
                part = await reader.next()
            except Exception:
                raise TrackerError("invalid_multipart", "Некорректное multipart-тело запроса.", 400)
            if part is None:
                break
            if part.filename:
                raw = await part.read(decode=False)
                if len(raw) > limit:
                    raise TrackerError("screenshot_too_large", "Кадр события слишком большой.", 413)
                image = raw
                mime = part.headers.get("Content-Type", mime) or mime
            else:
                fields[part.name] = (await part.text()).strip()
        return fields, image, mime

    body = await _read_json_body(request)
    mime = _clean_text(body.get("screenshot_mime"), 64) or "image/jpeg"
    raw_b64 = body.get("screenshot_base64")
    if not raw_b64:
        return body, None, mime

    try:
        image = base64.b64decode(str(raw_b64), validate=True)
    except (binascii.Error, ValueError):
        raise TrackerError("invalid_screenshot", "Кадр события не является корректным base64.", 400)
    if len(image) > limit:
        raise TrackerError("screenshot_too_large", "Кадр события слишком большой.", 413)
    return body, image, mime


# ─── HTTP-обработчики ─────────────────────────────────────────────────────────

async def handle_tracker_pair(request: web.Request) -> web.Response:
    """
    POST /api/tracker/auth/pair
    Body: {"pin_code": "1234", "device_info": "iPhone 15 Pro, iOS 18"}

    Меняет одноразовый ПИН на Bearer-токен сессии. Любая осечка с кодом —
    отсутствует, короткий, протух, уже использован — отвечает одинаковым 401
    без подробностей: подсказка отличала бы «нет такого кода» от «код чужой».
    """
    try:
        body = await _read_json_body(request)
    except TrackerError as e:
        return _error(e.code, e.message, e.status)

    pin_code = str(body.get("pin_code") or body.get("pin") or "").strip()
    unauthorized = _error(
        "invalid_pin",
        "Код недействителен или истёк. Запросите новый командой /tracker в боте.",
        401,
    )
    if len(pin_code) != PIN_CODE_LENGTH or not pin_code.isdigit():
        return unauthorized

    device_info = _clean_text(body.get("device_info"), MAX_DEVICE_INFO_LEN)

    # 🛠️ Режим разработки: код 7777 или 0000 для мгновенного теста без Telegram.
    # Строго за флагом — без него эти коды идут в общий путь и просто не
    # находят себя в consume_pin_code, как любой другой неверный код.
    if config.TRACKER_DEV_PIN_ENABLED and pin_code in ("7777", "0000"):
        test_user = {
            "id": 777777,
            "nickname": "Илез (Dev)",
            "club": "Манчестер Сити",
            "division": "Премьер-Лига",
        }
        token = create_session(777777, device_info)
        logger.info("TRACKER paired in DEV mode with code %s", pin_code)
        return web.json_response({
            "status": "ok",
            "token": token,
            "expires_in": int(config.TRACKER_SESSION_TTL_SECONDS),
            "user": test_user,
        })

    telegram_id = consume_pin_code(pin_code)
    if not telegram_id:
        return unauthorized

    if not check_user_access(telegram_id):
        return _error("LOGOVO_LOCKDOWN", "Logovo.bet временно закрыт для пользователей.", 403)

    device_info = _clean_text(body.get("device_info"), MAX_DEVICE_INFO_LEN)

    try:
        user = await asyncio.to_thread(_player_card, telegram_id)
    except Exception:
        logger.exception("TRACKER pair: не удалось собрать карточку игрока %s", telegram_id)
        return _error("internal_error", "Не удалось завершить привязку устройства.", 500)

    token = create_session(telegram_id, device_info)
    logger.info("TRACKER paired: user %s, device «%s»", telegram_id, device_info or "unknown")

    return web.json_response({
        "status": "ok",
        "token": token,
        "expires_in": int(config.TRACKER_SESSION_TTL_SECONDS),
        "user": user,
    })


async def handle_tracker_matches(request: web.Request) -> web.Response:
    """
    GET /api/tracker/matches
    Несыгранные матчи авторизованного игрока: тур, соперник, дом/выезд, лига/кубок.
    """
    session, err = _require_session(request)
    if err is not None:
        return err

    telegram_id = session["telegram_id"]
    try:
        matches = await asyncio.to_thread(_fetch_open_matches, telegram_id)
    except Exception:
        logger.exception("TRACKER matches: выборка матчей игрока %s не удалась", telegram_id)
        return _error("internal_error", "Не удалось загрузить матчи.", 500)

    return web.json_response({
        "status": "ok",
        "count": len(matches),
        "matches": matches,
    })


async def handle_tracker_logout(request: web.Request) -> web.Response:
    """
    POST /api/tracker/auth/logout
    Отзывает Bearer-токен текущего устройства немедленно, а не по истечении
    TTL сессии — «Выйти» в приложении должен реально закрыть доступ, а не
    просто спрятать экран локально.
    """
    token = _bearer_token(request)
    if token:
        revoke_session(token)
    return web.json_response({"status": "ok"})


async def _handle_simple_session_action(request: web.Request, action) -> web.Response:
    """Общий каркас для start/finish: авторизация -> match_id -> действие."""
    session, err = _require_session(request)
    if err is not None:
        return err

    telegram_id = session["telegram_id"]
    try:
        body = await _read_json_body(request)
        match_id = _match_id_from(body)
        result = await asyncio.to_thread(action, telegram_id, match_id)
    except TrackerError as e:
        return _error(e.code, e.message, e.status)
    except Exception:
        logger.exception("TRACKER %s: сбой для игрока %s", getattr(action, "__name__", "action"), telegram_id)
        return _error("internal_error", "Внутренняя ошибка трекера.", 500)

    return web.json_response({"status": "ok", **result})


async def handle_tracker_session_start(request: web.Request) -> web.Response:
    """
    POST /api/tracker/session/start
    Body: {"match_id": 123}
    Инициализирует трансляцию и переводит матч в LIVE.
    """
    return await _handle_simple_session_action(request, _start_session)


async def handle_tracker_session_finish(request: web.Request) -> web.Response:
    """
    POST /api/tracker/session/finish
    Body: {"match_id": 123}
    Завершает трансляцию (FINISHED). Официальный протокол и очки не трогает.
    """
    return await _handle_simple_session_action(request, _finish_session)


async def handle_tracker_session_tick(request: web.Request) -> web.Response:
    """
    POST /api/tracker/session/tick
    Body: {"match_id": 123, "minute": 34, "score_home": 1, "score_away": 0, "period": "1H"}
    Лёгкое обновление табло: одна запись в live_match_states, без побочных эффектов.
    """
    session, err = _require_session(request)
    if err is not None:
        return err

    telegram_id = session["telegram_id"]
    try:
        body = await _read_json_body(request)
        match_id = _match_id_from(body)
        minute = _require_int(body, "minute", 0, MAX_MINUTE)
        score_home = _require_int(body, "score_home", 0, config.MAX_MATCH_GOALS)
        score_away = _require_int(body, "score_away", 0, config.MAX_MATCH_GOALS)

        period_raw = _clean_text(body.get("period"), 32)
        period = None
        if period_raw is not None:
            period = PERIOD_ALIASES.get(period_raw.upper())
            if period is None:
                raise TrackerError(
                    "invalid_period",
                    "Неизвестный период. Допустимо: " + ", ".join(sorted(PERIOD_ALIASES)),
                    400,
                )

        result = await asyncio.to_thread(
            _apply_tick, telegram_id, match_id, minute, score_home, score_away, period
        )
    except TrackerError as e:
        return _error(e.code, e.message, e.status)
    except Exception:
        logger.exception("TRACKER tick: сбой для игрока %s", telegram_id)
        return _error("internal_error", "Внутренняя ошибка трекера.", 500)

    return web.json_response({"status": "ok", **result})


async def handle_tracker_session_event(request: web.Request) -> web.Response:
    """
    POST /api/tracker/session/event
    Body: {"match_id": 123, "minute": 34, "event_type": "GOAL",
           "player_name": "Haaland", "team_side": "home"}

    Принимает JSON (можно с полем screenshot_base64) или multipart с кадром
    плашки. Если фамилия не пришла, а кадр есть — зовётся OCR из
    services/ai/ai_recognizer.py. Счёт событием не меняется: табло ведут тики.
    """
    session, err = _require_session(request)
    if err is not None:
        return err

    telegram_id = session["telegram_id"]
    try:
        body, image_bytes, mime_type = await _read_event_payload(request)

        match_id = _match_id_from(body)
        minute = _require_int(body, "minute", 0, MAX_MINUTE)

        event_raw = _clean_text(body.get("event_type"), 32)
        event_code = EVENT_TYPES.get((event_raw or "").upper())
        if event_code is None:
            raise TrackerError(
                "invalid_event_type",
                "Неизвестный тип события. Допустимо: " + ", ".join(sorted(EVENT_TYPES)),
                400,
            )

        card_type = (_clean_text(body.get("card_type"), 16) or "").lower()
        if event_code == "yellow_card" and card_type in ("red", "красная"):
            event_code = "red_card"

        team_side = (_clean_text(body.get("team_side"), 8) or "home").lower()
        if team_side not in TEAM_SIDES:
            raise TrackerError("invalid_team_side", "Поле «team_side» — это home или away.", 400)

        player_name = _clean_text(body.get("player_name"), MAX_PLAYER_NAME_LEN)
        ocr_used = False
        if not player_name and image_bytes and event_code in PLAYER_EVENT_TYPES:
            player_name = await _recognize_player_name(image_bytes, mime_type, team_side)
            ocr_used = player_name is not None

        client_event_id = _clean_text(body.get("client_event_id"), MAX_CLIENT_EVENT_ID_LEN)
        # Свой id от приложения делает повтор при обрыве связи идемпотентным.
        event_ref = client_event_id or f"{minute}-{event_code}-{secrets.token_hex(4)}"
        provider_event_id = f"{match_id}:{event_ref}"

        payload_extra: dict[str, Any] = {"ocr_used": ocr_used}
        if card_type:
            payload_extra["card_type"] = card_type
        if client_event_id:
            payload_extra["client_event_id"] = client_event_id

        result = await asyncio.to_thread(
            _record_event,
            telegram_id, match_id, minute, event_code, player_name,
            team_side, provider_event_id, payload_extra,
        )
    except TrackerError as e:
        return _error(e.code, e.message, e.status)
    except Exception:
        logger.exception("TRACKER event: сбой для игрока %s", telegram_id)
        return _error("internal_error", "Внутренняя ошибка трекера.", 500)

    return web.json_response({"status": "ok", **result})
