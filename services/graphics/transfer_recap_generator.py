"""Итоговая карточка трансферного окна: оборот, топ-5 сделок, самый активный клуб.

Тот же светлый «журнальный» стиль и шрифты, что у карточки трансфера, поэтому рисует через её
помощники. Чистый рендерер: получает готовые строки и числа, о БД и `transfers/` ничего не знает.
Синхронный (Pillow) — из асинхронного кода его зовут через `asyncio.to_thread`. Нет логотипа —
пустое место, не ошибка.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw

from services.graphics.transfer_card_generator import (
    HEIGHT,
    INK,
    MUTED,
    PAPER,
    WIDTH,
    WHITE,
    _font,
    _fit,
    _paste_logo,
    accent_color,
)

TOP_LIMIT = 5
LIST_X0, LIST_X1 = 700, 1376           # правая колонка: топ сделок
ROW_Y0, ROW_H = 190, 116


@dataclass(frozen=True)
class RecapDeal:
    """Строка топа: игрок, маршрут одной строкой и сумма уже текстом."""
    player: str
    route: str
    price_text: str


def render_window_recap(
    *,
    title: str,
    turnover_text: str,
    deals: list[RecapDeal],
    requests_count: int = 0,
    swaps: int = 0,
    urn_sales: int = 0,
    top_club: str | None = None,
    top_club_count: int = 0,
) -> bytes:
    """PNG 1440×810. Пустой топ даёт карточку с одной пометкой «сделок не было»."""
    accent = accent_color(None, top_club, None)
    img = Image.new("RGBA", (WIDTH, HEIGHT), PAPER + (255,))
    draw = ImageDraw.Draw(img)

    # Шапка.
    draw.text((64, 58), "ИТОГИ ОКНА", fill=INK, font=_font(30))
    head_w = int(draw.textlength("ИТОГИ ОКНА", font=_font(30)))
    draw.line([(64, 104), (64 + max(head_w, 226), 104)], fill=INK, width=4)
    name, name_font = _fit(draw, (title or "ТРАНСФЕРНОЕ ОКНО").upper(), 560, 40, 22)
    draw.text((64, 122), name, fill=MUTED, font=name_font)

    # Оборот.
    draw.text((64, 250), "ОБОРОТ ОКНА", fill=MUTED, font=_font(26))
    price, font_price = _fit(draw, turnover_text, 590, 150, 60)
    draw.text((60, 400), price, fill=INK, font=font_price, anchor="ls")
    price_w = int(draw.textlength(price, font=font_price))
    draw.rectangle((64, 436, 64 + max(price_w, 200), 446), fill=accent)

    # Счётчики.
    stats = [("заявок одобрено", requests_count), ("обменов", swaps), ("продаж в урну", urn_sales)]
    x = 64
    for label, value in stats:
        draw.text((x, 520), str(value), fill=INK, font=_font(72))
        draw.text((x, 596), label.upper(), fill=MUTED, font=_font(20))
        x += 210

    # Самый активный клуб.
    if top_club:
        draw.text((64, 660), "САМЫЙ АКТИВНЫЙ КЛУБ", fill=MUTED, font=_font(22))
        box = 84
        drew = _paste_logo(img, top_club, box, 64, 692)
        draw = ImageDraw.Draw(img)
        text, font = _fit(draw, top_club, 380, 42, 22)
        tx = 64 + (box + 18 if drew else 0)
        draw.text((tx, 692 + box / 2 - 12), text, fill=INK, font=font, anchor="lm")
        draw.text((tx, 692 + box / 2 + 22), f"ЗАЯВОК: {top_club_count}", fill=MUTED, font=_font(22), anchor="lm")

    # Топ сделок: чёрная плашка-заголовок и пять строк.
    draw.rounded_rectangle((LIST_X0, 64, LIST_X1, 138), radius=18, fill=INK)
    draw.text((LIST_X0 + 28, 101), f"ТОП-{TOP_LIMIT} СДЕЛОК ПО СУММЕ", fill=WHITE, font=_font(30), anchor="lm")
    shown = deals[:TOP_LIMIT]
    if not shown:
        draw.text(((LIST_X0 + LIST_X1) // 2, 420), "СДЕЛОК НЕ БЫЛО", fill=MUTED, font=_font(48), anchor="mm")
    for i, deal in enumerate(shown):
        top = ROW_Y0 + i * ROW_H
        mid = top + ROW_H // 2
        if i:
            draw.line([(LIST_X0, top), (LIST_X1, top)], fill=(214, 210, 198), width=2)
        draw.text((LIST_X0 + 8, mid), str(i + 1), fill=accent if i == 0 else MUTED, font=_font(64), anchor="lm")
        text, font = _fit(draw, (deal.player or "—").upper(), 360, 46, 24)
        draw.text((LIST_X0 + 78, mid - 18), text, fill=INK, font=font, anchor="lm")
        route, route_font = _fit(draw, deal.route, 360, 24, 16)
        draw.text((LIST_X0 + 78, mid + 26), route, fill=MUTED, font=route_font, anchor="lm")
        sum_text, sum_font = _fit(draw, deal.price_text, 210, 44, 22)
        draw.text((LIST_X1 - 4, mid), sum_text, fill=INK, font=sum_font, anchor="rm")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
