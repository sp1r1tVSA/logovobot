"""
api/routes_outrights.py

Долгосрочные ставки в Mini App: победители дивизионов и кубков, бомбардиры.

  GET  /api/outrights                  рынки сезона с исходами и замками тренера
  GET  /api/outrights/{id}/history     история коэффициентов лидеров рынка (график)
  POST /api/outrights/bet              ординар на исход {selection_id, amount, odd, idempotency_key, freebet_id}
  GET  /api/outrights/my               мои долгосрочные ставки

Цены считает `services/outright_service` в фоне; здесь только чтение и приём
ставки через `database.place_outright_bet`, который сам проверяет всё заново —
замки в ответе нужны лишь для того, чтобы не показывать кнопку, которая
заведомо откажет.

Фрибет (`freebet_id`) заменяет ставку монетами: сумма берётся из самого
фрибета, `amount` при этом игнорируется, баланс не трогается. Доступные
фрибеты игрока приходят в `freebets` ответа `GET /api/outrights`.

Приём ставок ограничен сроком: дивизионы, их кубки и бомбардиры — до
`config.OUTRIGHT_BETS_CLOSE_AT` (по умолчанию 30.09.2026 18:00 МСК), общий кубок —
до появления в сетке 1/4 финала. Каждый рынок доски несёт `close_rule` /
`bets_until` / `bets_closed`, после срока ставка получает
`OUTRIGHT_BETTING_CLOSED` (409). Линия и расчёт живут дальше.
"""

import asyncio
import datetime
import logging

from aiohttp import web

import database
from api.auth import get_authenticated_user, check_user_access
from api.params import path_int, query_int

logger = logging.getLogger(__name__)

HISTORY_POINTS = 120
HISTORY_TOP_DEFAULT = 6

# Код отказа → HTTP-статус. Остальное — 400.
_ERROR_STATUS = {
    "ODDS_CHANGED": 409,
    database.OUTRIGHT_REPRICING_ERROR: 409,
    database.OUTRIGHT_OWN_SCOPE_ERROR: 403,
    database.OUTRIGHT_BETTING_CLOSED_ERROR: 409,
    "LOGOVO_LOCKDOWN": 403,
    "BETTING_BANNED": 403,
    "BETTING_PAUSED": 403,
    "MARKET_SUSPENDED": 409,
    "BETTING_UNAVAILABLE": 503,
    "RISK_CHECK_UNAVAILABLE": 503,
    "IDEMPOTENCY_KEY_REUSED": 409,
    "FREEBET_UNAVAILABLE": 409,
}


def _user(request: web.Request) -> tuple[int | None, web.Response | None]:
    user_info = get_authenticated_user(request.headers.get("X-Telegram-Init-Data", ""))
    if not user_info or "id" not in user_info:
        return None, web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not check_user_access(user_info["id"]):
        return None, web.json_response({
            "status": "error",
            "error": "access_restricted",
            "message": "Logovo.bet временно недоступен."
        }, status=403)
    return int(user_info["id"]), None


def _selection_payload(sel: dict, lock: str | None) -> dict:
    return {
        "id": sel["id"],
        "key": sel["selection_key"],
        "name": sel["name"],
        "team_name": sel.get("team_name"),
        "division_id": sel.get("division_id"),
        "probability": round(float(sel.get("probability") or 0), 4),
        "odds": round(float(sel["odds_value"]), 2),
        "status": sel["status"],
        "settle_factor": sel.get("settle_factor"),
        "locked": lock,
    }


def _market_payload(market: dict, coach: dict | None) -> dict:
    market_lock = database.outright_lock_reason(coach, market)
    selections = []
    for sel in market["selections"]:
        lock = market_lock or database.outright_lock_reason(coach, market, sel)
        selections.append(_selection_payload(sel, lock))
    return {
        "id": market["id"],
        "type": market["market_type"],
        "scope_key": market["scope_key"],
        "division_id": market["division_id"],
        "title": market["title"],
        "status": market["status"],
        "priced_at": market.get("priced_at"),
        "settled_at": market.get("settled_at"),
        "void_reason": market.get("void_reason"),
        "locked": market_lock,
        "selections": selections,
    }


def _bets_deadline() -> dict:
    """Общий срок приёма: момент закрытия («30.09.2026 18:00») и наступил ли он."""
    try:
        close_at = database.outright_bets_close_at()
    except ValueError:
        return {"bets_close_at": None, "bets_until": None, "bets_closed": True}
    if close_at is None:
        return {"bets_close_at": None, "bets_until": None, "bets_closed": False}
    return {"bets_close_at": close_at.strftime("%Y-%m-%d %H:%M:%S"),
            "bets_until": close_at.strftime("%d.%m.%Y %H:%M"),
            "bets_closed": database.outright_betting_closed()}


def _market_closing(market: dict, deadline: dict, cup_closed: bool) -> dict:
    """Правило закрытия рынка: общий кубок — до 1/4 финала, остальные — по общему сроку."""
    if database.is_general_cup_market(market):
        return {"close_rule": "cup_stage", "close_stage": database.OUTRIGHT_GENERAL_CUP_CLOSE_STAGE,
                "bets_until": None, "bets_closed": cup_closed}
    return {"close_rule": "deadline" if deadline["bets_until"] else None, "close_stage": None,
            "bets_until": deadline["bets_until"], "bets_closed": deadline["bets_closed"]}


def _load_board(user_id: int) -> dict:
    markets = database.get_outright_markets()
    coach = database.get_outright_coach_scope(user_id)
    divisions = [{"id": d["id"], "name": d["name"]} for d in database.get_divisions()]
    deadline = _bets_deadline()
    cup_closed = database.is_general_cup_outright_closed()
    return {
        "markets": [{**_market_payload(m, coach), **_market_closing(m, deadline, cup_closed)} for m in markets],
        "divisions": divisions,
        "coach": coach,
        "open_bets": database.count_user_open_outright_bets(user_id),
        "max_open_bets": database.MAX_OPEN_OUTRIGHT_BETS,
        "min_bet": database.OUTRIGHT_MIN_BET,
        "freebets": [{k: f[k] for k in ("id", "amount", "source", "source_id", "source_name", "source_icon", "granted_at")}
                     for f in database.get_user_freebets(user_id)],
        **deadline,
    }


async def handle_get_outrights(request: web.Request) -> web.Response:
    """GET /api/outrights — все рынки сезона: открытые, приостановленные и рассчитанные."""
    user_id, denied = _user(request)
    if denied is not None:
        return denied
    board = await asyncio.to_thread(_load_board, user_id)
    return web.json_response({"status": "ok", **board})


def _load_history(market_id: int, top: int) -> dict | None:
    market = database.get_outright_market(market_id)
    if not market:
        return None
    history = database.get_outright_history(market_id)
    # Лидеры по текущему шансу; «Другой игрок» и выбывшие на графике только шумят.
    leaders = [s for s in market["selections"]
               if s["selection_key"] != database.OUTRIGHT_OTHER_KEY and s["status"] in ("active", "suspended", "won")]
    leaders.sort(key=lambda s: (s["status"] != "won", -float(s.get("probability") or 0)))
    series = []
    for sel in leaders[:top]:
        points = history.get(sel["id"], [])[-HISTORY_POINTS:]
        series.append({"selection_id": sel["id"], "name": sel["name"], "team_name": sel.get("team_name"),
                       "points": points})
    return {"market_id": market_id, "title": market["title"], "series": series}


async def handle_get_outright_history(request: web.Request) -> web.Response:
    """GET /api/outrights/{id}/history?top=6"""
    _, denied = _user(request)
    if denied is not None:
        return denied
    market_id = path_int(request)
    top = query_int(request, "top", HISTORY_TOP_DEFAULT, minimum=1, maximum=40)
    payload = await asyncio.to_thread(_load_history, market_id, top)
    if payload is None:
        return web.json_response({"status": "error", "error": "not_found", "message": "Рынок не найден."},
                                 status=404)
    return web.json_response({"status": "ok", **payload})


async def handle_place_outright_bet(request: web.Request) -> web.Response:
    """POST /api/outrights/bet  {selection_id, amount, odd?, idempotency_key?, freebet_id?}"""
    user_id, denied = _user(request)
    if denied is not None:
        return denied
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Некорректный JSON тела запроса."}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"status": "error", "message": "Ожидается JSON-объект."}, status=400)

    idem = data.get("idempotency_key")
    if idem is not None and (not isinstance(idem, str) or len(idem) > 128):
        return web.json_response({"status": "error", "message": "Некорректный ключ идемпотентности."}, status=400)

    ok, result = await asyncio.to_thread(
        database.place_outright_bet, user_id, data.get("selection_id"), data.get("amount"),
        data.get("odd"), idem or None, data.get("freebet_id"),
    )
    if ok:
        return web.json_response({"status": "ok", **result})
    code = result.get("error", "")
    return web.json_response({"status": "error", **result}, status=_ERROR_STATUS.get(code, 400))


async def handle_get_my_outright_bets(request: web.Request) -> web.Response:
    """GET /api/outrights/my"""
    user_id, denied = _user(request)
    if denied is not None:
        return denied
    bets = await asyncio.to_thread(database.get_user_outright_bets, user_id)
    open_bets = await asyncio.to_thread(database.count_user_open_outright_bets, user_id)
    fields = ("id", "market_id", "selection_id", "amount", "odd", "potential_win", "status",
              "dead_heat_factor", "actual_payout", "created_at", "settled_at", "selection_name",
              "team_name", "selection_status", "current_odd", "market_title", "market_type", "market_status",
              "freebet_id")
    return web.json_response({
        "status": "ok",
        "bets": [{k: b.get(k) for k in fields} for b in bets],
        "open_bets": open_bets,
        "max_open_bets": database.MAX_OPEN_OUTRIGHT_BETS,
    })


def register_outright_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/outrights", handle_get_outrights)
    r.add_get("/api/outrights/my", handle_get_my_outright_bets)
    r.add_get("/api/outrights/{id}/history", handle_get_outright_history)
    r.add_post("/api/outrights/bet", handle_place_outright_bet)
