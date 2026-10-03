"""aiohttp HTTP-маршруты Mini App для трансферного окна (/api/transfers/...).

* GET  /api/transfers/status        — статус окна, бюджет и слоты клуба, заявки тренера
* GET  /api/transfers/history       — лента одобренных сделок
* GET  /api/transfers/urn           — доступные карты в урне для выкупа
* GET  /api/transfers/{id}/photo    — прокси фото заявки из Telegram
* POST /api/transfers/deal          — подать сделку с другим тренером (JSON или multipart с фото)
* POST /api/transfers/surcharge     — подать заявку на доплату за спешл
* POST /api/transfers/urn/sale      — продать карту в урну
* POST /api/transfers/urn/buy       — выкупить карту из урны
* POST /api/transfers/{id}/confirm  — подтвердить сделку второй стороной
* POST /api/transfers/{id}/decline  — отклонить сделку второй стороной
* POST /api/transfers/{id}/withdraw — отозвать свою заявку
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

from aiohttp import web
from PIL import Image

from api.auth import check_user_access, extract_init_data, get_authenticated_user
from transfers import notify, requests as req_mod, service

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10 MB
PHOTO_MAX_SIDE = 1920


def _auth(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Проверка Telegram initData и доступа к платформе (fail-closed)."""
    init_data = extract_init_data(request)
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return None, web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not check_user_access(user_info["id"]):
        return None, web.json_response({"status": "error", "error": "LOGOVO_LOCKDOWN"}, status=403)
    return user_info, None


def _get_bot(request: web.Request):
    return request.app.get("bot") or notify.get_bot()


def _process_uploaded_photo(raw: bytes) -> bytes:
    """Проверить и пережать загруженное фото в чистый JPEG."""
    if len(raw) > MAX_PHOTO_BYTES:
        raise service.InputError("Фото слишком большое (максимум 10 МБ).")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise service.InputError("Не удалось прочитать фото. Поддерживаются форматы JPEG, PNG, WEBP.")

    if img.mode != "RGB":
        img = img.convert("RGB")

    if img.width > PHOTO_MAX_SIDE or img.height > PHOTO_MAX_SIDE:
        img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


async def _read_request_payload(request: web.Request) -> tuple[dict[str, Any], bytes | None]:
    """Разобрать тело запроса (JSON или multipart/form-data)."""
    content_type = request.content_type or ""
    if "multipart" in content_type:
        reader = await request.multipart()
        fields: dict[str, Any] = {}
        photo_bytes: bytes | None = None
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "photo":
                raw = await part.read()
                if raw:
                    photo_bytes = _process_uploaded_photo(raw)
            else:
                text = await part.text()
                fields[part.name] = text
        return fields, photo_bytes
    else:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(
                text=json.dumps({"status": "error", "error": "invalid_json"}),
                content_type="application/json",
            )
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({"status": "error", "error": "expected_json_object"}),
                content_type="application/json",
            )
        return body, None


# ─── GET Endpoints ────────────────────────────────────────────────────────────

async def handle_get_status(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    try:
        data = req_mod.my_status(user_info["id"])
        return web.json_response({"status": "ok", "data": data})
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: handle_get_status failed")
        return web.json_response({"status": "error", "message": "Внутренняя ошибка сервера"}, status=500)


async def handle_get_history(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    try:
        data = req_mod.history()
        return web.json_response({"status": "ok", "data": data})
    except Exception as exc:
        logger.exception("transfers: handle_get_history failed")
        return web.json_response({"status": "error", "message": "Внутренняя ошибка сервера"}, status=500)


async def handle_get_urn(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    try:
        data = req_mod.urn_items(user_info["id"])
        return web.json_response({"status": "ok", "data": data})
    except Exception as exc:
        logger.exception("transfers: handle_get_urn failed")
        return web.json_response({"status": "error", "message": "Внутренняя ошибка сервера"}, status=500)


async def handle_get_photo(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err

    transfer_id = request.match_info.get("id")
    try:
        file_id = req_mod.photo_file_id(user_info["id"], transfer_id)
    except service.InputError:
        return web.json_response({"status": "error", "error": "not_found"}, status=404)

    if not file_id:
        return web.json_response({"status": "error", "error": "photo_not_found"}, status=404)

    # Прокси через бота: токен остаётся внутри python-telegram-bot и не попадает
    # ни в URL, которые мы собираем, ни в логи. Исключение логируем только по типу.
    bot = _get_bot(request)
    if bot is None:
        return web.json_response({"status": "error", "error": "bot_unavailable"}, status=503)

    try:
        tg_file = await bot.get_file(file_id)
        content = bytes(await tg_file.download_as_bytearray())
    except Exception as exc:
        logger.warning("transfers: failed to proxy photo for transfer %s: %s", transfer_id, type(exc).__name__)
        return web.json_response({"status": "error", "error": "failed_to_load_photo"}, status=502)

    # private: фото видны только авторизованным, общий кеш (туннель, CDN) их держать не должен.
    return web.Response(body=content, content_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


# ─── POST Endpoints ───────────────────────────────────────────────────────────

async def handle_post_deal(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        fields, photo_bytes = await _read_request_payload(request)
    except web.HTTPBadRequest:
        raise
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)

    try:
        deal = req_mod.create_deal(
            user_id,
            role=fields.get("role", ""),
            other_club=fields.get("other_club", ""),
            player=fields.get("player", ""),
            price=fields.get("price"),
            ovr=fields.get("ovr"),
        )
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: create_deal failed")
        return web.json_response({"status": "error", "message": "Не удалось создать заявку"}, status=500)

    # Уведомление второй стороне
    bot = _get_bot(request)
    if bot:
        try:
            await notify.notify_deal_proposal(bot, deal, photo_bytes=photo_bytes)
        except Exception:
            logger.exception("transfers: notify_deal_proposal error")

    return web.json_response({
        "status": "ok",
        "transfer": req_mod.serialize(deal, user_id, private=True),
    })


async def handle_post_surcharge(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        fields, photo_bytes = await _read_request_payload(request)
    except web.HTTPBadRequest:
        raise
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)

    try:
        transfer = req_mod.create_surcharge(
            user_id,
            player=fields.get("player", ""),
            ovr=fields.get("ovr"),
        )
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: create_surcharge failed")
        return web.json_response({"status": "error", "message": "Не удалось создать заявку"}, status=500)

    bot = _get_bot(request)
    if bot:
        try:
            await notify.post_request_card(bot, transfer, photo_bytes=photo_bytes)
        except Exception:
            logger.exception("transfers: post_request_card error for surcharge")

    return web.json_response({
        "status": "ok",
        "transfer": req_mod.serialize(transfer, user_id, private=True),
    })


async def handle_post_urn_sale(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        fields, photo_bytes = await _read_request_payload(request)
    except web.HTTPBadRequest:
        raise
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)

    sellable_val = fields.get("sellable", True)
    if isinstance(sellable_val, str):
        sellable = sellable_val.lower() not in ("false", "0", "no")
    else:
        sellable = bool(sellable_val)

    try:
        transfer = req_mod.create_urn_sale(
            user_id,
            player=fields.get("player", ""),
            tm_price=fields.get("tm_price"),
            special_price=fields.get("special_price"),
            sellable=sellable,
        )
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: create_urn_sale failed")
        return web.json_response({"status": "error", "message": "Не удалось создать заявку"}, status=500)

    bot = _get_bot(request)
    if bot:
        try:
            await notify.post_request_card(bot, transfer, photo_bytes=photo_bytes)
        except Exception:
            logger.exception("transfers: post_request_card error for urn_sale")

    return web.json_response({
        "status": "ok",
        "transfer": req_mod.serialize(transfer, user_id, private=True),
    })


async def handle_post_urn_buy(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]

    try:
        fields, _ = await _read_request_payload(request)
    except web.HTTPBadRequest:
        raise

    urn_item_id = fields.get("urn_item_id")
    if not urn_item_id:
        return web.json_response({"status": "error", "message": "Укажите карту из урны для выкупа."}, status=400)

    try:
        transfer = req_mod.create_urn_buy(user_id, urn_item_id=urn_item_id)
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: create_urn_buy failed")
        return web.json_response({"status": "error", "message": "Не удалось выкупить карту"}, status=500)

    bot = _get_bot(request)
    if bot:
        try:
            await notify.post_request_card(bot, transfer)
        except Exception:
            logger.exception("transfers: post_request_card error for urn_buy")

    return web.json_response({
        "status": "ok",
        "transfer": req_mod.serialize(transfer, user_id, private=True),
    })


async def handle_post_confirm(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]
    transfer_id = request.match_info.get("id")

    try:
        transfer = req_mod.confirm(user_id, transfer_id)
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: confirm failed")
        return web.json_response({"status": "error", "message": "Не удалось подтвердить сделку"}, status=500)

    bot = _get_bot(request)
    if bot:
        try:
            await notify.notify_deal_confirmed(bot, transfer)
        except Exception:
            logger.exception("transfers: notify_deal_confirmed error")

    return web.json_response({
        "status": "ok",
        "transfer": req_mod.serialize(transfer, user_id, private=True),
    })


async def handle_post_decline(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]
    transfer_id = request.match_info.get("id")

    try:
        transfer = req_mod.decline(user_id, transfer_id)
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: decline failed")
        return web.json_response({"status": "error", "message": "Не удалось отклонить сделку"}, status=500)

    bot = _get_bot(request)
    if bot:
        try:
            await notify.notify_deal_declined(bot, transfer)
        except Exception:
            logger.exception("transfers: notify_deal_declined error")

    return web.json_response({
        "status": "ok",
        "transfer": req_mod.serialize(transfer, user_id, private=True),
    })


async def handle_post_withdraw(request: web.Request) -> web.Response:
    user_info, err = _auth(request)
    if err is not None:
        return err
    user_id = user_info["id"]
    transfer_id = request.match_info.get("id")

    try:
        transfer = req_mod.withdraw(user_id, transfer_id)
    except service.InputError as exc:
        return web.json_response({"status": "error", "message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("transfers: withdraw failed")
        return web.json_response({"status": "error", "message": "Не удалось отозвать заявку"}, status=500)

    bot = _get_bot(request)
    if bot:
        try:
            await notify.notify_request_withdrawn(bot, transfer)
        except Exception:
            logger.exception("transfers: notify_request_withdrawn error")

    return web.json_response({
        "status": "ok",
        "transfer": req_mod.serialize(transfer, user_id, private=True),
    })


def register_routes(app: web.Application) -> None:
    """Регистрация всех HTTP-маршрутов трансферного окна в aiohttp."""
    app.router.add_get("/api/transfers/status", handle_get_status)
    app.router.add_get("/api/transfers/history", handle_get_history)
    app.router.add_get("/api/transfers/urn", handle_get_urn)
    app.router.add_get("/api/transfers/{id}/photo", handle_get_photo)

    app.router.add_post("/api/transfers/deal", handle_post_deal)
    app.router.add_post("/api/transfers/surcharge", handle_post_surcharge)
    app.router.add_post("/api/transfers/urn/sale", handle_post_urn_sale)
    app.router.add_post("/api/transfers/urn/buy", handle_post_urn_buy)
    app.router.add_post("/api/transfers/{id}/confirm", handle_post_confirm)
    app.router.add_post("/api/transfers/{id}/decline", handle_post_decline)
    app.router.add_post("/api/transfers/{id}/withdraw", handle_post_withdraw)
