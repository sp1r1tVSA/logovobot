"""
services/graphics/round_digest_generator.py

Картинка «Итоги тура» для топика АНАЛИТИКА: результаты тура с эмблемами,
игрок тура, разгром тура и движение команд по таблице.

Рендер построен по тем же правилам, что и table_generator: 2x supersampling,
общий load_font/get_team_logo_filename, тёмная бродкаст-палитра, на выходе
io.BytesIO с PNG.
"""

import io
import os

from PIL import Image, ImageDraw

from services.graphics.division_theme import draw_division_badge, resolve_theme
from services.graphics.table_generator import (
    LOGOS_DIR,
    SCALE,
    clean_and_prepare_logo,
    get_team_logo_filename,
    load_font,
    resize_logo_proportional,
)

# Палитра — та же, что у турнирной таблицы
BG_COLOR = (20, 20, 22)
CARD_BG = (26, 26, 30)
CARD_BG_ALT = (32, 32, 38)
TEXT_PRIMARY = (255, 255, 255)
TEXT_MUTED = (209, 213, 219)
TEXT_HEADER = (156, 163, 175)
RED_ACCENT = (239, 68, 68)
GREEN = (34, 197, 94)
GOLD = (250, 204, 21)
SEPARATOR = (45, 45, 52)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Обрезать строку по ширине, добавив многоточие: длинное имя иначе
    наезжает на эмблемы соседних команд."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def _draw_logo_badge(img: Image.Image, draw: ImageDraw.ImageDraw, team_name: str, center_x: int, center_y: int, diameter: int) -> None:
    """Белый круглый бейдж с эмблемой клуба по центру заданной точки."""
    x0 = center_x - diameter // 2
    y0 = center_y - diameter // 2
    draw.ellipse([x0, y0, x0 + diameter, y0 + diameter], fill=(255, 255, 255))

    logo_filename = get_team_logo_filename(team_name) or "default.png"
    logo_path = os.path.join(LOGOS_DIR, logo_filename)
    if not os.path.exists(logo_path):
        return
    try:
        clean_logo = clean_and_prepare_logo(Image.open(logo_path))
        inner = int(diameter * 0.78)
        logo_img, lw, lh = resize_logo_proportional(clean_logo, inner, inner)
        img.paste(logo_img, (x0 + (diameter - lw) // 2, y0 + (diameter - lh) // 2), logo_img)
    except Exception:
        pass


def generate_round_digest_image(payload: dict) -> io.BytesIO:
    """
    Отрисовать итоги тура по payload из services.round_preview.build_digest_payload.
    Возвращает io.BytesIO с PNG.
    """
    results = payload.get("results") or []
    movers = payload.get("movers") or []
    potr = payload.get("player_of_the_round")
    rout = payload.get("rout")

    # 1x-геометрия
    width_1x = 1000
    header_h_1x = 120
    result_row_1x = 66
    block_gap_1x = 26
    highlight_h_1x = 100 if (potr or rout) else 0
    movers_h_1x = (46 + len(movers) * 34) if movers else 0
    footer_1x = 40

    height_1x = (
        header_h_1x
        + max(len(results), 1) * result_row_1x
        + block_gap_1x
        + highlight_h_1x
        + (block_gap_1x if movers else 0)
        + movers_h_1x
        + footer_1x
    )

    width = width_1x * SCALE
    height = height_1x * SCALE

    img = Image.new("RGBA", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_title = load_font(24 * SCALE, bold=True)
    font_subtitle = load_font(14 * SCALE)
    font_section = load_font(13 * SCALE, bold=True)
    font_team = load_font(16 * SCALE, bold=True)
    font_score = load_font(22 * SCALE, bold=True)
    font_row = load_font(14 * SCALE)
    font_row_bold = load_font(14 * SCALE, bold=True)
    font_badge = load_font(12 * SCALE, bold=True)
    font_mvp = load_font(11 * SCALE, bold=True)

    margin = 35 * SCALE
    inner_right = width - margin

    # Акцент дивизиона: только заголовок и плашка. Красный у «РАЗГРОМА ТУРА» и
    # стрелки падения остаётся семантическим и теме не подчиняется.
    theme = resolve_theme(
        division_id=payload.get("division_id"),
        division_name=payload.get("division_name"),
    )

    # ─── Шапка ───
    draw.text((margin, 26 * SCALE), f"ИТОГИ ТУРА {payload.get('round_number', '')}", fill=theme.accent, font=font_title)
    draw.text(
        (margin, 62 * SCALE),
        str(payload.get("division_name") or ""),
        fill=TEXT_HEADER,
        font=font_subtitle,
    )
    draw_division_badge(draw, theme, inner_right, 26 * SCALE, font_badge, SCALE)
    summary = f"Матчей: {payload.get('matches_played', 0)}  •  Голов: {payload.get('goals_total', 0)}"
    draw.text((inner_right, 62 * SCALE), summary, fill=TEXT_HEADER, font=font_subtitle, anchor="ra")

    y = header_h_1x * SCALE - 16 * SCALE
    draw.line([(margin, y), (inner_right, y)], fill=SEPARATOR, width=1 * SCALE)

    # ─── Результаты ───
    y = header_h_1x * SCALE
    row_h = result_row_1x * SCALE
    badge = 34 * SCALE

    if not results:
        draw.text((width // 2, y + row_h // 2), "Нет подтверждённых матчей", fill=TEXT_MUTED, font=font_row, anchor="mm")
        y += row_h
    else:
        for idx, r in enumerate(results):
            bg = CARD_BG if idx % 2 == 0 else BG_COLOR
            draw.rectangle([(margin, y), (inner_right, y + row_h - 4 * SCALE)], fill=bg)
            y_center = y + (row_h - 4 * SCALE) // 2

            center = width // 2
            # Левая команда: название справа налево от бейджа
            _draw_logo_badge(img, draw, r["team1"], center - 180 * SCALE, y_center, badge)
            draw.text((center - 210 * SCALE, y_center), str(r["team1"]), fill=TEXT_PRIMARY, font=font_team, anchor="rm")

            # Правая команда
            _draw_logo_badge(img, draw, r["team2"], center + 180 * SCALE, y_center, badge)
            draw.text((center + 210 * SCALE, y_center), str(r["team2"]), fill=TEXT_PRIMARY, font=font_team, anchor="lm")

            # Счёт; при наличии короны он поднимается, освобождая строку под имя MVP
            score_color = GOLD if (rout and r.get("match_id") == rout.get("match_id")) else TEXT_PRIMARY
            mvp_name = str(r.get("mvp_player") or "").strip()
            score_y = y_center - 9 * SCALE if mvp_name else y_center
            draw.text((center, score_y), f"{r['score1']} : {r['score2']}", fill=score_color, font=font_score, anchor="mm")

            if mvp_name:
                # Эмодзи в Pillow не гарантированы (на сервере рисуется «тофу»),
                # поэтому корона обозначается подписью и золотым цветом.
                label = _fit_text(draw, f"MVP · {mvp_name}", font_mvp, 300 * SCALE)
                draw.text((center, y_center + 14 * SCALE), label, fill=GOLD, font=font_mvp, anchor="mm")

            y += row_h

    # ─── Игрок тура / разгром тура ───
    if potr or rout:
        y += block_gap_1x * SCALE
        card_h = (highlight_h_1x - 10) * SCALE
        half = (inner_right - margin - 16 * SCALE) // 2

        # Emoji-шрифт в Pillow не гарантирован (на сервере рисуется «тофу»),
        # поэтому акцент даётся цветной полосой слева, а не иконкой.
        def _highlight_card(x_left: int, x_right: int, accent, label: str, headline: str, sub: str) -> None:
            draw.rectangle([(x_left, y), (x_right, y + card_h)], fill=CARD_BG_ALT)
            draw.rectangle([(x_left, y), (x_left + 4 * SCALE, y + card_h)], fill=accent)
            draw.text((x_left + 20 * SCALE, y + 16 * SCALE), label, fill=accent, font=font_section)
            draw.text((x_left + 20 * SCALE, y + 40 * SCALE), headline, fill=TEXT_PRIMARY, font=font_team)
            draw.text((x_left + 20 * SCALE, y + 64 * SCALE), sub, fill=TEXT_MUTED, font=font_row)

        if potr:
            # Лучший по Г+П и обладатель корон — часто один и тот же игрок;
            # тогда награды дописываются к его строке, а не спорят с ней.
            round_mvp = payload.get("mvp_of_the_round") or {}
            sub = f"{potr.get('team_name') or ''}  •  {potr.get('goals', 0)} г + {potr.get('assists', 0)} п"
            round_mvp_name = str(round_mvp.get("player_name") or "").strip()
            if round_mvp_name and round_mvp_name.lower() == str(potr.get("player_name") or "").strip().lower():
                sub += f"  •  MVP x{round_mvp.get('mvp_count', 0)}"
            _highlight_card(
                margin, margin + half, GOLD, "ИГРОК ТУРА",
                str(potr.get("player_name") or ""),
                sub,
            )

        if rout:
            _highlight_card(
                margin + half + 16 * SCALE, inner_right, RED_ACCENT, "РАЗГРОМ ТУРА",
                f"{rout['team1']} {rout['score1']}:{rout['score2']} {rout['team2']}",
                f"Разница: {rout.get('margin', 0)} мяча",
            )

        y += card_h

    # ─── Движение по таблице ───
    if movers:
        y += block_gap_1x * SCALE
        draw.text((margin, y), "ДВИЖЕНИЕ В ТАБЛИЦЕ", fill=TEXT_HEADER, font=font_section)
        y += 26 * SCALE
        draw.line([(margin, y), (inner_right, y)], fill=SEPARATOR, width=1 * SCALE)
        y += 8 * SCALE

        for mv in movers:
            movement = mv.get("movement", 0)
            arrow = "▲" if movement > 0 else "▼"
            color = GREEN if movement > 0 else RED_ACCENT
            draw.text((margin, y), arrow, fill=color, font=font_row_bold)
            draw.text((margin + 26 * SCALE, y), str(mv.get("team") or ""), fill=TEXT_PRIMARY, font=font_row_bold)
            draw.text(
                (inner_right, y),
                f"{mv.get('previous_position')} → {mv.get('position')}   ({mv.get('points', 0)} очк.)",
                fill=TEXT_MUTED,
                font=font_row,
                anchor="ra",
            )
            y += 34 * SCALE

    resampled = img.resize((width_1x, height_1x), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    resampled.save(buffer, format="PNG", quality=95)
    buffer.seek(0)
    return buffer
