"""
api/routes_predictions.py

REST API handlers for bet slip placement, prediction history, details, and repeat predictions.
"""

import asyncio
import json
import logging
from aiohttp import web
import database
from api.auth import get_authenticated_user, check_user_access

logger = logging.getLogger(__name__)


async def handle_place_prediction(request: web.Request) -> web.Response:
    """
    POST /api/predictions
    Place a single or express prediction coupon.
    Body:
    {
        "amount": 250,
        "idempotency_key": "uuid-or-timestamp",
        "selections": [
            { "match_id": 101, "outcome": "p1", "odd": 2.10 },
            { "match_id": 102, "outcome": "over_2.5", "odd": 1.75 }
        ]
    }
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
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Некорректный JSON тела запроса."}, status=400)

    try:
        raw_amount = data.get("amount", 0)
        if isinstance(raw_amount, float) and not raw_amount.is_integer():
            return web.json_response({"status": "error", "message": "Сумма ставки должна быть целым числом."}, status=400)
        if isinstance(raw_amount, str):
            raw_amount_clean = raw_amount.strip()
            if "." in raw_amount_clean:
                return web.json_response({"status": "error", "message": "Сумма ставки должна быть целым числом."}, status=400)
            amount = int(raw_amount_clean)
        elif isinstance(raw_amount, (int, float)):
            amount = int(raw_amount)
        else:
            return web.json_response({"status": "error", "message": "Некорректная сумма ставки."}, status=400)
    except (ValueError, TypeError):
        return web.json_response({"status": "error", "message": "Некорректная сумма ставки."}, status=400)

    selections = data.get("selections", [])
    idempotency_key = data.get("idempotency_key")

    if amount < 10:
        return web.json_response({"status": "error", "message": "Минимальная сумма ставки — 10 🪙."}, status=400)

    if not selections or not isinstance(selections, list):
        return web.json_response({"status": "error", "message": "Купон не содержит выбранных исходов."}, status=400)

    normalized_selections = []
    seen_matches = set()
    for s in selections:
        if not isinstance(s, dict):
            return web.json_response({"status": "error", "message": "Некорректная структура исхода в купоне."}, status=400)
        raw_mid = s.get("match_id")
        try:
            if isinstance(raw_mid, float) and not raw_mid.is_integer():
                return web.json_response({"status": "error", "message": f"Некорректный ID матча: {raw_mid}"}, status=400)
            if isinstance(raw_mid, str):
                raw_mid_clean = raw_mid.strip()
                if "." in raw_mid_clean:
                    return web.json_response({"status": "error", "message": f"Некорректный ID матча: {raw_mid}"}, status=400)
                m_id = int(raw_mid_clean)
            elif isinstance(raw_mid, int):
                m_id = raw_mid
            else:
                return web.json_response({"status": "error", "message": f"Некорректный ID матча: {raw_mid}"}, status=400)
            if m_id <= 0:
                return web.json_response({"status": "error", "message": f"Некорректный ID матча: {raw_mid}"}, status=400)
        except (ValueError, TypeError):
            return web.json_response({"status": "error", "message": f"Некорректный ID матча: {raw_mid}"}, status=400)

        out_type = s.get("outcome") or s.get("selection_key")
        if not out_type:
            return web.json_response({"status": "error", "message": "Некорректный исход в купоне."}, status=400)

        if len(selections) > 1 and m_id in seen_matches:
            return web.json_response({
                "status": "error",
                "message": f"Нельзя добавлять несколько исходов из одного матча #{m_id} в стандартный экспресс."
            }, status=400)
        seen_matches.add(m_id)

        s_copy = dict(s)
        s_copy["match_id"] = m_id
        s_copy["outcome"] = str(out_type)
        normalized_selections.append(s_copy)

    success, result = await asyncio.to_thread(database.place_user_bet, 
        user_id=user_id,
        amount=amount,
        selections=normalized_selections,
        idempotency_key=idempotency_key
    )

    if not success:
        # Phase 5: structured error codes
        if isinstance(result, dict):
            error_code = result.get("error", "")
            if error_code == "ODDS_CHANGED":
                return web.json_response({
                    "status": "error",
                    "error": "ODDS_CHANGED",
                    "match_id": result.get("match_id"),
                    "outcome": result.get("outcome"),
                    "old_odd": result.get("old_odd"),
                    "new_odd": result.get("new_odd"),
                    "message": result.get("message", "Коэффициент изменился.")
                }, status=409)
            if error_code == "IDEMPOTENCY_KEY_REUSED":
                return web.json_response({
                    "status": "error",
                    "error": "IDEMPOTENCY_KEY_REUSED",
                    "message": result.get("message", "Ключ уже использован для другой ставки.")
                }, status=409)
            if error_code == "SELF_BET_PROHIBITED":
                # 322-защита: ставка на матч с собственным участием.
                # Код отдаём отдельно, чтобы Mini App показал причину, а не общий отказ.
                return web.json_response({
                    "status": "error",
                    "error": "SELF_BET_PROHIBITED",
                    "message": result.get("message", "Запрещено делать ставки на матчи с собственным участием.")
                }, status=400)
            if error_code == "OPEN_BETS_LIMIT":
                # Отдаём счётчик слотов: Mini App показывает его постоянно и
                # поправит по отказу, не дожидаясь следующего bootstrap.
                return web.json_response({
                    "status": "error",
                    "error": "OPEN_BETS_LIMIT",
                    "max_open_bets": result.get("max_open_bets"),
                    "open_bets": result.get("open_bets"),
                    "message": result.get("message", "Слишком много открытых купонов.")
                }, status=400)
            if error_code in ("MAX_BET_EXCEEDED", "MAX_PAYOUT_EXCEEDED"):
                return web.json_response({
                    "status": "error",
                    "error": error_code,
                    "message": result.get("message", "Превышен лимит ставки.")
                }, status=400)
            return web.json_response({"status": "error", "message": result.get("message", str(result))}, status=400)
        return web.json_response({"status": "error", "message": str(result)}, status=400)

    bet_id = result
    user_balance = await asyncio.to_thread(database.get_wallet_balance, user_id)
    bet_type = "single" if len(selections) == 1 else "express"

    # Progression and achievements hooks
    try:
        await asyncio.to_thread(database.add_user_xp, user_id, 30 if bet_type == "single" else 60)
        await asyncio.to_thread(database.evaluate_betting_achievements, user_id)
    except Exception as e:
        logger.warning(f"Error in gamification hook on bet placement: {e}")

    # Notify subscribed super-admins about new bet (Live alerts in PM)
    try:
        from handlers.admin_bets import notify_super_admins_new_bet
        asyncio.create_task(notify_super_admins_new_bet(bet_id=bet_id))
    except Exception as e:
        logger.debug(f"Failed to schedule super admin bet notification: {e}")

    return web.json_response({
        "status": "ok",
        "bet_id": bet_id,
        "bet_type": bet_type,
        "amount": amount,
        "new_balance": user_balance,
        "message": "🎉 Прогноз успешно принят!"
    })


async def handle_get_predictions(request: web.Request) -> web.Response:
    """
    GET /api/predictions?status=pending|won|lost|all&limit=30
    Retrieve user's bet history.
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

    # NOTE: settlement runs in settle_finished_bets_job (services/background_sync.py),
    # not on this request path — it blocked the shared bot/API event loop.

    # Phase 5: Bet History 2.0 — support all status filters including 'cancelled'
    VALID_STATUSES = {"pending", "won", "lost", "refunded", "cancelled", "void", "all"}
    status_filter = request.query.get("status")
    if status_filter and status_filter not in VALID_STATUSES:
        status_filter = None  # ignore unknown filter

    try:
        raw_limit = int(request.query.get("limit", 50))
        limit = max(1, min(100, raw_limit))
    except (ValueError, TypeError):
        limit = 50

    bets = await asyncio.to_thread(database.get_user_bets, user_id, status=status_filter, limit=limit)

    return web.json_response({
        "status": "ok",
        "bets": bets,
        "predictions": bets
    })


async def handle_get_prediction_detail(request: web.Request) -> web.Response:
    """
    GET /api/predictions/{id}
    Returns detailed view of a prediction with its items.
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
        bet_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID."}, status=400)

    matching = await asyncio.to_thread(database.get_user_bet_by_id, user_id, bet_id)
    if not matching:
        return web.json_response({"status": "error", "message": "Прогноз не найден."}, status=404)

    return web.json_response({
        "status": "ok",
        "prediction": matching
    })


def _filter_repeat_selections(items: list[dict]) -> list[dict]:
    """Helper executed in threadpool to check upcoming match statuses."""
    cloned = []
    with database.transaction() as conn:
        cursor = conn.cursor()
        for item in items:
            m_id = item["match_id"]
            out_type = item["outcome_type"]
            cursor.execute("SELECT status FROM matches WHERE id = ?", (m_id,))
            m_row = cursor.fetchone()
            if m_row and m_row["status"] in ("scheduled", "live"):
                cloned.append({
                    "match_id": m_id,
                    "outcome": out_type,
                    "odd": item["odd"],
                    "team1_name": item.get("team1_name"),
                    "team2_name": item.get("team2_name")
                })
    return cloned


async def handle_repeat_prediction(request: web.Request) -> web.Response:
    """
    POST /api/predictions/{id}/repeat
    Validates legs from a previous bet and returns fresh coupon data.
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
        bet_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID."}, status=400)

    matching = await asyncio.to_thread(database.get_user_bet_by_id, user_id, bet_id)
    if not matching:
        return web.json_response({"status": "error", "message": "Исходный прогноз не найден."}, status=404)

    cloned_selections = await asyncio.to_thread(_filter_repeat_selections, matching.get("items", []))

    if not cloned_selections:
        return web.json_response({
            "status": "error",
            "message": "Матчи из этого прогноза уже завершены или недоступны."
        }, status=400)

    return web.json_response({
        "status": "ok",
        "amount": matching["amount"],
        "selections": cloned_selections,
        "message": f"Скопировано {len(cloned_selections)} событий в купон."
    })


async def handle_get_cashout_quote(request: web.Request) -> web.Response:
    """
    GET /api/predictions/{id}/cashout-quote
    Returns live cashout valuation.
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
        bet_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID."}, status=400)

    from services.cashout_engine import quote_cashout
    quote = await asyncio.to_thread(quote_cashout, user_id=user_id, bet_id=bet_id)
    # Движок называет поля available/offer, клиент читает cashout_available/amount.
    # Алиасы кладём и внутрь quote, и на верхний уровень: вложенный объект нужен
    # текущему клиенту, плоский — уже задеплоенным старым версиям Mini App.
    payload = {
        **quote,
        "cashout_available": quote.get("available"),
        "amount": quote.get("offer"),
    }
    return web.json_response({**payload, "quote": payload, "status": "ok"})


async def handle_execute_cashout(request: web.Request) -> web.Response:
    """
    POST /api/predictions/{id}/cashout
    Executes atomic early cashout settlement.
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
        bet_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID."}, status=400)

    idempotency_key = None
    try:
        body = await request.json()
        idempotency_key = body.get("idempotency_key")
    except Exception:
        pass

    from services.cashout_engine import execute_cashout
    success, result = await asyncio.to_thread(
        execute_cashout, user_id=user_id, bet_id=bet_id, idempotency_key=idempotency_key
    )
    if not success:
        return web.json_response({
            "status": "error",
            "error": result.get("error") if isinstance(result, dict) else "CASHOUT_FAILED",
            "message": result.get("message") if isinstance(result, dict) else str(result)
        }, status=400)

    # result несёт balance/payout и собственный status="cashed_out". Дублируем
    # new_balance и раскрываем поля на верхний уровень, но "status": "ok" ставим
    # последним — иначе внутренний статус перебил бы статус ответа.
    payload = {**result, "new_balance": result.get("balance")}
    return web.json_response({**payload, "result": payload, "status": "ok"})
