"""
api/routes_cup.py

Общий кубок в Mini App: этапы сезона, сетка серий и линия этапа.

Ставки на кубок идут через тот же купон (`/api/predictions`): исход кубковой
игры — обычная пара `match_id` + `outcome`, а гейт этапа, запрет ничьей и
`SERIES_CORRELATED` проверяет `place_user_bet`. Здесь только чтение.
"""

import asyncio
import logging

from aiohttp import web

import database
from api.auth import get_authenticated_user, check_user_access

logger = logging.getLogger(__name__)


def _guard(request: web.Request) -> web.Response | None:
    """401/403 — тот же контракт, что у линии лиги."""
    user_info = get_authenticated_user(request.headers.get("X-Telegram-Init-Data", ""))
    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not check_user_access(user_info["id"]):
        return web.json_response({
            "status": "error",
            "error": "access_restricted",
            "message": "Logovo.bet временно недоступен."
        }, status=403)
    return None


def _stage_payload(stage: dict, series: list[dict]) -> dict:
    return {
        "id": stage["id"],
        "stage": stage["stage"],
        "stage_order": stage["stage_order"],
        "season_id": stage["season_id"],
        "is_open": bool(stage.get("is_open")),
        "bets_open": bool(stage.get("bets_open")),
        "deadline": stage.get("deadline"),
        "series_total": len(series),
        "series_completed": sum(1 for s in series if s.get("status") == "completed"),
    }


def _current_stage(stages: list[dict]) -> dict | None:
    """Этап, который Mini App открывает первым.

    Открытая линия важнее идущих игр: игрок пришёл ставить. Дальше — этап,
    который играется, затем последний, где уже есть сетка.
    """
    for key in ("bets_open", "is_open"):
        for s in stages:
            if s[key]:
                return s
    with_series = [s for s in stages if s["series_total"]]
    if with_series:
        return with_series[-1]
    return stages[0] if stages else None


def _load_overview() -> dict:
    stages = []
    for stage in database.list_cup_stages():
        series = database.get_cup_bracket(stage["stage"], season_id=stage["season_id"])
        stages.append(_stage_payload(stage, series))
    current = _current_stage(stages)
    return {"stages": stages, "current_stage_id": current["id"] if current else None}


async def handle_get_cup(request: web.Request) -> web.Response:
    """GET /api/cup — этапы активного сезона и этап, открываемый по умолчанию."""
    denied = _guard(request)
    if denied is not None:
        return denied
    overview = await asyncio.to_thread(_load_overview)
    return web.json_response({"status": "ok", **overview})


def _stage_from_request(request: web.Request) -> tuple[dict | None, web.Response | None]:
    try:
        stage_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return None, web.json_response({"status": "error", "message": "Некорректный ID этапа."}, status=400)
    stage = database.get_cup_stage_by_id(stage_id)
    if not stage:
        return None, web.json_response({"status": "error", "message": "Этап кубка не найден."}, status=404)
    return stage, None


def _load_bracket(stage: dict) -> list[dict]:
    series = database.get_cup_bracket(stage["stage"], season_id=stage["season_id"])
    games_by_series: dict[int, list[dict]] = {}
    for g in database.get_cup_stage_games(stage["id"]):
        games_by_series.setdefault(g["series_id"], []).append({
            "match_id": g["match_id"],
            "game_num": g["game_num_in_series"],
            "status": g["status"],
            "score1": g["player1_score"],
            "score2": g["player2_score"],
            "winner_team": g["cup_winner_team"],
        })
    return [
        {
            "series_id": s["id"],
            "series_num": s["series_num"],
            "team1_name": s["team1_name"],
            "team2_name": s["team2_name"],
            "team1_wins": s["team1_wins"] or 0,
            "team2_wins": s["team2_wins"] or 0,
            "winner_name": s["winner_name"],
            "status": s["status"],
            "games": games_by_series.get(s["id"], []),
        }
        for s in series
    ]


async def handle_get_cup_bracket(request: web.Request) -> web.Response:
    """GET /api/cup/stages/{id}/bracket — серии этапа со счётом серий и игр."""
    denied = _guard(request)
    if denied is not None:
        return denied
    stage, error = await asyncio.to_thread(_stage_from_request, request)
    if error is not None:
        return error
    series = await asyncio.to_thread(_load_bracket, stage)
    return web.json_response({
        "status": "ok",
        "stage": _stage_payload(stage, series),
        "series": series,
    })


async def handle_get_cup_line(request: web.Request) -> web.Response:
    """GET /api/cup/stages/{id}/line — открытая линия этапа.

    Коэффициенты приходят только из открытых рынков: до открытия ставок и после
    старта этапа тайлы отдаются с `is_line = False`, а не с выдуманными числами.
    """
    denied = _guard(request)
    if denied is not None:
        return denied
    stage, error = await asyncio.to_thread(_stage_from_request, request)
    if error is not None:
        return error
    line = await asyncio.to_thread(database.get_cup_stage_line, stage["stage"], stage["season_id"])
    bets_open = bool(stage.get("bets_open"))
    series = line.get("series") or []
    if not bets_open:
        # Закрытый этап линии не показывает, даже если рынок ещё не успели закрыть.
        for entry in series:
            for tile in [entry.get("header"), *entry.get("games", [])]:
                if tile:
                    tile["odds"] = {}
                    tile["is_line"] = False
    return web.json_response({
        "status": "ok",
        "stage": {
            "id": stage["id"],
            "stage": stage["stage"],
            "season_id": stage["season_id"],
            "is_open": bool(stage.get("is_open")),
            "bets_open": bets_open,
            "deadline": stage.get("deadline"),
        },
        "series": series,
    })
