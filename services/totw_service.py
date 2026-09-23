"""
services/totw_service.py

Символическая сборная (Team of the Week, TOTW) за блок туров: 1–5, 6–10, …

  * calculate_totw_player_score — TOTW Performance Index игрока по его линии;
  * build_totw_lineup           — 4-3-3, не больше двух игроков одного клуба,
                                  капитан и скамейка из четырёх (GK/DEF/MID/FWD);
  * build_totw_payload          — цифры блока из database + собранная сборная;
  * generate_totw_caption       — подпись в голосе «Темшика» (Gemini) с шаблонным
                                  фолбэком, чтобы блок не остался без публикации.

Модуль синхронный, как и services/round_preview.py: из хендлеров и джобов
вызывается через asyncio.to_thread.
"""

import html
import logging
import re

import database

logger = logging.getLogger(__name__)

BLOCK_SIZE = 5
MAX_PER_CLUB = 2
FORMATION = "4-3-3"

GK, DEF, MID, FWD = "GK", "DEF", "MID", "FWD"

POSITION_LINE: dict[str, str] = {
    "GK": GK,
    "LB": DEF, "LWB": DEF, "CB": DEF, "RB": DEF, "RWB": DEF,
    "CDM": MID, "CM": MID, "CAM": MID,
    "LW": FWD, "LM": FWD, "RW": FWD, "RM": FWD, "ST": FWD, "CF": FWD,
}

# Слоты 4-3-3 в порядке отрисовки: (код слота, линия, основные позиции).
SLOTS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("GK", GK, frozenset({"GK"})),
    ("LB", DEF, frozenset({"LB", "LWB"})),
    ("LCB", DEF, frozenset({"CB"})),
    ("RCB", DEF, frozenset({"CB"})),
    ("RB", DEF, frozenset({"RB", "RWB"})),
    ("CDM", MID, frozenset({"CDM"})),
    ("LCM", MID, frozenset({"CM", "CAM"})),
    ("RCM", MID, frozenset({"CM", "CAM"})),
    ("LW", FWD, frozenset({"LW", "LM"})),
    ("ST", FWD, frozenset({"ST", "CF"})),
    ("RW", FWD, frozenset({"RW", "RM"})),
)

# Фланговые слоты, куда игрок зеркального фланга встаёт без штрафа «не на своей
# позиции»: правый вингер закрывает левый фланг раньше, чем туда поставят
# кого-то слабее. Свой фланг всё равно пробуется первым.
MIRROR_SLOTS: dict[str, frozenset[str]] = {
    "LB": frozenset({"RB", "RWB"}),
    "RB": frozenset({"LB", "LWB"}),
    "LW": frozenset({"RW", "RM"}),
    "RW": frozenset({"LW", "LM"}),
}

# Как слот подписан на карточке.
SLOT_LABELS: dict[str, str] = {
    "GK": "GK", "LB": "LB", "LCB": "CB", "RCB": "CB", "RB": "RB",
    "CDM": "CDM", "LCM": "CM", "RCM": "CM", "LW": "LW", "ST": "ST", "RW": "RW",
}

# Очки TOTW Performance Index.
GK_CLEAN_SHEET, GK_LOW_CONCEDED, GK_WIN = 15, 10, 3
DEF_CLEAN_SHEET, DEF_GOAL, DEF_ASSIST, DEF_RELIABILITY = 10, 15, 10, 5
MID_GOAL, MID_ASSIST, MID_CLEAN_SHEET = 10, 10, 4
FWD_GOAL, FWD_ASSIST, FWD_BRACE = 12, 8, 5
MVP_POINTS = 12
LOW_CONCEDED_PER_MATCH = 1.0


def position_line(position: str | None) -> str:
    """Линия игрока по позиции; неизвестная позиция считается атакой, как и в детекторе."""
    return POSITION_LINE.get((position or "").strip().upper(), FWD)


def _int(stat: dict, key: str) -> int:
    try:
        return int(stat.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _low_conceded(stat: dict) -> bool:
    """Клуб пропускал в блоке не больше LOW_CONCEDED_PER_MATCH за матч."""
    matches = _int(stat, "matches")
    if matches <= 0:
        return False
    return _int(stat, "goals_conceded") / matches <= LOW_CONCEDED_PER_MATCH


def calculate_totw_player_score(player_stat: dict) -> float:
    """TOTW Performance Index игрока за блок.

    GK : +15 за сухарь, +10 если клуб пропускал ≤1.0 за матч, +12 за MVP, +3 за победу.
    DEF: +10 за сухарь, +15 за гол, +10 за ассист, +12 за MVP,
         +5 за надёжность (игрок основы, клуб пропускал ≤1.0 за матч).
    MID: +10 за ассист, +10 за гол, +12 за MVP, +4 за сухарь у CDM/CM.
    FWD: +12 за гол, +8 за ассист, +12 за MVP, +5 за каждый дубль/хет-трик в матче.

    Сухари в статистике есть только у игроков стартового состава
    (database.get_totw_stats) — запасной не обязательно выходил на поле.
    """
    position = (player_stat.get("position") or "").strip().upper()
    line = position_line(position)
    goals = _int(player_stat, "goals")
    assists = _int(player_stat, "assists")
    mvp = _int(player_stat, "mvp")
    clean_sheets = _int(player_stat, "clean_sheets")

    score = MVP_POINTS * mvp
    if line == GK:
        score += GK_CLEAN_SHEET * clean_sheets + GK_WIN * _int(player_stat, "wins")
        if _low_conceded(player_stat):
            score += GK_LOW_CONCEDED
    elif line == DEF:
        score += DEF_CLEAN_SHEET * clean_sheets + DEF_GOAL * goals + DEF_ASSIST * assists
        if player_stat.get("is_starter") and _low_conceded(player_stat):
            score += DEF_RELIABILITY
    elif line == MID:
        score += MID_GOAL * goals + MID_ASSIST * assists
        if position in ("CDM", "CM"):
            score += MID_CLEAN_SHEET * clean_sheets
    else:
        score += FWD_GOAL * goals + FWD_ASSIST * assists + FWD_BRACE * _int(player_stat, "braces")
    return float(score)


def _club_key(player: dict) -> str:
    return (player.get("team_name") or "").strip().lower()


def _rank_key(player: dict):
    return (
        -player["score"],
        -(_int(player, "goals") + _int(player, "assists")),
        -_int(player, "mvp"),
        player.get("player_name") or "",
    )


def build_totw_lineup(candidates: list[dict], max_per_club: int = MAX_PER_CLUB) -> dict:
    """Собрать 4-3-3 из кандидатов блока.

    Возвращает {"formation", "xi": [...], "captain": {...} | None, "bench": [...]}.
    Каждый игрок XI — копия кандидата плюс slot, slot_label, line, score,
    is_captain и out_of_position.

    Слоты заполняются в три прохода: сначала игроками своей позиции (лучшие
    первыми; вингеры и крайние защитники могут встать на зеркальный фланг),
    затем любым игроком той же линии, затем — для полевых слотов — любым
    полевым. Лимит `max_per_club` действует на всех проходах. Слот,
    который закрыть некем, остаётся пустым: вратаря полевым не подменяем.
    Капитан — лучший по очкам в XI. Скамейка — лучший оставшийся GK, DEF,
    MID и FWD; лимит клуба на неё не распространяется.
    """
    max_per_club = max(1, int(max_per_club))
    pool = []
    for c in candidates or []:
        if not (c.get("player_name") or "").strip():
            continue
        p = dict(c)
        p["position"] = (p.get("position") or "ST").strip().upper()
        p["line"] = position_line(p["position"])
        p["score"] = calculate_totw_player_score(p)
        pool.append(p)
    pool.sort(key=_rank_key)

    filled: dict[str, dict] = {}
    used: set[int] = set()
    per_club: dict[str, int] = {}

    def club_ok(p: dict) -> bool:
        return per_club.get(_club_key(p), 0) < max_per_club

    def place(slot: str, idx: int, out_of_position: bool) -> None:
        p = dict(pool[idx])
        p["slot"] = slot
        p["slot_label"] = SLOT_LABELS[slot]
        p["out_of_position"] = out_of_position
        filled[slot] = p
        used.add(idx)
        key = _club_key(p)
        per_club[key] = per_club.get(key, 0) + 1

    # 1. Своя позиция (затем зеркальный фланг), лучшие первыми.
    for idx, p in enumerate(pool):
        if not club_ok(p):
            continue
        options = [slot for slot, _l, positions in SLOTS if p["position"] in positions]
        options += [slot for slot, mirror in MIRROR_SLOTS.items() if p["position"] in mirror]
        for slot in options:
            if slot not in filled:
                place(slot, idx, False)
                break

    # 2. Своя линия. 3. Любой полевой в полевой слот.
    for same_line_only in (True, False):
        for slot, line, _positions in SLOTS:
            if slot in filled:
                continue
            if not same_line_only and line == GK:
                continue
            for idx, p in enumerate(pool):
                if idx in used or not club_ok(p):
                    continue
                if same_line_only and p["line"] != line:
                    continue
                if not same_line_only and p["line"] == GK:
                    continue
                place(slot, idx, True)
                break

    xi = [filled[slot] for slot, _l, _p in SLOTS if slot in filled]
    captain = min(xi, key=_rank_key) if xi else None
    for p in xi:
        p["is_captain"] = captain is not None and p is captain

    bench = []
    for line in (GK, DEF, MID, FWD):
        for idx, p in enumerate(pool):
            if idx not in used and p["line"] == line:
                b = dict(p)
                b["slot"] = f"SUB_{line}"
                b["slot_label"] = p["position"]
                b["is_captain"] = False
                b["out_of_position"] = False
                bench.append(b)
                used.add(idx)
                break

    return {"formation": FORMATION, "xi": xi, "captain": captain, "bench": bench}


def block_bounds(block_number: int, block_size: int = BLOCK_SIZE) -> tuple[int, int]:
    """Номер блока (1, 2, …) → (первый тур, последний тур)."""
    block_number = max(1, int(block_number))
    start = (block_number - 1) * block_size + 1
    return start, start + block_size - 1


_RANGE_RE = re.compile(r"(\d{1,3})\s*(?:[-–—]|\.\.)\s*(\d{1,3})")
_SINGLE_RE = re.compile(r"(?<![\d-])(\d{1,3})(?![\d-])")


def parse_round_range(text: str | None) -> tuple[int, int] | None:
    """«1-5», «6–10», «1 — 5», «1..5» → (1, 5); одно число N → (N, N); иначе None.

    Перевёрнутый диапазон («5-1») разворачивается; тур 0 и меньше — не тур.
    """
    if not text:
        return None
    m = _RANGE_RE.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        start, end = min(a, b), max(a, b)
    else:
        m = _SINGLE_RE.search(text)
        if not m:
            return None
        start = end = int(m.group(1))
    if start < 1:
        return None
    return start, end


def build_totw_payload(
    division_id: int,
    start_round: int,
    end_round: int,
    season_id: int | None = None,
) -> dict:
    """Цифры блока, собранная сборная и подписи для картинки/текста."""
    division = database.get_division(division_id) or {}
    season = None
    try:
        if season_id is not None:
            season = database.get_season(season_id)
        else:
            season = database.get_active_season()
    except Exception:
        logger.exception("TOTW: could not load season %s", season_id)
    candidates = database.get_totw_stats(start_round, end_round, division_id, season_id)
    lineup = build_totw_lineup(candidates)
    return {
        "division_id": division_id,
        "division_name": division.get("name") or f"Дивизион {division_id}",
        "division_code": division.get("code"),
        "season_id": (season or {}).get("id", season_id),
        "season_name": (season or {}).get("name") or "",
        "start_round": start_round,
        "end_round": end_round,
        "candidates_count": len(candidates),
        **lineup,
    }


def _rounds_label(start_round: int, end_round: int) -> str:
    if start_round == end_round:
        return f"ТУР {start_round}"
    return f"ТУРЫ {start_round}–{end_round}"


def _stat_line(p: dict) -> str:
    """Короткая строка цифр игрока: «3+2 · 1 MVP · 2 сухаря»."""
    parts = []
    goals, assists = _int(p, "goals"), _int(p, "assists")
    if goals or assists:
        parts.append(f"{goals}+{assists}")
    if _int(p, "mvp"):
        parts.append(f"{_int(p, 'mvp')} MVP")
    if p.get("line") in (GK, DEF) and _int(p, "clean_sheets"):
        parts.append(f"{_int(p, 'clean_sheets')} сух.")
    return " · ".join(parts)


def _totw_for_model(totw: dict, division_name: str, start_round: int, end_round: int) -> dict:
    def slim(p: dict) -> dict:
        return {
            "позиция": p.get("slot_label"),
            "игрок": p.get("player_name"),
            "клуб": p.get("team_name"),
            "очки_TOTW": int(p.get("score") or 0),
            "голы": _int(p, "goals"),
            "ассисты": _int(p, "assists"),
            "MVP": _int(p, "mvp"),
            "сухари": _int(p, "clean_sheets") if p.get("line") in (GK, DEF, MID) else None,
        }

    captain = totw.get("captain")
    return {
        "дивизион": division_name,
        "туры": f"{start_round}–{end_round}",
        "схема": totw.get("formation", FORMATION),
        "капитан": slim(captain) if captain else None,
        "сборная": [slim(p) for p in totw.get("xi", [])],
        "запас": [slim(p) for p in totw.get("bench", [])],
    }


def _instruction() -> str:
    from services.round_preview import CAPTION_MAX_CHARS, _COMMON_RULES

    return (
        "Ты — Темшик, аналитик и голос лиги «Логово Фифарей»: душевный 30+ мужик, батейный юмор, "
        "но по цифрам — строгий аналитик.\n\n"
        "ЗАДАЧА: написать ПОДПИСЬ к картинке «Символическая сборная» за блок туров по переданному JSON. "
        "На картинке уже видна вся расстановка 4-3-3 с цифрами — не перечисляй всех одиннадцать.\n"
        "СТРУКТУРА: заголовок (🌟, туры, дивизион) → строка про капитана с его цифрами → "
        "2-4 строки про самое интересное (кто закрыл оборону, чей клуб дал двоих, кто тащил атаку) → "
        "короткая концовка.\n\n"
        f"{_COMMON_RULES}"
        f"- Уложись в {CAPTION_MAX_CHARS} символов — это подпись к фото, лимит Telegram жёсткий.\n"
    )


def _fallback_totw_caption(totw: dict, division_name: str, start_round: int, end_round: int) -> str:
    from services.round_preview import CAPTION_MAX_CHARS, _fit_html

    esc = html.escape
    lines = [
        f"🌟 <b>СИМВОЛИЧЕСКАЯ СБОРНАЯ · {_rounds_label(start_round, end_round)}</b>",
        f"<i>{esc(str(division_name))}</i>",
    ]
    xi = totw.get("xi") or []
    if not xi:
        lines.append("")
        lines.append("Цифр за эти туры пока нет — сборную собрать не из кого.")
        return _fit_html("\n".join(lines), CAPTION_MAX_CHARS)

    captain = totw.get("captain")
    if captain:
        stat = _stat_line(captain)
        lines.append("")
        lines.append(
            f"⭐️ <b>Капитан:</b> {esc(captain['player_name'])} ({esc(captain.get('team_name') or '')})"
            f" — {int(captain['score'])} очк." + (f" · {stat}" if stat else "")
        )

    def slot_names(codes: tuple[str, ...]) -> str:
        by_slot = {p["slot"]: p for p in xi}
        return ", ".join(esc(by_slot[c]["player_name"]) for c in codes if c in by_slot)

    lines.append("")
    gk = slot_names(("GK",))
    if gk:
        lines.append(f"🧤 {gk}")
    for emoji, codes in (
        ("🛡", ("LB", "LCB", "RCB", "RB")),
        ("⚙️", ("CDM", "LCM", "RCM")),
        ("⚡️", ("LW", "ST", "RW")),
    ):
        row = slot_names(codes)
        if row:
            lines.append(f"{emoji} {row}")

    bench = totw.get("bench") or []
    if bench:
        lines.append("🪑 Запас: " + ", ".join(esc(p["player_name"]) for p in bench))
    return _fit_html("\n".join(lines), CAPTION_MAX_CHARS)


def generate_totw_caption(
    totw: dict,
    division_name: str,
    start_round: int,
    end_round: int,
    use_ai: bool = True,
) -> str:
    """Подпись к картинке сборной: Gemini в голосе Темшика, при сбое — шаблон."""
    if use_ai and totw.get("xi"):
        from services.round_preview import CAPTION_MAX_CHARS, _call_gemini, _fit_html

        text = _call_gemini(_instruction(), _totw_for_model(totw, division_name, start_round, end_round), max_output_tokens=500)
        if text:
            return _fit_html(text, CAPTION_MAX_CHARS)
    return _fallback_totw_caption(totw, division_name, start_round, end_round)
