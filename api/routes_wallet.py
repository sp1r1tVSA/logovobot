"""
api/routes_wallet.py

REST API handlers for bootstrap data, user wallet, daily bonus, and leaderboard.
"""

import asyncio
import json
import logging
from aiohttp import web
import database
from config import INITIAL_WALLET_BALANCE
from time_utils import now_msk, parse_msk
from api.auth import get_authenticated_user, check_user_access
from handlers.base import is_admin

logger = logging.getLogger(__name__)


def _resolve_user_bet_limits(user_id: int) -> dict:
    """
    Stake limits the Mini App coupon needs for its MAX chip.

    Advisory only — the risk engine re-checks every bet server-side, so a
    lookup failure falls back to the service defaults instead of failing
    the bootstrap.
    """
    from services.betting_limits import (
        BettingLimitsService, DEFAULT_MIN_BET, DEFAULT_MAX_BET, DEFAULT_MAX_PAYOUT,
        DEFAULT_MAX_OPEN_EXPOSURE,
    )
    try:
        limits = BettingLimitsService.get_user_effective_limits(user_id)
        return {
            "min_bet": int(limits["min_bet"]),
            "max_bet": int(limits["max_bet"]),
            "max_payout": int(limits["max_payout"]),
            "max_open_exposure": int(limits["max_open_exposure"]),
            "open_exposure": database.get_user_open_exposure(user_id),
        }
    except Exception as e:
        logger.warning(f"Could not resolve bet limits for user #{user_id}: {e}")
        return {
            "min_bet": DEFAULT_MIN_BET,
            "max_bet": DEFAULT_MAX_BET,
            "max_payout": DEFAULT_MAX_PAYOUT,
            "max_open_exposure": DEFAULT_MAX_OPEN_EXPOSURE,
            "open_exposure": 0,
        }


async def handle_bootstrap(request: web.Request) -> web.Response:
    """
    GET /api/bootstrap
    Returns current user info, balance, active tours count, and bonus cooldown.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)

    if not user_info or "id" not in user_info:
        return web.json_response(
            {"status": "error", "error": "unauthorized", "message": "Недействительные данные авторизации Telegram."},
            status=401
        )

    user_id = user_info["id"]
    is_adm = is_admin(user_id)
    has_access = check_user_access(user_id)

    # NOTE: settlement runs in settle_finished_bets_job (services/background_sync.py),
    # not on this request path — it blocked the shared bot/API event loop.

    # Fetch wallet
    wallet = await asyncio.to_thread(database.get_or_create_wallet, user_id)

    # Check bonus availability
    can_claim = True
    cooldown_sec = 0
    last_bonus = wallet.get("last_bonus_at")
    if last_bonus:
        try:
            last_dt = parse_msk(last_bonus)
            elapsed = (now_msk() - last_dt).total_seconds()
            if elapsed < 86400:
                can_claim = False
                cooldown_sec = int(86400 - elapsed)
        except Exception:
            pass

    # Fetch open tours summary
    open_tours = await asyncio.to_thread(database.get_open_betting_tours)

    bet_limits = await asyncio.to_thread(_resolve_user_bet_limits, user_id)

    return web.json_response({
        "status": "ok",
        "user": {
            "user_id": user_id,
            "first_name": user_info.get("first_name", "Игрок"),
            "username": user_info.get("username", ""),
            "photo_url": user_info.get("photo_url", ""),
            "balance": wallet.get("balance", INITIAL_WALLET_BALANCE),
            "total_wagered": wallet.get("total_wagered", 0),
            "total_won": wallet.get("total_won", 0),
            "bets_count": wallet.get("bets_count", 0),
            "bets_won": wallet.get("bets_won", 0),
            "is_admin": is_adm,
            "has_access": has_access,
            "bet_limits": bet_limits
        },
        "bonus": {
            "can_claim": can_claim,
            "cooldown_seconds": cooldown_sec,
            "reward_amount": 250
        },
        "open_tours_count": len(open_tours)
    })


async def handle_claim_bonus(request: web.Request) -> web.Response:
    """
    POST /api/bonus/claim
    Claim daily +250 coins bonus.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)

    if not user_info or "id" not in user_info:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    user_id = user_info["id"]
    if not check_user_access(user_id):
        return web.json_response(
            {"status": "error", "error": "access_restricted", "message": "Logovo.bet временно недоступен."},
            status=403
        )

    success, val, msg = await asyncio.to_thread(database.claim_daily_bonus, user_id, 250)
    if not success:
        return web.json_response({
            "status": "error",
            "error": "cooldown",
            "message": msg,
            "remaining_hours": val
        }, status=400)

    return web.json_response({
        "status": "ok",
        "message": msg,
        "new_balance": val,
        "claimed_amount": 250
    })


async def handle_leaderboard(request: web.Request) -> web.Response:
    """
    GET /api/leaderboard
    Get top bettors ranking. Supports ?division_id=X&season_id=Y&min_bets=Z query parameters.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    user_id = user_info.get("id") if user_info else 0
    if not check_user_access(user_id):
        return web.json_response({
            "status": "error",
            "error": "access_restricted",
            "message": "Logovo.bet временно недоступен."
        }, status=403)

    division_id_str = request.query.get("division_id")
    season_id_str = request.query.get("season_id")
    min_bets_str = request.query.get("min_bets")

    div_id = int(division_id_str) if division_id_str and division_id_str.isdigit() else None
    season_id = int(season_id_str) if season_id_str and season_id_str.isdigit() else None
    min_bets = int(min_bets_str) if min_bets_str and min_bets_str.isdigit() else 5

    from services.analytics_service import get_capper_leaderboard
    capper_leaders = get_capper_leaderboard(division_id=div_id, season_id=season_id, min_bets=min_bets)

    # Legacy coin leaders for backward compatibility
    coin_leaders = await asyncio.to_thread(database.get_top_bettors, 20)

    my_rank = None
    if user_id:
        for idx, item in enumerate(coin_leaders, 1):
            if item["user_id"] == user_id:
                my_rank = {
                    "rank": idx,
                    "balance": item["balance"],
                    "bets_won": item["bets_won"],
                    "bets_count": item["bets_count"]
                }
                break

    return web.json_response({
        "status": "ok",
        "leaders": coin_leaders,
        "capper_leaders": capper_leaders,
        "my_rank": my_rank
    })


async def handle_get_division_leaderboard(request: web.Request) -> web.Response:
    """
    GET /api/leaderboard/division/{division_id}
    Returns division-scoped capper leaderboard with ROI, win rate, and min bets filtering.
    """
    try:
        division_id = int(request.match_info["division_id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Некорректный ID дивизиона."}, status=400)

    season_id_str = request.query.get("season_id")
    season_id = int(season_id_str) if season_id_str and season_id_str.isdigit() else None
    min_bets_str = request.query.get("min_bets", "5")
    min_bets = int(min_bets_str) if min_bets_str and min_bets_str.isdigit() else 5
    limit_str = request.query.get("limit", "20")
    limit = int(limit_str) if limit_str and limit_str.isdigit() else 20

    from services.analytics_service import get_capper_leaderboard
    leaders = get_capper_leaderboard(division_id=division_id, season_id=season_id, min_bets=min_bets, limit=limit)

    return web.json_response({
        "status": "ok",
        "division_id": division_id,
        "season_id": season_id,
        "count": len(leaders),
        "leaders": leaders
    })


async def handle_get_wallet(request: web.Request) -> web.Response:
    """
    GET /api/wallet
    Returns authenticated user's wallet info: balance, stats, and bonus cooldown.
    """
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)

    if not user_info or "id" not in user_info:
        return web.json_response(
            {"status": "error", "error": "unauthorized", "message": "Недействительные данные авторизации Telegram."},
            status=401
        )

    user_id = user_info["id"]
    wallet = await asyncio.to_thread(database.get_or_create_wallet, user_id)

    can_claim = True
    cooldown_sec = 0
    last_bonus = wallet.get("last_bonus_at")
    if last_bonus:
        try:
            last_dt = parse_msk(last_bonus)
            elapsed = (now_msk() - last_dt).total_seconds()
            if elapsed < 86400:
                can_claim = False
                cooldown_sec = int(86400 - elapsed)
        except Exception:
            pass

    return web.json_response({
        "status": "ok",
        "wallet": {
            "user_id": user_id,
            "balance": wallet.get("balance", INITIAL_WALLET_BALANCE),
            "currency": "🪙",
            "total_wagered": wallet.get("total_wagered", 0),
            "total_won": wallet.get("total_won", 0),
            "bets_count": wallet.get("bets_count", 0),
            "bets_won": wallet.get("bets_won", 0),
            "can_claim_bonus": can_claim,
            "bonus_cooldown_seconds": cooldown_sec
        }
    })
