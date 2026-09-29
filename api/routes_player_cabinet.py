"""
api/routes_player_cabinet.py

Logovo.bet — Personal club cabinet («Мой Клуб») for the Telegram Mini App.
Портирует кабинет игрока из бота (handlers/cabinet.py) в отдельную вкладку:
- обзор клуба (дивизион, место, очки, предупреждения)
- матчи игрока (активные + последние сыгранные)
- состав клуба с индивидуальной статистикой
- согласование времени матча (предложить / подтвердить)

Доступ строго по telegram_id из валидированного Telegram initData:
идентификатор клуба никогда не приходит от клиента — он берётся из БД по
авторизованному пользователю.
"""

import asyncio
import logging

from aiohttp import web

import config
import database
from api.auth import get_authenticated_user, check_user_access

logger = logging.getLogger(__name__)

# Не даём клиенту раскрутить выборку: кабинет показывает текущий тур и хвост истории.
MAX_ACTIVE_MATCHES = 20
MAX_RECENT_MATCHES = 5
MAX_PROPOSED_TIME_LEN = 64


def _auth(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Validate initData and the rollout gate. Returns (user_info, error_response)."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return None, web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not check_user_access(user_info["id"]):
        return None, web.json_response({"status": "error", "error": "LOGOVO_LOCKDOWN"}, status=403)
    return user_info, None


def _unregistered_payload() -> dict:
    return {"status": "ok", "registered": False, "club": None}


async def handle_get_cabinet_overview(request: web.Request) -> web.Response:
    """
    GET /api/cabinet/overview
    Карточка клуба: команда, дивизион, место в таблице, очки, В/Н/П, голы,
    дисциплина (предупреждения), кубковые и общие показатели. Незарегистрированный
    игрок получает registered = false и пустой блок клуба.
    """
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        summary = await asyncio.to_thread(database.get_user_tournament_summary, user_id)
    except Exception as e:
        logger.error(f"cabinet/overview failed for {user_id}: {e}")
        return web.json_response({"status": "error", "error": "internal_error"}, status=500)

    if not summary or not summary.get("registered"):
        return web.json_response(_unregistered_payload())

    user_row = await asyncio.to_thread(database.get_user, user_id)
    user = dict(user_row) if user_row else {}

    team_name = summary.get("team_name")
    cup_summary = await asyncio.to_thread(database.get_club_cup_summary, team_name)

    tour_stats = {
        "position": summary.get("position"),
        "total_teams": summary.get("total_teams", 0),
        "points": summary.get("points", 0),
        "played": summary.get("played", 0),
        "wins": summary.get("wins", 0),
        "draws": summary.get("draws", 0),
        "losses": summary.get("losses", 0),
        "goals_scored": summary.get("goals_scored", 0),
        "goals_conceded": summary.get("goals_conceded", 0),
        "goal_diff": summary.get("goal_diff", 0),
        "form": summary.get("form", []),
    }

    total_played = tour_stats["played"] + cup_summary.get("played", 0)
    total_wins = tour_stats["wins"] + cup_summary.get("wins", 0)
    total_draws = tour_stats["draws"]
    total_losses = tour_stats["losses"] + cup_summary.get("losses", 0)
    total_scored = tour_stats["goals_scored"] + cup_summary.get("goals_scored", 0)
    total_conceded = tour_stats["goals_conceded"] + cup_summary.get("goals_conceded", 0)
    total_stats = {
        "played": total_played,
        "wins": total_wins,
        "draws": total_draws,
        "losses": total_losses,
        "goals_scored": total_scored,
        "goals_conceded": total_conceded,
        "goal_diff": total_scored - total_conceded,
        "winrate": round((total_wins / total_played) * 100, 1) if total_played > 0 else 0,
    }

    return web.json_response({
        "status": "ok",
        "registered": True,
        "telegram_id": user_id,
        "username": user.get("username") or user_info.get("username"),
        "club": {
            "team_name": team_name,
            "division_id": summary.get("division_id"),
            "division_name": summary.get("division_name"),
        },
        "discipline": {
            "warns": int(user.get("warn_count") or 0),
            "limit": config.MAX_WARNS_LIMIT,
        },
        "tournament": tour_stats,
        "cup": cup_summary,
        "total": total_stats,
    })


async def handle_get_cabinet_matches(request: web.Request) -> web.Response:
    """
    GET /api/cabinet/matches
    Все предстоящие и несыгранные матчи клуба (лига + кубок) плюс сыгранные матчи клуба.
    Поддерживает фильтр ?tournament_type=all|league|cup.
    """
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        team_name = await asyncio.to_thread(database.get_user_team, user_id)
        if not team_name:
            return web.json_response({"status": "ok", "registered": False, "matches": [], "recent": []})

        tour_filter = request.query.get("tournament_type")
        if tour_filter not in ("league", "cup"):
            tour_filter = None

        matches = await asyncio.to_thread(database.get_cabinet_matches, user_id, 100, False, tour_filter)
        recent = await asyncio.to_thread(database.get_cabinet_recent_matches, user_id, 100, tour_filter)
    except Exception as e:
        logger.error(f"cabinet/matches failed for {user_id}: {e}")
        return web.json_response({"status": "error", "error": "internal_error"}, status=500)

    return web.json_response({
        "status": "ok",
        "registered": True,
        "team_name": team_name,
        "tournament_type": tour_filter or "all",
        "matches": matches,
        "recent": recent,
    })


async def handle_get_cabinet_squad(request: web.Request) -> web.Response:
    """
    GET /api/cabinet/squad
    Состав клуба с индивидуальной статистикой (голы, ассисты, награды MVP)
    и лидерами клуба, со срезами по дивизиону и кубку.
    """
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        team_name = await asyncio.to_thread(database.get_user_team, user_id)
        if not team_name:
            return web.json_response({
                "status": "ok", "registered": False,
                "players": [], "top_scorer": None, "top_assistant": None,
                "top_mvp": None, "by_tournament": {}
            })

        requested_tour = request.query.get("tournament_type")
        squad_bundle = await asyncio.to_thread(database.get_cabinet_squad_all_scopes, team_name)
        active_stats = squad_bundle.get("by_tournament", {}).get(requested_tour) or squad_bundle
    except Exception as e:
        logger.error(f"cabinet/squad failed for {user_id}: {e}")
        return web.json_response({"status": "error", "error": "internal_error"}, status=500)

    return web.json_response({
        "status": "ok",
        "registered": True,
        "team_name": team_name,
        "tournament_type": requested_tour or "all",
        "players": active_stats.get("players", []),
        "top_scorer": active_stats.get("top_scorer"),
        "top_assistant": active_stats.get("top_assistant"),
        "top_mvp": active_stats.get("top_mvp"),
        "by_tournament": squad_bundle.get("by_tournament", {}),
    })


def _is_match_participant(match: dict, user_id: int, team_name: str | None) -> bool:
    """Участник матча — по telegram_id либо по имени клуба (матчи хранятся по клубам)."""
    if user_id in (match.get("player1_id"), match.get("player2_id")):
        return True
    own = (team_name or "").strip().lower()
    if not own:
        return False
    return own in {
        (match.get("player1_team") or "").strip().lower(),
        (match.get("player2_team") or "").strip().lower(),
    }


async def handle_post_cabinet_match_time(request: web.Request) -> web.Response:
    """
    POST /api/cabinet/match-time
    Body: {"match_id": int, "action": "propose"|"accept", "proposed_time": str}
    Предложить время матча или подтвердить предложение соперника.
    """
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "invalid_json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"status": "error", "error": "invalid_json"}, status=400)

    try:
        match_id = int(body.get("match_id"))
    except (TypeError, ValueError):
        return web.json_response({"status": "error", "error": "invalid_match_id"}, status=400)

    action = str(body.get("action") or "").strip().lower()
    if action not in ("propose", "accept"):
        return web.json_response({"status": "error", "error": "invalid_action"}, status=400)

    proposed_time = str(body.get("proposed_time") or "").strip()
    if action == "propose":
        if not proposed_time:
            return web.json_response({"status": "error", "error": "proposed_time_required"}, status=400)
        if len(proposed_time) > MAX_PROPOSED_TIME_LEN:
            return web.json_response({"status": "error", "error": "proposed_time_too_long"}, status=400)

    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        return web.json_response({"status": "error", "error": "match_not_found"}, status=404)

    team_name = await asyncio.to_thread(database.get_user_team, user_id)
    if not _is_match_participant(match, user_id, team_name):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    if match.get("status") == "confirmed":
        return web.json_response({"status": "error", "error": "match_finished"}, status=409)

    if action == "propose":
        await asyncio.to_thread(database.propose_match_time, match_id, user_id, proposed_time)
    else:
        if (match.get("time_status") or "none") != "proposed":
            return web.json_response({"status": "error", "error": "nothing_to_accept"}, status=409)
        proposed_by = match.get("proposed_by")
        if proposed_by and int(proposed_by) == int(user_id):
            return web.json_response({"status": "error", "error": "cannot_accept_own_proposal"}, status=409)
        await asyncio.to_thread(database.accept_match_time, match_id)

    updated = await asyncio.to_thread(database.get_match, match_id)
    return web.json_response({
        "status": "ok",
        "match_id": match_id,
        "time_status": (updated or {}).get("time_status") or "none",
        "proposed_time": (updated or {}).get("proposed_time"),
        "proposed_by_me": bool((updated or {}).get("proposed_by")) and int(updated["proposed_by"]) == int(user_id),
    })
