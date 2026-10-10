"""
services/graphics/mid_season_generator.py

Постер «Символическая сборная 1-го круга» (Mid-Season Best XI & Bench):
  * Поле 4-3-3 с карточками 11 лучших футболистов первой половины сезона;
  * Капитан (MVP 1-го круга) в золотом Prime-тире;
  * Иконка ⭐ POTR (Игрок тура) на карточках обладателей награды;
  * Скамья запасных из 4 игроков (GK, DEF, MID, FWD);
  * Премиальная эстетика экватора сезона: 2x supersampling (2400×3300),
    бродкаст-палитра, золото, платина и акцент дивизиона.
"""

import functools
import io
import logging
import math
import os
import zlib
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from services.graphics import player_photos
from services.graphics.division_theme import draw_division_badge, resolve_theme
from services.graphics.round_digest_generator import _fit_text
from services.graphics.table_generator import (
    LOGOS_DIR,
    SCALE,
    clean_and_prepare_logo,
    get_team_logo_filename,
    load_font,
    resize_logo_proportional,
)

logger = logging.getLogger(__name__)

RGB = tuple[int, int, int]

WIDTH_1X = 1200
HEIGHT_1X = 1650

BG_COLOR = (14, 16, 22)
TEXT_PRIMARY = (255, 255, 255)
TEXT_MUTED = (209, 213, 219)
TEXT_HEADER = (156, 163, 175)
GOLD = (255, 215, 0)
GOLD_DEEP = (255, 165, 0)
INK = (14, 14, 18)
PITCH_DARK = (15, 34, 26)
PITCH_LIGHT = (19, 42, 32)
PITCH_LINE = (255, 215, 0, 55)

# Геометрия (1x)
HEADER_H = 170
PITCH_TOP = 185
PITCH_BOTTOM = 1365
PITCH_MARGIN = 30
CARD_W = 176
CARD_H = 244
BENCH_TOP = 1385
COMPACT_W = 0.82
COMPACT_H = 0.88

CARD_BLEED = 26
PHOTO_POP_OUT = 12

SLOT_CENTERS: dict[str, tuple[int, int]] = {
    "LW": (250, 345), "ST": (600, 315), "RW": (950, 345),
    "LCM": (330, 630), "CDM": (600, 690), "RCM": (870, 630),
    "LB": (160, 950), "LCB": (445, 975), "RCB": (755, 975), "RB": (1040, 950),
    "GK": (600, 1235),
}

DEFAULT_OVR = 75
PRIME_MIN_OVR = 92
STAR_MIN_OVR = 87

AA_SS = 3


def _s(v: float) -> int:
    return int(round(v * SCALE))


@dataclass(frozen=True)
class CardTier:
    key: str
    neon: RGB
    glow_alpha: int
    body_top: RGB
    body_bottom: RGB
    tint: tuple[RGB, RGB]
    tint_alpha: int
    metal: tuple[RGB, RGB]
    ovr: RGB
    pos_fill: tuple[RGB, RGB]
    pos_text: RGB
    pts_fill: tuple[RGB, RGB]
    pts_text: RGB
    backlight: RGB


def _darken(rgb: RGB, k: float) -> RGB:
    return tuple(max(0, int(c * k)) for c in rgb)


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


PRIME_TIER = CardTier(
    key="prime",
    neon=GOLD, glow_alpha=160,
    body_top=(12, 14, 22), body_bottom=(26, 20, 10),
    tint=(GOLD, GOLD_DEEP), tint_alpha=55,
    metal=((255, 240, 160), (160, 105, 20)),
    ovr=GOLD,
    pos_fill=(GOLD, GOLD_DEEP), pos_text=INK,
    pts_fill=(GOLD, GOLD_DEEP), pts_text=INK,
    backlight=GOLD,
)

STAR_TIER = CardTier(
    key="star",
    neon=(56, 189, 248), glow_alpha=140,
    body_top=(12, 15, 24), body_bottom=(18, 22, 38),
    tint=((99, 102, 241), (14, 165, 233)), tint_alpha=95,
    metal=((224, 231, 255), (79, 70, 229)),
    ovr=TEXT_PRIMARY,
    pos_fill=((99, 102, 241), (14, 165, 233)), pos_text=TEXT_PRIMARY,
    pts_fill=((99, 102, 241), (14, 165, 233)), pts_text=TEXT_PRIMARY,
    backlight=(56, 189, 248),
)


def _standard_tier(theme) -> CardTier:
    accent = tuple(theme.accent)
    return CardTier(
        key="standard",
        neon=accent, glow_alpha=100,
        body_top=(10, 13, 20), body_bottom=(34, 38, 48),
        tint=((50, 56, 70), (20, 24, 32)), tint_alpha=75,
        metal=((210, 215, 225), (75, 82, 95)),
        ovr=TEXT_PRIMARY,
        pos_fill=(accent, _darken(accent, 0.72)), pos_text=theme.on_accent,
        pts_fill=(accent, _darken(accent, 0.72)), pts_text=theme.on_accent,
        backlight=accent,
    )


def card_tier(player: dict, ovr: int, theme) -> CardTier:
    if player.get("is_captain") or ovr >= PRIME_MIN_OVR or int(player.get("potr_count") or 0) >= 2:
        return PRIME_TIER
    if ovr >= STAR_MIN_OVR or int(player.get("potr_count") or 0) >= 1:
        return STAR_TIER
    return _standard_tier(theme)


DISPLAY_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "LiberationSansNarrow-Bold.ttf")


@functools.lru_cache(maxsize=64)
def _display_font(size: int):
    for name in (DISPLAY_FONT_PATH, "LiberationSansNarrow-Bold.ttf", "DejaVuSansCondensed-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    logger.warning("Mid-Season: condensed font not found, falling back to load_font")
    return load_font(size, bold=True)


def _player_ovr(player: dict) -> int:
    try:
        from services.graphics.fc_card_generator import calculate_fut_attributes
        attrs = calculate_fut_attributes({
            "player_name": player.get("player_name"),
            "team_name": player.get("team_name"),
            "position": player.get("position") or "ST",
            "total_goals": int(player.get("goals") or 0),
            "total_assists": int(player.get("assists") or 0),
            "matches_played": int(player.get("matches") or 0),
        })
        base_ovr = int(attrs.get("ovr") or DEFAULT_OVR)
        # Бонус к OVR за признания Игроком тура
        potr = int(player.get("potr_count") or 0)
        return min(99, base_ovr + potr)
    except Exception:
        logger.debug("Mid-Season: OVR failed for %r", player.get("player_name"), exc_info=True)
        return DEFAULT_OVR


def _stat_chips(player: dict) -> list[tuple[str, int, str]]:
    chips = []
    potr = int(player.get("potr_count") or 0)
    if potr:
        chips.append(("star", potr, "POTR"))
    goals, assists = int(player.get("goals") or 0), int(player.get("assists") or 0)
    mvp, cs = int(player.get("mvp") or 0), int(player.get("clean_sheets") or 0)
    if goals:
        chips.append(("ball", goals, "G"))
    if assists:
        chips.append(("boot", assists, "A"))
    if cs and player.get("line") in ("GK", "DEF"):
        chips.append(("shield", cs, "CS"))
    if mvp:
        chips.append(("crown", mvp, "MVP"))
    return chips


def _initials(name: str) -> str:
    parts = [p for p in str(name or "").replace("-", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _load_photo(player: dict, fetch_photos: bool) -> Image.Image | None:
    name = player.get("player_name") or ""
    team = player.get("team_name")
    try:
        if fetch_photos:
            path = player_photos.get_player_photo(name, team)
        else:
            path = player_photos.get_photo_path(name, team)
        if path:
            return Image.open(path).convert("RGBA")
    except Exception:
        logger.debug("Mid-Season: photo failed for %r", name, exc_info=True)
    return None


def _aa_mask(size: tuple[int, int], draw_fn) -> Image.Image:
    large = Image.new("L", (size[0] * AA_SS, size[1] * AA_SS), 0)
    draw_fn(ImageDraw.Draw(large), AA_SS)
    return large.resize(size, Image.LANCZOS)


def _card_contour_pts(box: tuple[int, int, int, int], inset: float, c_top: int, c_bot: int):
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    return [
        (x0 + c_top, y0), (x1 - c_top, y0), (x1, y0 + c_top), (x1, y1 - c_bot),
        (x0 + (x1 - x0) / 2, y1), (x0, y1 - c_bot), (x0, y0 + c_top),
    ]


def _shape_mask(size: tuple[int, int], box: tuple[int, int, int, int], inset: float, c_top: int, c_bot: int) -> Image.Image:
    def draw(d: ImageDraw.ImageDraw, k: int):
        pts = [(x * k, y * k) for x, y in _card_contour_pts(box, inset, c_top, c_bot)]
        d.polygon(pts, fill=255)
    return _aa_mask(size, draw)


def _shield_polygon(cx: float, cy: float, w: float, h: float):
    x0, x1 = cx - w / 2, cx + w / 2
    y0, y1 = cy - h / 2, cy + h / 2
    c = w * 0.16
    return [
        (x0 + c, y0), (x1 - c, y0), (x1, y0 + c), (x1, y0 + h * 0.58),
        (cx, y1), (x0, y0 + h * 0.58), (x0, y0 + c),
    ]


def _vgrad(size: tuple[int, int], top, bottom) -> Image.Image:
    top = tuple(top) + ((255,) if len(top) == 3 else ())
    bottom = tuple(bottom) + ((255,) if len(bottom) == 3 else ())
    mask = Image.linear_gradient("L").resize(size)
    return Image.composite(Image.new("RGBA", size, bottom), Image.new("RGBA", size, top), mask)


def _dgrad(size: tuple[int, int], a: RGB, b: RGB, alpha: int) -> Image.Image:
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    t = ((xx / max(w - 1, 1)) + (yy / max(h - 1, 1))) / 2
    rgb = np.empty((h, w, 4), dtype=np.uint8)
    for i in range(3):
        rgb[..., i] = (a[i] + (b[i] - a[i]) * t).astype(np.uint8)
    rgb[..., 3] = alpha
    return Image.fromarray(rgb, "RGBA")


def _carbon(size: tuple[int, int], step: int) -> Image.Image:
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    weave = (((xx + yy) // step) % 2) ^ (((xx - yy) // step) % 2)
    alpha = np.where(weave == 1, 9, 0).astype(np.uint8)
    out = np.zeros((h, w, 4), dtype=np.uint8)
    out[..., :3] = 255
    out[..., 3] = alpha
    return Image.fromarray(out, "RGBA")


def _colored(mask: Image.Image, color, alpha_k: float = 1.0) -> Image.Image:
    layer = Image.new("RGBA", mask.size, tuple(color[:3]) + (0,))
    a = mask if alpha_k == 1.0 else mask.point(lambda v: int(v * alpha_k))
    layer.putalpha(a)
    return layer


def _fill_with(target: Image.Image, fill: Image.Image, mask: Image.Image, offset: tuple[int, int] = (0, 0)) -> None:
    fill = fill.copy()
    fill.putalpha(ImageChops.multiply(fill.getchannel("A"), mask))
    target.alpha_composite(fill, dest=offset)


def _icon(kind: str, size: int, color) -> Image.Image:
    n = size * AA_SS * 2
    mask = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(mask)

    def pt(x: float, y: float) -> tuple[float, float]:
        return x * n, y * n

    if kind == "star":
        cx, cy, r_out, r_in = 0.5 * n, 0.5 * n, 0.46 * n, 0.20 * n
        pts = []
        for i in range(10):
            angle = i * 36 - 90
            r = r_out if i % 2 == 0 else r_in
            rad = math.radians(angle)
            pts.append((cx + r * math.cos(rad), cy + r * math.sin(rad)))
        d.polygon(pts, fill=255)
    elif kind == "ball":
        lw = max(1, int(n * 0.09))
        d.ellipse([pt(0.06, 0.06), pt(0.94, 0.94)], outline=255, width=lw)
        cx, cy, r = 0.5, 0.5, 0.17
        pent = [pt(cx + r * math.sin(math.radians(a)), cy - r * math.cos(math.radians(a))) for a in range(0, 360, 72)]
        d.polygon(pent, fill=255)
        for a in range(0, 360, 72):
            s, c = math.sin(math.radians(a)), math.cos(math.radians(a))
            d.line([pt(cx + r * s, cy - r * c), pt(cx + 0.42 * s, cy - 0.42 * c)], fill=255, width=lw)
    elif kind == "boot":
        d.polygon([pt(0.10, 0.18), pt(0.44, 0.18), pt(0.50, 0.46), pt(0.86, 0.56), pt(0.94, 0.70),
                   pt(0.92, 0.76), pt(0.10, 0.76)], fill=255)
        for x in (0.16, 0.36, 0.60, 0.80):
            d.rectangle([pt(x, 0.76), pt(x + 0.08, 0.90)], fill=255)
    elif kind == "shield":
        d.polygon(_shield_polygon(0.5 * n, 0.5 * n, 0.76 * n, 0.9 * n), fill=255)
    elif kind == "crown":
        d.polygon([pt(0.08, 0.80), pt(0.08, 0.28), pt(0.30, 0.52), pt(0.50, 0.16), pt(0.70, 0.52),
                   pt(0.92, 0.28), pt(0.92, 0.80)], fill=255)
    elif kind == "flame":
        d.polygon([pt(0.50, 0.04), pt(0.66, 0.30), pt(0.80, 0.22), pt(0.86, 0.52), pt(0.80, 0.78),
                   pt(0.50, 0.96), pt(0.20, 0.78), pt(0.14, 0.52), pt(0.26, 0.34), pt(0.36, 0.46)], fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    return _colored(mask, color)


def _shadow_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, anchor: str, offset: int) -> None:
    draw.text((xy[0] + offset, xy[1] + offset), text, font=font, fill=(0, 0, 0, 160), anchor=anchor)
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _draw_pitch(img: Image.Image, accent: tuple[int, int, int]) -> None:
    x0, y0 = _s(PITCH_MARGIN), _s(PITCH_TOP)
    x1, y1 = _s(WIDTH_1X - PITCH_MARGIN), _s(PITCH_BOTTOM)
    pitch = Image.new("RGBA", (x1 - x0, y1 - y0), PITCH_DARK)
    pd = ImageDraw.Draw(pitch)
    stripes = 10
    stripe_h = (y1 - y0) / stripes
    for i in range(stripes):
        if i % 2:
            pd.rectangle([0, int(i * stripe_h), x1 - x0, int((i + 1) * stripe_h)], fill=PITCH_LIGHT)

    lines = Image.new("RGBA", pitch.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    w, h = pitch.size
    lw = _s(2)
    pad = _s(18)
    ld.rectangle([pad, pad, w - pad, h - pad], outline=PITCH_LINE, width=lw)
    # Половина поля, центральный круг
    ld.ellipse([w // 2 - _s(95), pad - _s(95), w // 2 + _s(95), pad + _s(95)], outline=PITCH_LINE, width=lw)
    box_w, box_h = _s(560), _s(190)
    ld.rectangle([w // 2 - box_w // 2, h - pad - box_h, w // 2 + box_w // 2, h - pad], outline=PITCH_LINE, width=lw)
    six_w, six_h = _s(260), _s(70)
    ld.rectangle([w // 2 - six_w // 2, h - pad - six_h, w // 2 + six_w // 2, h - pad], outline=PITCH_LINE, width=lw)
    ld.arc([w // 2 - _s(95), h - pad - box_h - _s(75), w // 2 + _s(95), h - pad - box_h + _s(75)],
           start=180, end=360, fill=PITCH_LINE, width=lw)
    pitch = Image.alpha_composite(pitch, lines)

    mask = Image.new("L", pitch.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=_s(22), fill=255)
    img.paste(pitch, (x0, y0), mask)
    ImageDraw.Draw(img).rounded_rectangle([x0, y0, x1, y1], radius=_s(22), outline=GOLD, width=_s(2))


class _CardGeometry:
    def __init__(self, compact: bool):
        self.k = COMPACT_H if compact else 1.0
        self.w = _s(CARD_W * (COMPACT_W if compact else 1))
        self.h = _s(CARD_H * self.k)
        self.bleed = _s(CARD_BLEED)
        self.x0, self.y0 = self.bleed, self.bleed
        self.x1, self.y1 = self.x0 + self.w, self.y0 + self.h
        self.cx = self.x0 + self.w // 2
        self.size = (self.w + 2 * self.bleed, self.h + 2 * self.bleed)
        self.c_top = self.v(20)
        self.c_bot = self.v(12)
        self.pad = self.v(11)

    def v(self, value: float) -> int:
        return _s(value * self.k)


def _draw_card_body(layer: Image.Image, g: _CardGeometry, tier: CardTier) -> Image.Image:
    box = (g.x0, g.y0, g.x1, g.y1)
    outer = _shape_mask(g.size, box, 0, g.c_top, g.c_bot)

    halo = _shape_mask(g.size, box, -_s(2), g.c_top, g.c_bot).filter(ImageFilter.GaussianBlur(_s(8)))
    layer.alpha_composite(_colored(halo, tier.neon, tier.glow_alpha / 255))
    layer.alpha_composite(_colored(outer, tier.neon))

    body_mask = _shape_mask(g.size, box, _s(2), g.c_top, g.c_bot)
    body = _vgrad(g.size, tier.body_top, tier.body_bottom)
    body.alpha_composite(_dgrad(g.size, tier.tint[0], tier.tint[1], tier.tint_alpha))
    body.alpha_composite(_carbon(g.size, max(2, _s(2))))
    _fill_with(layer, body, body_mask)

    ring_outer = _shape_mask(g.size, box, _s(4), g.c_top, g.c_bot)
    ring_inner = _shape_mask(g.size, box, _s(6.5), g.c_top, g.c_bot)
    ring = ImageChops.subtract(ring_outer, ring_inner)
    light, dark = tier.metal
    half = (g.size[0], g.size[1] // 2 + 1)
    metal = Image.new("RGBA", g.size)
    metal.paste(_vgrad(half, light, dark), (0, 0))
    metal.paste(_vgrad(half, dark, _mix(dark, light, 0.6)), (0, g.size[1] // 2))
    _fill_with(layer, metal, ring)

    sheen = Image.new("L", g.size, 0)
    sd = ImageDraw.Draw(sheen)
    band = g.w * 0.22
    sx = g.x0 + g.w * 0.55
    sd.polygon([(sx, g.y0), (sx + band, g.y0), (sx + band - g.h * 0.55, g.y1), (sx - g.h * 0.55, g.y1)], fill=26)
    sheen = sheen.filter(ImageFilter.GaussianBlur(_s(6)))
    gloss = _vgrad((g.size[0], g.v(80)), (255, 255, 255, 22), (255, 255, 255, 0))
    gloss_full = Image.new("RGBA", g.size, (0, 0, 0, 0))
    gloss_full.paste(gloss, (0, g.y0))
    gloss_full.alpha_composite(_colored(sheen, (255, 255, 255)))
    _fill_with(layer, gloss_full, ring_inner)
    return ring_inner


def _draw_sparkles(layer: Image.Image, g: _CardGeometry, player: dict, clip: Image.Image) -> None:
    rng = np.random.default_rng(zlib.crc32(str(player.get("player_name") or "").encode("utf-8")))
    sparks = Image.new("L", g.size, 0)
    d = ImageDraw.Draw(sparks)
    for _ in range(16):
        x = g.x0 + rng.uniform(0.08, 0.92) * g.w
        y = g.y0 + rng.uniform(0.06, 0.62) * g.h
        r = rng.uniform(0.6, 2.0) * SCALE
        a = int(rng.uniform(100, 230))
        d.polygon([(x, y - r * 2.4), (x + r * 0.5, y), (x, y + r * 2.4), (x - r * 0.5, y)], fill=a)
        d.polygon([(x - r * 2.4, y), (x, y + r * 0.5), (x + r * 2.4, y), (x, y - r * 0.5)], fill=a)
    sparks = ImageChops.multiply(sparks, clip)
    layer.alpha_composite(_colored(sparks.filter(ImageFilter.GaussianBlur(0.6)), (255, 226, 120)))


def _is_cutout(photo: Image.Image) -> bool:
    return photo.getchannel("A").getextrema()[0] < 16


def _draw_visual(layer: Image.Image, g: _CardGeometry, player: dict, tier: CardTier, fetch_photos: bool, clip: Image.Image) -> None:
    vis_cy = g.y0 + g.v(100)
    shield_w, shield_h = g.v(80), g.v(96)

    back = Image.new("L", g.size, 0)
    ImageDraw.Draw(back).ellipse([g.cx - shield_w * 0.62, vis_cy - shield_h * 0.5,
                                  g.cx + shield_w * 0.62, vis_cy + shield_h * 0.55], fill=150)
    back = ImageChops.multiply(back.filter(ImageFilter.GaussianBlur(g.v(14))), clip)
    layer.alpha_composite(_colored(back, tier.backlight, 0.55))

    photo = _load_photo(player, fetch_photos)
    if photo is not None and _is_cutout(photo):
        pw = int(g.w * 0.88)
        ph = int(pw * photo.size[1] / max(photo.size[0], 1))
        max_h = g.v(145)
        if ph > max_h:
            pw, ph = int(pw * max_h / ph), max_h
        photo = photo.resize((pw, ph), Image.LANCZOS)
        px, py = g.cx - pw // 2, g.y0 + g.v(28) - _s(PHOTO_POP_OUT)
        # Pop-out вырезка с контурным светом
        rim = photo.filter(ImageFilter.GaussianBlur(_s(3))).getchannel("A")
        rim_layer = _colored(rim, tier.neon, 0.8)
        layer.alpha_composite(rim_layer, dest=(px, py))
        layer.alpha_composite(photo, dest=(px, py))
        return

    face = _aa_mask(g.size, lambda d, k: d.polygon([(x * k, y * k) for x, y in _shield_polygon(g.cx, vis_cy, shield_w, shield_h)], fill=255))
    if photo is not None:
        photo = photo.resize((shield_w, shield_h), Image.LANCZOS)
    else:
        photo = Image.new("RGBA", (shield_w, shield_h), (35, 40, 52))
        pd = ImageDraw.Draw(photo)
        pd.text((shield_w // 2, shield_h // 2), _initials(player.get("player_name") or ""),
                font=_display_font(g.v(32)), fill=TEXT_MUTED, anchor="mm")
    full = Image.new("RGBA", g.size, (0, 0, 0, 0))
    full.paste(photo, (int(g.cx - shield_w / 2), int(vis_cy - shield_h / 2)))
    _fill_with(layer, full, face)


def _draw_club_badge(layer: Image.Image, g: _CardGeometry, team: str, tier: CardTier) -> None:
    d = g.v(38)
    cx, cy = g.x1 - g.pad - d // 2, g.y0 + g.pad + d // 2
    aura = Image.new("L", g.size, 0)
    ImageDraw.Draw(aura).ellipse([cx - d * 0.62, cy - d * 0.62, cx + d * 0.62, cy + d * 0.62], fill=170)
    layer.alpha_composite(_colored(aura.filter(ImageFilter.GaussianBlur(_s(5))), tier.neon, 0.6))

    logo = None
    filename = get_team_logo_filename(team) if team else None
    if filename:
        path = os.path.join(LOGOS_DIR, filename)
        if os.path.exists(path):
            try:
                logo = clean_and_prepare_logo(Image.open(path))
            except Exception:
                logger.debug("Mid-Season: logo failed for %r", team, exc_info=True)
    if logo is not None:
        logo_img, lw, lh = resize_logo_proportional(logo, d, d)
        layer.alpha_composite(logo_img, dest=(cx - lw // 2, cy - lh // 2))
        return

    ring = _aa_mask(g.size, lambda dr, k: dr.ellipse([(cx - d / 2) * k, (cy - d / 2) * k,
                                                       (cx + d / 2) * k, (cy + d / 2) * k], fill=255))
    face = _aa_mask(g.size, lambda dr, k: dr.ellipse([(cx - d / 2 + _s(2)) * k, (cy - d / 2 + _s(2)) * k,
                                                       (cx + d / 2 - _s(2)) * k, (cy + d / 2 - _s(2)) * k], fill=255))
    _fill_with(layer, _vgrad(g.size, *tier.metal), ring)
    _fill_with(layer, _vgrad(g.size, (40, 44, 56), (14, 16, 22)), face)
    ImageDraw.Draw(layer).text((cx, cy + _s(0.5)), _initials(team), font=_display_font(g.v(15)),
                               fill=TEXT_PRIMARY, anchor="mm")


def _draw_captain_badge(layer: Image.Image, cx: int, cy: int, r: int) -> None:
    def hexagon(rr: float, k: int):
        return [((cx + rr * math.cos(math.radians(a))) * k, (cy + rr * math.sin(math.radians(a))) * k)
                for a in range(30, 390, 60)]

    size = layer.size
    edge = _aa_mask(size, lambda d, k: d.polygon(hexagon(r + _s(2), k), fill=255))
    face = _aa_mask(size, lambda d, k: d.polygon(hexagon(r, k), fill=255))
    layer.alpha_composite(_colored(edge, INK))
    _fill_with(layer, _vgrad(size, (255, 236, 140), GOLD_DEEP), face)
    ImageDraw.Draw(layer).text((cx, cy), "👑", font=_display_font(int(r * 1.35)), fill=INK, anchor="mm")


def _draw_pos_chip(draw: ImageDraw.ImageDraw, layer: Image.Image, x: int, y: int, label: str, g: _CardGeometry, tier: CardTier) -> None:
    font = _display_font(g.v(15))
    h = g.v(19)
    w = int(draw.textlength(label, font=font)) + g.v(16)
    slant = g.v(5)
    poly = [(x + slant, y), (x + w + slant, y), (x + w, y + h), (x, y + h)]
    mask = _aa_mask(layer.size, lambda d, k: d.polygon([(px * k, py * k) for px, py in poly], fill=255))
    _fill_with(layer, _vgrad(layer.size, *tier.pos_fill), mask)
    ImageDraw.Draw(layer).text((x + slant // 2 + w // 2, y + h // 2 + _s(0.5)), label, font=font,
                               fill=tier.pos_text, anchor="mm")


def _draw_stat_chips(layer: Image.Image, g: _CardGeometry, player: dict, y: int) -> None:
    chips = _stat_chips(player)
    draw = ImageDraw.Draw(layer)
    h = g.v(19)
    avail = g.w - 2 * g.pad

    def layout(font_px: float, pad_px: float, with_label: bool):
        font = _display_font(g.v(font_px))
        icon_sz, gap, pad_x = g.v(font_px * 0.8), g.v(pad_px * 0.66), g.v(pad_px)
        ws = [pad_x * 2 + icon_sz + g.v(3) + int(draw.textlength(f"{n}{lab if with_label else ''}", font=font))
              for _icon_kind, n, lab in chips]
        return font, icon_sz, gap, pad_x, ws

    def fits(ws: list[int], gap: int) -> bool:
        return sum(ws) + gap * (len(ws) - 1) <= avail

    for font_px, pad_px, with_label in ((14, 6, True), (12.5, 4.5, True), (14, 6, False)):
        font, icon_sz, gap, pad_x, ws = layout(font_px, pad_px, with_label)
        if fits(ws, gap):
            break
    while chips and not fits(ws, gap):
        chips, ws = chips[:-1], ws[:-1]

    if not chips:
        draw.text((g.cx, y + h // 2), "—", font=_display_font(g.v(14)), fill=TEXT_HEADER, anchor="mm")
        return

    total = sum(ws) + gap * (len(ws) - 1)
    x = g.cx - total // 2
    for (kind, n, lab), w in zip(chips, ws):
        box = [x, y, x + w, y + h]
        # Если это чип POTR — выделяем золотым фоном
        chip_fill = (255, 215, 0, 45) if kind == "star" else (255, 255, 255, 20)
        chip_border = (255, 215, 0, 110) if kind == "star" else (255, 255, 255, 46)
        draw.rounded_rectangle(box, radius=h // 2, fill=chip_fill, outline=chip_border, width=max(1, _s(0.6)))
        icon_color = GOLD if kind == "star" else TEXT_PRIMARY
        icon = _icon(kind, icon_sz, icon_color)
        layer.alpha_composite(icon, dest=(x + pad_x, y + (h - icon_sz) // 2))
        draw.text((x + pad_x + icon_sz + g.v(3), y + h // 2 + _s(0.5)), f"{n}{lab if with_label else ''}",
                  font=font, fill=TEXT_PRIMARY, anchor="lm")
        x += w + gap


def _draw_pts_pill(layer: Image.Image, g: _CardGeometry, score: int, tier: CardTier) -> None:
    draw = ImageDraw.Draw(layer)
    font = _display_font(g.v(16))
    text = f"{score} PTS"
    icon_sz = g.v(13)
    h = g.v(23)
    w = int(draw.textlength(text, font=font)) + icon_sz + g.v(26)
    x0, y0 = g.cx - w // 2, g.y1 - g.v(12) - h
    box = (x0, y0, x0 + w, y0 + h)

    def pill(inset: float):
        return lambda d, k: d.rounded_rectangle([(box[0] + inset) * k, (box[1] + inset) * k,
                                                 (box[2] - inset) * k, (box[3] - inset) * k],
                                                radius=(h / 2 - inset) * k, fill=255)

    shadow = _aa_mask(layer.size, pill(0)).filter(ImageFilter.GaussianBlur(_s(2)))
    layer.alpha_composite(_colored(shadow, (0, 0, 0), 0.6), dest=(0, _s(2)))
    edge = _aa_mask(layer.size, pill(0))
    face = _aa_mask(layer.size, pill(_s(1.2)))
    _fill_with(layer, _vgrad(layer.size, _mix(tier.pts_fill[0], (255, 255, 255), 0.35), _darken(tier.pts_fill[1], 0.55)), edge)
    _fill_with(layer, _vgrad(layer.size, *tier.pts_fill), face)

    top = Image.new("L", layer.size, 0)
    ImageDraw.Draw(top).rectangle([0, 0, layer.size[0], y0 + h * 0.48], fill=255)
    gloss = Image.new("RGBA", layer.size, (255, 255, 255, 0))
    gloss.paste(_vgrad((layer.size[0], int(h * 0.5)), (255, 255, 255, 80), (255, 255, 255, 8)), (0, y0 + _s(1)))
    _fill_with(layer, gloss, ImageChops.multiply(face, top))

    icon = _icon("flame", icon_sz, tier.pts_text)
    ix = x0 + g.v(10)
    layer.alpha_composite(icon, dest=(ix, y0 + (h - icon_sz) // 2))
    draw.text((ix + icon_sz + g.v(4), y0 + h // 2 + _s(0.5)), text, font=font, fill=tier.pts_text, anchor="lm")


def _draw_card(
    img: Image.Image,
    player: dict,
    cx: int,
    cy: int,
    theme,
    fetch_photos: bool,
    compact: bool = False,
) -> None:
    g = _CardGeometry(compact)
    ovr = _player_ovr(player)
    tier = card_tier(player, ovr, theme)
    layer = Image.new("RGBA", g.size, (0, 0, 0, 0))

    shadow = _shape_mask(g.size, (g.x0, g.y0, g.x1, g.y1), 0, g.c_top, g.c_bot)
    shadow = shadow.filter(ImageFilter.GaussianBlur(_s(9)))
    ox, oy = cx - g.size[0] // 2, cy - g.size[1] // 2
    shadow_layer = Image.new("RGBA", g.size, (0, 0, 0, 0))
    shadow_layer.alpha_composite(_colored(shadow, (0, 0, 0), 0.8), dest=(_s(3), _s(10)))
    img.alpha_composite(shadow_layer, dest=(ox, oy))

    clip = _draw_card_body(layer, g, tier)
    if tier is PRIME_TIER:
        _draw_sparkles(layer, g, player, clip)
    _draw_visual(layer, g, player, tier, fetch_photos, clip)

    draw = ImageDraw.Draw(layer)
    _shadow_text(draw, (g.x0 + g.pad + _s(1), g.y0 + g.pad - g.v(3)), str(ovr),
                 _display_font(g.v(40)), tier.ovr, "la", _s(1))
    pos_label = str(player.get("slot_label") or player.get("position") or "")
    if pos_label:
        _draw_pos_chip(draw, layer, g.x0 + g.pad, g.y0 + g.v(54), pos_label, g, tier)
    _draw_club_badge(layer, g, player.get("team_name") or "", tier)

    if player.get("is_captain"):
        _draw_captain_badge(layer, g.x1 - g.pad - g.v(12), g.y0 + g.v(138), g.v(12))

    name_font = _display_font(g.v(18))
    name = _fit_text(draw, str(player.get("player_name") or "").upper(), name_font, g.w - 2 * g.pad)
    _shadow_text(draw, (g.cx, g.y0 + g.v(162)), name, name_font, TEXT_PRIMARY, "mm", _s(1))
    _draw_stat_chips(layer, g, player, g.y0 + g.v(175))
    _draw_pts_pill(layer, g, int(player.get("score") or 0), tier)

    img.alpha_composite(layer, dest=(ox, oy))


def generate_mid_season_image(
    payload: dict,
    division_id: int | None = None,
    start_round: int | None = None,
    end_round: int | None = None,
    fetch_photos: bool = False,
) -> io.BytesIO:
    """Отрисовать постер сборной 1-го круга.

    `fetch_photos=False` — только кэш фото на диске.
    Возвращает io.BytesIO с PNG 2400×3300.
    """
    division_id = division_id if division_id is not None else payload.get("division_id")
    start_round = start_round if start_round is not None else int(payload.get("start_round") or 1)
    end_round = end_round if end_round is not None else int(payload.get("end_round") or start_round)

    theme = resolve_theme(
        division_id=division_id,
        division_code=payload.get("division_code"),
        division_name=payload.get("division_name"),
    )

    width, height = _s(WIDTH_1X), _s(HEIGHT_1X)
    img = Image.new("RGBA", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    fonts = {
        "title": load_font(_s(44), bold=True),
        "subtitle": load_font(_s(20), bold=True),
        "meta": load_font(_s(15)),
        "badge": load_font(_s(12), bold=True),
        "section": load_font(_s(16), bold=True),
        "footer": load_font(_s(12)),
    }

    margin = _s(PITCH_MARGIN)

    # ─── Шапка ───
    draw.rectangle([0, 0, width, _s(8)], fill=GOLD)
    draw.text((margin, _s(34)), "СБОРНАЯ 1-ГО КРУГА", fill=GOLD, font=fonts["title"])
    season = str(payload.get("season_name") or "").strip()
    subtitle = f"ТУРЫ {start_round} — {end_round} • ЭКВАТОР СЕЗОНА" + (f" • {season.upper()}" if season else "")
    draw.text((margin, _s(92)), subtitle, fill=TEXT_PRIMARY, font=fonts["subtitle"])
    meta = f"{payload.get('division_name') or ''} • {payload.get('formation') or '4-3-3'} • MID-SEASON BEST XI"
    draw.text((margin, _s(126)), meta.strip(" •"), fill=TEXT_HEADER, font=fonts["meta"])
    draw_division_badge(draw, theme, width - margin, _s(40), fonts["badge"], scale=SCALE)

    # ─── Поле ───
    _draw_pitch(img, theme.accent)
    draw = ImageDraw.Draw(img)

    xi = payload.get("xi") or []
    if not xi:
        draw.text((width // 2, _s((PITCH_TOP + PITCH_BOTTOM) / 2)), "Матчи 1-го круга ещё не сыграны",
                  fill=TEXT_MUTED, font=fonts["subtitle"], anchor="mm")

    for player in sorted(xi, key=lambda p: SLOT_CENTERS.get(p.get("slot") or "", (0, 0))[1]):
        center = SLOT_CENTERS.get(player.get("slot") or "")
        if not center:
            continue
        try:
            _draw_card(img, player, _s(center[0]), _s(center[1]), theme, fetch_photos)
        except Exception:
            logger.exception("Mid-Season: card failed for %r", player.get("player_name"))

    # ─── Скамейка ───
    draw = ImageDraw.Draw(img)
    bench = payload.get("bench") or []
    draw.text((margin, _s(BENCH_TOP)), "СКАМЬЯ ЗАПАСНЫХ / BENCH", fill=GOLD, font=fonts["section"])
    if bench:
        slot_w = (WIDTH_1X - 2 * PITCH_MARGIN) / 4
        for i, player in enumerate(bench[:4]):
            cx = PITCH_MARGIN + slot_w * (i + 0.5)
            cy = BENCH_TOP + 26 + CARD_H * COMPACT_H / 2
            try:
                _draw_card(img, player, _s(cx), _s(cy), theme, fetch_photos, compact=True)
            except Exception:
                logger.exception("Mid-Season: bench card failed for %r", player.get("player_name"))
    else:
        draw.text((margin, _s(BENCH_TOP + 40)), "—", fill=TEXT_MUTED, font=fonts["meta"])

    # ─── Подвал ───
    draw = ImageDraw.Draw(img)
    total_goals = payload.get("total_goals") or 0
    footer_text = f"Логово Фифарей • ИИ «Темшик» • Всего голов в 1-м круге: {total_goals}"
    draw.text((width // 2, height - _s(9)), footer_text, fill=TEXT_HEADER, font=fonts["footer"], anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    out.seek(0)
    return out
