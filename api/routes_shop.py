"""
api/routes_shop.py

REST API endpoints for shop catalog, inventory, purchases,
Wheel of Fortune roulette spins, and secret player claim management.
"""

from __future__ import annotations

import asyncio
import logging
from aiohttp import web

import database
from api.auth import get_authenticated_user
from handlers.base import is_admin, is_super_admin
from services import shop_service

logger = logging.getLogger(__name__)


def _auth(request: web.Request) -> dict | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return get_authenticated_user(init_data)


async def handle_get_shop_catalog(request: web.Request) -> web.Response:
    """GET /api/shop/catalog - каталог наград с учётом цен, статуса ТО и баланса."""
    user_info = _auth(request)
    if not user_info:
        return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)

    user_id = user_info["id"]
    try:
        catalog = await asyncio.to_thread(shop_service.get_shop_catalog, user_id)
        return web.json_response({"status": "ok", **catalog})
    except Exception as e:
        logger.exception("Failed to get shop catalog")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_get_shop_inventory(request: web.Request) -> web.Response:
    """GET /api/shop/inventory - активный инвентарь наград пользователя."""
    user_info = _auth(request)
    if not user_info:
        return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)

    user_id = user_info["id"]
    try:
        inventory = await asyncio.to_thread(database.get_user_shop_inventory, user_id)
        return web.json_response({"status": "ok", "inventory": inventory})
    except Exception as e:
        logger.exception("Failed to get shop inventory")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_post_shop_buy(request: web.Request) -> web.Response:
    """POST /api/shop/buy - покупка товара из каталога."""
    user_info = _auth(request)
    if not user_info:
        return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Некорректный JSON запрос"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"status": "error", "message": "Ожидается JSON объект"}, status=400)

    item_id = str(data.get("item_id", "")).strip()
    if not item_id:
        return web.json_response({"status": "error", "message": "Не указан item_id"}, status=400)

    notes = data.get("notes")
    user_id = user_info["id"]

    try:
        result = await asyncio.to_thread(shop_service.buy_shop_item, user_id, item_id, notes)
        return web.json_response(result)
    except ValueError as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)
    except Exception as e:
        logger.exception(f"Failed to buy shop item {item_id}")
        return web.json_response({"status": "error", "message": "Внутренняя ошибка покупки"}, status=500)


async def handle_post_shop_roulette_spin(request: web.Request) -> web.Response:
    """POST /api/shop/roulette/spin - прокрут Колеса Фортуны (25 000 🪙)."""
    user_info = _auth(request)
    if not user_info:
        return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)

    # Validate JSON payload if present
    try:
        if request.can_read_body:
            body = await request.read()
            if body.strip():
                import json
                json.loads(body.decode("utf-8"))
    except Exception:
        return web.json_response({"status": "error", "message": "Некорректный JSON запрос"}, status=400)

    user_id = user_info["id"]

    try:
        result = await asyncio.to_thread(shop_service.spin_roulette, user_id)
        return web.json_response(result)
    except ValueError as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)
    except Exception as e:
        logger.exception("Failed to spin roulette")
        return web.json_response({"status": "error", "message": "Внутренняя ошибка рулетки"}, status=500)


async def handle_get_admin_shop_claims(request: web.Request) -> web.Response:
    """GET /api/admin/shop/claims - список заявок на секретного игрока (только админы)."""
    user_info = _auth(request)
    if not user_info:
        return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)

    user_id = user_info["id"]
    if not is_admin(user_id) and not is_super_admin(user_id):
        return web.json_response({"status": "error", "message": "Доступ запрещён"}, status=403)

    status_filter = request.query.get("status")
    try:
        claims = await asyncio.to_thread(database.list_secret_player_claims, status_filter)
        return web.json_response({"status": "ok", "claims": claims})
    except Exception as e:
        logger.exception("Failed to list secret player claims")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_post_admin_shop_claim_resolve(request: web.Request) -> web.Response:
    """POST /api/admin/shop/claims/{id}/resolve - утвердить/отклонить заявку на секретного игрока."""
    user_info = _auth(request)
    if not user_info:
        return web.json_response({"status": "error", "message": "Unauthorized"}, status=401)

    user_id = user_info["id"]
    if not is_admin(user_id) and not is_super_admin(user_id):
        return web.json_response({"status": "error", "message": "Доступ запрещён"}, status=403)

    try:
        claim_id = int(request.match_info["id"])
    except (ValueError, KeyError):
        return web.json_response({"status": "error", "message": "Некорректный ID заявки"}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Некорректный JSON запрос"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"status": "error", "message": "Ожидается JSON объект"}, status=400)

    action = str(data.get("action", "approved")).strip()
    player_name = str(data.get("player_name", "")).strip()
    notes = data.get("notes")

    try:
        ok, msg = await asyncio.to_thread(
            database.resolve_secret_player_claim,
            claim_id,
            user_id,
            player_name,
            action,
            notes,
        )
        if not ok:
            return web.json_response({"status": "error", "message": msg}, status=400)
        return web.json_response({"status": "ok", "message": msg})
    except Exception as e:
        logger.exception(f"Failed to resolve claim {claim_id}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


def register_shop_routes(app: web.Application) -> None:
    """Регистрация всех маршрутов магазина наград в aiohttp приложении."""
    app.router.add_get("/api/shop/catalog", handle_get_shop_catalog)
    app.router.add_get("/api/shop/inventory", handle_get_shop_inventory)
    app.router.add_post("/api/shop/buy", handle_post_shop_buy)
    app.router.add_post("/api/shop/roulette/spin", handle_post_shop_roulette_spin)
    app.router.add_get("/api/admin/shop/claims", handle_get_admin_shop_claims)
    app.router.add_post("/api/admin/shop/claims/{id}/resolve", handle_post_admin_shop_claim_resolve)
