"""
api/routes_cup.py

Кубки в Mini App: общий и кубки дивизионов — этапы сезона, сетка серий и
линия этапа. Кубок выбирается `?division_id=N` (0 или без параметра — общий);
ответ `/api/cup` перечисляет кубки сезона, у которых есть этапы, для
переключателя в чипе «🏆 Кубок».

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
        "division_id": stage.get("division_id"),
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


def _requested_scope(request: web.Request) -> tuple[bool, int | None]:
    """(задан ли кубок явно, кубок). Мусор в параметре — 400 выше по стеку."""
    raw = (request.query.get("division_id") or "").strip()
    if not raw:
        return False, None
    return True, database.cup_scope(raw)


def _load_overview(explicit: bool, scope: int | None) -> dict:
    scopes = database.list_cup_scopes()
    if not explicit and scopes and scope not in scopes:
        # Без параметра открывается общий кубок, а если его нет — первый из заведённых.
        scope = scopes[0]
    stages = []
    for stage in database.list_cup_stages(division_id=scope):
        series = database.get_cup_bracket(stage["stage"], season_id=stage["season_id"], division_id=scope)
        stages.append(_stage_payload(stage, series))
    current = _current_stage(stages)
    return {
        "division_id": scope,
        "cup_label": database.cup_scope_label(scope),
        "cups": [
            {"division_id": s, "label": database.cup_scope_short(s), "title": database.cup_scope_label(s)}
            for s in scopes
        ],
        "stages": stages,
        "current_stage_id": current["id"] if current else None,
    }


async def handle_get_cup(request: web.Request) -> web.Response:
    """GET /api/cup[?division_id=N] — кубки сезона, этапы выбранного и этап по умолчанию."""
    denied = _guard(request)
    if denied is not None:
        return denied
    try:
        explicit, scope = _requested_scope(request)
    except ValueError:
        return web.json_response({"status": "error", "message": "Некорректный дивизион кубка."}, status=400)
    overview = await asyncio.to_thread(_load_overview, explicit, scope)
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


def _attach_usernames(series: list[dict]) -> list[dict]:
    """Ники тренеров под клубами — как в карточке матча лиги.

    Серии хранят только названия клубов, поэтому ники подтягиваются одним
    запросом на весь этап. Клуб без тренера или без ника получает None.
    """
    names = [s.get(k) for s in series for k in ("team1_name", "team2_name")]
    usernames = database.get_usernames_by_teams(names)
    for s in series:
        for side in ("team1", "team2"):
            key = (s.get(f"{side}_name") or "").strip().casefold()
            s[f"{side}_username"] = usernames.get(key)
    return series


def _load_line(stage: dict) -> dict:
    line = database.get_cup_stage_line(stage["stage"], stage["season_id"], division_id=stage.get("division_id"))
    _attach_usernames(line.get("series") or [])
    return line


def _load_bracket(stage: dict) -> list[dict]:
    series = database.get_cup_stage_series(stage["id"])
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
    return _attach_usernames([
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
    ])


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
    line = await asyncio.to_thread(_load_line, stage)
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
            "division_id": stage.get("division_id"),
            "is_open": bool(stage.get("is_open")),
            "bets_open": bets_open,
            "deadline": stage.get("deadline"),
        },
        "series": series,
    })
