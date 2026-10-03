"""
api/routes_irl.py

Ставки на реальные футбольные матчи в Mini App («Реальные матчи»).

  GET  /api/irl/today       матчи дня с кэфами 1X2 и моей ставкой на каждый
  POST /api/irl/bets        ординар {match_id, outcome, amount, odd?}
  GET  /api/irl/bets/mine   мои ставки, новые сверху

Матчи выбирает и публикует `services/irl_jobs`, здесь только чтение и приём
ставки через `database.place_irl_bet`, который сам проверяет всё заново
(открыт ли матч, бан, цену клиента, повтор, баланс). Черновики игрокам не
показываются. Расчёт идёт по счёту основного времени, без доп. времени и
пенальти — об этом `note` в ответе `/today`.

Без `IRL_ENABLED=true` роуты отвечают 404 `irl_disabled`; проверка идёт после
авторизации, чтобы анонимный запрос по-прежнему получал 401.
"""

import asyncio
import logging

from aiohttp import web

import config
import database
from api.auth import get_authenticated_user, check_user_access
from services import irl_betting as irl
from time_utils import now_msk, today_msk_str

logger = logging.getLogger(__name__)

MAIN_TIME_NOTE = "Расчёт по счёту основного времени (90 минут), без доп. времени и пенальти."
MINE_LIMIT = 100

# Игрокам видны только опубликованные матчи.
_VISIBLE_STATUSES = ("open", "closed", "settled", "void")

# Код отказа → HTTP-статус. Остальное — 400.
_ERROR_STATUS = {
    "ODDS_CHANGED": 409,
    database.IRL_ALREADY_BET_ERROR: 409,
    database.IRL_BETTING_CLOSED_ERROR: 409,
    "MARKET_SUSPENDED": 409,
    "LOGOVO_LOCKDOWN": 403,
    "BETTING_BANNED": 403,
    "BETTING_PAUSED": 403,
    "BETTING_UNAVAILABLE": 503,
    "RISK_CHECK_UNAVAILABLE": 503,
}


def _user(request: web.Request) -> tuple[int | None, web.Response | None]:
    user_info = get_authenticated_user(request.headers.get("X-Telegram-Init-Data", ""))
    if not user_info or "id" not in user_info:
        return None, web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not check_user_access(user_info["id"]):
        return None, web.json_response({
            "status": "error",
            "error": "access_restricted",
            "message": "Logovo.bet временно недоступен.",
        }, status=403)
    if not config.IRL_ENABLED:
        return None, web.json_response({
            "status": "error",
            "error": "irl_disabled",
            "message": "Ставки на реальные матчи отключены.",
        }, status=404)
    return int(user_info["id"]), None


def _bet_payload(bet: dict | None) -> dict | None:
    if not bet:
        return None
    return {k: bet.get(k) for k in ("id", "outcome", "amount", "odd", "potential_win", "status",
                                    "actual_payout", "created_at", "settled_at")}


def _match_payload(match: dict, bet: dict | None, now) -> dict:
    return {
        "id": match["id"],
        "league_name": match.get("league_name"),
        "home": match["home"],
        "away": match["away"],
        "kickoff_at": match["kickoff_at"],
        "status": match["status"],
        "betting_open": irl.betting_open(match["status"], match["kickoff_at"], now),
        "odds": {"home": match.get("odd_home"), "draw": match.get("odd_draw"), "away": match.get("odd_away")},
        "result": match.get("result"),
        "home_goals": match.get("home_goals"),
        "away_goals": match.get("away_goals"),
        "void_reason": match.get("void_reason") if match["status"] == "void" else None,
        "my_bet": _bet_payload(bet),
    }


def _load_today(user_id: int) -> dict:
    day = today_msk_str()
    now = now_msk()
    matches = database.list_irl_matches(bet_day=day, statuses=_VISIBLE_STATUSES)
    items = [_match_payload(m, database.get_user_irl_bet_for_match(user_id, m["id"]), now) for m in matches]
    return {"bet_day": day, "matches": items, "max_bet": config.IRL_MAX_BET, "note": MAIN_TIME_NOTE}


async def handle_get_irl_today(request: web.Request) -> web.Response:
    """GET /api/irl/today"""
    user_id, denied = _user(request)
    if denied is not None:
        return denied
    board = await asyncio.to_thread(_load_today, user_id)
    return web.json_response({"status": "ok", **board})


async def handle_place_irl_bet(request: web.Request) -> web.Response:
    """POST /api/irl/bets  {match_id, outcome: home|draw|away|1|X|2, amount, odd?}"""
    user_id, denied = _user(request)
    if denied is not None:
        return denied
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Некорректный JSON тела запроса."}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"status": "error", "message": "Ожидается JSON-объект."}, status=400)

    ok, result = await asyncio.to_thread(
        database.place_irl_bet, user_id, data.get("match_id"), data.get("outcome"),
        data.get("amount"), data.get("odd"),
    )
    if ok:
        return web.json_response({"status": "ok", **result})
    code = result.get("error", "")
    return web.json_response({"status": "error", **result}, status=_ERROR_STATUS.get(code, 400))


def _load_mine(user_id: int) -> list[dict]:
    fields = ("id", "irl_match_id", "outcome", "amount", "odd", "potential_win", "status", "actual_payout",
              "created_at", "settled_at", "home", "away", "league_name", "kickoff_at", "match_status",
              "result", "home_goals", "away_goals")
    return [{k: b.get(k) for k in fields} for b in database.get_user_irl_bets(user_id, MINE_LIMIT)]


async def handle_get_my_irl_bets(request: web.Request) -> web.Response:
    """GET /api/irl/bets/mine"""
    user_id, denied = _user(request)
    if denied is not None:
        return denied
    bets = await asyncio.to_thread(_load_mine, user_id)
    return web.json_response({"status": "ok", "bets": bets})


def register_irl_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/irl/today", handle_get_irl_today)
    r.add_get("/api/irl/bets/mine", handle_get_my_irl_bets)
    r.add_post("/api/irl/bets", handle_place_irl_bet)
