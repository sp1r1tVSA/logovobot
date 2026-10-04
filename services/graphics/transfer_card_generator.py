"""Карточка одобренного трансфера для ленты окна: портрет, маршрут клубов, цена и OVR.

Чистый рендерер: получает готовые данные и путь к портрету, ничего не знает о БД и о
`transfers/`. Синхронный (Pillow) — из асинхронного кода его зовут через `asyncio.to_thread`.
Нет портрета — монограмма, нет логотипа — пустой значок; сами по себе они не ошибка.
"""

from __future__ import annotations

import functools
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from services.graphics.table_generator import (
    clean_and_prepare_logo,
    get_team_logo_filename,
    load_font,
    resize_logo_proportional,
)

logger = logging.getLogger(__name__)

LOGOS_DIR = str(Path(__file__).resolve().parents[2] / "assets" / "logos")
DISPLAY_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "LiberationSansNarrow-Bold.ttf")

SCALE = 2
W1, H1 = 720, 405
WIDTH, HEIGHT = W1 * SCALE, H1 * SCALE

BG_TOP = (11, 13, 20)
BG_BOT = (22, 26, 38)
SURFACE = (26, 30, 42)
BORDER = (52, 60, 82)
GOLD = (251, 191, 36)
WHITE = (255, 255, 255)
MUTED = (140, 154, 176)
TEXT = (214, 222, 235)
GREEN = (34, 197, 94)
CYAN = (56, 189, 248)

# Шапка по виду заявки. Здесь только то, что рисуется на картинке.
HEADLINES = {
    "deal": "HERE WE GO",
    "free_agent": "HERE WE GO",
    "urn_buy": "HERE WE GO",
    "surcharge": "СПЕЦКАРТА",
    "urn_sale": "В УРНУ",
}
PRICE_CAPTIONS = {
    "deal": "СУММА СДЕЛКИ",
    "free_agent": "СУММА",
    "urn_buy": "ВЫКУП ИЗ УРНЫ",
    "surcharge": "ДОПЛАТА",
    "urn_sale": "ВЫПЛАТА ИЗ УРНЫ",
}


@dataclass(frozen=True)
class Side:
    """Одна сторона маршрута: клуб, «урна» или пусто."""
    kind: str                  # "club" | "urn" | "none"
    name: str = ""


@functools.lru_cache(maxsize=64)
def _font(size: int):
    for name in (DISPLAY_FONT_PATH, "LiberationSansNarrow-Bold.ttf", "DejaVuSansCondensed-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return load_font(size, bold=True)


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, size: int, min_size: int):
    """Самый крупный шрифт ≤ `size`, в который текст влезает; на минимуме обрезаем с «…»."""
    while size > min_size and draw.textlength(text, font=_font(size)) > max_w:
        size -= 2
    font = _font(size)
    if draw.textlength(text, font=font) > max_w:
        while len(text) > 1 and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, font


def route_sides(kind: str | None, from_club: str | None, to_club: str | None) -> tuple[Side, Side | None]:
    """(слева, справа) маршрута. Доплата — одна сторона, справа `None`."""
    club = lambda name: Side("club", name) if name else Side("none")
    if kind == "urn_sale":
        return club(from_club), Side("urn")
    if kind == "urn_buy":
        return Side("urn"), club(to_club)
    if kind == "surcharge":
        return club(to_club), None
    return club(from_club), club(to_club)


def _gradient(img: Image.Image) -> None:
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        draw.line([(0, y), (WIDTH, y)],
                  fill=tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)) + (255,))


def _load_logo(club: str, box: int) -> Image.Image | None:
    filename = get_team_logo_filename(club)
    path = os.path.join(LOGOS_DIR, filename) if filename else None
    if not path or not os.path.exists(path):
        return None
    try:
        with Image.open(path) as raw:
            logo, _, _ = resize_logo_proportional(clean_and_prepare_logo(raw), box, box)
        return logo
    except Exception:
        logger.debug("transfer card: logo %s unreadable", path, exc_info=True)
        return None


def _draw_logo_tile(img: Image.Image, draw: ImageDraw.ImageDraw, side: Side, x: int, y: int, size: int) -> None:
    """Плитка `size`×`size` со значком клуба; нет файла — пустая плитка, урна — подпись."""
    draw.rounded_rectangle((x, y, x + size, y + size), radius=size // 5, fill=SURFACE, outline=BORDER, width=SCALE)
    if side.kind == "urn":
        font = _font(size // 3)
        draw.text((x + size / 2, y + size / 2), "УРНА", fill=MUTED, font=font, anchor="mm")
        return
    if side.kind != "club":
        return
    pad = size // 7
    logo = _load_logo(side.name, size - pad * 2)
    if logo is not None:
        img.paste(logo, (x + (size - logo.width) // 2, y + (size - logo.height) // 2), logo)


def _draw_portrait(img: Image.Image, draw: ImageDraw.ImageDraw, path: str | None, name: str,
                   box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    radius = 18 * SCALE
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    tile = None
    if path:
        try:
            with Image.open(path) as raw:
                photo = raw.convert("RGBA")
            backdrop = Image.new("RGBA", photo.size, SURFACE + (255,))
            backdrop.alpha_composite(photo)
            tile = ImageOps.fit(backdrop.convert("RGB"), (w, h), Image.Resampling.LANCZOS, centering=(0.5, 0.2))
        except Exception:
            logger.debug("transfer card: portrait %s unreadable", path, exc_info=True)
            tile = None
    if tile is None:
        tile = Image.new("RGB", (w, h), SURFACE)
        letters = "".join(part[0] for part in name.split()[:2]).upper() or "?"
        ImageDraw.Draw(tile).text((w / 2, h / 2), letters, fill=BORDER, font=_font(88 * SCALE), anchor="mm")
    img.paste(tile, (x0, y0), mask)
    draw.rounded_rectangle(box, radius=radius, outline=GOLD, width=2 * SCALE)


def _chip(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, fill, ink) -> int:
    font = _font(15 * SCALE)
    pad = 10 * SCALE
    w = int(draw.textlength(text, font=font)) + pad * 2
    h = 26 * SCALE
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=fill)
    draw.text((x + w / 2, y + h / 2), text, fill=ink, font=font, anchor="mm")
    return x + w + 8 * SCALE


def render_transfer_card(
    *,
    kind: str | None,
    player_name: str,
    price_text: str,
    ovr: int | None = None,
    from_club: str | None = None,
    to_club: str | None = None,
    portrait_path: str | None = None,
    transfer_id: int | None = None,
) -> bytes:
    """PNG-карточка трансфера. Ничего из окружения не требует: без файлов рисует заглушки."""
    s = SCALE
    img = Image.new("RGBA", (WIDTH, HEIGHT), BG_TOP)
    _gradient(img)
    draw = ImageDraw.Draw(img)

    # Золотая полоса слева и мягкое свечение за заголовком.
    draw.rectangle((0, 0, 6 * s, HEIGHT), fill=GOLD)
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-120 * s, -160 * s, 320 * s, 120 * s), fill=GOLD + (34,))
    img.alpha_composite(glow)
    draw = ImageDraw.Draw(img)

    # Шапка: бейдж и номер заявки.
    headline = HEADLINES.get(kind or "", "ТРАНСФЕР")
    font_head = _font(20 * s)
    head_w = int(draw.textlength(headline, font=font_head)) + 28 * s
    draw.rounded_rectangle((32 * s, 24 * s, 32 * s + head_w, 56 * s), radius=16 * s, fill=GOLD)
    draw.text((32 * s + head_w / 2, 40 * s), headline, fill=(20, 16, 4), font=font_head, anchor="mm")
    if transfer_id:
        draw.text((688 * s, 40 * s), f"ТО · #{transfer_id}", fill=MUTED, font=_font(16 * s), anchor="rm")

    # Портрет.
    _draw_portrait(img, draw, portrait_path, player_name, (32 * s, 78 * s, 212 * s, 288 * s))

    # Правая колонка: имя, чипы, цена.
    col_x, col_w = 240 * s, 448 * s
    name, font_name = _fit(draw, player_name or "—", col_w, 44 * s, 24 * s)
    draw.text((col_x, 84 * s), name, fill=WHITE, font=font_name)
    chip_x = col_x
    if ovr:
        chip_x = _chip(draw, chip_x, 142 * s, f"OVR {ovr}", GREEN, (6, 30, 14))
    _chip(draw, chip_x, 142 * s, {
        "deal": "СДЕЛКА", "free_agent": "СВОБОДНЫЙ АГЕНТ", "urn_buy": "ПОКУПКА ИЗ УРНЫ",
        "surcharge": "ДОПЛАТА", "urn_sale": "ПРОДАЖА В УРНУ",
    }.get(kind or "", "ТРАНСФЕР"), SURFACE, TEXT)
    draw.text((col_x, 192 * s), PRICE_CAPTIONS.get(kind or "", "СУММА"), fill=MUTED, font=_font(14 * s))
    price, font_price = _fit(draw, price_text, col_w, 58 * s, 30 * s)
    draw.text((col_x, 208 * s), price, fill=GOLD, font=font_price)

    # Маршрут клубов.
    top, bottom = 306 * s, 380 * s
    draw.rounded_rectangle((32 * s, top, 688 * s, bottom), radius=16 * s, fill=SURFACE, outline=BORDER, width=s)
    left, right = route_sides(kind, from_club, to_club)
    tile = 52 * s
    ty = top + (bottom - top - tile) // 2
    label_w = 200 * s

    def label(side: Side) -> str:
        return {"urn": "Урна", "none": "—"}.get(side.kind, side.name)

    _draw_logo_tile(img, draw, left, 46 * s, ty, tile)
    text, font = _fit(draw, label(left), label_w, 24 * s, 14 * s)
    draw.text((46 * s + tile + 12 * s, (top + bottom) / 2), text, fill=WHITE, font=font, anchor="lm")
    if right is not None:
        draw.text((360 * s, (top + bottom) / 2), "→", fill=GOLD, font=_font(40 * s), anchor="mm")
        _draw_logo_tile(img, draw, right, 674 * s - tile, ty, tile)
        text, font = _fit(draw, label(right), label_w, 24 * s, 14 * s)
        draw.text((674 * s - tile - 12 * s, (top + bottom) / 2), text, fill=WHITE, font=font, anchor="rm")

    # Подпись снизу.
    draw.text((360 * s, 393 * s), "ЛОГОВО ФИФАРЕЙ · ТРАНСФЕРНОЕ ОКНО", fill=BORDER, font=_font(12 * s), anchor="mm")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
