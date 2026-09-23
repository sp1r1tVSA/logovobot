"""
services/graphics/totw_generator.py

Постер «Символическая сборная» (TOTW) за блок туров: поле 4-3-3 с карточками
игроков, капитан, скамейка из четырёх и акцент дивизиона.

Правила рендера те же, что у таблицы и итогов тура: 2x supersampling
(1200×1650 → 2400×3300), общий load_font/get_team_logo_filename, тёмная
бродкаст-палитра, на выходе io.BytesIO с PNG.

Всё fail-soft: нет логотипа — пустой белый бейдж, нет фото — круг с инициалами,
не посчитался OVR — нейтральные 70. Фото по умолчанию берутся только из
дискового кэша (assets/players): рендер не ходит в сеть, подгрузкой фото
заранее занимается вызывающий код.
"""

import io
import logging

from PIL import Image, ImageChops, ImageDraw

from services.graphics import player_photos
from services.graphics.division_theme import draw_division_badge, resolve_theme
from services.graphics.round_digest_generator import _draw_logo_badge, _fit_text
from services.graphics.table_generator import SCALE, load_font

logger = logging.getLogger(__name__)

WIDTH_1X = 1200
HEIGHT_1X = 1650

BG_COLOR = (20, 20, 22)
CARD_BG = (26, 26, 30)
CARD_BG_ALT = (38, 38, 46)
TEXT_PRIMARY = (255, 255, 255)
TEXT_MUTED = (209, 213, 219)
TEXT_HEADER = (156, 163, 175)
GOLD = (250, 204, 21)
PITCH_DARK = (17, 38, 28)
PITCH_LIGHT = (21, 46, 34)
PITCH_LINE = (255, 255, 255, 46)

# Геометрия (1x)
HEADER_H = 170
PITCH_TOP = 185
PITCH_BOTTOM = 1365
PITCH_MARGIN = 30
CARD_W = 164
CARD_H = 232
BENCH_TOP = 1385
BENCH_H = 210
COMPACT_W = 0.82
COMPACT_H = 0.9

# Центры карточек по слотам, 1x. Атака сверху, вратарь снизу.
SLOT_CENTERS: dict[str, tuple[int, int]] = {
    "LW": (250, 345), "ST": (600, 315), "RW": (950, 345),
    "LCM": (330, 640), "CDM": (600, 700), "RCM": (870, 640),
    "LB": (160, 960), "LCB": (445, 985), "RCB": (755, 985), "RB": (1040, 960),
    "GK": (600, 1238),
}

DEFAULT_OVR = 70


def _s(v: float) -> int:
    return int(round(v * SCALE))


def _player_ovr(player: dict) -> int:
    """OVR карточки по цифрам блока — та же формула, что у FC-карточек."""
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
        return int(attrs.get("ovr") or DEFAULT_OVR)
    except Exception:
        logger.debug("TOTW: OVR failed for %r", player.get("player_name"), exc_info=True)
        return DEFAULT_OVR


def _stat_tokens(player: dict) -> str:
    tokens = []
    goals, assists = int(player.get("goals") or 0), int(player.get("assists") or 0)
    mvp, cs = int(player.get("mvp") or 0), int(player.get("clean_sheets") or 0)
    if goals:
        tokens.append(f"{goals} Г")
    if assists:
        tokens.append(f"{assists} А")
    if mvp:
        tokens.append(f"{mvp} MVP")
    if cs and player.get("line") in ("GK", "DEF"):
        tokens.append(f"{cs} СУХ")
    return " · ".join(tokens) or "—"


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
        logger.debug("TOTW: photo failed for %r", name, exc_info=True)
    return None


def prefetch_photos(totw_data: dict, max_workers: int = 4) -> int:
    """Подтянуть в дисковый кэш фото игроков XI и запаса. Ходит в сеть — только из потока.

    Возвращает число игроков, у которых фото есть после прогона.
    """
    from concurrent.futures import ThreadPoolExecutor

    players = list(totw_data.get("xi") or []) + list(totw_data.get("bench") or [])
    missing = [
        p for p in players
        if p.get("player_name") and not player_photos.get_photo_path(p["player_name"], p.get("team_name"))
    ]

    def _fetch(player: dict) -> None:
        try:
            player_photos.get_player_photo(player["player_name"], player.get("team_name"))
        except Exception:
            logger.debug("TOTW: photo prefetch failed for %r", player.get("player_name"), exc_info=True)

    if missing:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            list(pool.map(_fetch, missing))
    return sum(
        1 for p in players
        if p.get("player_name") and player_photos.get_photo_path(p["player_name"], p.get("team_name"))
    )


def _paste_round_photo(img: Image.Image, photo: Image.Image, cx: int, cy: int, diameter: int) -> None:
    """Вписать фото в круг: заполнить квадрат и срезать по кругу, лицо — у верхнего края."""
    w, h = photo.size
    side = min(w, h)
    left = (w - side) // 2
    square = photo.crop((left, 0, left + side, side)).resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter - 1, diameter - 1], fill=255)
    alpha = square.split()[3]
    mask = ImageChops.multiply(mask, alpha)
    img.paste(square, (cx - diameter // 2, cy - diameter // 2), mask)


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
    # Половина поля, центральный круг — у верхней кромки (атакуем вверх).
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
    ImageDraw.Draw(img).rounded_rectangle([x0, y0, x1, y1], radius=_s(22), outline=accent, width=_s(2))


def _draw_card(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    player: dict,
    cx: int,
    cy: int,
    theme,
    fonts: dict,
    fetch_photos: bool,
    compact: bool = False,
) -> None:
    """Карточка игрока с центром в (cx, cy), координаты уже в 2x."""
    card_w = _s(CARD_W * (COMPACT_W if compact else 1))
    card_h = _s(CARD_H * (COMPACT_H if compact else 1))
    x0, y0 = cx - card_w // 2, cy - card_h // 2
    x1, y1 = x0 + card_w, y0 + card_h
    is_captain = bool(player.get("is_captain"))
    border = GOLD if is_captain else theme.accent

    draw.rounded_rectangle([x0 + _s(4), y0 + _s(6), x1 + _s(4), y1 + _s(6)], radius=_s(16), fill=(0, 0, 0))
    draw.rounded_rectangle([x0, y0, x1, y1], radius=_s(16), fill=CARD_BG, outline=border,
                           width=_s(3 if is_captain else 2))

    # OVR и позиция — левый верхний угол.
    pad = _s(10)
    draw.text((x0 + pad, y0 + pad - _s(2)), str(_player_ovr(player)), fill=TEXT_PRIMARY, font=fonts["ovr"])
    pos_label = str(player.get("slot_label") or player.get("position") or "")
    pos_y = y0 + pad + _s(30 if not compact else 26)
    pos_w = int(draw.textlength(pos_label, font=fonts["pos"])) + _s(12)
    draw.rounded_rectangle([x0 + pad, pos_y, x0 + pad + pos_w, pos_y + _s(18)], radius=_s(9), fill=theme.accent)
    draw.text((x0 + pad + pos_w // 2, pos_y + _s(9)), pos_label, fill=theme.on_accent, font=fonts["pos"], anchor="mm")

    # Эмблема клуба — правый верхний угол.
    logo_d = _s(36 if not compact else 30)
    _draw_logo_badge(img, draw, player.get("team_name") or "", x1 - pad - logo_d // 2, y0 + pad + logo_d // 2, logo_d)

    # Фото.
    photo_d = _s(92 if not compact else 72)
    photo_cy = y0 + _s(98 if not compact else 78)
    draw.ellipse([cx - photo_d // 2 - _s(3), photo_cy - photo_d // 2 - _s(3),
                  cx + photo_d // 2 + _s(3), photo_cy + photo_d // 2 + _s(3)], fill=border)
    draw.ellipse([cx - photo_d // 2, photo_cy - photo_d // 2, cx + photo_d // 2, photo_cy + photo_d // 2], fill=CARD_BG_ALT)
    photo = _load_photo(player, fetch_photos)
    if photo is not None:
        try:
            _paste_round_photo(img, photo, cx, photo_cy, photo_d)
        except Exception:
            logger.debug("TOTW: could not paste photo", exc_info=True)
            photo = None
    if photo is None:
        draw.text((cx, photo_cy), _initials(player.get("player_name")), fill=TEXT_MUTED, font=fonts["initials"], anchor="mm")

    # Капитанская повязка.
    if is_captain:
        arm_r = _s(15)
        ax, ay = x0 + card_w - _s(16), photo_cy + photo_d // 2 - _s(10)
        draw.ellipse([ax - arm_r, ay - arm_r, ax + arm_r, ay + arm_r], fill=GOLD, outline=CARD_BG, width=_s(2))
        draw.text((ax, ay), "C", fill=(18, 18, 20), font=fonts["pos"], anchor="mm")

    # Имя, клуб, цифры, очки.
    text_w = card_w - 2 * pad
    name_y = photo_cy + photo_d // 2 + _s(18 if not compact else 15)
    name = _fit_text(draw, str(player.get("player_name") or ""), fonts["name"], text_w)
    draw.text((cx, name_y), name, fill=TEXT_PRIMARY, font=fonts["name"], anchor="mm")
    stats = _fit_text(draw, _stat_tokens(player), fonts["stats"], text_w)
    draw.text((cx, name_y + _s(21 if not compact else 18)), stats, fill=TEXT_MUTED, font=fonts["stats"], anchor="mm")

    pts = f"{int(player.get('score') or 0)} PTS"
    pts_w = int(draw.textlength(pts, font=fonts["pts"])) + _s(16)
    pts_y = y1 - pad - _s(20)
    fill = GOLD if is_captain else theme.accent
    on_fill = (18, 18, 20) if is_captain else theme.on_accent
    draw.rounded_rectangle([cx - pts_w // 2, pts_y, cx + pts_w // 2, pts_y + _s(20)], radius=_s(10), fill=fill)
    draw.text((cx, pts_y + _s(10)), pts, fill=on_fill, font=fonts["pts"], anchor="mm")


def _rounds_label(start_round: int, end_round: int) -> str:
    if start_round == end_round:
        return f"ТУР {start_round}"
    return f"ТУРЫ {start_round} — {end_round}"


def generate_totw_image(
    totw_data: dict,
    division_id: int | None = None,
    start_round: int | None = None,
    end_round: int | None = None,
    fetch_photos: bool = False,
) -> io.BytesIO:
    """Отрисовать постер сборной по payload из services.totw_service.build_totw_payload.

    `fetch_photos=False` — только кэш фото на диске, без сети.
    Возвращает io.BytesIO с PNG 2400×3300.
    """
    division_id = division_id if division_id is not None else totw_data.get("division_id")
    start_round = start_round if start_round is not None else int(totw_data.get("start_round") or 1)
    end_round = end_round if end_round is not None else int(totw_data.get("end_round") or start_round)

    theme = resolve_theme(
        division_id=division_id,
        division_code=totw_data.get("division_code"),
        division_name=totw_data.get("division_name"),
    )

    width, height = _s(WIDTH_1X), _s(HEIGHT_1X)
    img = Image.new("RGBA", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    fonts = {
        "title": load_font(_s(44), bold=True),
        "subtitle": load_font(_s(20), bold=True),
        "meta": load_font(_s(15)),
        "badge": load_font(_s(12), bold=True),
        "ovr": load_font(_s(26), bold=True),
        "pos": load_font(_s(11), bold=True),
        "initials": load_font(_s(28), bold=True),
        "name": load_font(_s(15), bold=True),
        "stats": load_font(_s(12)),
        "pts": load_font(_s(12), bold=True),
        "section": load_font(_s(16), bold=True),
        "footer": load_font(_s(12)),
    }
    compact_fonts = dict(fonts, ovr=load_font(_s(21), bold=True), initials=load_font(_s(22), bold=True),
                         name=load_font(_s(13), bold=True), stats=load_font(_s(11)))

    margin = _s(PITCH_MARGIN)

    # ─── Шапка ───
    draw.rectangle([0, 0, width, _s(8)], fill=theme.accent)
    draw.text((margin, _s(34)), "СИМВОЛИЧЕСКАЯ СБОРНАЯ", fill=theme.accent, font=fonts["title"])
    season = str(totw_data.get("season_name") or "").strip()
    subtitle = _rounds_label(start_round, end_round) + (f" • {season.upper()}" if season else "")
    draw.text((margin, _s(92)), subtitle, fill=TEXT_PRIMARY, font=fonts["subtitle"])
    meta = f"{totw_data.get('division_name') or ''} • {totw_data.get('formation') or '4-3-3'} • TOTW Performance Index"
    draw.text((margin, _s(126)), meta.strip(" •"), fill=TEXT_HEADER, font=fonts["meta"])
    draw_division_badge(draw, theme, width - margin, _s(40), fonts["badge"], scale=SCALE)

    # ─── Поле ───
    _draw_pitch(img, theme.accent)
    draw = ImageDraw.Draw(img)

    xi = totw_data.get("xi") or []
    if not xi:
        draw.text((width // 2, _s((PITCH_TOP + PITCH_BOTTOM) / 2)), "Сыгранных матчей за эти туры пока нет",
                  fill=TEXT_MUTED, font=fonts["subtitle"], anchor="mm")
    for player in xi:
        center = SLOT_CENTERS.get(player.get("slot") or "")
        if not center:
            continue
        try:
            _draw_card(img, draw, player, _s(center[0]), _s(center[1]), theme, fonts, fetch_photos)
        except Exception:
            logger.exception("TOTW: card failed for %r", player.get("player_name"))

    # ─── Скамейка ───
    bench = totw_data.get("bench") or []
    draw.text((margin, _s(BENCH_TOP)), "ЗАПАС", fill=theme.accent, font=fonts["section"])
    if bench:
        slot_w = (WIDTH_1X - 2 * PITCH_MARGIN) / 4
        for i, player in enumerate(bench[:4]):
            cx = PITCH_MARGIN + slot_w * (i + 0.5)
            cy = BENCH_TOP + 28 + CARD_H * COMPACT_H / 2
            try:
                _draw_card(img, draw, player, _s(cx), _s(cy), theme, compact_fonts, fetch_photos, compact=True)
            except Exception:
                logger.exception("TOTW: bench card failed for %r", player.get("player_name"))
    else:
        draw.text((margin, _s(BENCH_TOP + 40)), "—", fill=TEXT_MUTED, font=fonts["meta"])

    # ─── Подвал ───
    draw.text((width // 2, height - _s(11)), "Логово Фифарей • ИИ «Темшик»",
              fill=TEXT_HEADER, font=fonts["footer"], anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    out.seek(0)
    return out
