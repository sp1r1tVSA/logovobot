"""Карточка-картинка одобренного трансфера для ленты: сборка данных и рендер.

Сам рисунок — `services/graphics/transfer_card_generator.py`; здесь только перевод заявки в его
аргументы и безопасная обёртка. Портрет берётся из кэша, сети нет. Ничего не бросает наружу:
нет картинки — лента получит обычный текст.
"""

from __future__ import annotations

import asyncio
import logging

from transfers import requests as req_mod
from transfers.engine import format_k

logger = logging.getLogger(__name__)


def build_card(transfer: dict) -> bytes | None:
    """PNG-карточка заявки или `None`, если нарисовать не удалось. Блокирует — зовите в потоке."""
    try:
        from services.graphics.transfer_card_generator import render_transfer_card

        name = transfer.get("player_name") or ""
        return render_transfer_card(
            kind=transfer.get("kind"),
            player_name=name,
            price_text=format_k(transfer.get("price_k")),
            ovr=transfer.get("ovr"),
            from_club=transfer.get("from_club"),
            to_club=transfer.get("to_club"),
            portrait_path=req_mod.portrait_path(name, transfer.get("from_club"), transfer.get("to_club")),
            transfer_id=transfer.get("id"),
        )
    except Exception:
        logger.exception("transfers: card render failed for #%s", transfer.get("id"))
        return None


async def build_card_async(transfer: dict) -> bytes | None:
    return await asyncio.to_thread(build_card, transfer)
