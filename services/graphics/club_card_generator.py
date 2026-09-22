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
CARD_WIDTH_1X   = 740
CARD_PADDING_1X = 28

CARD_WIDTH   = CARD_WIDTH_1X * SCALE
CARD_PADDING = CARD_PADDING_1X * SCALE

# ── Color Palette (EA FC 25 / Modern Dark Broadcast) ───────────────────────
BG_GRAD_TOP    = (12, 14, 20)        # #0C0E14
BG_GRAD_BOT    = (18, 21, 30)        # #12151E
SURFACE_COLOR  = (24, 28, 38)        # #181C26
SURFACE_ALT    = (19, 22, 31)        # #13161F
BORDER_COLOR   = (42, 48, 66)        # #2A3042
BORDER_LIGHT   = (60, 70, 96)        # #3C4660

CUP_SURFACE    = (36, 30, 16)        # Gold tinted luxury surface
CUP_BORDER     = (120, 92, 30)
CUP_GOLD       = (251, 191, 36)      # #FBBF24

WHITE          = (255, 255, 255)
MUTED          = (135, 150, 172)     # #8796AC
TEXT_SECONDARY = (200, 210, 224)     # #C8D2E0

WIN_COLOR      = (34, 197, 94)       # #22C55E  green
DRAW_COLOR     = (245, 158, 11)      # #F59E0B  amber
LOSS_COLOR     = (239, 68, 68)       # #EF4444  red
ACCENT_CYAN    = (56, 189, 248)      # #38BDF8  sky blue

GOAL_COLOR     = (34, 197, 94)       # #22C55E
ASSIST_COLOR   = (56, 189, 248)      # #38BDF8

GOLD_RANK      = (251, 191, 36)      # 1st place gold
SILVER_RANK    = (203, 213, 225)     # 2nd place silver
BRONZE_RANK    = (217, 140, 74)      # 3rd place bronze


def _draw_rounded_rect(draw: ImageDraw.ImageDraw, xy: tuple, radius: int, fill: tuple, outline: tuple | None = None, width: int = 1):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _draw_vertical_gradient(img: Image.Image, top_color: tuple, bot_color: tuple):
    """Draw a smooth vertical gradient across the image background."""
    w, h = img.size
    draw = ImageDraw.Draw(img)
    for y in range(h):
        ratio = y / max(h - 1, 1)
        r = int(top_color[0] + (bot_color[0] - top_color[0]) * ratio)
        g = int(top_color[1] + (bot_color[1] - top_color[1]) * ratio)
        b = int(top_color[2] + (bot_color[2] - top_color[2]) * ratio)
        draw.line([(0, y), (w, y)], fill=(r, g, b, 255))


def _clean_white_background_if_needed(img: Image.Image) -> Image.Image:
    """Remove rectangular white backgrounds from logos and convert to alpha transparent."""
    img = img.convert("RGBA")
    w, h = img.size
    if w == 0 or h == 0:
        return img

    corners = [img.getpixel((0, 0)), img.getpixel((w - 1, 0)), img.getpixel((0, h - 1)), img.getpixel((w - 1, h - 1))]
    has_white_corner = any(c[0] > 240 and c[1] > 240 and c[2] > 240 and c[3] > 200 for c in corners)

    if has_white_corner:
        datas = img.getdata()
        new_data = []
        for item in datas:
            if item[0] > 245 and item[1] > 245 and item[2] > 245:
                new_data.append((255, 255, 255, 0))
            else:
                new_data.append(item)
        img.putdata(new_data)
    return img


def generate_club_card(data: dict, avatar_path: str | None = None, division_id: int | None = None) -> io.BytesIO:
    """
    Generate an immaculate, clean and balanced Club Stats Card.

    division_id picks the division accent (top line + pill). Without it the club
    itself is used to look the division up, so old call sites keep working.
    """
    team_name    = data.get("team_name", "Клуб")
    manager      = data.get("manager")
    mgr_name     = f"@{manager['username']}" if manager and manager.get("username") else ("Свободен" if not manager else "ID " + str(manager.get("telegram_id", "")))
    warn_count   = manager.get("warn_count", 0) if manager else 0
    l_stats      = data.get("league_stats") or {}
    form         = data.get("recent_form") or []
    cup          = data.get("cup_stats")
    top_scorers  = data.get("top_scorers") or []
    top_assists  = data.get("top_assists") or []
    squad_count  = data.get("squad_count", 0)
    debts_count  = data.get("debts_count", 0)

    # Акцент дивизиона: верхняя линия и плашка. Золото кубка, зелёный/жёлтый/
    # красный формы и цвета мест — семантические, темой не трогаются.
    theme = resolve_theme(
        division_id=division_id,
        division_name=data.get("division_name"),
        team_name=team_name,
    )

    # ── Fonts ──────────────────────────────────────────────────────────────
    font_title   = load_font(28 * SCALE, bold=True)
    font_sub     = load_font(13 * SCALE)
    font_badge   = load_font(14 * SCALE, bold=True)
    font_badge_sm= load_font(12 * SCALE, bold=True)
    font_big_num = load_font(30 * SCALE, bold=True)
    font_lbl     = load_font(11 * SCALE, bold=True)
    font_row_hd  = load_font(13 * SCALE, bold=True)
    font_row_val = load_font(13 * SCALE)
    font_pill    = load_font(13 * SCALE, bold=True)
    font_rank    = load_font(13 * SCALE, bold=True)
    font_owner   = load_font(17 * SCALE, bold=True)
    font_sm      = load_font(12 * SCALE)

    # ── Dimensions ─────────────────────────────────────────────────────────
    HEADER_H     = 100 * SCALE
    STATS_BAR_H  = 84 * SCALE
    FORM_BAR_H   = 56 * SCALE
    CUP_BAR_H    = (74 * SCALE) if cup else 0
    LEADERS_H    = 156 * SCALE
    OWNER_CARD_H = 72 * SCALE
    FOOTER_H     = 28 * SCALE

    TOTAL_HEIGHT = CARD_PADDING * 2 + HEADER_H + STATS_BAR_H + FORM_BAR_H + CUP_BAR_H + LEADERS_H + OWNER_CARD_H + FOOTER_H + 50 * SCALE

    img = Image.new("RGBA", (CARD_WIDTH, TOTAL_HEIGHT))
    _draw_vertical_gradient(img, BG_GRAD_TOP, BG_GRAD_BOT)
    draw = ImageDraw.Draw(img)

    # Top accent line
    draw.line([(CARD_PADDING, 4 * SCALE), (CARD_WIDTH - CARD_PADDING, 4 * SCALE)], fill=theme.accent, width=3 * SCALE)

    curr_y = CARD_PADDING

    # ── 1. HEADER (Club Crest Tile + Name + Clean Subline + Rank Badge) ────
    tile_size = 80 * SCALE
    logo_pad  = 7 * SCALE
    inner_logo_size = tile_size - logo_pad * 2

    tile_x = CARD_PADDING
    tile_y = curr_y + 4 * SCALE

    _draw_rounded_rect(draw, (tile_x, tile_y, tile_x + tile_size, tile_y + tile_size),
                       radius=16 * SCALE, fill=(22, 27, 38), outline=BORDER_LIGHT, width=2)

    logo_file = get_team_logo_filename(team_name)
    logo_img = None
    paste_x = tile_x + logo_pad
    paste_y = tile_y + logo_pad
    if logo_file:
        full_logo_path = os.path.join(LOGOS_DIR, logo_file)
        if os.path.exists(full_logo_path):
            try:
                raw_logo = Image.open(full_logo_path)
                clean_logo = clean_and_prepare_logo(raw_logo)
                logo_img, lw, lh = resize_logo_proportional(clean_logo, inner_logo_size, inner_logo_size)
                paste_x = tile_x + (tile_size - lw) // 2
                paste_y = tile_y + (tile_size - lh) // 2
            except Exception:
                logo_img = None

    if logo_img:
        img.paste(logo_img, (paste_x, paste_y), logo_img)
    else:
        initials = (team_name[:2]).upper()
        bbox = draw.textbbox((0, 0), initials, font=font_title)
        draw.text((tile_x + (tile_size - (bbox[2] - bbox[0])) // 2,
                   tile_y + (tile_size - (bbox[3] - bbox[1])) // 2), initials, font=font_title, fill=WHITE)

    text_x = tile_x + tile_size + 18 * SCALE

    # Club Name
    draw.text((text_x, curr_y + 6 * SCALE), team_name.upper(), font=font_title, fill=WHITE)

    # Clean Unified Subline
    debt_str = f"Долги: {debts_count}" if debts_count > 0 else "Без долгов"
    sub_text = f"Заявка: {squad_count} игр.   •   {debt_str}   •   СЕЗОН 2026"
    
    sub_y = curr_y + 50 * SCALE
    draw.text((text_x, sub_y), f"Заявка: {squad_count} игр.   •   ", font=font_sub, fill=TEXT_SECONDARY)
    
    # Measure offset for dynamic debt coloring
    w1 = draw.textlength(f"Заявка: {squad_count} игр.   •   ", font=font_sub)
    debt_color = LOSS_COLOR if debts_count > 0 else WIN_COLOR
    draw.text((text_x + w1, sub_y), debt_str, font=font_sub, fill=debt_color)
    
    w2 = draw.textlength(debt_str, font=font_sub)
    draw.text((text_x + w1 + w2, sub_y), "   •   СЕЗОН 2026", font=font_sub, fill=MUTED)

    # Rank Badge on Right
    rank = l_stats.get("rank", 0)
    pts = l_stats.get("points", 0)
    rank_text = f"#{rank} МЕСТО" if rank > 0 else "ЛИГА"
    pts_text  = f"{pts} PTS"

    badge_w = 126 * SCALE
    badge_h = 58 * SCALE
    badge_x = CARD_WIDTH - CARD_PADDING - badge_w
    badge_y = curr_y + 10 * SCALE

    _draw_rounded_rect(draw, (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
                       radius=12 * SCALE, fill=(34, 28, 16), outline=CUP_BORDER, width=2)
    
    rb_bbox = draw.textbbox((0, 0), rank_text, font=font_badge)
    draw.text((badge_x + (badge_w - (rb_bbox[2] - rb_bbox[0])) // 2, badge_y + 12 * SCALE), rank_text, font=font_badge, fill=CUP_GOLD)
    
    pb_bbox = draw.textbbox((0, 0), pts_text, font=font_badge_sm)
    draw.text((badge_x + (badge_w - (pb_bbox[2] - pb_bbox[0])) // 2, badge_y + 35 * SCALE), pts_text, font=font_badge_sm, fill=WHITE)

    # Плашка дивизиона под бейджем места
    draw_division_badge(draw, theme, CARD_WIDTH - CARD_PADDING, badge_y + badge_h + 6 * SCALE, font_badge_sm, SCALE)

    curr_y += HEADER_H + 10 * SCALE

    # ── 2. STATS TILES (6 Columns: ИГРЫ / В / Н / П / ГОЛЫ / РАЗНИЦА) ─────
    _draw_rounded_rect(draw, (CARD_PADDING, curr_y, CARD_WIDTH - CARD_PADDING, curr_y + STATS_BAR_H),
                       radius=14 * SCALE, fill=SURFACE_COLOR, outline=BORDER_COLOR, width=2)

    cols = [
        ("ИГРЫ", str(l_stats.get("played", 0)), WHITE),
        ("ПОБЕДЫ", str(l_stats.get("wins", 0)), WIN_COLOR),
        ("НИЧЬИ", str(l_stats.get("draws", 0)), DRAW_COLOR),
        ("ПОРАЖЕНИЯ", str(l_stats.get("losses", 0)), LOSS_COLOR),
        ("ГОЛЫ", f"{l_stats.get('goals_scored', 0)}:{l_stats.get('goals_conceded', 0)}", WHITE),
        ("РАЗНИЦА", f"{'+' if l_stats.get('goal_diff', 0) > 0 else ''}{l_stats.get('goal_diff', 0)}", ACCENT_CYAN),
    ]

    col_w = (CARD_WIDTH - CARD_PADDING * 2) / len(cols)
    for idx, (label, val, col_color) in enumerate(cols):
        cx = CARD_PADDING + idx * col_w + col_w / 2
        
        # Sub-divider line between cells
        if idx > 0:
            sep_x = CARD_PADDING + idx * col_w
            draw.line([(sep_x, curr_y + 16 * SCALE), (sep_x, curr_y + STATS_BAR_H - 16 * SCALE)], fill=BORDER_COLOR, width=1)

        # Label
        l_bbox = draw.textbbox((0, 0), label, font=font_lbl)
        draw.text((cx - (l_bbox[2] - l_bbox[0]) / 2, curr_y + 14 * SCALE), label, font=font_lbl, fill=MUTED)

        # Value
        v_bbox = draw.textbbox((0, 0), val, font=font_big_num)
        draw.text((cx - (v_bbox[2] - v_bbox[0]) / 2, curr_y + 36 * SCALE), val, font=font_big_num, fill=col_color)

    curr_y += STATS_BAR_H + 12 * SCALE

    # ── 3. FORM BAR (Last 5 matches) ───────────────────────────────────────
    _draw_rounded_rect(draw, (CARD_PADDING, curr_y, CARD_WIDTH - CARD_PADDING, curr_y + FORM_BAR_H),
                       radius=12 * SCALE, fill=SURFACE_COLOR, outline=BORDER_COLOR, width=2)

    draw.text((CARD_PADDING + 18 * SCALE, curr_y + 18 * SCALE), "ФОРМА (ПОСЛЕДНИЕ МАТЧИ В ЛИГЕ):", font=font_row_hd, fill=TEXT_SECONDARY)

    badge_start_x = CARD_WIDTH - CARD_PADDING - 16 * SCALE
    b_size = 30 * SCALE
    spacing = 8 * SCALE

    if not form:
        draw.text((badge_start_x - 120 * SCALE, curr_y + 18 * SCALE), "Матчей нет", font=font_sub, fill=MUTED)
    else:
        for idx, outcome in enumerate(reversed(form[-5:])):
            bx = badge_start_x - (idx + 1) * (b_size + spacing)
            by = curr_y + (FORM_BAR_H - b_size) // 2

            if outcome == 'W':
                b_fill, b_text = WIN_COLOR, 'В'
            elif outcome == 'D':
                b_fill, b_text = DRAW_COLOR, 'Н'
            else:
                b_fill, b_text = LOSS_COLOR, 'П'

            _draw_rounded_rect(draw, (bx, by, bx + b_size, by + b_size), radius=7 * SCALE, fill=b_fill)
            t_bbox = draw.textbbox((0, 0), b_text, font=font_badge)
            draw.text((bx + (b_size - (t_bbox[2] - t_bbox[0])) // 2, by + (b_size - (t_bbox[3] - t_bbox[1])) // 2),
                      b_text, font=font_badge, fill=WHITE if outcome != 'D' else (20, 20, 20))

    curr_y += FORM_BAR_H + 12 * SCALE

    # ── 4. CUP BLOCK (If played/active in Cup) ─────────────────────────────
    if cup:
        _draw_rounded_rect(draw, (CARD_PADDING, curr_y, CARD_WIDTH - CARD_PADDING, curr_y + CUP_BAR_H),
                           radius=12 * SCALE, fill=CUP_SURFACE, outline=CUP_BORDER, width=2)
        
        # Left golden accent line
        draw.line([(CARD_PADDING + 4 * SCALE, curr_y + 8 * SCALE), (CARD_PADDING + 4 * SCALE, curr_y + CUP_BAR_H - 8 * SCALE)],
                  fill=CUP_GOLD, width=3 * SCALE)

        stage_raw = str(cup.get("stage", "1/8")).upper()
        if "ФИНАЛ" in stage_raw:
            stage_display = stage_raw
        elif stage_raw in ("1/8", "1/4", "1/2"):
            stage_display = f"{stage_raw} ФИНАЛА"
        elif "FINAL" in stage_raw:
            stage_display = stage_raw.replace("FINAL", "ФИНАЛ")
        else:
            stage_display = f"{stage_raw} ФИНАЛА"

        opp = cup.get("opponent", "Соперник")
        c_w = cup.get("club_wins", 0)
        o_w = cup.get("opp_wins", 0)
        status = cup.get("status", "active")
        
        title_text = f"КУБОК 2026  •  {stage_display}"
        draw.text((CARD_PADDING + 18 * SCALE, curr_y + 14 * SCALE), title_text, font=font_row_hd, fill=CUP_GOLD)

        cup_desc = f"Серия против «{opp}»  |  Счёт серии: {c_w} : {o_w}  |  {'Завершена' if status == 'completed' else 'В процессе'}"
        draw.text((CARD_PADDING + 18 * SCALE, curr_y + 41 * SCALE), cup_desc, font=font_sub, fill=TEXT_SECONDARY)

        curr_y += CUP_BAR_H + 12 * SCALE

    # ── 5. TOP SCORERS & ASSISTS (Clean Leaderboard Cards) ────────────────
    half_w = (CARD_WIDTH - CARD_PADDING * 2 - 12 * SCALE) // 2

    # Left: Top Scorers
    _draw_rounded_rect(draw, (CARD_PADDING, curr_y, CARD_PADDING + half_w, curr_y + LEADERS_H),
                       radius=14 * SCALE, fill=SURFACE_COLOR, outline=BORDER_COLOR, width=2)
    
    _draw_rounded_rect(draw, (CARD_PADDING + 4 * SCALE, curr_y + 4 * SCALE, CARD_PADDING + half_w - 4 * SCALE, curr_y + 36 * SCALE),
                       radius=10 * SCALE, fill=SURFACE_ALT)
    
    draw.text((CARD_PADDING + 16 * SCALE, curr_y + 10 * SCALE), "БОМБАРДИРЫ КЛУБА", font=font_row_hd, fill=GOAL_COLOR)

    if not top_scorers:
        draw.text((CARD_PADDING + 18 * SCALE, curr_y + 54 * SCALE), "Нет забитых голов", font=font_sub, fill=MUTED)
    else:
        rank_colors = [GOLD_RANK, SILVER_RANK, BRONZE_RANK]
        for s_idx, sc in enumerate(top_scorers[:3]):
            sy = curr_y + 46 * SCALE + s_idx * 35 * SCALE
            p_n = sc["player_name"]
            p_g = sc["goals"]
            
            # Clean colored rank prefix: 1., 2., 3.
            r_col = rank_colors[s_idx] if s_idx < len(rank_colors) else MUTED
            draw.text((CARD_PADDING + 16 * SCALE, sy + 3 * SCALE), f"{s_idx + 1}.", font=font_rank, fill=r_col)
            draw.text((CARD_PADDING + 38 * SCALE, sy + 3 * SCALE), p_n, font=font_row_val, fill=WHITE)
            
            # Clean Stat Pill
            g_str = f"{p_g} Г"
            gb = draw.textbbox((0, 0), g_str, font=font_pill)
            pill_w = (gb[2] - gb[0]) + 20 * SCALE
            pill_h = 24 * SCALE
            pill_x = CARD_PADDING + half_w - 14 * SCALE - pill_w

            _draw_rounded_rect(draw, (pill_x, sy, pill_x + pill_w, sy + pill_h),
                               radius=6 * SCALE, fill=(18, 42, 28), outline=(34, 197, 94), width=1)
            draw.text((pill_x + 10 * SCALE, sy + 2 * SCALE), g_str, font=font_pill, fill=GOAL_COLOR)

    # Right: Top Assists
    right_x = CARD_PADDING + half_w + 12 * SCALE
    _draw_rounded_rect(draw, (right_x, curr_y, right_x + half_w, curr_y + LEADERS_H),
                       radius=14 * SCALE, fill=SURFACE_COLOR, outline=BORDER_COLOR, width=2)
    
    _draw_rounded_rect(draw, (right_x + 4 * SCALE, curr_y + 4 * SCALE, right_x + half_w - 4 * SCALE, curr_y + 36 * SCALE),
                       radius=10 * SCALE, fill=SURFACE_ALT)
    
    draw.text((right_x + 16 * SCALE, curr_y + 10 * SCALE), "АССИСТЕНТЫ КЛУБА", font=font_row_hd, fill=ASSIST_COLOR)

    if not top_assists:
        draw.text((right_x + 18 * SCALE, curr_y + 54 * SCALE), "Нет голевых передач", font=font_sub, fill=MUTED)
    else:
        rank_colors = [GOLD_RANK, SILVER_RANK, BRONZE_RANK]
        for a_idx, ac in enumerate(top_assists[:3]):
            ay = curr_y + 46 * SCALE + a_idx * 35 * SCALE
            p_n = ac["player_name"]
            p_a = ac["assists"]
            
            r_col = rank_colors[a_idx] if a_idx < len(rank_colors) else MUTED
            draw.text((right_x + 16 * SCALE, ay + 3 * SCALE), f"{a_idx + 1}.", font=font_rank, fill=r_col)
            draw.text((right_x + 38 * SCALE, ay + 3 * SCALE), p_n, font=font_row_val, fill=WHITE)
            
            # Clean Stat Pill
            a_str = f"{p_a} А"
            ab = draw.textbbox((0, 0), a_str, font=font_pill)
            pill_w = (ab[2] - ab[0]) + 20 * SCALE
            pill_h = 24 * SCALE
            pill_x = right_x + half_w - 14 * SCALE - pill_w

            _draw_rounded_rect(draw, (pill_x, ay, pill_x + pill_w, ay + pill_h),
                               radius=6 * SCALE, fill=(16, 36, 52), outline=(56, 189, 248), width=1)
            draw.text((pill_x + 10 * SCALE, ay + 2 * SCALE), a_str, font=font_pill, fill=ASSIST_COLOR)

    curr_y += LEADERS_H + 14 * SCALE

    # ── 6. OWNER / MANAGER FOOTER CARD ────────────────────────────────────
    _draw_rounded_rect(draw, (CARD_PADDING, curr_y, CARD_WIDTH - CARD_PADDING, curr_y + OWNER_CARD_H),
                       radius=14 * SCALE, fill=SURFACE_COLOR, outline=BORDER_LIGHT, width=2)

    av_size = 48 * SCALE
    av_x = CARD_PADDING + 14 * SCALE
    av_y = curr_y + (OWNER_CARD_H - av_size) // 2

    # Draw Owner Avatar
    avatar_loaded = False
    if avatar_path and os.path.exists(avatar_path):
        try:
            av_raw = Image.open(avatar_path).convert("RGBA")
            # Center crop to square to prevent stretching/distortion
            min_side = min(av_raw.size)
            if min_side > 0:
                crop_left = (av_raw.width - min_side) // 2
                crop_top = (av_raw.height - min_side) // 2
                av_raw = av_raw.crop((crop_left, crop_top, crop_left + min_side, crop_top + min_side))
            av_raw = av_raw.resize((av_size, av_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (av_size, av_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, av_size, av_size), fill=255)
            
            img.paste(av_raw, (av_x, av_y), mask)
            draw.ellipse((av_x - 2 * SCALE, av_y - 2 * SCALE, av_x + av_size + 2 * SCALE, av_y + av_size + 2 * SCALE),
                         outline=ACCENT_CYAN, width=2 * SCALE)
            avatar_loaded = True
        except Exception:
            avatar_loaded = False

    if not avatar_loaded:
        draw.ellipse((av_x, av_y, av_x + av_size, av_y + av_size), fill=(35, 42, 60), outline=ACCENT_CYAN, width=2 * SCALE)
        initial = (mgr_name.replace("@", "")[:1] or "U").upper()
        ibbox = draw.textbbox((0, 0), initial, font=font_badge)
        draw.text((av_x + (av_size - (ibbox[2] - ibbox[0])) // 2,
                   av_y + (av_size - (ibbox[3] - ibbox[1])) // 2), initial, font=font_badge, fill=ACCENT_CYAN)

    ow_text_x = av_x + av_size + 16 * SCALE
    owner_title = f"{mgr_name}" if manager else "Клуб свободен"
    draw.text((ow_text_x, curr_y + 13 * SCALE), owner_title, font=font_owner, fill=WHITE if manager else MUTED)

    # Subtitle
    mgr_sub_y = curr_y + 42 * SCALE
    warn_text = f"Нарушения: {warn_count}/4" if warn_count > 0 else "Без нарушений (0/4)"
    warn_c = LOSS_COLOR if warn_count > 0 else WIN_COLOR
    
    draw.text((ow_text_x, mgr_sub_y), "Владелец клуба   •   ", font=font_sm, fill=TEXT_SECONDARY)
    ow_w = draw.textlength("Владелец клуба   •   ", font=font_sm)
    draw.text((ow_text_x + ow_w, mgr_sub_y), warn_text, font=font_sm, fill=warn_c)

    # Right side: Verified Owner Pill Badge
    pill_text = "ВЛАДЕЛЕЦ" if manager else "СВОБОДЕН"
    pill_color = ACCENT_CYAN if manager else MUTED
    op_bbox = draw.textbbox((0, 0), pill_text, font=font_badge_sm)
    op_w = (op_bbox[2] - op_bbox[0]) + 22 * SCALE
    op_h = 30 * SCALE
    op_x = CARD_WIDTH - CARD_PADDING - 16 * SCALE - op_w
    op_y = curr_y + (OWNER_CARD_H - op_h) // 2
    _draw_rounded_rect(draw, (op_x, op_y, op_x + op_w, op_y + op_h),
                       radius=8 * SCALE, fill=(16, 28, 44) if manager else (24, 28, 36),
                       outline=pill_color, width=1)
    draw.text((op_x + 11 * SCALE, op_y + 6 * SCALE), pill_text, font=font_badge_sm, fill=pill_color)

    curr_y += OWNER_CARD_H + 16 * SCALE

    # ── 7. FOOTER ──────────────────────────────────────────────────────────
    footer_text = "ЛОГОВО ФИФАРЕЙ 2026"
    f_bbox = draw.textbbox((0, 0), footer_text, font=font_sm)
    draw.text(((CARD_WIDTH - (f_bbox[2] - f_bbox[0])) // 2, curr_y + 2 * SCALE), footer_text, font=font_sm, fill=MUTED)

    # ── Export to buffer ───────────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
