"""
services/graphics/cup_bracket_generator.py

Сетка кубка (общего или дивизиона) — картинка, которую бот закрепляет первым
сообщением в теме «Кубок» и редактирует после каждого внесённого результата.

Колонка = этап. Этапы, которых ещё нет в базе, дорисовываются пустыми слотами до
финала: сетку видно целиком с первого дня. Серия следующего этапа стоит между
двумя сериями, из которых выходят её участники (номера 2k-1 и 2k), — ровно так
пары строит `scripts/seed_cup_bracket.py --from-winners`.

Рендер по тем же правилам, что остальная графика: 2x supersampling, тёмная
бродкаст-палитра, эмблемы через `get_team_logo_filename`, на выходе io.BytesIO с
PNG. Модуль чистый — данные готовит вызывающий (`services.cup_broadcast`).
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont

from constants import CUP_STAGES
from services.graphics.division_theme import resolve_theme
from services.graphics.table_generator import (
    LOGOS_DIR,
    SCALE,
    clean_and_prepare_logo,
    get_team_logo_filename,
    load_font,
    resize_logo_proportional,
)

BG_COLOR = (20, 20, 22)
CARD_BG = (30, 30, 35)
CARD_BG_EMPTY = (25, 25, 29)
CARD_BORDER = (48, 48, 56)
TEXT_PRIMARY = (255, 255, 255)
TEXT_MUTED = (156, 163, 175)
TEXT_DIM = (95, 99, 110)
LINE_COLOR = (70, 70, 80)
GOLD = (250, 204, 21)
GENERAL_ACCENT = (250, 204, 21)

NARROW_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "LiberationSansNarrow-Bold.ttf")

# 1x-геометрия
PAD_X = 40
HEADER_H = 118
STAGE_LABEL_H = 40
BOX_W = 250
BOX_H = 64
BOX_GAP = 14
COL_GAP = 46
CHAMP_W = 210
FOOTER_H = 36
# Telegram не принимает фото с суммой сторон больше 10 000 px: сетка общего
# кубка с 1/64 (64 серии в колонке) в 2x её превышает — такую уменьшаем.
MAX_SIDES_SUM = 9600


def _narrow(size: int):
    for name in (NARROW_FONT_PATH, "LiberationSansNarrow-Bold.ttf", "DejaVuSansCondensed-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return load_font(size, bold=True)


def stage_caption(stage: str) -> str:
    """«1/8» → «1/8 финала», «final» → «Финал»."""
    if stage == "final":
        return "Финал"
    return f"{stage} финала"


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def _plan_columns(bracket: list[dict]) -> list[dict]:
    """Колонки сетки: заведённые этапы плюс пустые будущие до финала.

    Будущий этап получает вдвое меньше серий, чем предыдущий; колонка с одной
    серией — последняя, как бы она ни называлась (у общего кубка стадия и число
    пар не обязаны совпадать).
    """
    columns = [
        {"stage": b["stage"]["stage"], "series": list(b["series"]), "row": b["stage"]}
        for b in bracket
        if b.get("series")
    ]
    if not columns:
        return []
    while len(columns[-1]["series"]) > 1 and columns[-1]["stage"] != "final":
        last = columns[-1]
        try:
            next_stage = CUP_STAGES[CUP_STAGES.index(last["stage"]) + 1]
        except (ValueError, IndexError):
            break
        count = max(1, (len(last["series"]) + 1) // 2)
        if count == 1:
            next_stage = "final"
        columns.append({"stage": next_stage, "series": [None] * count, "row": None})
    return columns


def _champion(columns: list[dict]) -> str | None:
    last = columns[-1]["series"] if columns else []
    if len(last) == 1 and last[0] and last[0].get("winner_name"):
        return last[0]["winner_name"]
    return None


def _paste_logo(img: Image.Image, draw: ImageDraw.ImageDraw, team: str, cx: int, cy: int, d: int, accent) -> None:
    """Круглый бейдж клуба: эмблема на белом или инициал на акценте, если PNG нет."""
    x0, y0 = cx - d // 2, cy - d // 2
    logo_file = get_team_logo_filename(team)
    path = os.path.join(LOGOS_DIR, logo_file) if logo_file else None
    if path and os.path.exists(path):
        draw.ellipse([x0, y0, x0 + d, y0 + d], fill=(255, 255, 255))
        try:
            logo = clean_and_prepare_logo(Image.open(path))
            inner = int(d * 0.78)
            logo_img, lw, lh = resize_logo_proportional(logo, inner, inner)
            img.paste(logo_img, (x0 + (d - lw) // 2, y0 + (d - lh) // 2), logo_img)
            return
        except Exception:
            pass
    draw.ellipse([x0, y0, x0 + d, y0 + d], fill=(55, 55, 64))
    letter = (team or "?").strip()[:1].upper() or "?"
    font = _narrow(int(d * 0.55))
    draw.text((cx, cy), letter, font=font, fill=accent, anchor="mm")


def _draw_series_box(img, draw, series: dict | None, x: int, y: int, accent, s: int) -> None:
    w, h = BOX_W * s, BOX_H * s
    radius = 10 * s
    if series is None:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=CARD_BG_EMPTY,
                               outline=CARD_BORDER, width=max(1, s))
        font = _narrow(16 * s)
        for i in range(2):
            ly = y + h // 4 + i * h // 2
            draw.text((x + 16 * s, ly), "—", font=font, fill=TEXT_DIM, anchor="lm")
        draw.line([x + 10 * s, y + h // 2, x + w - 10 * s, y + h // 2], fill=CARD_BORDER, width=max(1, s // 2))
        return

    winner = (series.get("winner_name") or "").strip().lower()
    decided = bool(winner)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=CARD_BG,
                           outline=accent if not decided else CARD_BORDER, width=max(1, s))
    draw.line([x + 10 * s, y + h // 2, x + w - 10 * s, y + h // 2], fill=CARD_BORDER, width=max(1, s // 2))

    name_font = _narrow(17 * s)
    score_font = _narrow(19 * s)
    logo_d = 22 * s
    rows = (
        (series.get("team1_name") or "", series.get("team1_wins") or 0),
        (series.get("team2_name") or "", series.get("team2_wins") or 0),
    )
    for i, (team, wins) in enumerate(rows):
        cy = y + h // 4 + i * h // 2
        is_winner = decided and team.strip().lower() == winner
        is_loser = decided and not is_winner
        if is_winner:
            draw.rectangle([x + 2 * s, cy - h // 4 + 5 * s, x + 5 * s, cy + h // 4 - 5 * s], fill=accent)
        _paste_logo(img, draw, team, x + 22 * s, cy, logo_d, accent)
        name_color = TEXT_DIM if is_loser else TEXT_PRIMARY
        score_color = accent if is_winner else (TEXT_DIM if is_loser else TEXT_MUTED)
        name = _fit(draw, team, name_font, w - 80 * s)
        draw.text((x + 40 * s, cy), name, font=name_font, fill=name_color, anchor="lm")
        draw.text((x + w - 14 * s, cy), str(wins), font=score_font, fill=score_color, anchor="rm")


def _draw_trophy(draw, cx: int, top: int, size: int, color) -> None:
    """Кубок из примитивов: чаша, ручки, ножка, подставка."""
    cup_w = size
    cup_h = int(size * 0.62)
    x0 = cx - cup_w // 2
    draw.pieslice([x0, top - cup_h, x0 + cup_w, top + cup_h], 0, 180, fill=color)
    hw = max(2, size // 10)
    draw.arc([x0 - size // 4, top + size // 12, x0 + size // 6, top + size // 2], 90, 270, fill=color, width=hw)
    draw.arc([x0 + cup_w - size // 6, top + size // 12, x0 + cup_w + size // 4, top + size // 2], 270, 90, fill=color, width=hw)
    stem_w = max(4, size // 7)
    draw.rectangle([cx - stem_w // 2, top + cup_h, cx + stem_w // 2, top + int(size * 0.86)], fill=color)
    base_w = int(size * 0.62)
    draw.rounded_rectangle([cx - base_w // 2, top + int(size * 0.86), cx + base_w // 2, top + size],
                           radius=max(2, size // 20), fill=color)


def generate_cup_bracket_image(
    bracket: list[dict],
    title: str,
    division_id: int | None = None,
    subtitle: str | None = None,
) -> io.BytesIO:
    """Отрисовать сетку кубка.

    `bracket` — `database.get_cup_full_bracket(...)`: этапы по порядку, у каждого
    `stage` (строка этапа) и `series` (серии по номерам). Пустой кубок рисуется
    заглушкой «Сетка ещё не заведена» — закреп в теме нужен и до жеребьёвки.
    """
    s = SCALE
    accent = GENERAL_ACCENT if division_id is None else resolve_theme(division_id=division_id).accent
    columns = _plan_columns(bracket)
    first_count = len(columns[0]["series"]) if columns else 0
    champion = _champion(columns)

    body_h = max(first_count, 2) * (BOX_H + BOX_GAP) - BOX_GAP
    width_1x = PAD_X * 2 + max(1, len(columns)) * BOX_W + max(0, len(columns) - 1) * COL_GAP
    width_1x += COL_GAP + CHAMP_W if columns else 0
    width_1x = max(width_1x, 720)
    height_1x = HEADER_H + STAGE_LABEL_H + body_h + FOOTER_H + 24

    img = Image.new("RGB", (width_1x * s, height_1x * s), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Шапка: акцентная полоса, трофей, название и подпись
    draw.rectangle([0, 0, width_1x * s, 6 * s], fill=accent)
    _draw_trophy(draw, (PAD_X + 22) * s, 34 * s, 44 * s, accent)
    title_font = _narrow(40 * s)
    draw.text(((PAD_X + 72) * s, 52 * s), title.upper(), font=title_font, fill=TEXT_PRIMARY, anchor="lm")
    if subtitle:
        draw.text(((PAD_X + 74) * s, 90 * s), subtitle, font=load_font(16 * s), fill=TEXT_MUTED, anchor="lm")

    if not columns:
        draw.text((width_1x * s // 2, (HEADER_H + 70) * s), "Сетка ещё не заведена",
                  font=_narrow(26 * s), fill=TEXT_MUTED, anchor="mm")
        return _to_png(img)

    top = (HEADER_H + STAGE_LABEL_H) * s
    label_font = _narrow(18 * s)
    centers: list[list[int]] = []  # центр каждой серии каждой колонки (y, в пикселях)
    for ci, col in enumerate(columns):
        x = (PAD_X + ci * (BOX_W + COL_GAP)) * s
        stage_row = col["row"] or {}
        label_color = accent if stage_row.get("is_open") else TEXT_MUTED
        draw.text((x, (HEADER_H + 14) * s), stage_caption(col["stage"]).upper(),
                  font=label_font, fill=label_color, anchor="lm")

        count = len(col["series"])
        prev = centers[-1] if centers else None
        # Серия k следующего этапа — между сериями 2k и 2k+1 предыдущего (при
        # нечётном числе последняя проходит напрямую). Если этапы не делятся 2:1
        # (сетку завели вручную как попало), серии просто равномерно по высоте.
        linked = prev is not None and len(prev) in (2 * count, 2 * count - 1)
        col_centers = []
        for k in range(count):
            if linked:
                feeders = [prev[i] for i in (2 * k, 2 * k + 1) if i < len(prev)]
                cy = sum(feeders) // len(feeders)
            elif prev is None and count == first_count:
                cy = top + k * (BOX_H + BOX_GAP) * s + BOX_H * s // 2
            else:
                slot = body_h * s / count
                cy = int(top + slot * k + slot / 2)
            col_centers.append(cy)

        # Линии от пар предыдущего этапа к этой серии
        if linked:
            px = x - COL_GAP * s
            mid_x = x - COL_GAP * s // 2
            lw = max(2, s)
            for k, cy in enumerate(col_centers):
                feeders = [prev[i] for i in (2 * k, 2 * k + 1) if i < len(prev)]
                for fy in feeders:
                    draw.line([px, fy, mid_x, fy], fill=LINE_COLOR, width=lw)
                draw.line([mid_x, min(feeders + [cy]), mid_x, max(feeders + [cy])], fill=LINE_COLOR, width=lw)
                draw.line([mid_x, cy, x, cy], fill=LINE_COLOR, width=lw)

        for k, series in enumerate(col["series"]):
            _draw_series_box(img, draw, series, x, col_centers[k] - BOX_H * s // 2, accent, s)
        centers.append(col_centers)

    # Чемпион справа от финала
    fx = (PAD_X + len(columns) * (BOX_W + COL_GAP)) * s
    fy = centers[-1][0]
    draw.line([fx - COL_GAP * s, fy, fx, fy], fill=accent if champion else LINE_COLOR, width=max(2, s))
    ch = 120 * s
    cw = CHAMP_W * s
    draw.rounded_rectangle([fx, fy - ch // 2, fx + cw, fy + ch // 2], radius=14 * s,
                           fill=CARD_BG if champion else CARD_BG_EMPTY,
                           outline=accent if champion else CARD_BORDER, width=max(2, s))
    _draw_trophy(draw, fx + cw // 2, fy - ch // 2 + 16 * s, 34 * s, accent if champion else TEXT_DIM)
    draw.text((fx + cw // 2, fy + 18 * s), "ОБЛАДАТЕЛЬ", font=_narrow(14 * s), fill=TEXT_MUTED, anchor="mm")
    champ_font = _narrow(20 * s)
    champ_text = _fit(draw, champion or "—", champ_font, cw - 20 * s)
    draw.text((fx + cw // 2, fy + 42 * s), champ_text, font=champ_font,
              fill=accent if champion else TEXT_DIM, anchor="mm")

    draw.text((PAD_X * s, (height_1x - FOOTER_H // 2 - 6) * s), "Серии до 2 побед · Логово Фифарей",
              font=load_font(14 * s), fill=TEXT_DIM, anchor="lm")
    return _to_png(img)


def _to_png(img: Image.Image) -> io.BytesIO:
    w, h = img.size
    if w + h > MAX_SIDES_SUM:
        k = MAX_SIDES_SUM / (w + h)
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "cup_bracket.png"
    return buf
