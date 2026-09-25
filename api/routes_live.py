"""
api/routes_live.py

Logovo.bet — Live Match Center & Odds Movement API Routes.
Provides:
- GET /api/live
- GET /api/live/{id}
- GET /api/live/{id}/events
- GET /api/live/{id}/stats
- GET /api/live/{id}/markets
- GET /api/odds/movers
Strict rule: NO FAKE DATA. If live feed is unavailable, returns explicit "LIVE DATA UNAVAILABLE".
"""

import logging
from typing import Any, Optional
from aiohttp import web
import database
from api.auth import get_authenticated_user, check_user_access
from services.live_ingestion import (
    get_live_events,
    get_live_match_state,
    get_live_statistics,
)
from services.odds_movers import get_odds_movers
from services.sports.freshness import evaluate_match_freshness
from services.sports_provider import get_sports_provider


logger = logging.getLogger(__name__)


def _auth(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Validate initData and the rollout gate. Returns (user_info, error_response)."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return None, web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not check_user_access(user_info["id"]):
        return None, web.json_response({"status": "error", "error": "LOGOVO_LOCKDOWN"}, status=403)
    return user_info, None


async def handle_get_live_matches(request: web.Request) -> web.Response:
    """
    GET /api/live
    List all ongoing live matches.
    """
    _, err = _auth(request)
    if err is not None:
        return err

    division_id_str = request.query.get("division_id")
    season_id_str = request.query.get("season_id")

    div_id = int(division_id_str) if division_id_str and division_id_str.isdigit() else None
    season_id = int(season_id_str) if season_id_str and season_id_str.isdigit() else None

    provider = get_sports_provider()
    provider_status = provider.get_provider_status()

    with database.transaction() as conn:
        cursor = conn.cursor()
        base_query = """
            SELECT lms.*, m.player1_team, m.player2_team, m.round_number
            FROM live_match_states lms
            JOIN matches m ON lms.match_id = m.id
            WHERE lms.status IN ('LIVE', 'HALFTIME')
        """
        params: list[Any] = []
        if div_id is not None:
            base_query += " AND lms.division_id = ?"
            params.append(div_id)
        if season_id is not None:
            base_query += " AND lms.season_id = ?"
            params.append(season_id)

        base_query += " ORDER BY lms.match_id DESC"
        cursor.execute(base_query, params)
        matches = [dict(r) for r in cursor.fetchall()]
        for m in matches:
            m["freshness"] = evaluate_match_freshness(m.get("last_updated_at"))

    return web.json_response({
        "status": "ok",
        "provider_status": provider_status,
        "count": len(matches),
        "matches": matches
    })


async def handle_get_live_match_detail(request: web.Request) -> web.Response:
    """
    GET /api/live/{id}
    Retrieve full real-time match state, period, and minute.
    """
    _, err = _auth(request)
    if err is not None:
        return err

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID матча."}, status=400)

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT lms.*, m.player1_team, m.player2_team, m.round_number, m.status as match_status
            FROM matches m
            LEFT JOIN live_match_states lms ON m.id = lms.match_id
            WHERE m.id = ?
        """, (match_id,))
        m = cursor.fetchone()

        if not m:
            return web.json_response({"status": "error", "message": "Матч не найден."}, status=404)

        match_data = dict(m)

    freshness = evaluate_match_freshness(match_data.get("last_updated_at"))
    match_data["freshness"] = freshness
    provider = get_sports_provider()
    return web.json_response({
        "status": "ok",
        "match": match_data,
        "freshness": freshness,
        "provider_status": provider.get_provider_status()
    })


async def handle_get_live_events(request: web.Request) -> web.Response:
    """
    GET /api/live/{id}/events
    Chronological live match timeline (goals, cards, substitutions, VAR).
    """
    _, err = _auth(request)
    if err is not None:
        return err

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID матча."}, status=400)

    events = get_live_events(match_id, limit=100)
    return web.json_response({
        "status": "ok",
        "match_id": match_id,
        "events": events
    })


async def handle_get_live_stats(request: web.Request) -> web.Response:
    """
    GET /api/live/{id}/stats
    Real-time in-play statistics. Unavailable metrics are strictly null (never fabricated 0s).
    """
    _, err = _auth(request)
    if err is not None:
        return err

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID матча."}, status=400)

    stats = get_live_statistics(match_id)
    return web.json_response({
        "status": "ok",
        "match_id": match_id,
        "stats": stats,
        "has_data": stats is not None
    })


async def handle_get_live_markets(request: web.Request) -> web.Response:
    """
    GET /api/live/{id}/markets
    Retrieve in-play markets with status (open, suspended, closed).
    """
    _, err = _auth(request)
    if err is not None:
        return err

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID матча."}, status=400)

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, match_id, market_key, market_name, category, status
            FROM markets
            WHERE match_id = ?
            ORDER BY id ASC
        """, (match_id,))
        markets_raw = cursor.fetchall()

        markets = []
        for m in markets_raw:
            m_dict = dict(m)
            cursor.execute("""
                SELECT id, selection_key, selection_name, odds_value, previous_odds, odds_version, status
                FROM market_selections
                WHERE market_id = ?
                ORDER BY id ASC
            """, (m["id"],))
            m_dict["selections"] = [dict(s) for s in cursor.fetchall()]
            markets.append(m_dict)

    # Запрещённые в панели виды ставок в роспись не попадают.
    try:
        bans = database.get_match_bet_bans(match_id)
    except Exception:
        logger.warning("Could not read bet bans for match %s", match_id, exc_info=True)
        bans = set()
    if bans:
        markets = [m for m in markets if database.bet_ban_group(m.get("market_key")) not in bans]

    return web.json_response({
        "status": "ok",
        "match_id": match_id,
        "markets": markets
    })


async def handle_get_odds_movers(request: web.Request) -> web.Response:
    """
    GET /api/odds/movers
    Categorized odds movement intelligence: biggest drops, rises, fastest velocity, suspended.
    """
    _, err = _auth(request)
    if err is not None:
        return err

    division_id_str = request.query.get("division_id")
    season_id_str = request.query.get("season_id")
    limit_str = request.query.get("limit", "10")

    div_id = int(division_id_str) if division_id_str and division_id_str.isdigit() else None
    season_id = int(season_id_str) if season_id_str and season_id_str.isdigit() else None
    limit = int(limit_str) if limit_str and limit_str.isdigit() else 10

    res = get_odds_movers(division_id=div_id, season_id=season_id, limit=limit)
    return web.json_response(res)


async def handle_get_live_intelligence(request: web.Request) -> web.Response:
    """
    GET /api/live/{id}/intelligence
    Sports intelligence report: form, H2H, implied vs model probability, value edge, verifiable insights.
    """
    _, err = _auth(request)
    if err is not None:
        return err

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID матча."}, status=400)

    try:
        from services.intelligence_engine import IntelligenceEngine
        report = IntelligenceEngine.get_match_intelligence(match_id)
        return web.json_response(report)
    except ValueError as e:
        return web.json_response({"status": "error", "message": str(e)}, status=404)
    except Exception as e:
        logger.error(f"Error calculating intelligence for match #{match_id}: {e}")
        return web.json_response({"status": "error", "message": "Ошибка расчета аналитики."}, status=500)

