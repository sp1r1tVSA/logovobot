"""
api/routes_tournaments.py

Logovo.bet — Tournament Hub & Results Center API:
- Tournaments catalog
- Standings tables
- Finished matches & results archive
- Top scorers / stats leaders
"""

import asyncio
import logging
from aiohttp import web
import config
import database
from api.auth import get_authenticated_user
from api.params import query_int

logger = logging.getLogger(__name__)


async def handle_get_tournaments(request: web.Request) -> web.Response:
    """
    GET /api/tournaments
    List active tournaments and championships.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tournaments WHERE is_active = 1 ORDER BY id ASC")
        tournaments = [dict(r) for r in cursor.fetchall()]

        if not tournaments:
            tournaments = [{
                "id": 1,
                "name": "Логово Фифарей (Основная Лига)",
                "type": "league",
                "season": "Сезон 2026",
                "is_active": 1
            }]

    return web.json_response({
        "status": "ok",
        "tournaments": tournaments
    })


async def handle_get_divisions(request: web.Request) -> web.Response:
    """
    GET /api/divisions
    List all active divisions.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    divisions = await asyncio.to_thread(database.get_divisions, only_active=True)
    return web.json_response({
        "status": "ok",
        "divisions": divisions
    })


async def handle_get_seasons(request: web.Request) -> web.Response:
    """
    GET /api/seasons
    List all seasons.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    seasons = await asyncio.to_thread(database.list_seasons)
    return web.json_response({
        "status": "ok",
        "seasons": seasons
    })


async def handle_get_season_by_id(request: web.Request) -> web.Response:
    """
    GET /api/seasons/{id}
    Get specific season details.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    s_id_str = request.match_info.get("id")
    if not s_id_str or not s_id_str.isdigit():
        return web.json_response({"status": "error", "error": "invalid_season_id"}, status=400)

    season = await asyncio.to_thread(database.get_season, int(s_id_str))
    if not season:
        return web.json_response({"status": "error", "error": "season_not_found"}, status=404)

    return web.json_response({
        "status": "ok",
        "season": season
    })


async def handle_get_standings(request: web.Request) -> web.Response:
    """
    GET /api/tournaments/{id}/standings?division_id=X&season_id=Y
    Returns tournament standings table strictly isolated by division and season.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    division_id = request.query.get("division_id")
    season_id = request.query.get("season_id")
    div_id = int(division_id) if division_id and division_id.isdigit() else None
    s_id = int(season_id) if season_id and season_id.isdigit() else None

    try:
        standings = await asyncio.to_thread(database.get_standings, division_id=div_id, season_id=s_id)
    except Exception as e:
        logger.warning(f"Error fetching standings: {e}")
        standings = []

    # Last-5 form per team, used by the sortable Mini App table.
    # Failing form must not take the table down with it.
    try:
        form = await asyncio.to_thread(database.get_teams_recent_form, 5, div_id, s_id)
    except Exception as e:
        logger.warning(f"Error fetching recent form: {e}")
        form = {}

    euro_slots = config.get_eurocup_slots(div_id)
    ucl_places = euro_slots.get("ucl_places", 0)
    uel_places = euro_slots.get("uel_places", 0)

    enriched_standings = []
    for idx, s in enumerate(standings, 1):
        row = dict(s)
        if ucl_places and idx <= ucl_places:
            row["eurocup_zone"] = "ucl"
        elif uel_places and idx <= ucl_places + uel_places:
            row["eurocup_zone"] = "uel"
        else:
            row["eurocup_zone"] = None
        enriched_standings.append(row)

    return web.json_response({
        "status": "ok",
        "standings": enriched_standings,
        "form": form,
        "eurocup_rules": {
            "ucl_places": ucl_places,
            "uel_places": uel_places,
            "start_round": config.EUROCUP_START_ROUND,
        }
    })


async def handle_get_results(request: web.Request) -> web.Response:
    """
    GET /api/tournaments/{id}/results?limit=30&division_id=X&season_id=Y
    Returns finished match results archive filtered by division and season.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    limit = min(50, query_int(request, "limit", 30, minimum=1))
    div_param = request.query.get("division_id")
    season_param = request.query.get("season_id")

    with database.transaction() as conn:
        cursor = conn.cursor()
        query = """
            SELECT m.*, 
                   COALESCE(m.player1_team, 'Хозяева') as team1_name,
                   COALESCE(m.player2_team, 'Гости') as team2_name
            FROM matches m
            WHERE m.status IN ('confirmed', 'completed', 'finished')
              AND m.player1_score IS NOT NULL AND m.player2_score IS NOT NULL
        """
        params = []
        if div_param and div_param.isdigit():
            query += " AND m.division_id = ?"
            params.append(int(div_param))
        if season_param and season_param.isdigit():
            query += " AND (m.season_id = ? OR m.season_id IS NULL)"
            params.append(int(season_param))

        query += " ORDER BY m.played_at DESC, m.id DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, tuple(params))
        results = [dict(r) for r in cursor.fetchall()]

    return web.json_response({
        "status": "ok",
        "results": results
    })


async def handle_get_top_scorers(request: web.Request) -> web.Response:
    """
    GET /api/tournaments/{id}/top-scorers?division_id=X
    Returns top goalscorers, assist leaders and MVP (player-of-the-match) leaders.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    div_param = request.query.get("division_id")
    if not div_param:
        path_id = request.match_info.get("id")
        if path_id and path_id.isdigit():
            div_param = path_id
    div_id = int(div_param) if div_param and div_param.isdigit() else None

    raw_scorers = await asyncio.to_thread(database.get_top_scorers, limit=15, division_id=div_id)
    raw_assists = await asyncio.to_thread(database.get_top_assists, limit=15, division_id=div_id)
    raw_mvps = await asyncio.to_thread(database.get_top_mvps, division_id=div_id, limit=15)

    top_scorers = [
        {
            **sc,
            "goals": sc.get("total_goals", 0),
            "total_goals": sc.get("total_goals", 0),
        }
        for sc in raw_scorers
    ]
    top_assists = [
        {
            **a,
            "assists": a.get("total_assists", 0),
            "total_assists": a.get("total_assists", 0),
        }
        for a in raw_assists
    ]

    # 👑 Лидеры по наградам «Игрок матча». Ключ mvp_count оставлен как есть —
    # фронт читает его напрямую, алиасов вида goals/total_goals здесь не нужно.
    top_mvps = [
        {
            **mv,
            "mvp_count": mv.get("mvp_count", 0),
        }
        for mv in raw_mvps
    ]

    return web.json_response({
        "status": "ok",
        "top_scorers": top_scorers,
        "top_assists": top_assists,
        "top_mvps": top_mvps
    })


async def handle_get_my_tournament_stats(request: web.Request) -> web.Response:
    """
    GET /api/profile/tournament-stats
    Турнирная (не беттинговая) статистика текущего участника для кабинета:
    место в дивизионе, очки, В/Н/П, голы и форма последних матчей.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    summary = await asyncio.to_thread(database.get_user_tournament_summary, user_info["id"])

    return web.json_response({
        "status": "ok",
        "tournament_stats": summary
    })
