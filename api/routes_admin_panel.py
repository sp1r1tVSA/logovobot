"""
api/routes_admin_panel.py

Вкладка «Управление» в Mini App — панель Logovo.bet для админов.

  GET  /api/admin/panel/me                      область видимости, дивизионы, остановка приёма
  GET  /api/admin/panel/dashboard               сводка: риск, оборот, GGR, рынки, алерты
  GET  /api/admin/panel/markets                 матчи → рынки → исходы с нагрузкой
  POST /api/admin/panel/markets/{id}/action     suspend | resume | close | void
  POST /api/admin/panel/selections/{id}/odds    ручная правка коэффициента
  GET  /api/admin/panel/bets                    лента купонов с фильтрами
  GET  /api/admin/panel/bets/{id}               карточка купона
  POST /api/admin/panel/bets/{id}/void          аннулировать купон с возвратом
  GET  /api/admin/panel/players                 поиск игроков            (глобальный админ)
  GET  /api/admin/panel/players/{id}            карточка игрока          (глобальный админ)
  POST /api/admin/panel/players/{id}/adjust     начислить / списать      (глобальный админ)
  POST /api/admin/panel/players/{id}/ban        запретить ставки         (глобальный админ)
  POST /api/admin/panel/players/{id}/unban      снять запрет             (глобальный админ)
  GET  /api/admin/panel/limits                  лимиты, настройки купона и экономики
  POST /api/admin/panel/limits                  задать / сбросить лимит  (глобальный админ)
  POST /api/admin/panel/pause                   экстренная остановка приёма

Права: панель открыта только тем, кто указан в ADMIN_IDS (is_super_admin), —
ни роль admin в базе, ни назначение админом дивизиона доступа к ней не дают.
Разбор по дивизионам (Scope.division_ids, _narrow) остаётся на случай, если
дивизионным админам панель когда-нибудь вернут; сейчас эти ветки не достижимы.
SQL здесь нет — всё через database.py.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiohttp import web

import database
from api.auth import get_authenticated_user
from api.params import body_int, path_int, query_int
from handlers.base import is_super_admin
from services.betting_limits import BettingLimitsService

logger = logging.getLogger(__name__)

LIMIT_KEYS = (
    "min_bet", "max_bet", "max_payout", "max_daily_stake", "max_daily_loss",
    "max_open_exposure", "max_open_bets", "market_exposure_limit",
    "division_exposure_limit", "global_exposure_limit",
)
# Настройки купона и экономики: только глобальные, читает их database.
SETTING_KEYS = ("max_express_events", "initial_balance")
# Какие ключи вообще читаются на каждом уровне (см. BettingLimitsService):
# переопределение другого ключа легло бы в таблицу и ничего бы не изменило.
LIMIT_KEYS_BY_SCOPE = {
    "global": LIMIT_KEYS + SETTING_KEYS,
    "division": ("max_bet", "max_payout", "max_open_bets", "market_exposure_limit", "division_exposure_limit"),
    "user": ("max_bet", "max_payout", "max_daily_stake", "max_daily_loss", "max_open_exposure", "max_open_bets"),
}
# Допустимый диапазон значения; ключа нет — 1..100 000 000.
LIMIT_BOUNDS = {
    "max_express_events": (database.MIN_EXPRESS_EVENTS, 50),
    "max_open_bets": (1, 1_000),
    "initial_balance": (1, 1_000_000),
}
DEFAULT_LIMIT_BOUNDS = (1, 100_000_000)
MARKET_ACTIONS = {"suspend": "suspended", "resume": "open", "close": "closed"}
BET_STATUSES = ("all", "pending", "won", "lost", "refunded", "cashed_out")


@dataclass
class Scope:
    actor_id: int
    is_global: bool
    division_ids: list[int] | None  # None — все дивизионы

    def sees(self, divisions: set[int] | None) -> bool:
        if divisions is None:
            return False
        return self.is_global or divisions.issubset(self.division_ids or [])


def _error(status: int, error: str, message: str | None = None) -> web.Response:
    body = {"status": "error", "error": error}
    if message:
        body["message"] = message
    return web.json_response(body, status=status)


def _resolve_scope(request: web.Request) -> Scope | web.Response:
    user = get_authenticated_user(request.headers.get("X-Telegram-Init-Data", ""))
    if not user or "id" not in user:
        return _error(401, "unauthorized")
    actor_id = int(user["id"])
    if not is_super_admin(actor_id):
        return _error(403, "forbidden", "Панель доступна только главным администраторам.")
    return Scope(actor_id, True, None)


def _global_only(scope: Scope) -> web.Response | None:
    # Проверять `is not None`: web.Response — MutableMapping, и пустой ответ
    # ложен в булевом контексте, так что `if denied:` пропускал всех.
    if not scope.is_global:
        return _error(403, "forbidden", "Доступно только главным администраторам.")
    return None


def _narrow(scope: Scope, request: web.Request) -> list[int] | None | web.Response:
    """Список дивизионов запроса: ?division_id сужает область, но не расширяет."""
    division_id = query_int(request, "division_id", None, minimum=0)
    if division_id is None:
        return scope.division_ids
    if not scope.is_global and division_id not in scope.division_ids:
        return _error(403, "forbidden", "Нет доступа к этому дивизиону.")
    return [division_id]


async def _json_body(request: web.Request) -> dict | web.Response:
    try:
        data = await request.json()
    except Exception:
        return _error(400, "invalid_json", "Некорректный JSON.")
    if not isinstance(data, dict):
        return _error(400, "invalid_json", "Ожидается JSON-объект.")
    return data


def _text(data: dict, name: str, max_len: int = 300) -> str:
    value = data.get(name)
    return value.strip()[:max_len] if isinstance(value, str) else ""


async def handle_panel_me(request: web.Request) -> web.Response:
    """GET /api/admin/panel/me"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    divisions = await asyncio.to_thread(database.get_divisions)
    if not scope.is_global:
        divisions = [d for d in divisions if d["id"] in scope.division_ids]
    try:
        pause = await asyncio.to_thread(database.get_betting_pause)
    except Exception:
        logger.exception("Betting pause state is unreadable")
        pause = {"global": {"reason": "Повреждённое состояние — приём ставок закрыт"}, "divisions": {}}
    return web.json_response({
        "status": "ok",
        "is_global": scope.is_global,
        "divisions": [{"id": d["id"], "name": d["name"]} for d in divisions],
        "pause": pause,
        "limit_keys": LIMIT_KEYS_BY_SCOPE,
    })


async def handle_panel_dashboard(request: web.Request) -> web.Response:
    """GET /api/admin/panel/dashboard?division_id="""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    division_ids = _narrow(scope, request)
    if isinstance(division_ids, web.Response):
        return division_ids
    dashboard = await asyncio.to_thread(database.get_betting_dashboard, division_ids)
    return web.json_response({"status": "ok", "dashboard": dashboard})


async def handle_panel_markets(request: web.Request) -> web.Response:
    """GET /api/admin/panel/markets?state=active|closed|finished|all&q=&division_id=&limit=&offset="""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    division_ids = _narrow(scope, request)
    if isinstance(division_ids, web.Response):
        return division_ids
    state = request.query.get("state", "active")
    if state not in ("active", "closed", "finished", "all"):
        return _error(400, "invalid_state", "Неизвестный фильтр рынков.")
    limit = query_int(request, "limit", 15, minimum=1, maximum=50)
    offset = query_int(request, "offset", 0, minimum=0)
    matches, total = await asyncio.to_thread(
        database.get_admin_market_board, division_ids, state, request.query.get("q", ""), limit, offset,
    )
    return web.json_response({"status": "ok", "matches": matches, "total": total,
                              "limit": limit, "offset": offset})


async def handle_panel_market_action(request: web.Request) -> web.Response:
    """POST /api/admin/panel/markets/{id}/action  {action, reason, confirm}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    market_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data

    divisions = await asyncio.to_thread(database.get_betting_entity_divisions, "market", market_id)
    if divisions is None:
        return _error(404, "not_found", f"Рынок #{market_id} не найден.")
    if not scope.sees(divisions):
        return _error(403, "forbidden", "Рынок вне ваших дивизионов.")

    action = data.get("action")
    reason = _text(data, "reason")
    try:
        if action == "void":
            if data.get("confirm") is not True:
                return _error(400, "confirmation_required", "Аннулирование нужно подтвердить.")
            if not reason:
                return _error(400, "reason_required", "Укажите причину аннулирования.")
            result = await asyncio.to_thread(database.void_market, market_id, scope.actor_id, reason)
        elif action in MARKET_ACTIONS:
            result = await asyncio.to_thread(
                database.transition_market_status, market_id, MARKET_ACTIONS[action], scope.actor_id,
            )
            if reason:
                await asyncio.to_thread(
                    database.log_betting_audit, scope.actor_id, f"market_{action}_reason",
                    "market", market_id, None, {"reason": reason},
                )
        else:
            return _error(400, "invalid_action", "Неизвестное действие с рынком.")
    except ValueError as e:
        return _error(409, "invalid_transition", str(e))
    return web.json_response({"status": "ok", "result": result})


async def handle_panel_selection_odds(request: web.Request) -> web.Response:
    """POST /api/admin/panel/selections/{id}/odds  {odds}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    selection_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data

    raw = data.get("odds")
    if isinstance(raw, bool):
        raw = None
    try:
        odds = float(raw)
    except (TypeError, ValueError):
        return _error(400, "invalid_odds", "Коэффициент должен быть числом.")
    if not (1.01 <= odds <= 1000):
        return _error(400, "invalid_odds", "Коэффициент — от 1.01 до 1000.")

    divisions = await asyncio.to_thread(database.get_betting_entity_divisions, "selection", selection_id)
    if divisions is None:
        return _error(404, "not_found", f"Исход #{selection_id} не найден.")
    if not scope.sees(divisions):
        return _error(403, "forbidden", "Исход вне ваших дивизионов.")
    try:
        result = await asyncio.to_thread(database.update_selection_odds, selection_id, odds, scope.actor_id)
    except ValueError as e:
        return _error(400, "invalid_odds", str(e))
    return web.json_response({"status": "ok", "result": result})


async def handle_panel_bets(request: web.Request) -> web.Response:
    """GET /api/admin/panel/bets?status=&division_id=&user_id=&limit=&offset="""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    division_ids = _narrow(scope, request)
    if isinstance(division_ids, web.Response):
        return division_ids
    status = request.query.get("status", "all")
    if status not in BET_STATUSES:
        return _error(400, "invalid_status", "Неизвестный статус купона.")
    user_id = query_int(request, "user_id", None, minimum=1)
    limit = query_int(request, "limit", 20, minimum=1, maximum=50)
    offset = query_int(request, "offset", 0, minimum=0)
    bets, total = await asyncio.to_thread(
        database.get_all_bets, status, None, user_id, limit, offset, division_ids,
    )
    return web.json_response({"status": "ok", "bets": bets, "total": total,
                              "limit": limit, "offset": offset})


async def handle_panel_bet_detail(request: web.Request) -> web.Response:
    """GET /api/admin/panel/bets/{id}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    bet_id = path_int(request)
    divisions = await asyncio.to_thread(database.get_betting_entity_divisions, "bet", bet_id)
    if divisions is None:
        return _error(404, "not_found", f"Купон #{bet_id} не найден.")
    if not scope.sees(divisions):
        return _error(403, "forbidden", "Купон вне ваших дивизионов.")
    bet = await asyncio.to_thread(database.get_bet_by_id, bet_id)
    return web.json_response({"status": "ok", "bet": bet})


async def handle_panel_bet_void(request: web.Request) -> web.Response:
    """POST /api/admin/panel/bets/{id}/void  {reason, confirm}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    bet_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data
    if data.get("confirm") is not True:
        return _error(400, "confirmation_required", "Аннулирование нужно подтвердить.")

    divisions = await asyncio.to_thread(database.get_betting_entity_divisions, "bet", bet_id)
    if divisions is None:
        return _error(404, "not_found", f"Купон #{bet_id} не найден.")
    if not scope.sees(divisions):
        return _error(403, "forbidden", "Купон задевает матчи вне ваших дивизионов.")
    try:
        result = await asyncio.to_thread(database.void_user_bet, bet_id, scope.actor_id)
    except ValueError as e:
        return _error(409, "invalid_state", str(e))
    reason = _text(data, "reason")
    if reason:
        await asyncio.to_thread(
            database.log_betting_audit, scope.actor_id, "bet_void_reason", "bet", bet_id, None, {"reason": reason},
        )
    return web.json_response({"status": "ok", "result": result})


async def handle_panel_players(request: web.Request) -> web.Response:
    """GET /api/admin/panel/players?q=&banned=1&sort=balance|wagered|open|name"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied
    limit = query_int(request, "limit", 50, minimum=1, maximum=200)
    players = await asyncio.to_thread(
        database.search_betting_players,
        request.query.get("q", ""),
        request.query.get("banned") == "1",
        request.query.get("sort", "balance"),
        limit,
    )
    return web.json_response({"status": "ok", "players": players})


async def handle_panel_player(request: web.Request) -> web.Response:
    """GET /api/admin/panel/players/{id}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied
    user_id = path_int(request)
    player = await asyncio.to_thread(database.get_betting_player, user_id)
    if not player:
        return _error(404, "not_found", f"Игрок #{user_id} не найден.")
    player["effective_limits"] = await asyncio.to_thread(
        BettingLimitsService.get_user_effective_limits, user_id, player.get("division_id"),
    )
    return web.json_response({"status": "ok", "player": player})


async def handle_panel_player_adjust(request: web.Request) -> web.Response:
    """POST /api/admin/panel/players/{id}/adjust  {amount, reason}  — amount < 0 списывает."""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied
    user_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data
    amount = body_int(data, "amount")
    try:
        result = await asyncio.to_thread(
            database.admin_adjust_wallet, user_id, amount, scope.actor_id, _text(data, "reason"),
        )
    except ValueError as e:
        return _error(400, "invalid_adjustment", str(e))
    return web.json_response({"status": "ok", "result": result})


async def handle_panel_player_ban(request: web.Request) -> web.Response:
    """POST /api/admin/panel/players/{id}/ban  {reason}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied
    user_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data
    try:
        ban = await asyncio.to_thread(database.set_betting_ban, user_id, scope.actor_id, _text(data, "reason"))
    except ValueError as e:
        return _error(404, "not_found", str(e))
    return web.json_response({"status": "ok", "ban": ban})


async def handle_panel_player_unban(request: web.Request) -> web.Response:
    """POST /api/admin/panel/players/{id}/unban"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied
    user_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data
    lifted = await asyncio.to_thread(database.lift_betting_ban, user_id, scope.actor_id)
    return web.json_response({"status": "ok", "lifted": lifted})


async def handle_panel_limits(request: web.Request) -> web.Response:
    """GET /api/admin/panel/limits — действующие лимиты и что переопределено на каждом уровне."""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope

    def collect() -> dict:
        divisions = database.get_divisions()
        if not scope.is_global:
            divisions = [d for d in divisions if d["id"] in scope.division_ids]
        system = {
            **BettingLimitsService.get_system_limits(),
            "max_express_events": database.get_max_express_events(),
            "initial_balance": database.get_initial_wallet_balance(),
        }
        return {
            "system": system,
            "defaults": BettingLimitsService.get_default_limits(),
            "bounds": {k: LIMIT_BOUNDS.get(k, DEFAULT_LIMIT_BOUNDS) for k in LIMIT_KEYS + SETTING_KEYS},
            "global_overrides": database.get_risk_limit_overrides("global", 0),
            # Личные лимиты — кошельки игроков, а их видит только главный админ.
            "user_overrides": database.get_user_limit_overrides() if scope.is_global else [],
            "divisions": [
                {
                    "id": d["id"],
                    "name": d["name"],
                    "effective": BettingLimitsService.get_division_limits(d["id"]),
                    "overrides": database.get_risk_limit_overrides("division", d["id"]),
                }
                for d in divisions
            ],
        }

    limits = await asyncio.to_thread(collect)
    return web.json_response({"status": "ok", "can_edit": scope.is_global, **limits})


async def handle_panel_set_limit(request: web.Request) -> web.Response:
    """POST /api/admin/panel/limits  {scope_type, scope_id, limit_key, value}  — value null сбрасывает."""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data

    scope_type = data.get("scope_type")
    if scope_type not in LIMIT_KEYS_BY_SCOPE:
        return _error(400, "invalid_scope", "Уровень лимита: global, division или user.")
    scope_id = 0 if scope_type == "global" else body_int(data, "scope_id", minimum=0)
    limit_key = data.get("limit_key")
    if limit_key not in LIMIT_KEYS_BY_SCOPE[scope_type]:
        return _error(400, "invalid_limit_key", "Этот лимит на этом уровне не настраивается.")

    reset = data.get("value") is None
    low, high = LIMIT_BOUNDS.get(limit_key, DEFAULT_LIMIT_BOUNDS)
    value = None if reset else body_int(data, "value", minimum=low, maximum=high)

    # Мин. ставка выше макс. закрыла бы приём ставок целиком.
    if value is not None and limit_key in ("min_bet", "max_bet"):
        system = await asyncio.to_thread(BettingLimitsService.get_system_limits)
        if limit_key == "min_bet" and value > system["max_bet"]:
            return _error(400, "limits_conflict",
                          f"Минимальная ставка не может быть больше максимальной ({system['max_bet']}).")
        if limit_key == "max_bet" and value < system["min_bet"]:
            return _error(400, "limits_conflict",
                          f"Максимальная ставка не может быть меньше минимальной ({system['min_bet']}).")

    def apply() -> dict:
        overrides = database.get_risk_limit_overrides(scope_type, scope_id)
        old = overrides.get(limit_key)
        if reset:
            database.delete_risk_limit_override(scope_type, scope_id, limit_key)
        else:
            BettingLimitsService.set_limit(scope_type, scope_id, limit_key, value)
        database.log_betting_audit(
            scope.actor_id, "limit_reset" if reset else "limit_set", "risk_limit", scope_id,
            {"scope_type": scope_type, "limit_key": limit_key, "value": old},
            {"scope_type": scope_type, "limit_key": limit_key, "value": value},
            scope_id if scope_type == "division" else None,
        )
        return {"scope_type": scope_type, "scope_id": scope_id, "limit_key": limit_key,
                "old_value": old, "value": value}

    result = await asyncio.to_thread(apply)
    return web.json_response({"status": "ok", "result": result})


async def handle_panel_pause(request: web.Request) -> web.Response:
    """POST /api/admin/panel/pause  {paused, division_id?, reason}

    Общую остановку ставит и снимает только глобальный админ; админ дивизиона —
    только для своих дивизионов.
    """
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data
    paused = data.get("paused")
    if not isinstance(paused, bool):
        return _error(400, "invalid_paused", "Поле paused — true или false.")
    division_id = body_int(data, "division_id", None, minimum=0)
    if division_id is None:
        denied = _global_only(scope)
        if denied is not None:
            return denied
    elif not scope.is_global and division_id not in scope.division_ids:
        return _error(403, "forbidden", "Нет доступа к этому дивизиону.")
    reason = _text(data, "reason")
    if paused and not reason:
        return _error(400, "reason_required", "Укажите причину остановки.")
    state = await asyncio.to_thread(
        database.set_betting_pause, scope.actor_id, paused, division_id, reason,
    )
    return web.json_response({"status": "ok", "pause": state})


def register_admin_panel_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/admin/panel/me", handle_panel_me)
    r.add_get("/api/admin/panel/dashboard", handle_panel_dashboard)
    r.add_get("/api/admin/panel/markets", handle_panel_markets)
    r.add_post("/api/admin/panel/markets/{id}/action", handle_panel_market_action)
    r.add_post("/api/admin/panel/selections/{id}/odds", handle_panel_selection_odds)
    r.add_get("/api/admin/panel/bets", handle_panel_bets)
    r.add_get("/api/admin/panel/bets/{id}", handle_panel_bet_detail)
    r.add_post("/api/admin/panel/bets/{id}/void", handle_panel_bet_void)
    r.add_get("/api/admin/panel/players", handle_panel_players)
    r.add_get("/api/admin/panel/players/{id}", handle_panel_player)
    r.add_post("/api/admin/panel/players/{id}/adjust", handle_panel_player_adjust)
    r.add_post("/api/admin/panel/players/{id}/ban", handle_panel_player_ban)
    r.add_post("/api/admin/panel/players/{id}/unban", handle_panel_player_unban)
    r.add_get("/api/admin/panel/limits", handle_panel_limits)
    r.add_post("/api/admin/panel/limits", handle_panel_set_limit)
    r.add_post("/api/admin/panel/pause", handle_panel_pause)
