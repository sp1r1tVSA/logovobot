"""Карточка одобренного трансфера для ленты окна: портрет, маршрут клубов, цена и OVR.

Светлый «журнальный» макет: огромное имя слева, вырезанный портрет в круге справа, круг
окрашен в цвет клуба-получателя (берётся из его логотипа). Чистый рендерер: получает готовые
данные и путь к портрету, ничего не знает о БД и о `transfers/`. Синхронный (Pillow) — из
асинхронного кода его зовут через `asyncio.to_thread`. Нет портрета — монограмма, нет логотипа —
пустое место; сами по себе они не ошибка.
"""

from __future__ import annotations

import colorsys
import io
import functools
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

_resolved = Path(__file__).resolve()
LOGOS_DIR = str(_resolved.parents[2] / "assets" / "logos")
for _p in _resolved.parents:
    _candidate = _p / "assets" / "logos"
    if _candidate.is_dir() and any(_candidate.glob("*.png")):
        LOGOS_DIR = str(_candidate)
        break
DISPLAY_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "LiberationSansNarrow-Bold.ttf")

SCALE = 2
W1, H1 = 720, 405
WIDTH, HEIGHT = W1 * SCALE, H1 * SCALE          # рисуем сразу в итоговых пикселях

PAPER = (244, 241, 232)
INK = (16, 16, 20)
MUTED = (110, 110, 118)
WHITE = (255, 255, 255)
DEFAULT_ACCENT = (217, 142, 12)                  # когда нет ни одного логотипа

# Круг с портретом.
CIRCLE_CX, CIRCLE_CY, CIRCLE_R = 1050, 440, 340
PORTRAIT_H = 800

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


def _dominant_color(logo: Image.Image) -> tuple[int, int, int] | None:
    """Самый «цветной» оттенок логотипа; белое, серое и почти чёрное не считаются."""
    small = logo.convert("RGBA").resize((48, 48), Image.Resampling.BILINEAR)
    data = small.tobytes()
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for i in range(0, len(data), 4):
        r, g, b, a = data[i:i + 4]
        if a < 200:
            continue
        _, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if val < 0.18 or sat < 0.25 and val > 0.8:
            continue
        cell = buckets.setdefault((r // 32, g // 32, b // 32), [0, 0, 0, 0])
        cell[0] += 1
        cell[1] += r
        cell[2] += g
        cell[3] += b
    if not buckets:
        return None

    def weight(cell):
        n, r, g, b = cell
        return n * (0.3 + colorsys.rgb_to_hsv(r / n / 255, g / n / 255, b / n / 255)[1])

    n, r, g, b = max(buckets.values(), key=weight)
    return r // n, g // n, b // n


def _readable(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """Оттенок не светлее фона-бумаги, иначе круг сливается с ним."""
    lum = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    if lum <= 190:
        return color
    k = 190 / lum
    return tuple(int(c * k) for c in color)


def accent_color(kind: str | None, from_club: str | None, to_club: str | None) -> tuple[int, int, int]:
    """Цвет круга: клуб-получатель, а нет его (продажа в урну) — клуб-отправитель."""
    for club in (to_club, from_club):
        if not club:
            continue
        logo = _load_logo(club, 96)
        color = _dominant_color(logo) if logo is not None else None
        if color:
            return _readable(color)
    return DEFAULT_ACCENT


def _paste_logo(img: Image.Image, club: str, box: int, x: int, y: int) -> bool:
    logo = _load_logo(club, box)
    if logo is None:
        return False
    img.alpha_composite(logo.convert("RGBA"), (x + (box - logo.width) // 2, y + (box - logo.height) // 2))
    return True


def _draw_portrait(img: Image.Image, path: str | None, name: str, accent) -> None:
    """Вырезанный портрет стоит на низу карточки; фото с фоном — кадрируется в круг; нет фото — монограмма."""
    photo = None
    if path:
        resolved_path = path
        if not os.path.isabs(resolved_path) and not os.path.exists(resolved_path):
            for _p in _resolved.parents:
                cand = _p / resolved_path
                if cand.is_file():
                    resolved_path = str(cand)
                    break
        try:
            with Image.open(resolved_path) as raw:
                photo = raw.convert("RGBA")
        except Exception:
            logger.debug("transfer card: portrait %s unreadable", path, exc_info=True)
    if photo is None:
        letters = "".join(part[0] for part in name.split()[:2]).upper() or "?"
        ImageDraw.Draw(img).text((CIRCLE_CX, CIRCLE_CY), letters, fill=WHITE, font=_font(260), anchor="mm")
        return
    if photo.getchannel("A").getextrema()[0] >= 250:           # без прозрачности — фон не вырезать
        d = CIRCLE_R * 2 - 56
        tile = ImageOps.fit(photo, (d, d), Image.Resampling.LANCZOS, centering=(0.5, 0.25))
        mask = Image.new("L", (d, d), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
        img.paste(tile, (CIRCLE_CX - d // 2, CIRCLE_CY - d // 2), mask)
        return
    height = PORTRAIT_H
    width = max(1, int(photo.width * height / photo.height))
    photo = photo.resize((width, height), Image.Resampling.LANCZOS)
    x, y = CIRCLE_CX - width // 2, HEIGHT - height
    # alpha_composite не принимает выход за край — подрезаем сами.
    left, right = max(0, -x), min(width, WIDTH - x)
    if right > left:
        img.alpha_composite(photo.crop((left, 0, right, height)), (max(x, 0), y))


def _draw_card(img: Image.Image, path: str | None, name: str, accent: tuple[int, int, int]) -> bool:
    """Оригинальная карточка FC Mobile/Renderz по центру справа с улучшением резкости и мягкой тенью."""
    card = None
    if path:
        resolved_path = path
        if not os.path.isabs(resolved_path) and not os.path.exists(resolved_path):
            for _p in _resolved.parents:
                cand = _p / resolved_path
                if cand.is_file():
                    resolved_path = str(cand)
                    break
        try:
            with Image.open(resolved_path) as raw:
                card = raw.convert("RGBA")
        except Exception:
            logger.debug("transfer card: FC card %s unreadable", path, exc_info=True)

    if card is None:
        return False

    # 1. Тщательная очистка фона скриншота RenderZ (17, 17, 34)
    try:
        # Заливка по всему внешнему периметру для устранения неровностей
        for x in range(0, card.width, 2):
            ImageDraw.floodfill(card, (x, 0), (0, 0, 0, 0), thresh=35)
            ImageDraw.floodfill(card, (x, card.height - 1), (0, 0, 0, 0), thresh=35)
        for y in range(0, card.height, 2):
            ImageDraw.floodfill(card, (0, y), (0, 0, 0, 0), thresh=35)
            ImageDraw.floodfill(card, (card.width - 1, y), (0, 0, 0, 0), thresh=35)

        # Удаление застрявших теневых островков в крайних 20% ширины
        import numpy as np
        arr = np.array(card)
        bg = np.array([17, 17, 34])
        diffs = np.sum(np.abs(arr[:, :, :3] - bg), axis=2)
        margin_w = int(card.width * 0.20)
        outer_mask = (arr[:, :, 3] == 255) & (diffs < 45) & (
            (np.arange(card.width) < margin_w) | (np.arange(card.width) > (card.width - margin_w))
        )
        if np.any(outer_mask):
            arr[outer_mask, 3] = 0
            card = Image.fromarray(arr)
    except Exception:
        pass

    bbox = card.getbbox()
    if bbox:
        card = card.crop(bbox)

    target_h = 710
    target_w = max(1, int(card.width * (target_h / card.height)))

    # 2. Улучшение чёткости и сглаживание контура при увеличении
    try:
        from PIL import ImageEnhance, ImageFilter
        rgb = card.convert("RGB")
        alpha = card.getchannel("A")

        # Масштабирование с фильтром Lanczos
        rgb_hi = rgb.resize((target_w, target_h), Image.Resampling.LANCZOS)
        # Фильтр повышения резкости текста, лица и деталей золотой рамки
        rgb_hi = rgb_hi.filter(ImageFilter.UnsharpMask(radius=2.2, percent=170, threshold=1))
        # Микро-контраст и насыщенность для сочной картинки
        rgb_hi = ImageEnhance.Contrast(rgb_hi).enhance(1.08)
        rgb_hi = ImageEnhance.Color(rgb_hi).enhance(1.05)

        # Сглаживание альфа-маски (убирает пиксельные лесенки по контуру)
        alpha_hi = alpha.resize((target_w, target_h), Image.Resampling.LANCZOS)
        alpha_hi = alpha_hi.filter(ImageFilter.GaussianBlur(0.7))

        card_hi = rgb_hi.convert("RGBA")
        card_hi.putalpha(alpha_hi)
    except Exception:
        card_hi = card.resize((target_w, target_h), Image.Resampling.LANCZOS)

    card_x = CIRCLE_CX - target_w // 2
    card_y = (HEIGHT - target_h) // 2

    # 3. Мягкая объемная тень под карточкой
    try:
        from PIL import ImageFilter
        shadow_mask = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        shadow_layer = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 110))
        shadow_mask.paste(shadow_layer, (card_x + 12, card_y + 22), mask=card_hi.getchannel("A"))
        shadow_blurred = shadow_mask.filter(ImageFilter.GaussianBlur(22))
        img.paste(shadow_blurred, (0, 0), shadow_blurred)
    except Exception:
        pass

    img.alpha_composite(card_hi, (card_x, card_y))
    return True


def render_transfer_card(
    *,
    kind: str | None,
    player_name: str,
    price_text: str,
    ovr: int | None = None,
    from_club: str | None = None,
    to_club: str | None = None,
    portrait_path: str | None = None,
    card_path: str | None = None,
    transfer_id: int | None = None,
) -> bytes:
    """PNG-карточка трансфера. Если передан `card_path` — рисует полную карточку FC Mobile без фонового круга."""
    accent = accent_color(kind, from_club, to_club)
    img = Image.new("RGBA", (WIDTH, HEIGHT), PAPER + (255,))
    draw = ImageDraw.Draw(img)

    drew_card = False
    has_card = bool(card_path and (os.path.isfile(card_path) or not os.path.isabs(card_path)))
    if has_card:
        drew_card = _draw_card(img, card_path, player_name or "", accent)

    if not drew_card:
        # Круг цвета клуба с тонким белым кантом (только для портретного режима).
        cx, cy, r = CIRCLE_CX, CIRCLE_CY, CIRCLE_R
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=accent)
        draw.ellipse((cx - r + 26, cy - r + 26, cx + r - 26, cy + r - 26), outline=WHITE + (90,), width=4)
        _draw_portrait(img, portrait_path, player_name or "", accent)

    draw = ImageDraw.Draw(img)

    # Шапка.
    headline = HEADLINES.get(kind or "", "ТРАНСФЕР")
    draw.text((64, 58), headline, fill=INK, font=_font(30))
    head_w = int(draw.textlength(headline, font=_font(30)))
    draw.line([(64, 104), (64 + max(head_w, 226), 104)], fill=INK, width=4)
    sub = "ТРАНСФЕРНОЕ ОКНО" + (f" · ЗАЯВКА #{transfer_id}" if transfer_id else "")
    draw.text((64, 122), sub, fill=MUTED, font=_font(24))

    # Имя — огромное, по базовой линии, чтобы любая длина стояла ровно.
    name, font_name = _fit(draw, (player_name or "—").upper(), 650, 230, 72)
    draw.text((56, 380), name, fill=INK, font=font_name, anchor="ls")

    # Цена.
    draw.text((64, 440), PRICE_CAPTIONS.get(kind or "", "СУММА"), fill=MUTED, font=_font(26))
    price, font_price = _fit(draw, price_text, 620, 130, 60)
    draw.text((60, 572), price, fill=INK, font=font_price, anchor="ls")
    price_w = int(draw.textlength(price, font=font_price))
    draw.rectangle((64, 624, 64 + max(price_w, 200), 634), fill=accent)

    # OVR (в портретном режиме выносим в правый угол; на карточке OVR уже нарисован крупно).
    if ovr and not has_card:
        draw.ellipse((1260, 70, 1380, 190), fill=INK)
        draw.text((1320, 118), str(ovr), fill=WHITE, font=_font(62), anchor="mm")
        draw.text((1320, 164), "OVR", fill=(190, 190, 200), font=_font(22), anchor="mm")

    # Маршрут клубов: значок + название, стрелка между ними.
    left, right = route_sides(kind, from_club, to_club)
    box, gap, label_w, y0 = 96, 16, 170, 676

    def label(side: Side) -> str:
        return {"urn": "", "none": "—"}.get(side.kind, side.name)

    def place(side: Side, x: int) -> int:
        """Рисует сторону с левого края `x`, возвращает правый край."""
        drew_logo = False
        if side.kind == "club":
            drew_logo = _paste_logo(img, side.name, box, x, y0)
        elif side.kind == "urn":
            draw.rounded_rectangle((x, y0, x + box, y0 + box), radius=box // 5, fill=INK)
            draw.text((x + box / 2, y0 + box / 2), "УРНА", fill=WHITE, font=_font(30), anchor="mm")
            drew_logo = True
        tx = x + (box + gap if drew_logo else 0)
        if not label(side):
            return x + box
        text, font = _fit(draw, label(side), label_w, 40, 22)
        draw.text((tx, y0 + box / 2), text, fill=INK, font=font, anchor="lm")
        return tx + int(draw.textlength(text, font=font))

    end = place(left, 64)
    if right is not None:
        mid = end + 40
        draw.text((mid, y0 + box / 2), "→", fill=INK, font=_font(54), anchor="mm")
        place(right, mid + 40)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
