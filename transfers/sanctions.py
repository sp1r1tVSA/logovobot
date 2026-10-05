"""Санкции трансферного окна: лишение клуба или тренера окна на N сезонов.

Ставит и снимает ответственный кнопками панели `/to`. Сама блокировка уже
работает в `requests._check` (заявки) и `slots` (докупка слотов): они спрашивают
`repo.is_sanctioned` по сезону окна. Здесь — постановка, снятие, список и
«что видит сам тренер».

Срок — число сезонов, считая текущий: `from = текущий сезон`, `until = from + N − 1`.
Это верно, пока id сезонов идут подряд (`seasons.id` — AUTOINCREMENT, сезоны
заводятся по одному); тестовые сезоны с TEST/LAB в названии id съедают, но
`get_active_season` их пропускает, так что на практике срок может оказаться
чуть короче. Для сезонного окна этого достаточно.

Санкция на клуб держится за клуб (новый тренер клуба тоже не подаёт заявки),
санкция на тренера — за человека (он не подаёт заявки, куда бы ни перешёл).
"""

from __future__ import annotations

import difflib

import database
from club_registry import resolve_team_name
from transfers import repo, service
from transfers.engine import norm_club
from transfers.service import InputError

MIN_SEASONS = 1
MAX_SEASONS = 5
REASON_MAX_LEN = 300


def current_season_id() -> int | None:
    """Сезон, по которому окно сверяется с санкциями: сезон незакрытого окна, иначе активный."""
    window = repo.get_active_window()
    if window and window.get("season_id"):
        return int(window["season_id"])
    season = database.get_active_season()
    return int(season) if season else None


def _season_for_viewer() -> int | None:
    """Сезон для тренера: сезон последнего окна (оно же — окно, которое он видит), иначе активный."""
    window = repo.get_active_window() or repo.get_latest_window()
    if window and window.get("season_id"):
        return int(window["season_id"])
    season = database.get_active_season()
    return int(season) if season else None


_CLOSE_CUTOFF = 0.7
_CLOSE_LIMIT = 3


def close_clubs(text: str, clubs: list[str], limit: int = _CLOSE_LIMIT) -> list[str]:
    """Клубы, похожие на запрос (опечатка, часть названия): подсказка, когда точного совпадения нет."""
    wanted = norm_club(text)
    if len(wanted) < 3:
        return []
    scored = []
    for club in clubs:
        key = norm_club(club)
        ratio = max(difflib.SequenceMatcher(None, wanted, part).ratio() for part in [key, *key.split()])
        if ratio >= _CLOSE_CUTOFF:
            scored.append((-ratio, key, club))
    return [club for _, _, club in sorted(scored)[:limit]]


def find_club(text: str) -> str:
    """Клуб лиги по тексту ответственного: точное имя, алиас реестра или единственное вхождение."""
    raw = (text or "").strip()
    if not raw:
        raise InputError("Пришлите название клуба.")
    clubs = service.league_clubs()
    by_key = {norm_club(c): c for c in clubs}
    key = norm_club(resolve_team_name(raw) or raw)
    if key in by_key:
        return by_key[key]
    wanted = norm_club(raw)
    partial = [c for c in clubs if wanted and wanted in norm_club(c)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise InputError("Под запрос подходит несколько клубов: " + ", ".join(partial[:6]) + ". Уточните.")
    near = close_clubs(raw, clubs)
    hint = " Похоже на: " + ", ".join(near) + "." if near else ""
    raise InputError("Такого клуба нет в лиге." + hint + " Пришлите название точнее.")


def find_coach(text: str) -> dict:
    """Тренер по `@username` или Telegram ID: `{user_id, username, club}`. Должен быть в базе бота."""
    raw = (text or "").strip()
    if not raw:
        raise InputError("Пришлите @username или Telegram ID тренера.")
    if raw.lstrip("@").isdigit() and not raw.startswith("@"):
        user_id = int(raw)
    else:
        user_id = database.find_telegram_id_by_username(raw)
    user = database.get_user(user_id) if user_id else None
    if user is None:
        raise InputError("Такого тренера нет в базе бота: он должен хотя бы раз запустить бота.")
    team = (user["team_name"] or "").strip()
    return {"user_id": int(user["telegram_id"]), "username": user["username"],
            "club": (resolve_team_name(team) or team) if team else None}


def parse_seasons(text: str | int) -> int:
    try:
        n = int(str(text).strip())
    except ValueError:
        raise InputError(f"Число сезонов — от {MIN_SEASONS} до {MAX_SEASONS}.") from None
    if not MIN_SEASONS <= n <= MAX_SEASONS:
        raise InputError(f"Число сезонов — от {MIN_SEASONS} до {MAX_SEASONS}.")
    return n


def add(actor_id: int, *, club_name: str | None = None, user_id: int | None = None,
        seasons: int, reason: str | None = None) -> dict:
    """Поставить санкцию с текущего сезона на `seasons` сезонов. Возвращает строку санкции."""
    if bool(club_name) == bool(user_id):
        raise InputError("Санкция ставится либо на клуб, либо на тренера.")
    seasons = parse_seasons(seasons)
    season = current_season_id()
    if season is None:
        raise InputError("Сезон не определён — санкцию поставить не к чему.")
    reason = (reason or "").strip() or None
    if reason and len(reason) > REASON_MAX_LEN:
        raise InputError(f"Причина длиннее {REASON_MAX_LEN} символов.")
    if repo.is_sanctioned(season, club_name=club_name, user_id=user_id):
        raise InputError("Уже под санкцией в этом сезоне. Сначала снимите её.")
    sid = repo.add_sanction(club_name=club_name, user_id=user_id, from_season_id=season,
                            until_season_id=season + seasons - 1, reason=reason, created_by=actor_id)
    return repo.get_sanction(sid)


def lift(sanction_id: int, actor_id: int) -> dict:
    """Снять санкцию досрочно. Уже снята или нет такой — `InputError`."""
    sanction = repo.get_sanction(sanction_id)
    if sanction is None:
        raise InputError("Такой санкции нет.")
    if not repo.lift_sanction(sanction_id, actor_id):
        raise InputError("Санкция уже снята.")
    return repo.get_sanction(sanction_id)


def subject_label(sanction: dict) -> str:
    """«клуб Челси» / «тренер @name»; для тренера без username — его ID."""
    if sanction.get("club_name"):
        return f"клуб {sanction['club_name']}"
    user = database.get_user(int(sanction["user_id"])) if sanction.get("user_id") else None
    name = user["username"] if user else None
    return f"тренер @{name}" if name else f"тренер {sanction.get('user_id')}"


def span_label(sanction: dict, names: dict[int, str] | None = None) -> str:
    """«сезон 12» или «сезоны 12–13» — по названиям сезонов, если они известны."""
    names = names if names is not None else repo.season_names(
        [sanction["from_season_id"], sanction["until_season_id"]])

    def label(season_id: int) -> str:
        return names.get(season_id) or f"№{season_id}"

    first, last = sanction["from_season_id"], sanction["until_season_id"]
    return f"сезон {label(first)}" if first == last else f"сезоны {label(first)} — {label(last)}"


def overview() -> dict:
    """Для панели: действующие (по сезону окна) и последние снятые/истёкшие санкции."""
    season = current_season_id()
    every = repo.list_sanctions(limit=40)
    active_ids = {s["id"] for s in repo.list_active_sanctions(season)} if season else set()
    active = [s for s in every if s["id"] in active_ids]
    past = [s for s in every if s["id"] not in active_ids]
    return {"season": season, "active": active, "past": past}


def coaches_to_notify(sanction: dict) -> list[int]:
    """Кому сообщить о санкции: сам тренер или все тренеры клуба."""
    if sanction.get("user_id"):
        return [int(sanction["user_id"])]
    return [int(c["telegram_id"]) for c in repo.coaches_of_club(sanction["club_name"])]


def for_user(user_id: int, club: str | None) -> dict | None:
    """Санкция, под которой сейчас тренер (личная или на его клуб), для экрана «Статус»."""
    season = _season_for_viewer()
    if season is None:
        return None
    key = norm_club(club)
    for s in repo.list_active_sanctions(season):
        personal = s["user_id"] is not None and int(s["user_id"]) == int(user_id)
        by_club = bool(key) and bool(s["club_name"]) and norm_club(s["club_name"]) == key
        if personal or by_club:
            return {
                "scope": "coach" if personal else "club",
                "club": s["club_name"],
                "reason": s["reason"],
                "from_season_id": s["from_season_id"],
                "until_season_id": s["until_season_id"],
                "seasons_left": s["until_season_id"] - season + 1,
                "created_at": s["created_at"],
            }
    return None
