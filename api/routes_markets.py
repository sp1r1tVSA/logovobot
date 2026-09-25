"""
api/routes_markets.py

REST API handlers for open tours, matches schedule, and relational odds lines.
"""

import asyncio
import logging
from aiohttp import web
import database
from api.auth import get_authenticated_user, check_user_access
import services.odds_engine as odds_engine
from services import line_refresh

logger = logging.getLogger(__name__)

# Статусы сыгранных матчей: такие игры не попадают в линию ставок.
FINISHED_MATCH_STATUSES = ("confirmed", "completed", "finished", "cancelled")


def _load_season_tours(div_id):
    """Туры активного сезона, открытые для игры (is_open) или для ставок (bets_open)."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        act = database.get_active_season()
        s_id = act["id"] if act else 1
        query = """
            SELECT
                r.round_number, r.deadline, r.division_id, r.season_id,
                r.is_open, COALESCE(r.bets_open, 0) AS bets_open,
                COUNT(m.id) as total_matches,
                SUM(CASE WHEN m.status NOT IN ('confirmed', 'completed') THEN 1 ELSE 0 END) as unplayed_matches
            FROM rounds r
            LEFT JOIN matches m
              ON m.round_number = r.round_number
             AND COALESCE(m.division_id, 1) = r.division_id
             AND COALESCE(m.season_id, 1) = r.season_id
            WHERE (r.is_open = 1 OR COALESCE(r.bets_open, 0) = 1)
              AND r.season_id = ?
        """
        params = [s_id]
        if div_id is not None:
            query += " AND r.division_id = ?"
            params.append(div_id)
        query += " GROUP BY r.round_number, r.deadline, r.division_id, r.season_id, r.is_open, r.bets_open ORDER BY r.round_number ASC"
        cursor.execute(query, params)
        return s_id, [dict(r) for r in cursor.fetchall()]


async def handle_get_tours(request: web.Request) -> web.Response:
    """
    GET /api/markets/tours
    Returns all open tours with their matches and active odds.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)

    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    user_id = user_info["id"]
    if not check_user_access(user_id):
        return web.json_response({
            "status": "error",
            "error": "access_restricted",
            "message": "Logovo.bet временно недоступен."
        }, status=403)

    division_id_param = request.query.get("division_id")
    div_id = int(division_id_param) if division_id_param and division_id_param.isdigit() else None

    s_id, season_tours = await asyncio.to_thread(_load_season_tours, div_id)

    for t in season_tours:
        t["is_early"] = (not t.get("is_open")) and bool(t.get("bets_open"))

    open_tours = season_tours
    if not open_tours:
        open_tours = await asyncio.to_thread(database.get_open_betting_tours, division_id=div_id)

    results = []
    bans_by_division: dict = {}

    for t in open_tours:
        r_num = t["round_number"]
        if t.get("total_matches") == 0:
            continue
        # Линия переоценивается в потоке и не чаще раза в минуту на тур:
        # раньше каждый запрос лобби пересчитывал её прямо в event loop бота.
        await line_refresh.ensure_round_line(r_num, division_id=div_id, season_id=s_id)

        markets = await asyncio.to_thread(database.get_active_bet_markets, r_num, division_id=div_id, season_id=s_id)
        round_matches = await asyncio.to_thread(database.get_matches_by_round, r_num, division_id=div_id, season_id=s_id)

        matches_list = []
        seen_match_ids = set()

        for m in markets:
            m_id = m["match_id"]
            m_div = m.get("division_id") or div_id or 1
            if m_div not in bans_by_division:
                bans_by_division[m_div] = await asyncio.to_thread(_safe_bans, m_div)
            seen_match_ids.add(m_id)
            # Сыгранный матч в линии не нужен: его результат живёт в «Турнирах».
            if (m.get("match_status") or "pending") in FINISHED_MATCH_STATUSES:
                continue

            matches_list.append({
                "match_id": m["match_id"],
                "tour": m["tour"],
                "team1_name": m["team1_name"],
                "team2_name": m["team2_name"],
                "player1_username": m.get("player1_username"),
                "player2_username": m.get("player2_username"),
                "status": m.get("match_status") or "pending",
                "division_id": m_div,
                "player1_score": m.get("player1_score"),
                "player2_score": m.get("player2_score"),
                "is_line": True,
                # Виды ставок, закрытые в панели: клиент рисует их закрытыми.
                "banned": sorted(bans_by_division[m_div]),
                "odds": {
                    "p1": round(m["odd_p1"], 2),
                    "x": round(m["odd_x"], 2),
                    "p2": round(m["odd_p2"], 2),
                    "tb25": round(m["odd_tb25"], 2),
                    "tm25": round(m["odd_tm25"], 2),
                    "btts_yes": round(m["odd_btts_yes"], 2),
                    "btts_no": round(m["odd_btts_no"], 2)
                }
            })

        # Матчи тура без сгенерированного рынка: отдаём их помеченными is_line = False,
        # чтобы клиент не принял заглушку за открытую линию. Сыгранные игры в ответ
        # для линии не подмешиваем вовсе — их место в разделе «Турниры».
        for rm in round_matches:
            rm_id = rm["id"]
            if rm_id in seen_match_ids:
                continue
            seen_match_ids.add(rm_id)
            rm_status = rm.get("status") or "pending"
            if rm_status in FINISHED_MATCH_STATUSES:
                continue
            matches_list.append({
                "match_id": rm_id,
                "tour": rm.get("round_number") or r_num,
                "team1_name": rm.get("player1_team") or "Команда 1",
                "team2_name": rm.get("player2_team") or "Команда 2",
                "player1_username": rm.get("player1_username") or rm.get("player1_nickname"),
                "player2_username": rm.get("player2_username") or rm.get("player2_nickname"),
                "status": rm_status,
                "division_id": rm.get("division_id") or div_id or 1,
                "player1_score": rm.get("player1_score"),
                "player2_score": rm.get("player2_score"),
                "is_line": False,
                "odds": None
            })

        results.append({
            "round_number": r_num,
            "deadline": t.get("deadline"),
            "total_matches": t.get("total_matches", len(matches_list)),
            "unplayed_matches": t.get("unplayed_matches", len(matches_list)),
            "is_early": bool(t.get("is_early")),
            "matches": matches_list
        })

    return web.json_response({
        "status": "ok",
        "tours": results
    })


def _safe_bans(division_id) -> set:
    """Запреты видов ставок для витрины; сбой — показать всё (приём проверит RiskEngine)."""
    try:
        return database.get_bet_bans(division_id)
    except Exception:
        logger.warning("Could not read bet bans for division %s", division_id, exc_info=True)
        return set()


def _safe_match_bans(match_id) -> set:
    try:
        return database.get_match_bet_bans(match_id)
    except Exception:
        logger.warning("Could not read bet bans for match %s", match_id, exc_info=True)
        return set()


def _load_match_row(match_id):
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        return cursor.fetchone()


async def handle_get_match_markets(request: web.Request) -> web.Response:
    """
    GET /api/matches/{id}/markets
    Returns full categorized market tree with all selections and odds.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)

    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    user_id = user_info["id"]
    if not check_user_access(user_id):
        return web.json_response({"status": "error", "error": "access_restricted", "message": "Logovo.bet временно недоступен."}, status=403)

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID матча."}, status=400)

    match_row = await asyncio.to_thread(_load_match_row, match_id)

    if not match_row:
        return web.json_response({"status": "error", "message": "Матч не найден."}, status=404)

    is_cup = database.match_is_cup(match_row)
    t1 = match_row["player1_team"] or "Команда 1"
    t2 = match_row["player2_team"] or "Команда 2"
    if is_cup:
        # Пара кубка живёт в `cup_series`: у заголовка серии своих имён нет.
        pair = await asyncio.to_thread(database.get_cup_series_pair, match_row["cup_series_id"])
        if pair:
            t1, t2 = pair

    markets = await asyncio.to_thread(odds_engine.get_match_markets, match_id)
    if not markets and not is_cup:
        # Generate on the fly if not existing. Кубок сюда не идёт: его линию
        # выставляет панель этапа, а лиговая модель записала бы ему ничью.
        markets = await asyncio.to_thread(odds_engine.generate_match_markets, match_id, t1, t2)

    # Запрещённые в панели виды ставок в роспись не попадают.
    bans = await asyncio.to_thread(_safe_match_bans, match_id)
    if bans:
        markets = [mkt for mkt in markets
                   if database.bet_ban_group(mkt.get("market_key")) not in bans]

    # Normalize field name: alias odds_value -> current_odd for frontend consistency
    for mkt in markets:
        for sel in mkt.get("selections", []):
            if "current_odd" not in sel:
                sel["current_odd"] = sel.get("odds_value", 1.90)

    return web.json_response({
        "status": "ok",
        "match_id": match_id,
        "team1_name": t1,
        "team2_name": t2,
        "match_status": match_row["status"],
        "markets": markets,
        "banned": sorted(bans),
    })


async def handle_get_odds_history(request: web.Request) -> web.Response:
    """
    GET /api/markets/{id}/odds-history?selection_key=p1
    Returns chronological timeline of odds changes for a market selection.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)

    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    user_id = user_info["id"]
    if not check_user_access(user_id):
        return web.json_response({
            "status": "error",
            "error": "access_restricted",
            "message": "Logovo.bet временно недоступен."
        }, status=403)

    try:
        market_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID рынка."}, status=400)

    selection_key = request.query.get("selection_key")
    history = await asyncio.to_thread(
        odds_engine.get_odds_history, market_id=market_id, selection_key=selection_key, limit=30
    )

    return web.json_response({
        "status": "ok",
        "market_id": market_id,
        "selection_key": selection_key,
        "history": history
    })
