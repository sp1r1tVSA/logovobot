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
  GET  /api/admin/panel/picks                   ИИ-прогноз: исходы по шансу захода
  GET  /api/admin/panel/picks/review            сверка ИИ-прогноза с сыгранными матчами
  GET  /api/admin/panel/outrights               долгосрочные рынки с нагрузкой по исходам
  POST /api/admin/panel/outrights/refresh       пересчитать цены сейчас
  POST /api/admin/panel/outrights/{id}/action   suspend | resume | settle | void
  POST /api/admin/panel/outright-selections/{id} статус исхода и ручной коэффициент
  GET  /api/admin/panel/analysis                анализ рынка и предложения лимитов (глобальный админ)

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

import config
from services import admin_journal
from time_utils import now_msk, today_msk_str

import database
from api.auth import get_authenticated_user
from api.params import body_int, path_int, query_int
from handlers.base import is_super_admin
from services import outright_service
from services.ai import bet_picks, market_analysis, pick_review
from services.betting_limits import (
    BAN_SCOPES, DEFAULT_LIMIT_BOUNDS, LIMIT_BOUNDS, LIMIT_KEYS, LIMIT_KEYS_BY_SCOPE, SETTING_KEYS,
    BettingLimitsService,
)

logger = logging.getLogger(__name__)

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
            "express_margin_pct": database.get_express_margin_pct(),
            "initial_balance": database.get_initial_wallet_balance(),
        }
        return {
            "system": system,
            "defaults": BettingLimitsService.get_default_limits(),
            "bounds": {k: LIMIT_BOUNDS.get(k, DEFAULT_LIMIT_BOUNDS) for k in LIMIT_KEYS + SETTING_KEYS},
            "global_overrides": database.get_risk_limit_overrides("global", 0),
            "ban_groups": [{"id": g, "label": label}
                           for g, (label, _keys) in database.BET_BAN_GROUPS.items()],
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
    is_ban = scope_type in BAN_SCOPES and limit_key in database.BET_BAN_KEYS
    if limit_key not in LIMIT_KEYS_BY_SCOPE[scope_type] and not is_ban:
        return _error(400, "invalid_limit_key", "Этот лимит на этом уровне не настраивается.")

    reset = data.get("value") is None
    low, high = (1, 1) if is_ban else LIMIT_BOUNDS.get(limit_key, DEFAULT_LIMIT_BOUNDS)
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


async def handle_panel_picks(request: web.Request) -> web.Response:
    """GET /api/admin/panel/picks?division_id=&markets=total,btts&odds_min=&odds_max=&refresh=1

    Открытые исходы по оценке шанса захода, от самого уверенного. Считает
    бесплатная модель OpenRouter, без неё — вероятность по линии. Ответ
    кэшируется, refresh пересчитывает не чаще раза в пару минут. markets —
    группы рынков (bet_picks.MARKET_GROUPS), odds_min / odds_max — диапазон кэфа.
    """
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    division_ids = _narrow(scope, request)
    if isinstance(division_ids, web.Response):
        return division_ids
    try:
        filters = bet_picks.normalize_filters(
            request.query.get("markets"),
            request.query.get("odds_min"),
            request.query.get("odds_max"),
        )
    except ValueError:
        return _error(400, "bad_filters", "Неверный фильтр: группа рынка или диапазон кэфа.")
    refresh = request.query.get("refresh") in ("1", "true")
    picks = await asyncio.to_thread(bet_picks.get_picks, division_ids, refresh, filters)
    return web.json_response({"status": "ok", **picks})


async def handle_panel_picks_review(request: web.Request) -> web.Response:
    """GET /api/admin/panel/picks/review?division_id=

    Исходы, которые показывал ИИ, против итогов сыгранных матчей: попадания,
    Brier score модели и линии на одних и тех же исходах, окупаемость «ценных».
    """
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    division_ids = _narrow(scope, request)
    if isinstance(division_ids, web.Response):
        return division_ids
    review = await asyncio.to_thread(pick_review.get_review, division_ids)
    return web.json_response({"status": "ok", **review})


# ─── Долгосрочные рынки ──────────────────────────────────────────────────────

OUTRIGHT_MARKET_ACTIONS = {"suspend": "suspended", "resume": "open"}


def _outright_board() -> dict:
    markets = database.get_outright_markets()
    exposure = database.get_outright_exposure([m["id"] for m in markets])
    empty = {"bets": 0, "stake": 0, "liability": 0}
    for m in markets:
        for sel in m["selections"]:
            sel["exposure"] = exposure.get(sel["id"], empty)
        stake = sum(sel["exposure"]["stake"] for sel in m["selections"])
        # Худший исход для книги: выигрывает самый нагруженный.
        worst = max((sel["exposure"]["liability"] for sel in m["selections"]), default=0)
        m["exposure"] = {
            "bets": sum(sel["exposure"]["bets"] for sel in m["selections"]),
            "stake": stake,
            "liability": sum(sel["exposure"]["liability"] for sel in m["selections"]),
            "worst_case": worst - stake,
        }
    return {"markets": markets}


async def handle_panel_outrights(request: web.Request) -> web.Response:
    """GET /api/admin/panel/outrights"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    board = await asyncio.to_thread(_outright_board)
    return web.json_response({"status": "ok", **board})


async def handle_panel_outrights_refresh(request: web.Request) -> web.Response:
    """POST /api/admin/panel/outrights/refresh — пересчёт всех рынков, не дожидаясь джобы."""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    result = await asyncio.to_thread(outright_service.refresh_outrights, True)
    if result.get("skipped") == "no_season":
        return _error(409, "no_season", "Нет активного сезона — считать нечего.")
    if result.get("skipped"):
        return _error(409, "busy", "Пересчёт уже идёт — обновите через минуту.")
    await asyncio.to_thread(
        database.log_betting_audit, scope.actor_id, "outright_refresh", "outright_market", 0, None, result,
    )
    return web.json_response({"status": "ok", "result": result})


def _outright_winner_factors(market: dict, raw) -> dict[int, float] | str:
    """Победители ручного расчёта: список id исходов, при нескольких — делёж поровну (dead heat)."""
    if not isinstance(raw, list) or not raw:
        return "Укажите победителя."
    try:
        ids = {int(x) for x in raw if not isinstance(x, bool)}
    except (TypeError, ValueError):
        return "Некорректный исход."
    if not ids or not ids.issubset({s["id"] for s in market["selections"]}):
        return "Победитель не принадлежит рынку."
    return {sid: 1.0 / len(ids) for sid in ids}


async def handle_panel_outright_action(request: web.Request) -> web.Response:
    """POST /api/admin/panel/outrights/{id}/action  {action, winners?, reason?, confirm?}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    market_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data
    market = await asyncio.to_thread(database.get_outright_market, market_id)
    if not market:
        return _error(404, "not_found", f"Рынок #{market_id} не найден.")

    action = data.get("action")
    reason = _text(data, "reason")
    if action in OUTRIGHT_MARKET_ACTIONS:
        ok, result = await asyncio.to_thread(
            database.set_outright_market_status, market_id, OUTRIGHT_MARKET_ACTIONS[action],
        )
        new = {"status": result, "reason": reason or None}
    elif action == "settle":
        if data.get("confirm") is not True:
            return _error(400, "confirmation_required", "Расчёт нужно подтвердить.")
        factors = _outright_winner_factors(market, data.get("winners"))
        if isinstance(factors, str):
            return _error(400, "invalid_winners", factors)
        ok, result = await asyncio.to_thread(database.settle_outright_market, market_id, factors, scope.actor_id)
        new = {"winners": sorted(factors), "result": result if ok else None}
    elif action == "void":
        if data.get("confirm") is not True:
            return _error(400, "confirmation_required", "Аннулирование нужно подтвердить.")
        if not reason:
            return _error(400, "reason_required", "Укажите причину аннулирования.")
        ok, result = await asyncio.to_thread(database.void_outright_market, market_id, reason, scope.actor_id)
        new = {"reason": reason, "result": result if ok else None}
    else:
        return _error(400, "invalid_action", "Неизвестное действие с рынком.")

    if not ok:
        return _error(409, "invalid_transition", result)
    await asyncio.to_thread(
        database.log_betting_audit, scope.actor_id, f"outright_{action}", "outright_market", market_id,
        {"status": market["status"]}, new, market["division_id"], market["season_id"],
    )
    return web.json_response({"status": "ok", "result": result})


async def handle_panel_outright_selection(request: web.Request) -> web.Response:
    """POST /api/admin/panel/outright-selections/{id}  {status?: active|suspended, odds?: number|null}

    `odds: null` снимает ручную цену и возвращает цену модели.
    """
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    selection_id = path_int(request)
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data
    if "status" not in data and "odds" not in data:
        return _error(400, "nothing_to_change", "Укажите статус или коэффициент.")
    if "status" in data and data.get("status") not in ("active", "suspended"):
        return _error(400, "invalid_status", "Статус исхода — active или suspended.")
    if isinstance(data.get("odds"), bool):
        return _error(400, "invalid_odds", "Коэффициент должен быть числом.")

    result: dict = {}
    if "status" in data:
        status = data["status"]
        ok, msg = await asyncio.to_thread(database.set_outright_selection_status, selection_id, status)
        if not ok:
            return _error(409, "invalid_transition", msg)
        result["status"] = status
        await asyncio.to_thread(
            database.log_betting_audit, scope.actor_id, f"outright_selection_{status}",
            "outright_selection", selection_id, None, {"status": status},
        )
    if "odds" in data:
        ok, res = await asyncio.to_thread(database.set_outright_odds_override, selection_id, data["odds"])
        if not ok:
            return _error(400, "invalid_odds", res)
        result.update(res)
        await asyncio.to_thread(
            database.log_betting_audit, scope.actor_id, "outright_odds_override",
            "outright_selection", selection_id, None, res,
        )
    return web.json_response({"status": "ok", "result": result})


async def handle_panel_analysis(request: web.Request) -> web.Response:
    """GET /api/admin/panel/analysis?days=14&refresh=1

    Анализ рынка за 7 / 14 / 30 дней: где игроки обыгрывают линию, откуда
    приходят монеты, и какие лимиты ужесточить. Сам ничего не меняет —
    предложения ставятся обычным POST /limits.
    """
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied
    try:
        days = market_analysis.normalize_period(request.query.get("days"))
    except ValueError:
        return _error(400, "bad_period", "Период анализа: 7, 14 или 30 дней.")
    refresh = request.query.get("refresh") in ("1", "true")
    result = await asyncio.to_thread(market_analysis.get_analysis, days, refresh)
    return web.json_response({"status": "ok", **result})



# ─── IRL (Ставки на реальные матчи) ──────────────────────────────────────────

async def handle_panel_irl_matches(request: web.Request) -> web.Response:
    """GET /api/admin/panel/irl/matches?day=YYYY-MM-DD"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    day = request.query.get("day") or today_msk_str()
    matches = await asyncio.to_thread(database.list_irl_matches, bet_day=day, limit=100)
    for m in matches:
        m["bet_stats"] = await asyncio.to_thread(database.get_irl_match_bet_stats, m["id"])

    days = await asyncio.to_thread(database.get_irl_distinct_days, 14)
    today = today_msk_str()
    if today not in days:
        days.insert(0, today)

    return web.json_response({
        "status": "ok",
        "day": day,
        "today": today,
        "days": days,
        "matches": matches,
        "irl_enabled": bool(config.IRL_ENABLED),
        "auto_publish": bool(config.IRL_AUTO_PUBLISH),
        "bookmaker_id": config.IRL_BOOKMAKER_ID,
    })


async def handle_panel_irl_candidates(request: web.Request) -> web.Response:
    """GET /api/admin/panel/irl/candidates?day=YYYY-MM-DD"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    day = request.query.get("day") or today_msk_str()
    from services.sports import get_sports_provider
    provider = get_sports_provider()
    priority = list(config.IRL_COMPETITION_PRIORITY)
    try:
        fixtures = await provider.get_prematch_fixtures(day, priority)
    except Exception as e:
        logger.warning("Failed to fetch prematch fixtures from provider: %s", e)
        fixtures = None

    if fixtures is None:
        return _error(503, "provider_unavailable", "Провайдер не вернул расписание матчей.")

    existing_matches = await asyncio.to_thread(database.list_irl_matches, bet_day=day, limit=100)
    existing_fids = {str(m["provider_fixture_id"]) for m in existing_matches}

    fresh = [f for f in fixtures if str(f.fixture_id) not in existing_fids and f.kickoff > now_msk()]
    rank = {league: i for i, league in enumerate(priority)}
    fresh.sort(key=lambda f: (rank.get(f.league_id, len(rank)), f.kickoff))

    return web.json_response({
        "status": "ok",
        "day": day,
        "candidates": [
            {
                "fixture_id": f.fixture_id,
                "league_id": f.league_id,
                "league_name": f.league_name,
                "home": f.home,
                "away": f.away,
                "kickoff_at": f.kickoff.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for f in fresh[:25]
        ],
    })


async def handle_panel_irl_add_match(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/matches  {fixture_id, day, replace_id?}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data

    fixture_id = str(data.get("fixture_id") or "").strip()
    if not fixture_id:
        return _error(400, "missing_fixture", "Укажите fixture_id матча.")

    day = str(data.get("day") or today_msk_str()).strip()
    replace_id = data.get("replace_id")
    if replace_id is not None:
        try:
            replace_id = int(replace_id)
        except (ValueError, TypeError):
            replace_id = None

    if not config.IRL_BOOKMAKER_ID:
        return _error(400, "no_bookmaker", "Не задан IRL_BOOKMAKER_ID в конфигурации.")

    from services.sports import get_sports_provider
    provider = get_sports_provider()
    fx = await provider.get_prematch_fixture(fixture_id)
    if not fx:
        return _error(404, "fixture_not_found", "Матч не найден у провайдера.")

    odds = await provider.get_match_winner_odds(fx.fixture_id, int(config.IRL_BOOKMAKER_ID))
    if not odds:
        return _error(400, "no_odds", "У выбранного букмекера нет коэффициентов 1X2 на этот матч.")

    if replace_id:
        old = await asyncio.to_thread(database.get_irl_match, replace_id)
        if old and old["status"] == "draft":
            await asyncio.to_thread(database.void_irl_match, replace_id, "Заменён админом в панели", actor_id=scope.actor_id)
            await admin_journal.record(scope.actor_id, "irl_match_replaced", "irl_match", replace_id,
                                       old=f"{old['home']} — {old['away']}", new=f"{fx.home} — {fx.away}")

    match_id, is_new = await asyncio.to_thread(
        database.create_irl_draft,
        fx.fixture_id, fx.league_id, fx.league_name, fx.home, fx.away, fx.kickoff,
        odds.home, odds.draw, odds.away, bet_day=day, picked_by="admin",
    )
    await admin_journal.record(scope.actor_id, "irl_match_added", "irl_match", match_id, new=f"{fx.home} — {fx.away}")
    return web.json_response({"status": "ok", "match_id": match_id, "is_new": is_new})


async def handle_panel_irl_publish(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/matches/{id}/publish"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    match_id = path_int(request, "id")
    ok, info = await asyncio.to_thread(database.publish_irl_match, match_id)
    if not ok:
        return _error(400, "publish_failed", str(info))
    await admin_journal.record(scope.actor_id, "irl_match_published", "irl_match", match_id)
    return web.json_response({"status": "ok", "match_id": match_id})


async def handle_panel_irl_publish_all(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/matches/publish-all  {day}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    data = await _json_body(request)
    if isinstance(data, web.Response):
        day = today_msk_str()
    else:
        day = str(data.get("day") or today_msk_str()).strip()

    drafts = await asyncio.to_thread(database.list_irl_matches, bet_day=day, statuses=("draft",))
    published = 0
    for d in drafts:
        ok, _ = await asyncio.to_thread(database.publish_irl_match, d["id"])
        if ok:
            published += 1
            await admin_journal.record(scope.actor_id, "irl_match_published", "irl_match", d["id"])
    return web.json_response({"status": "ok", "published": published})


async def handle_panel_irl_cancel(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/matches/{id}/cancel  {reason?}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    match_id = path_int(request, "id")
    m = await asyncio.to_thread(database.get_irl_match, match_id)
    if not m:
        return _error(404, "not_found", "Матч не найден.")

    data = await _json_body(request)
    reason = "Отменён админом"
    if not isinstance(data, web.Response) and data.get("reason"):
        reason = str(data["reason"]).strip()[:300]

    ok, info = await asyncio.to_thread(database.void_irl_match, match_id, reason, actor_id=scope.actor_id)
    if not ok:
        return _error(400, "cancel_failed", str(info))
    refunded = info.get("refunded", 0) if isinstance(info, dict) else 0
    await admin_journal.record(scope.actor_id, "irl_match_cancelled", "irl_match", match_id,
                               old=m["status"], new=f"возвращено ставок: {refunded}")
    return web.json_response({"status": "ok", "refunded": refunded})


async def handle_panel_irl_settle(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/matches/{id}/settle  {result, home_goals?, away_goals?}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    match_id = path_int(request, "id")
    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data

    result = str(data.get("result") or "").lower().strip()
    if result not in ("home", "draw", "away"):
        return _error(400, "invalid_result", "Исход должен быть 'home', 'draw' или 'away'.")

    home_goals = data.get("home_goals")
    away_goals = data.get("away_goals")
    try:
        home_goals = int(home_goals) if home_goals is not None and home_goals != "" else None
        away_goals = int(away_goals) if away_goals is not None and away_goals != "" else None
    except (ValueError, TypeError):
        return _error(400, "invalid_goals", "Количество голов должно быть целым числом.")

    ok, info = await asyncio.to_thread(database.settle_irl_match, match_id, result, home_goals, away_goals, actor_id=scope.actor_id)
    if not ok:
        return _error(400, "settle_failed", str(info))
    await admin_journal.record(scope.actor_id, "irl_match_settled", "irl_match", match_id,
                               new=f"{result} ({home_goals}:{away_goals})")
    return web.json_response({"status": "ok", "info": info})


async def handle_panel_irl_refresh_odds(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/matches/{id}/refresh-odds"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    match_id = path_int(request, "id")
    m = await asyncio.to_thread(database.get_irl_match, match_id)
    if not m:
        return _error(404, "not_found", "Матч не найден.")
    if m["status"] not in ("draft", "open"):
        return _error(400, "not_editable", "Кэфы можно обновлять только у черновиков и открытых матчей.")

    from services.sports import get_sports_provider
    provider = get_sports_provider()
    odds = await provider.get_match_winner_odds(m["provider_fixture_id"], int(config.IRL_BOOKMAKER_ID))
    if not odds:
        return _error(400, "no_odds", "Провайдер не вернул обновлённые коэффициенты.")

    ok = await asyncio.to_thread(database.update_irl_odds, match_id, odds.home, odds.draw, odds.away)
    return web.json_response({
        "status": "ok",
        "updated": ok,
        "odds": {"home": odds.home, "draw": odds.draw, "away": odds.away},
    })


async def handle_panel_irl_update_score(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/matches/{id}/score  {home_goals?, away_goals?, from_api?}"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    match_id = path_int(request, "id")
    m = await asyncio.to_thread(database.get_irl_match, match_id)
    if not m:
        return _error(404, "not_found", "Матч не найден.")
    if m["status"] not in ("open", "closed"):
        return _error(400, "not_editable", "Счёт можно обновлять только у активных матчей.")

    data = await _json_body(request)
    if isinstance(data, web.Response):
        return data

    from_api = bool(data.get("from_api"))
    if from_api:
        from services.sports import get_sports_provider
        provider = get_sports_provider()
        fx = await provider.get_prematch_fixture(m["provider_fixture_id"])
        if not fx:
            return _error(400, "provider_error", "Провайдер не вернул данные по матчу.")
        home_goals = fx.home_goals
        away_goals = fx.away_goals
    else:
        raw_h = data.get("home_goals")
        raw_a = data.get("away_goals")
        try:
            home_goals = int(raw_h) if raw_h is not None and raw_h != "" else None
            away_goals = int(raw_a) if raw_a is not None and raw_a != "" else None
        except (ValueError, TypeError):
            return _error(400, "invalid_goals", "Количество голов должно быть целым числом.")

    ok = await asyncio.to_thread(database.update_irl_live_score, match_id, home_goals, away_goals)
    await admin_journal.record(scope.actor_id, "irl_match_score_updated", "irl_match", match_id,
                               new=f"{home_goals}:{away_goals}")
    return web.json_response({
        "status": "ok",
        "updated": ok,
        "home_goals": home_goals,
        "away_goals": away_goals,
    })


async def handle_panel_irl_match_bets(request: web.Request) -> web.Response:
    """GET /api/admin/panel/irl/matches/{id}/bets"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    match_id = path_int(request, "id")
    m = await asyncio.to_thread(database.get_irl_match, match_id)
    if not m:
        return _error(404, "not_found", "Матч не найден.")

    bets = await asyncio.to_thread(database.get_irl_match_bets, match_id, 100)
    return web.json_response({"status": "ok", "match": m, "bets": bets})


async def handle_panel_irl_run_pick(request: web.Request) -> web.Response:
    """POST /api/admin/panel/irl/run-pick"""
    scope = _resolve_scope(request)
    if isinstance(scope, web.Response):
        return scope
    denied = _global_only(scope)
    if denied is not None:
        return denied

    from services import irl_jobs
    report = await irl_jobs.run_pick(bot=None)
    return web.json_response({"status": "ok", "report": report})


def register_admin_panel_routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/admin/panel/irl/matches", handle_panel_irl_matches)
    r.add_get("/api/admin/panel/irl/candidates", handle_panel_irl_candidates)
    r.add_post("/api/admin/panel/irl/matches", handle_panel_irl_add_match)
    r.add_post("/api/admin/panel/irl/matches/publish-all", handle_panel_irl_publish_all)
    r.add_post("/api/admin/panel/irl/matches/{id}/publish", handle_panel_irl_publish)
    r.add_post("/api/admin/panel/irl/matches/{id}/cancel", handle_panel_irl_cancel)
    r.add_post("/api/admin/panel/irl/matches/{id}/settle", handle_panel_irl_settle)
    r.add_post("/api/admin/panel/irl/matches/{id}/refresh-odds", handle_panel_irl_refresh_odds)
    r.add_post("/api/admin/panel/irl/matches/{id}/score", handle_panel_irl_update_score)
    r.add_get("/api/admin/panel/irl/matches/{id}/bets", handle_panel_irl_match_bets)
    r.add_post("/api/admin/panel/irl/run-pick", handle_panel_irl_run_pick)
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
    r.add_get("/api/admin/panel/picks", handle_panel_picks)
    r.add_get("/api/admin/panel/picks/review", handle_panel_picks_review)
    r.add_get("/api/admin/panel/outrights", handle_panel_outrights)
    r.add_post("/api/admin/panel/outrights/refresh", handle_panel_outrights_refresh)
    r.add_post("/api/admin/panel/outrights/{id}/action", handle_panel_outright_action)
    r.add_post("/api/admin/panel/outright-selections/{id}", handle_panel_outright_selection)
    r.add_get("/api/admin/panel/analysis", handle_panel_analysis)
