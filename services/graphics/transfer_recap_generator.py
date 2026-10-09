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


import os

@dataclass(frozen=True)
class RecapDeal:
    """Строка топа: игрок, маршрут одной строкой, сумма текстом и путь к портрету."""
    player: str
    route: str
    price_text: str
    portrait_path: str | None = None


def _paste_mini_avatar(
    img: Image.Image,
    photo_path: str | None,
    player_name: str,
    size: int,
    x: int,
    y: int,
    accent: tuple,
) -> None:
    """Круглый мини-портрет игрока или стильная монограмма, если файла нет."""
    draw_base = ImageDraw.Draw(img)
    p_img = None
    resolved = photo_path

    if not resolved or not os.path.isfile(resolved):
        try:
            from services.graphics import player_photos
            cached = player_photos.get_cached_photo_path(player_name, None)
            if cached and os.path.isfile(cached):
                resolved = cached
        except Exception:
            pass

    if resolved and os.path.isfile(resolved) and os.path.getsize(resolved) > 0:
        try:
            with Image.open(resolved) as raw:
                raw_rgba = raw.convert("RGBA")
                ratio = max(size / raw_rgba.width, size / raw_rgba.height)
                new_w = max(size, int(raw_rgba.width * ratio))
                new_h = max(size, int(raw_rgba.height * ratio))
                scaled = raw_rgba.resize((new_w, new_h), Image.Resampling.LANCZOS)
                off_x = (new_w - size) // 2
                off_y = (new_h - size) // 2
                cropped = scaled.crop((off_x, off_y, off_x + size, off_y + size))

                mask = Image.new("L", (size, size), 0)
                draw_m = ImageDraw.Draw(mask)
                draw_m.ellipse((0, 0, size - 1, size - 1), fill=255)

                p_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                p_img.paste(cropped, (0, 0), mask)
        except Exception:
            p_img = None

    if p_img is not None:
        img.alpha_composite(p_img, (x, y))
        draw_base.ellipse((x, y, x + size - 1, y + size - 1), outline=WHITE + (160,), width=3)
    else:
        draw_base.ellipse((x, y, x + size - 1, y + size - 1), fill=(232, 228, 220))
        draw_base.ellipse((x, y, x + size - 1, y + size - 1), outline=(206, 202, 192), width=2)
        parts = (player_name or "").strip().split()
        initials = (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else ""))[:2].upper()
        if not initials:
            initials = "★"
        f_mono = _font(int(size * 0.42))
        draw_base.text((x + size / 2, y + size / 2), initials, fill=INK, font=f_mono, anchor="mm")


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

        # Порядковый номер
        draw.text((LIST_X0 + 8, mid), str(i + 1), fill=accent if i == 0 else MUTED, font=_font(56), anchor="lm")

        # Мини-аватар игрока (портрет или монограмма)
        av_size = 72
        av_x = LIST_X0 + 56
        av_y = mid - av_size // 2
        _paste_mini_avatar(img, deal.portrait_path, deal.player, av_size, av_x, av_y, accent)

        # Текстовые подписи
        text_x = av_x + av_size + 16
        sum_text, sum_font = _fit(draw, deal.price_text, 210, 44, 22)
        sum_w = int(draw.textlength(sum_text, font=sum_font))
        available_w = (LIST_X1 - 10) - text_x - sum_w - 20

        text, font = _fit(draw, (deal.player or "—").upper(), available_w, 36, 22)
        draw.text((text_x, mid - 16), text, fill=INK, font=font, anchor="lm")

        route, route_font = _fit(draw, deal.route, available_w, 22, 16)
        draw.text((text_x, mid + 24), route, fill=MUTED, font=route_font, anchor="lm")

        draw.text((LIST_X1 - 4, mid), sum_text, fill=INK, font=sum_font, anchor="rm")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
