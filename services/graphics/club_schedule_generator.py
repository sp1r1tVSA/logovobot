import os
import io
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from services.graphics.division_theme import draw_division_badge, resolve_theme
from services.graphics.table_generator import get_team_logo_filename, load_font, clean_and_prepare_logo, resize_logo_proportional

# Project root directory (services/graphics -> services -> root)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = str(PROJECT_ROOT)
LOGOS_DIR = str(PROJECT_ROOT / "assets" / "logos")

SCALE = 2

# Card dimensions (1x base)
CARD_WIDTH_1X   = 760
CARD_PADDING_1X = 28

CARD_WIDTH   = CARD_WIDTH_1X * SCALE
CARD_PADDING = CARD_PADDING_1X * SCALE

# Colors
BG_GRAD_TOP    = (14, 16, 22)        # #0E1016
BG_GRAD_BOT    = (20, 23, 32)        # #141720
SURFACE_COLOR  = (25, 29, 40)        # #191D28
SURFACE_ALT    = (20, 24, 34)        # #141822
BORDER_COLOR   = (46, 52, 70)        # #2E3446
BORDER_LIGHT   = (65, 74, 98)        # #414A62

CUP_GOLD       = (251, 191, 36)      # #FBBF24
WHITE          = (255, 255, 255)
MUTED          = (148, 163, 184)     # #94A3B8
TEXT_SECONDARY = (203, 213, 225)     # #CBD5E1

WIN_COLOR      = (34, 197, 94)       # #22C55E  green
DRAW_COLOR     = (245, 158, 11)      # #F59E0B  amber
LOSS_COLOR     = (239, 68, 68)       # #EF4444  red
ACCENT_CYAN    = (56, 189, 248)      # #38BDF8  sky blue
PENDING_COLOR  = (148, 163, 184)     # #94A3B8  gray


def _draw_rounded_rect(draw: ImageDraw.ImageDraw, xy: tuple, radius: int, fill: tuple, outline: tuple | None = None, width: int = 1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _draw_vertical_gradient(img: Image.Image, top_color: tuple, bot_color: tuple):
    """Draw smooth vertical gradient across image."""
    w, h = img.size
    draw = ImageDraw.Draw(img)
    for y in range(h):
        ratio = y / max(h - 1, 1)
        r = int(top_color[0] + (bot_color[0] - top_color[0]) * ratio)
        g = int(top_color[1] + (bot_color[1] - top_color[1]) * ratio)
        b = int(top_color[2] + (bot_color[2] - top_color[2]) * ratio)
        draw.line([(0, y), (w, y)], fill=(r, g, b, 255))


def _draw_ball_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 14 * SCALE):
    """Draw a clean vector soccer ball icon."""
    draw.ellipse((x, y, x + size, y + size), fill=(245, 248, 255), outline=(100, 115, 140), width=1)
    cx, cy = x + size / 2, y + size / 2
    p_r = size * 0.28
    draw.polygon([
        (cx, cy - p_r),
        (cx + p_r * 0.95, cy - p_r * 0.31),
        (cx + p_r * 0.59, cy + p_r * 0.81),
        (cx - p_r * 0.59, cy + p_r * 0.81),
        (cx - p_r * 0.95, cy - p_r * 0.31),
    ], fill=(24, 28, 38))


def _draw_trophy_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 14 * SCALE, color: tuple = CUP_GOLD):
    """Draw a vector gold trophy icon."""
    bowl_top_w = size * 0.7
    bowl_bot_w = size * 0.35
    bowl_h = size * 0.45
    cx = x + size / 2

    draw.polygon([
        (cx - bowl_top_w / 2, y + 2 * SCALE),
        (cx + bowl_top_w / 2, y + 2 * SCALE),
        (cx + bowl_bot_w / 2, y + bowl_h),
        (cx - bowl_bot_w / 2, y + bowl_h),
    ], fill=color)

    draw.arc((x, y + 2 * SCALE, x + size * 0.35, y + bowl_h * 0.85), start=90, end=270, fill=color, width=2 * SCALE)
    draw.arc((x + size * 0.65, y + 2 * SCALE, x + size, y + bowl_h * 0.85), start=270, end=90, fill=color, width=2 * SCALE)

    draw.rectangle((cx - 1 * SCALE, y + bowl_h, cx + 1 * SCALE, y + size * 0.75), fill=color)
    base_w = size * 0.55
    draw.rounded_rectangle((cx - base_w / 2, y + size * 0.75, cx + base_w / 2, y + size), radius=2 * SCALE, fill=color)


def _draw_calendar_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 14 * SCALE, color: tuple = TEXT_SECONDARY):
    """Draw a vector calendar icon."""
    draw.rounded_rectangle((x, y, x + size, y + size), radius=3 * SCALE, outline=color, width=1 * SCALE)
    draw.line([(x, y + size * 0.32), (x + size, y + size * 0.32)], fill=color, width=1 * SCALE)
    draw.line([(x + size * 0.3, y - 2 * SCALE), (x + size * 0.3, y + size * 0.2)], fill=color, width=1 * SCALE)
    draw.line([(x + size * 0.7, y - 2 * SCALE), (x + size * 0.7, y + size * 0.2)], fill=color, width=1 * SCALE)


_logo_cache: dict[str, tuple[Image.Image, int, int]] = {}


def _get_club_logo(team_name: str, max_w: int, max_h: int | None = None) -> tuple[Image.Image | None, int, int]:
    """Retrieve proportionally resized club logo without distortion."""
    if not team_name:
        return None, 0, 0
    if max_h is None:
        max_h = max_w
    key = f"{team_name.lower()}_{max_w}_{max_h}"
    if key in _logo_cache:
        return _logo_cache[key]

    logo_file = get_team_logo_filename(team_name)
    if not logo_file:
        return None, 0, 0

    full_path = os.path.join(LOGOS_DIR, logo_file)
    if not os.path.exists(full_path):
        return None, 0, 0

    try:
        raw = Image.open(full_path)
        clean = clean_and_prepare_logo(raw)
        resized, rw, rh = resize_logo_proportional(clean, max_w, max_h)
        _logo_cache[key] = (resized, rw, rh)
        return resized, rw, rh
    except Exception:
        return None, 0, 0


def generate_club_schedule(data: dict, max_matches: int = 12, division_id: int | None = None) -> io.BytesIO:
    """
    Generate high-resolution 2x supersampled Schedule & Results Card.

    division_id picks the division accent (top line + pill). Without it the club
    itself is used to look the division up, so old call sites keep working.
    """
    team_name     = data.get("team_name", "Клуб")
    played_count  = data.get("played_count", 0)
    pending_count = data.get("pending_count", 0)
    matches       = (data.get("matches") or [])[:max_matches]

    # Акцент дивизиона: верхняя линия и плашка. Цвета результатов матчей
    # (победа/ничья/поражение) семантические и темой не трогаются.
    theme = resolve_theme(
        division_id=division_id,
        division_name=data.get("division_name"),
        team_name=team_name,
    )

    # ── Fonts ──────────────────────────────────────────────────────────────
    font_title    = load_font(26 * SCALE, bold=True)
    font_sub      = load_font(13 * SCALE)
    font_badge    = load_font(13 * SCALE, bold=True)
    font_badge_sm = load_font(11 * SCALE, bold=True)
    font_team_hd  = load_font(14 * SCALE, bold=True)
    font_score    = load_font(18 * SCALE, bold=True)
    font_scorers  = load_font(11 * SCALE)
    font_sm       = load_font(11 * SCALE)

    # ── Sizing calculations ────────────────────────────────────────────────
    HEADER_H = 88 * SCALE
    FOOTER_H = 30 * SCALE

    row_heights = []
    for m in matches:
        if m.get("subline") or m.get("scorers"):
            row_heights.append(76 * SCALE)
        else:
            row_heights.append(58 * SCALE)

    if not matches:
        matches_block_h = 100 * SCALE
    else:
        matches_block_h = sum(row_heights) + (len(matches) - 1) * (10 * SCALE)

    TOTAL_HEIGHT = CARD_PADDING * 2 + HEADER_H + matches_block_h + FOOTER_H + 36 * SCALE

    img = Image.new("RGBA", (CARD_WIDTH, TOTAL_HEIGHT))
    _draw_vertical_gradient(img, BG_GRAD_TOP, BG_GRAD_BOT)
    draw = ImageDraw.Draw(img)

    # Accent Top Bar
    draw.line([(CARD_PADDING, 4 * SCALE), (CARD_WIDTH - CARD_PADDING, 4 * SCALE)], fill=theme.accent, width=3 * SCALE)

    curr_y = CARD_PADDING

    # ── 1. HEADER (Club Logo + Title + Stats Pill) ─────────────────────────
    logo_size = 68 * SCALE
    logo_img, lw, lh = _get_club_logo(team_name, logo_size - 10 * SCALE, logo_size - 10 * SCALE)
    logo_x = CARD_PADDING
    logo_y = curr_y + 4 * SCALE

    _draw_rounded_rect(draw, (logo_x, logo_y, logo_x + logo_size, logo_y + logo_size),
                       radius=14 * SCALE, fill=(20, 24, 34), outline=BORDER_LIGHT, width=2)

    if logo_img:
        img.paste(logo_img, (logo_x + (logo_size - lw) // 2, logo_y + (logo_size - lh) // 2), logo_img)
    else:
        inits = (team_name[:2]).upper()
        bbox = draw.textbbox((0, 0), inits, font=font_title)
        draw.text((logo_x + (logo_size - (bbox[2] - bbox[0])) // 2,
                   logo_y + (logo_size - (bbox[3] - bbox[1])) // 2), inits, font=font_title, fill=WHITE)

    text_x = logo_x + logo_size + 18 * SCALE
    draw.text((text_x, curr_y + 2 * SCALE), team_name.upper(), font=font_title, fill=WHITE)

    _draw_calendar_icon(draw, text_x, curr_y + 45 * SCALE, size=13 * SCALE, color=TEXT_SECONDARY)
    sub_title = "РАСПИСАНИЕ И РЕЗУЛЬТАТЫ МАТЧЕЙ • СЕЗОН 2026"
    draw.text((text_x + 18 * SCALE, curr_y + 44 * SCALE), sub_title, font=font_sub, fill=TEXT_SECONDARY)

    # Right Stats Pill (Played / Upcoming)
    stat_pill_w = 170 * SCALE
    stat_pill_h = 52 * SCALE
    stat_pill_x = CARD_WIDTH - CARD_PADDING - stat_pill_w
    stat_pill_y = curr_y + 8 * SCALE
    _draw_rounded_rect(draw, (stat_pill_x, stat_pill_y, stat_pill_x + stat_pill_w, stat_pill_y + stat_pill_h),
                       radius=10 * SCALE, fill=SURFACE_COLOR, outline=BORDER_COLOR, width=2)

    sp_text1 = f"СЫГРАНО: {played_count}"
    sp_text2 = f"ПРЕДСТОИТ: {pending_count}"
    draw.text((stat_pill_x + 14 * SCALE, stat_pill_y + 8 * SCALE), sp_text1, font=font_badge_sm, fill=CUP_GOLD)
    draw.text((stat_pill_x + 14 * SCALE, stat_pill_y + 28 * SCALE), sp_text2, font=font_badge_sm, fill=WHITE)

    # Плашка дивизиона под пилюлей со статистикой
    draw_division_badge(draw, theme, CARD_WIDTH - CARD_PADDING, stat_pill_y + stat_pill_h + 6 * SCALE, font_badge_sm, SCALE)

    curr_y += HEADER_H + 12 * SCALE

    # ── 2. MATCH FIXTURE ROWS ──────────────────────────────────────────────
    if not matches:
        _draw_rounded_rect(draw, (CARD_PADDING, curr_y, CARD_WIDTH - CARD_PADDING, curr_y + 90 * SCALE),
                           radius=12 * SCALE, fill=SURFACE_COLOR, outline=BORDER_COLOR, width=2)
        draw.text((CARD_PADDING + 24 * SCALE, curr_y + 34 * SCALE), "Матчи и расписание пока отсутствуют.", font=font_sub, fill=MUTED)
        curr_y += 100 * SCALE
    else:
        for idx, m in enumerate(matches):
            rh = row_heights[idx]
            _draw_rounded_rect(draw, (CARD_PADDING, curr_y, CARD_WIDTH - CARD_PADDING, curr_y + rh),
                               radius=12 * SCALE, fill=SURFACE_COLOR, outline=BORDER_COLOR, width=1)

            # Left Tour / Stage Badge
            tour_title = m.get("tour_title", f"ТУР {m.get('round_number', 1)}")
            is_cup = m.get("is_cup", False)
            tour_badge_w = 126 * SCALE
            tour_badge_h = 30 * SCALE
            tour_badge_x = CARD_PADDING + 14 * SCALE
            tour_badge_y = curr_y + 14 * SCALE

            if is_cup:
                tb_fill = (38, 32, 20)
                tb_border = CUP_GOLD
                tb_text_color = CUP_GOLD
                _draw_rounded_rect(draw, (tour_badge_x, tour_badge_y, tour_badge_x + tour_badge_w, tour_badge_y + tour_badge_h),
                                   radius=6 * SCALE, fill=tb_fill, outline=tb_border, width=1)
                _draw_trophy_icon(draw, tour_badge_x + 6 * SCALE, tour_badge_y + 8 * SCALE, size=13 * SCALE, color=CUP_GOLD)
                tb_text_x = tour_badge_x + 22 * SCALE
            else:
                tb_fill = (20, 26, 38)
                tb_border = (56, 189, 248) if m.get("status") == "confirmed" else BORDER_LIGHT
                tb_text_color = (224, 242, 254) if m.get("status") == "confirmed" else MUTED
                _draw_rounded_rect(draw, (tour_badge_x, tour_badge_y, tour_badge_x + tour_badge_w, tour_badge_y + tour_badge_h),
                                   radius=6 * SCALE, fill=tb_fill, outline=tb_border, width=1)
                _draw_ball_icon(draw, tour_badge_x + 6 * SCALE, tour_badge_y + 8 * SCALE, size=13 * SCALE)
                tb_text_x = tour_badge_x + 22 * SCALE

            draw.text((tb_text_x, tour_badge_y + 7 * SCALE), tour_title, font=font_badge_sm, fill=tb_text_color)

            # Center Match Display: Home Team - Score - Away Team
            center_x = (CARD_WIDTH) // 2 + 10 * SCALE
            home_t = m.get("home_team", "Клуб 1")
            away_t = m.get("away_team", "Клуб 2")
            h_score = m.get("home_score")
            a_score = m.get("away_score")
            status  = m.get("status", "pending")
            outcome = m.get("outcome", "PENDING")

            # Score / Status Badge
            score_pill_w = 78 * SCALE
            score_pill_h = 34 * SCALE
            score_pill_x = center_x - score_pill_w // 2
            score_pill_y = curr_y + 12 * SCALE

            if status == "confirmed" and h_score is not None and a_score is not None:
                score_str = f"{h_score} : {a_score}"
                sp_bg = (18, 24, 36)
                sp_border = ACCENT_CYAN
                sp_color = WHITE
            else:
                score_str = "VS"
                sp_bg = (28, 32, 44)
                sp_border = BORDER_LIGHT
                sp_color = MUTED

            _draw_rounded_rect(draw, (score_pill_x, score_pill_y, score_pill_x + score_pill_w, score_pill_y + score_pill_h),
                               radius=8 * SCALE, fill=sp_bg, outline=sp_border, width=1)
            s_bbox = draw.textbbox((0, 0), score_str, font=font_score)
            draw.text((score_pill_x + (score_pill_w - (s_bbox[2] - s_bbox[0])) // 2,
                       score_pill_y + (score_pill_h - (s_bbox[3] - s_bbox[1])) // 2),
                      score_str, font=font_score, fill=sp_color)

            # Home Team (left of score pill)
            home_logo_size = 28 * SCALE
            home_logo, hlw, hlh = _get_club_logo(home_t, home_logo_size, home_logo_size)
            home_text_end_x = score_pill_x - 14 * SCALE

            ht_bbox = draw.textbbox((0, 0), home_t, font=font_team_hd)
            ht_w = ht_bbox[2] - ht_bbox[0]
            draw.text((home_text_end_x - ht_w, curr_y + 18 * SCALE), home_t, font=font_team_hd, fill=WHITE if home_t.lower() == team_name.lower() else TEXT_SECONDARY)
            
            if home_logo:
                img.paste(home_logo, (home_text_end_x - ht_w - home_logo_size - 8 * SCALE + (home_logo_size - hlw) // 2, curr_y + 15 * SCALE + (home_logo_size - hlh) // 2), home_logo)

            # Away Team (right of score pill)
            away_logo_size = 28 * SCALE
            away_logo, alw, alh = _get_club_logo(away_t, away_logo_size, away_logo_size)
            away_text_start_x = score_pill_x + score_pill_w + 14 * SCALE

            if away_logo:
                img.paste(away_logo, (away_text_start_x + (away_logo_size - alw) // 2, curr_y + 15 * SCALE + (away_logo_size - alh) // 2), away_logo)
                away_name_x = away_text_start_x + away_logo_size + 8 * SCALE
            else:
                away_name_x = away_text_start_x

            draw.text((away_name_x, curr_y + 18 * SCALE), away_t, font=font_team_hd, fill=WHITE if away_t.lower() == team_name.lower() else TEXT_SECONDARY)

            # Outcome badge on the far right
            out_w = 98 * SCALE
            out_h = 28 * SCALE
            out_x = CARD_WIDTH - CARD_PADDING - 14 * SCALE - out_w
            out_y = curr_y + 15 * SCALE

            if outcome == "W":
                out_fill, out_lbl = (20, 48, 30), "ПОБЕДА"
                out_txt_c = WIN_COLOR
                dot_c = WIN_COLOR
            elif outcome == "D":
                out_fill, out_lbl = (48, 40, 20), "НИЧЬЯ"
                out_txt_c = DRAW_COLOR
                dot_c = DRAW_COLOR
            elif outcome == "L":
                out_fill, out_lbl = (48, 20, 20), "ПОРАЖЕНИЕ"
                out_txt_c = LOSS_COLOR
                dot_c = LOSS_COLOR
            elif outcome == "IN_PROGRESS":
                out_fill, out_lbl = (20, 36, 48), "СЕРИЯ"
                out_txt_c = ACCENT_CYAN
                dot_c = ACCENT_CYAN
            else:
                out_fill, out_lbl = (28, 32, 44), "ПРЕДСТОИТ"
                out_txt_c = MUTED
                dot_c = MUTED

            _draw_rounded_rect(draw, (out_x, out_y, out_x + out_w, out_y + out_h),
                               radius=6 * SCALE, fill=out_fill)
            
            # Draw colored dot + text
            dot_r = 3 * SCALE
            draw.ellipse((out_x + 8 * SCALE, out_y + 11 * SCALE, out_x + 8 * SCALE + dot_r * 2, out_y + 11 * SCALE + dot_r * 2), fill=dot_c)
            
            o_bbox = draw.textbbox((0, 0), out_lbl, font=font_badge_sm)
            draw.text((out_x + 18 * SCALE + (out_w - 24 * SCALE - (o_bbox[2] - o_bbox[0])) // 2,
                       out_y + (out_h - (o_bbox[3] - o_bbox[1])) // 2),
                      out_lbl, font=font_badge_sm, fill=out_txt_c)

            # Subrow: Match scores / Goalscorers with mini ball icon
            subline = m.get("subline")
            scorers = m.get("scorers") or []
            if subline:
                _draw_ball_icon(draw, CARD_PADDING + 16 * SCALE, curr_y + 50 * SCALE, size=11 * SCALE)
                draw.text((CARD_PADDING + 32 * SCALE, curr_y + 49 * SCALE), subline, font=font_scorers, fill=MUTED)
            elif scorers:
                _draw_ball_icon(draw, CARD_PADDING + 16 * SCALE, curr_y + 50 * SCALE, size=11 * SCALE)
                sc_str = f"Голы клуба: {', '.join(scorers)}"
                draw.text((CARD_PADDING + 32 * SCALE, curr_y + 49 * SCALE), sc_str, font=font_scorers, fill=MUTED)

            curr_y += rh + 10 * SCALE

    # ── 3. FOOTER ──────────────────────────────────────────────────────────
    curr_y += 6 * SCALE
    footer_text = "ЛОГОВО ФИФАРЕЙ 2026"
    f_bbox = draw.textbbox((0, 0), footer_text, font=font_sm)
    draw.text(((CARD_WIDTH - (f_bbox[2] - f_bbox[0])) // 2, curr_y), footer_text, font=font_sm, fill=MUTED)

    # ── Export ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
