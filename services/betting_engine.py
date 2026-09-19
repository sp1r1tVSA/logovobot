"""
services/betting_engine.py

Logovo.bet — Mathematical Odds Calculation & Market Generation Engine.
Calculates realistic sportsbook odds based on:
1. Elo / Current Tournament Standings (Wins, Goal Difference)
2. Average Goal Output (xG / Over 2.5 Total)
3. Both Teams to Score (BTTS)
4. Built-in 5% Bookmaker Margin (Vigorish)
"""

import math
import logging
import database
from services import preseason_seeds
from services.preseason_seeds import NEUTRAL_STRENGTH

logger = logging.getLogger(__name__)

from services.poisson_odds import TARGET_MARGIN, calculate_poisson_market_odds

# The line (bet_markets) and the relational markets placement validates against
# must price with the same margin, or every line pick fails with ODDS_CHANGED.
BOOKMAKER_MARGIN = TARGET_MARGIN

# Ровно столько центральных матчей тура попадает в линию БК.
CENTRAL_MATCHES_PER_ROUND = 4

# Сколько сыгранных туров нужно, чтобы таблица набрала максимальный вес в отборе.
TABLE_FULL_WEIGHT_ROUNDS = 6

# Потолок веса таблицы: предсезонный рейтинг остаётся базой до конца сезона.
MAX_TABLE_WEIGHT = 0.8

# Вес разрыва в силе в формуле привлекательности: чем дальше команды друг от
# друга, тем менее интересен матч.
_GAP_PENALTY = 1.5

# Статусы матча, при которых ставить уже не на что.
_PLAYED_STATUSES = ("completed", "confirmed")


def _get_team_strength_score(
    standings: list[dict],
    team_name: str,
    nickname: str | None = None
) -> float:
    """
    Calculate relative strength score based on tournament standings and preseason player seed.
    Blends preseason player seed rating with standings table performance via _table_weight.
    """
    norm_team = database.normalize_team_name(team_name).lower()

    # 1. Resolve player nickname if not provided directly
    resolved_nick = nickname
    if not resolved_nick:
        for row in standings:
            if database.normalize_team_name(row.get("team_name", "")).lower() == norm_team:
                resolved_nick = row.get("username")
                break
    if not resolved_nick:
        try:
            u = database.find_user_by_team(team_name)
            if u:
                resolved_nick = u.get("username")
        except Exception:
            pass

    # 2. Preseason seed strength (0.0 .. 1.0, 0.5 is neutral)
    seed = preseason_seeds.get_seed_strength(resolved_nick, team_name)
    if seed is None:
        seed = NEUTRAL_STRENGTH
    seed_score = max(1.0, 10.0 + (seed - 0.5) * 10.0)

    # 3. Table weight and standings points/GD score
    played_rounds = max((row.get("played") or 0) for row in standings) if standings else 0
    table_w = _table_weight(played_rounds)

    table_score = 10.0
    for row in standings:
        row_team = database.normalize_team_name(row.get("team_name", "")).lower()
        if row_team == norm_team:
            played = max(1, row.get("played", 0))
            pts = row.get("points", 0)
            if "gd" in row:
                gd = row.get("gd") or 0
            else:
                gd = (row.get("goals_scored") or 0) - (row.get("goals_conceded") or 0)
            ppg = pts / played
            gd_pg = gd / played
            table_score = max(1.0, 10.0 + (ppg * 4.0) + (gd_pg * 1.5))
            break

    # Blend seed rating and table performance smoothly
    return (1.0 - table_w) * seed_score + table_w * table_score


def _match_team_names(m: dict) -> tuple[str, str]:
    """Имена клубов пары в том же порядке, в каком они уйдут в рынок."""
    t1 = m.get("player1_team") or m.get("team1") or "Команда 1"
    t2 = m.get("player2_team") or m.get("team2") or "Команда 2"
    return t1, t2


def _table_weight(played_rounds: int) -> float:
    """Насколько таблица перевешивает предсезонный рейтинг при данном числе туров.

    0.0 до первого сыгранного тура, дальше линейный разгон до `MAX_TABLE_WEIGHT`.
    Потолок ниже единицы намеренно: рейтинг участников остаётся базой весь сезон,
    поэтому оторвавшаяся серия результатов не стирает статусность имени целиком.
    """
    if played_rounds <= 0:
        return 0.0
    return min(MAX_TABLE_WEIGHT, played_rounds / TABLE_FULL_WEIGHT_ROUNDS)


def _build_strength_index(standings: list[dict], table_weight: float) -> dict[str, float]:
    """Индекс «нормализованное имя клуба → сила 0.0..1.0» для отбора матчей.

    Сила — смесь двух источников в одной шкале:
      * предсезонный рейтинг участника (`services/preseason_seeds.py`), вес `1 − w`;
      * место в таблице, вес `w` (см. `_table_weight`).

    Место берётся позицией в списке: `get_standings` возвращает строки, уже
    отсортированные, и отдельной колонки `place` в них нет. Оно нормируется в
    0.0..1.0 (первое место — 1.0), потому что очки растут весь сезон и в смеси с
    фиксированной шкалой рейтинга быстро перестали бы с ней соотноситься.

    Участник вне рейтинга получает `NEUTRAL_STRENGTH` — не поднимается и не тонет.
    """
    total = len(standings)
    index: dict[str, float] = {}
    for position, row in enumerate(standings, start=1):
        key = database.normalize_team_name(row.get("team_name", "")).lower()
        if not key:
            continue
        table_strength = (total - position) / (total - 1) if total > 1 else NEUTRAL_STRENGTH
        seed = preseason_seeds.get_seed_strength(row.get("username"), row.get("team_name"))
        if seed is None:
            seed = NEUTRAL_STRENGTH
        index[key] = (1.0 - table_weight) * seed + table_weight * table_strength
    return index


def select_top_round_matches(
    tour: int,
    division_id: int | None = None,
    season_id: int | None = None,
    limit: int = CENTRAL_MATCHES_PER_ROUND,
) -> list[dict]:
    """Отбирает ровно 4 самых статусных матча тура для выставления в линию БК.

    Статусность пары — `Score = (S₁ + S₂) − |S₁ − S₂| × 1.5`, где `S` — сила
    участника в 0.0..1.0. Топ-матч по этой формуле это пара сильных И близких по
    силе соперников: разгром лидером аутсайдера в линию не идёт.

    Сила смешивает предсезонный рейтинг участника с местом в таблице, и вес
    таблицы растёт по мере сыгранных туров (см. `_build_strength_index`). До
    первого тура таблицы нет вовсе, и четвёрку целиком определяет рейтинг — без
    него все пары получали одинаковый Score, а отбор вырождался в «первые четыре
    матча по id».

    Уже сыгранные матчи в линию не попадают. При равенстве Score порядок
    определяется id матча, чтобы повторный вызов давал тот же набор.
    Возвращает строки матчей с добавленным ключом `line_score`.
    """
    matches = database.get_matches_by_round(tour, division_id=division_id, season_id=season_id)
    if season_id is not None:
        matches = [m for m in matches if m.get("season_id") in (season_id, None)]
    matches = [m for m in matches if m.get("status") not in _PLAYED_STATUSES]
    if not matches:
        return []

    standings: list[dict] = []
    try:
        standings = database.get_standings(division_id=division_id, season_id=season_id)
    except Exception as e:
        logger.debug(f"Could not load standings for round selection: {e}")

    played_rounds = max((row.get("played") or 0) for row in standings) if standings else 0
    table_weight = _table_weight(played_rounds)
    strength = _build_strength_index(standings, table_weight)

    def team_strength(team_name: str, nickname: str | None) -> float:
        """Сила клуба: из таблицы, иначе из рейтинга, иначе нейтральная.

        Клуба может не быть в таблице — например, тренер ещё не привязан к нему.
        Тогда остаётся только рейтинг, и он ищется по логину из строки матча:
        `get_matches_by_round` отдаёт его в `playerN_nickname`.
        """
        key = database.normalize_team_name(team_name).lower()
        if key in strength:
            return strength[key]
        seed = preseason_seeds.get_seed_strength(nickname, team_name)
        return seed if seed is not None else NEUTRAL_STRENGTH

    scored: list[dict] = []
    for m in matches:
        t1, t2 = _match_team_names(m)
        s1 = team_strength(t1, m.get("player1_nickname"))
        s2 = team_strength(t2, m.get("player2_nickname"))
        score = (s1 + s2) - abs(s1 - s2) * _GAP_PENALTY

        row = dict(m)
        row["line_score"] = round(float(score), 3)
        scored.append(row)

    scored.sort(key=lambda r: (-r["line_score"], r.get("id") or 0))
    return scored[:limit]


def calculate_match_odds(
    team1: str,
    team2: str,
    division_id: int | None = None,
    season_id: int | None = None,
    p1_nick: str | None = None,
    p2_nick: str | None = None
) -> dict:
    """
    Calculate realistic European decimal odds for a fixture.
    Returns:
    {
        'odd_p1': float, 'odd_x': float, 'odd_p2': float,
        'odd_tb25': float, 'odd_tm25': float,
        'odd_btts_yes': float, 'odd_btts_no': float
    }
    """
    standings = []
    try:
        standings = database.get_standings(division_id=division_id, season_id=season_id)
    except Exception as e:
        logger.debug(f"Could not load standings for odds: {e}")

    s1 = _get_team_strength_score(standings, team1, nickname=p1_nick)
    s2 = _get_team_strength_score(standings, team2, nickname=p2_nick)

    # Compute realistic odds using unified Poisson model (7.5% margin, realistic draw odds, dynamic totals)
    odds = calculate_poisson_market_odds(s1, s2, margin=BOOKMAKER_MARGIN)

    return {
        "odd_p1": odds["odd_p1"],
        "odd_x": odds["odd_x"],
        "odd_p2": odds["odd_p2"],
        "odd_tb25": odds["odd_tb25"],
        "odd_tm25": odds["odd_tm25"],
        "odd_btts_yes": odds["odd_btts_yes"],
        "odd_btts_no": odds["odd_btts_no"]
    }


# Line tile field -> (market_key, selection_key) in market_selections.
_TILE_SELECTIONS = {
    "odd_p1": ("1x2", "p1"),
    "odd_x": ("1x2", "x"),
    "odd_p2": ("1x2", "p2"),
    "odd_tb25": ("total_goals", "over_2.5"),
    "odd_tm25": ("total_goals", "under_2.5"),
    "odd_btts_yes": ("btts", "btts_yes"),
    "odd_btts_no": ("btts", "btts_no"),
}


def _price_match(match_id: int, t1: str, t2: str, odds: dict) -> dict:
    """Reprice the match's relational markets and return the tile odds.

    The relational markets step toward the model by at most ±15% per model
    change (odds_engine.smooth_match_repricing), and placement validates line
    picks against them — so the tile shows their odds, not the raw model ones.
    The raw ``odds`` stay only as a fallback if the markets cannot be built.
    """
    try:
        from services.odds_engine import generate_match_markets
        markets = generate_match_markets(match_id, t1, t2)
    except Exception as e:
        logger.debug(f"Could not update relational markets for match #{match_id}: {e}")
        return odds
    priced = {
        (m["market_key"], s["selection_key"]): s["odds_value"]
        for m in markets for s in m["selections"]
    }
    return {
        field: priced.get(key, odds[field])
        for field, key in _TILE_SELECTIONS.items()
    }


def generate_round_markets(tour: int, division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """
    Generate or update odds markets for the central matches of a tour, optionally filtered by division and season.

    В линию выставляются ровно `CENTRAL_MATCHES_PER_ROUND` самых статусных
    матчей тура (см. `select_top_round_matches`). Рынки остальных матчей тура
    гасятся, чтобы после пересчёта в линии не оставалось лишних пар.
    """
    if season_id is None:
        act = database.get_active_season()
        season_id = act["id"] if act else 1

    selected = select_top_round_matches(tour, division_id=division_id, season_id=season_id)
    keep_ids = [m.get("id") for m in selected if m.get("id")]

    # Сначала убираем неактуальные рынки тура, потом выставляем новые:
    # матчи с уже принятыми ставками из линии не выпадают (см. prune_round_markets).
    try:
        database.prune_round_markets(tour, keep_ids, division_id=division_id, season_id=season_id)
    except Exception as e:
        logger.warning(f"Could not prune stale markets for Tour #{tour}: {e}")

    markets = []

    for m in selected:
        m_id = m.get("id")
        # Линию начавшегося тура не воскрешаем: её закрыл старт тура, а
        # показ линии (Mini App, Telegram) вызывает генерацию и для него.
        if not database.match_line_is_open(m_id):
            continue
        t1, t2 = _match_team_names(m)
        p1_nick = m.get("player1_nickname") or m.get("player1_username")
        p2_nick = m.get("player2_nickname") or m.get("player2_username")

        odds = calculate_match_odds(
            t1, t2,
            division_id=division_id,
            season_id=season_id,
            p1_nick=p1_nick,
            p2_nick=p2_nick
        )
        try:
            # Матч мог быть погашен раньше, когда не входил в центральные.
            database.reopen_match_markets(m_id)
        except Exception as e:
            logger.debug(f"Could not reopen markets for match #{m_id}: {e}")
        odds = _price_match(m_id, t1, t2, odds)
        database.save_bet_market(
            match_id=m_id,
            tour=tour,
            team1_name=t1,
            team2_name=t2,
            odd_p1=odds["odd_p1"],
            odd_x=odds["odd_x"],
            odd_p2=odds["odd_p2"],
            odd_tb25=odds["odd_tb25"],
            odd_tm25=odds["odd_tm25"],
            odd_btts_yes=odds["odd_btts_yes"],
            odd_btts_no=odds["odd_btts_no"]
        )

        markets.append({
            "match_id": m_id,
            "tour": tour,
            "team1_name": t1,
            "team2_name": t2,
            **odds
        })

    logger.info(f"✅ Generated Logovo.bet markets for {len(markets)} central matches in Tour #{tour} (division={division_id}, season={season_id})")
    return markets


def regenerate_all_active_markets() -> int:
    """
    Recalculate and refresh odds for the unplayed matches already in the line.
    Ensures that existing fixtures reflect the current calibrated Poisson engine without stale odds.

    Only matches with an active bet_markets row are repriced: save_bet_market
    reactivates a row, so touching every unplayed match would put back the
    non-central pairs generate_round_markets pruned from the line.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                m.id, m.round_number, m.division_id, m.season_id,
                COALESCE(m.player1_team, u1.team_name) AS player1_team,
                COALESCE(m.player2_team, u2.team_name) AS player2_team,
                u1.username AS player1_nickname,
                u2.username AS player2_nickname,
                m.status
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE m.status NOT IN ('completed', 'confirmed', 'cancelled')
              AND EXISTS (SELECT 1 FROM bet_markets bm WHERE bm.match_id = m.id AND bm.is_active = 1)
        """)
        matches = [dict(r) for r in cursor.fetchall()]

    updated_count = 0
    for m in matches:
        m_id = m.get("id")
        t1, t2 = _match_team_names(m)
        if not t1 or not t2 or t1 in ("Команда 1", "") or t2 in ("Команда 2", ""):
            continue
        p1_nick = m.get("player1_nickname")
        p2_nick = m.get("player2_nickname")

        odds = calculate_match_odds(
            t1, t2,
            division_id=m.get("division_id"),
            season_id=m.get("season_id"),
            p1_nick=p1_nick,
            p2_nick=p2_nick
        )
        odds = _price_match(m_id, t1, t2, odds)
        database.save_bet_market(
            match_id=m_id,
            tour=m.get("round_number") or 1,
            team1_name=t1,
            team2_name=t2,
            odd_p1=odds["odd_p1"],
            odd_x=odds["odd_x"],
            odd_p2=odds["odd_p2"],
            odd_tb25=odds["odd_tb25"],
            odd_tm25=odds["odd_tm25"],
            odd_btts_yes=odds["odd_btts_yes"],
            odd_btts_no=odds["odd_btts_no"]
        )
        updated_count += 1

    logger.info(f"🔄 Recalculated Poisson betting markets for {updated_count} active matches.")
    return updated_count

