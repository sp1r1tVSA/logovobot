"""
services/mid_season_team_service.py

Символическая сборная 1-го круга дивизионов (Mid-Season Best XI & Bench):
  * calculate_mid_season_player_score — Mid-Season Performance Index игрока с весом «Игрок тура» (+15);
  * build_mid_season_lineup           — 4-3-3, капитан (MVP 1-го круга) и скамейка из четырёх (GK/DEF/MID/FWD);
  * build_mid_season_payload          — сбор статистики 1-го круга из database + собранная сборная;
  * generate_mid_season_caption       — подпись в голосе «Темшика» (Gemini) с шаблонным фолбэком.

Модуль синхронный: из хендлеров вызывается через asyncio.to_thread.
"""

import html
import logging

import database

logger = logging.getLogger(__name__)

FORMATION = "4-3-3"

GK, DEF, MID, FWD = "GK", "DEF", "MID", "FWD"

POSITION_LINE: dict[str, str] = {
    "GK": GK,
    "LB": DEF, "LWB": DEF, "CB": DEF, "RB": DEF, "RWB": DEF,
    "CDM": MID, "CM": MID, "CAM": MID,
    "LW": FWD, "LM": FWD, "RW": FWD, "RM": FWD, "ST": FWD, "CF": FWD,
}

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

MIRROR_SLOTS: dict[str, frozenset[str]] = {
    "LB": frozenset({"RB", "RWB"}),
    "RB": frozenset({"LB", "LWB"}),
    "LW": frozenset({"RW", "RM"}),
    "RW": frozenset({"LW", "LM"}),
}

SLOT_LABELS: dict[str, str] = {
    "GK": "GK", "LB": "LB", "LCB": "CB", "RCB": "CB", "RB": "RB",
    "CDM": "CDM", "LCM": "CM", "RCM": "CM", "LW": "LW", "ST": "ST", "RW": "RW",
}

# Веса очков Mid-Season Performance Index
POTR_POINTS = 15          # ⭐ Игрок тура (Player of the Round)
MVP_POINTS = 12           # 👑 Игрок матча (MVP)
GK_CLEAN_SHEET = 15
GK_LOW_CONCEDED = 10
GK_WIN = 3
DEF_CLEAN_SHEET = 10
DEF_GOAL = 15
DEF_ASSIST = 10
DEF_RELIABILITY = 8
MID_GOAL = 10
MID_ASSIST = 10
MID_CLEAN_SHEET = 5
FWD_GOAL = 12
FWD_ASSIST = 8
FWD_BRACE = 6
LOW_CONCEDED_PER_MATCH = 1.0


def position_line(position: str | None) -> str:
    """Линия игрока по позиции; неизвестная позиция считается атакой."""
    return POSITION_LINE.get((position or "").strip().upper(), FWD)


def _int(stat: dict, key: str) -> int:
    try:
        return int(stat.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _low_conceded(stat: dict) -> bool:
    """Клуб пропускал в первом круге не больше LOW_CONCEDED_PER_MATCH за матч."""
    matches = _int(stat, "matches")
    if matches <= 0:
        return False
    return _int(stat, "goals_conceded") / matches <= LOW_CONCEDED_PER_MATCH


def calculate_mid_season_player_score(player_stat: dict) -> float:
    """Mid-Season Performance Index игрока за 1-й круг с учётом званий «Игрок тура».

    ⭐ Игрок тура (POTR) : +15 за каждый титул.
    👑 Игрок матча (MVP) : +12 за каждую награду.
    GK : +15 за сухарь, +10 если клуб пропускал ≤1.0 за матч, +3 за победу.
    DEF: +10 за сухарь, +15 за гол, +10 за ассист, +8 за надежность обороны клуба.
    MID: +10 за гол, +10 за ассист, +5 за сухарь у CDM/CM.
    FWD: +12 за гол, +8 за ассист, +6 за каждый дубль/хет-трик.
    """
    position = (player_stat.get("position") or "").strip().upper()
    line = position_line(position)
    goals = _int(player_stat, "goals")
    assists = _int(player_stat, "assists")
    mvp = _int(player_stat, "mvp")
    potr_count = _int(player_stat, "potr_count")
    clean_sheets = _int(player_stat, "clean_sheets")

    score = POTR_POINTS * potr_count + MVP_POINTS * mvp

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
    """Сортировка кандидатов: очки -> титулы POTR -> Г+П -> MVP -> имя."""
    return (
        -player["score"],
        -_int(player, "potr_count"),
        -(_int(player, "goals") + _int(player, "assists")),
        -_int(player, "mvp"),
        player.get("player_name") or "",
    )


def build_mid_season_lineup(candidates: list[dict], max_per_club: int | None = None) -> dict:
    """Собрать 4-3-3 (11 основы + 4 на скамейке) из кандидатов 1-го круга.

    max_per_club: лимит игроков одного клуба в стартовом составе (None = без ограничений).
    """
    pool = []
    for c in candidates or []:
        if not (c.get("player_name") or "").strip():
            continue
        p = dict(c)
        p["position"] = (p.get("position") or "ST").strip().upper()
        p["line"] = position_line(p["position"])
        p["score"] = calculate_mid_season_player_score(p)
        pool.append(p)
    pool.sort(key=_rank_key)

    filled: dict[str, dict] = {}
    used: set[int] = set()
    per_club: dict[str, int] = {}

    def club_ok(p: dict) -> bool:
        if max_per_club is None or max_per_club <= 0:
            return True
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

    # 1. Своя позиция (затем зеркальный фланг), лучшие первыми
    for idx, p in enumerate(pool):
        if not club_ok(p):
            continue
        options = [slot for slot, _l, positions in SLOTS if p["position"] in positions]
        options += [slot for slot, mirror in MIRROR_SLOTS.items() if p["position"] in mirror]
        for slot in options:
            if slot not in filled:
                place(slot, idx, False)
                break

    # 2. Своя линия. 3. Любой полевой в полевой слот
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

    # Скамейка: 4 лучших оставшихся игрока строго по линиям (GK, DEF, MID, FWD)
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


def build_mid_season_payload(
    division_id: int,
    start_round: int | None = None,
    end_round: int | None = None,
    season_id: int | None = None,
    max_per_club: int | None = None,
) -> dict:
    """Собрать данные для инфографики и текста сборной 1-го круга."""
    division = database.get_division(division_id) or {}
    season = None
    try:
        if season_id is not None:
            season = database.get_season(season_id)
        else:
            season = database.get_active_season()
    except Exception:
        logger.exception("Mid-Season: could not load season %s", season_id)

    if start_round is None or end_round is None:
        bounds = database.get_division_first_half_bounds(division_id, season_id)
        if bounds:
            start_round, end_round = bounds
        else:
            start_round, end_round = 1, 15

    candidates = database.get_mid_season_stats(division_id, start_round, end_round, season_id)
    lineup = build_mid_season_lineup(candidates, max_per_club=max_per_club)

    # Статистика 1-го круга
    total_goals = sum(_int(c, "goals") for c in candidates)
    potr_leaders = [c for c in candidates if _int(c, "potr_count") > 0]
    potr_leaders.sort(key=lambda c: (-_int(c, "potr_count"), c.get("player_name") or ""))

    return {
        "division_id": division_id,
        "division_name": division.get("name") or f"Дивизион {division_id}",
        "division_code": division.get("code"),
        "season_id": (season or {}).get("id", season_id),
        "season_name": (season or {}).get("name") or "",
        "start_round": start_round,
        "end_round": end_round,
        "candidates_count": len(candidates),
        "total_goals": total_goals,
        "potr_leaders": potr_leaders[:5],
        **lineup,
    }


def build_all_divisions_mid_season_payload(
    season_id: int | None = None,
    max_per_club: int | None = None,
) -> dict:
    """Собрать данные для инфографики и текста главной сборной 1-го круга всех дивизионов."""
    season = None
    try:
        if season_id is not None:
            season = database.get_season(season_id)
        else:
            season = database.get_active_season()
    except Exception:
        logger.exception("Mid-Season All-Divisions: could not load season %s", season_id)

    candidates = database.get_all_divisions_mid_season_stats(season_id)
    lineup = build_mid_season_lineup(candidates, max_per_club=max_per_club)

    total_goals = sum(_int(c, "goals") for c in candidates)
    potr_leaders = [c for c in candidates if _int(c, "potr_count") > 0]
    potr_leaders.sort(key=lambda c: (-_int(c, "potr_count"), c.get("player_name") or ""))

    return {
        "is_league_wide": True,
        "division_id": None,
        "division_name": "ВСЕ ДИВИЗИОНЫ",
        "division_code": "ALL",
        "season_id": (season or {}).get("id", season_id),
        "season_name": (season or {}).get("name") or "",
        "start_round": 1,
        "end_round": None,
        "candidates_count": len(candidates),
        "total_goals": total_goals,
        "potr_leaders": potr_leaders[:5],
        **lineup,
    }


def _stat_line(p: dict) -> str:
    """Короткая строка цифр игрока: «⭐ 2x POTR · 14+8 · 3 MVP · 4 сух.»."""
    parts = []
    potr = _int(p, "potr_count")
    if potr > 0:
        parts.append(f"⭐ {potr}x Игрок тура")
    goals, assists = _int(p, "goals"), _int(p, "assists")
    if goals or assists:
        parts.append(f"{goals}+{assists}")
    if _int(p, "mvp"):
        parts.append(f"{_int(p, 'mvp')} MVP")
    if p.get("line") in (GK, DEF) and _int(p, "clean_sheets"):
        parts.append(f"{_int(p, 'clean_sheets')} сух.")
    return " · ".join(parts)


def _instruction(is_league: bool = False) -> str:
    from services.round_preview import CAPTION_MAX_CHARS, _COMMON_RULES

    title = "«СБОРНАЯ 1-ГО КРУГА ЛИГИ» (главная сборная чемпионата всех дивизионов)" if is_league else "«СБОРНАЯ 1-ГО КРУГА» (итоги экватора сезона)"
    heading = "1. Эпичный заголовок (👑 СБОРНАЯ 1-ГО КРУГА ЛИГИ, все дивизионы).\n" if is_league else "1. Эпичный заголовок (🏆 СБОРНАЯ 1-ГО КРУГА, дивизион, туры 1–N).\n"
    return (
        "Ты — Темшик, аналитик и голос лиги «Логово Фифарей»: душевный 30+ мужик, батейный юмор, "
        "но по цифрам — строгий и авторитетный футбольный эксперт.\n\n"
        f"ЗАДАЧА: написать ПОДПИСЬ к картинке {title} по переданному JSON.\n"
        "На картинке уже видна вся расстановка 4-3-3 с карточками игроков — не перечисляй всех списком.\n"
        "СТРУКТУРА:\n"
        f"{heading}"
        "2. Главный герой / Капитан 1-го круга (его голы, ассисты, титулы «Игрок тура»).\n"
        "3. 2–3 ярких акцента экватора: кто стал непробиваемой стеной, главные бомбардиры и обладатели «Игрока тура».\n"
        "4. Короткая интрига перед стартом 2-го круга.\n\n"
        f"{_COMMON_RULES}"
        f"- Уложись в {CAPTION_MAX_CHARS} символов — это подпись к фото, лимит Telegram строгий.\n"
    )


def _mid_season_for_model(payload: dict, division_name: str, start_round: int, end_round: int) -> dict:
    def slim(p: dict) -> dict:
        return {
            "позиция": p.get("slot_label"),
            "игрок": p.get("player_name"),
            "клуб": p.get("team_name"),
            "очки": int(p.get("score") or 0),
            "игрок_тура": _int(p, "potr_count"),
            "голы": _int(p, "goals"),
            "ассисты": _int(p, "assists"),
            "MVP": _int(p, "mvp"),
            "сухари": _int(p, "clean_sheets") if p.get("line") in (GK, DEF, MID) else None,
        }

    is_league = bool(payload.get("is_league_wide"))
    captain = payload.get("captain")
    return {
        "событие": "Сборная 1-го круга Лиги (все дивизионы)" if is_league else "Сборная 1-го круга",
        "дивизион": "Все дивизионы" if is_league else division_name,
        "туры": "1-й круг" if is_league else f"{start_round}–{end_round}",
        "капитан": slim(captain) if captain else None,
        "сборная_11": [slim(p) for p in payload.get("xi", [])],
        "запас_4": [slim(p) for p in payload.get("bench", [])],
    }


def _fallback_caption(payload: dict, division_name: str, start_round: int, end_round: int) -> str:
    from services.round_preview import CAPTION_MAX_CHARS, _fit_html

    esc = html.escape
    if payload.get("is_league_wide"):
        lines = [
            "👑 <b>СБОРНАЯ 1-ГО КРУГА ЛИГИ · ВСЕ ДИВИЗИОНЫ</b>",
            "<i>15 лучших футболистов чемпионата • Экватор сезона</i>",
        ]
    else:
        lines = [
            f"🏆 <b>СБОРНАЯ 1-ГО КРУГА · ТУРЫ {start_round}–{end_round}</b>",
            f"<i>{esc(str(division_name))} • Экватор сезона</i>",
        ]
    xi = payload.get("xi") or []
    if not xi:
        lines.append("")
        lines.append("Матчи 1-го круга ещё не завершены — сборную собрать не из кого.")
        return _fit_html("\n".join(lines), CAPTION_MAX_CHARS)

    captain = payload.get("captain")
    if captain:
        stat = _stat_line(captain)
        lines.append("")
        lines.append(
            f"👑 <b>MVP и Капитан 1-го круга:</b> {esc(captain['player_name'])} ({esc(captain.get('team_name') or '')})"
            f" — {int(captain['score'])} очк." + (f" · {stat}" if stat else "")
        )

    def slot_names(codes: tuple[str, ...]) -> str:
        by_slot = {p["slot"]: p for p in xi}
        return ", ".join(esc(by_slot[c]["player_name"]) for c in codes if c in by_slot)

    lines.append("")
    gk = slot_names(("GK",))
    if gk:
        lines.append(f"🧤 <b>Вратарь:</b> {gk}")
    defenders = slot_names(("LB", "LCB", "RCB", "RB"))
    if defenders:
        lines.append(f"🛡 <b>Оборона:</b> {defenders}")
    midfielders = slot_names(("CDM", "LCM", "RCM"))
    if midfielders:
        lines.append(f"⚙️ <b>Полузащита:</b> {midfielders}")
    attackers = slot_names(("LW", "ST", "RW"))
    if attackers:
        lines.append(f"⚡️ <b>Атака:</b> {attackers}")

    bench = payload.get("bench") or []
    if bench:
        lines.append("")
        lines.append("🪑 <b>Запас:</b> " + ", ".join(esc(p["player_name"]) for p in bench))

    potr_leaders = payload.get("potr_leaders") or []
    if potr_leaders:
        names = ", ".join(f"{esc(p['player_name'])} ({_int(p, 'potr_count')}x)" for p in potr_leaders[:3])
        lines.append(f"⭐ <b>Лидеры по «Игроку тура»:</b> {names}")

    lines.append("\n<i>Впереди решающий 2-й круг и битва за чемпионство!</i>")
    return _fit_html("\n".join(lines), CAPTION_MAX_CHARS)


def generate_mid_season_caption(
    payload: dict,
    division_name: str,
    start_round: int,
    end_round: int,
    use_ai: bool = True,
) -> str:
    """Подпись к картинке сборной 1-го круга: Gemini в голосе Темшика, при сбое — шаблон."""
    is_league = bool(payload.get("is_league_wide"))
    if use_ai and payload.get("xi"):
        from services.round_preview import CAPTION_MAX_CHARS, _call_gemini, _fit_html

        text = _call_gemini(_instruction(is_league), _mid_season_for_model(payload, division_name, start_round, end_round), max_output_tokens=600)
        if text:
            return _fit_html(text, CAPTION_MAX_CHARS)
    return _fallback_caption(payload, division_name, start_round, end_round)
