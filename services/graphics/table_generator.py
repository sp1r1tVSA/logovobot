import os
import io
import config
import database
from PIL import Image, ImageDraw, ImageFont

from pathlib import Path

from services.graphics.division_theme import draw_division_badge, resolve_theme

# Project root directory (services/graphics -> services -> root)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = str(PROJECT_ROOT)
LOGOS_DIR = str(PROJECT_ROOT / "assets" / "logos")

# Map of Russian club names to PNG logo filenames.
# Все 80 клубов сезона, сгруппированы как в config.DIVISION_CLUBS — так видно,
# что карта покрывает ростер целиком (это стережёт TestLogoMapCoversTheRoster).
# assets/logos/ вычищена вместе с сезоном и не лежит в git, поэтому самих PNG
# сейчас нет ни у одного клуба: имя файла здесь — договорённость о том, как он
# будет называться, когда его положат обратно. Промах не ошибка — каждая загрузка
# обёрнута в os.path.exists и деградирует в пустой бейдж.
TEAM_LOGO_MAP = {
    # DIV_1
    "Лидс": "leeds.png",
    "Ренн": "rennes.png",
    "Ницца": "nice.png",
    "Нэшвилл": "nashville.png",
    "Порту": "porto.png",
    "Вест Хэм": "west_ham.png",
    "Вольфсбург": "wolfsburg.png",
    "Фиорентина": "fiorentina.png",
    "Лацио": "lazio.png",
    "Марсель": "marseille.png",
    "Лилль": "lille.png",
    "Айнтрахт": "eintracht.png",
    "Майнц": "mainz.png",
    "Бернли": "burnley.png",
    "Будё Глимт": "bodo_glimt.png",
    "Кельн": "koln.png",
    # DIV_2
    "Вулверхэмптон": "wolverhampton.png",
    "Бурирам": "buriram.png",
    "Валенсия": "valencia.png",
    "Сельта": "celta.png",
    "Ривер Плейт": "river_plate.png",
    "Аякс": "ajax.png",
    "Спортинг": "sporting.png",
    "Монако": "monaco.png",
    "Бенфика": "benfica.png",
    "Фулхэм": "fulham.png",
    "Хоффенхайм": "hoffenheim.png",
    "Ланс": "lens.png",
    "Аль-Кадисия": "al_qadsiah.png",
    "Торино": "torino.png",
    "Лос Анджелес": "los_angeles.png",
    "ПСВ": "psv.png",
    # DIV_3
    "Сандерленд": "sunderland.png",
    "Ноттингем Форест": "nottingham_forest.png",
    "Реал Сосьедад": "real_sociedad.png",
    "Париж": "paris_fc.png",
    "Фенербахче": "fenerbahce.png",
    "Комо": "como.png",
    "Брентфорд": "brentford.png",
    "Кристал Пэлас": "crystal_palace.png",
    "Аль-Ахли": "al_ahli.png",
    "Лион": "lyon.png",
    "Борнмут": "bournemouth.png",
    "Аль-Иттихад": "al_ittihad.png",
    "Трабзонспор": "trabzonspor.png",
    "Вильярреал": "villarreal.png",
    "Штутгарт": "stuttgart.png",
    "Болонья": "bologna.png",
    # DIV_4
    "Байя": "bahia.png",
    "Милан": "milan.png",
    "Боруссия Дортмунд": "borussia_dortmund.png",
    "Интер Милан": "inter_milan.png",
    "Брайтон": "brighton.png",
    "Байер": "bayer_leverkusen.png",
    "Лейпциг": "leipzig.png",
    "Эвертон": "everton.png",
    "Аталанта": "atalanta.png",
    "Астон Вилла": "aston_villa.png",
    "Бешикташ": "besiktas.png",
    "Интер Майами": "inter_miami.png",
    "Бетис": "betis.png",
    "Аль-Хиляль": "al_hilal.png",
    "Ньюкасл": "newcastle.png",
    "Атлетик Бильбао": "athletic_bilbao.png",
    # DIV_5
    "Арсенал": "arsenal.png",
    "Манчестер Сити": "manchester_city.png",
    "Манчестер Юнайтед": "manchester_united.png",
    "Тоттенхэм": "tottenham.png",
    "Атлетико Мадрид": "atletico_madrid.png",
    "Барселона": "barcelona.png",
    "Реал Мадрид": "real_madrid.png",
    "Бавария": "bayern.png",
    "Ливерпуль": "liverpool.png",
    "Челси": "chelsea.png",
    "Наполи": "napoli.png",
    "Ювентус": "juventus.png",
    "Рома": "roma.png",
    "ПСЖ": "psg.png",
    "Галатасарай": "galatasaray.png",
    "Аль-Наср": "al_nassr.png",
}

# Also ensure lowercase keys are directly present
for _k, _v in list(TEAM_LOGO_MAP.items()):
    TEAM_LOGO_MAP[_k.lower()] = _v


def get_team_logo_filename(team_name: str) -> str | None:
    """Case-insensitive and alias-aware club logo filename lookup."""
    if not team_name:
        return None
    canon = database.resolve_team_name(team_name) or team_name
    t_clean = canon.strip().lower()
    for k, v in TEAM_LOGO_MAP.items():
        if k.lower() == t_clean:
            return v
    # Подстрочный хвост для форм, которые не поймал resolve_team_name.
    # Порядок значим: «Спортинг» содержит «порт», поэтому проверяется раньше «Порту».
    if "спортинг" in t_clean or "sporting" in t_clean:
        return "sporting.png"
    if "буд" in t_clean or "bodo" in t_clean:
        return "bodo_glimt.png"
    if "ривер" in t_clean or "river" in t_clean:
        return "river_plate.png"
    if "аякс" in t_clean or "ajax" in t_clean:
        return "ajax.png"
    if "псв" in t_clean or "psv" in t_clean:
        return "psv.png"
    if "порт" in t_clean or "porto" in t_clean:
        return "porto.png"
    if "бенфик" in t_clean or "benfica" in t_clean:
        return "benfica.png"
    return None


SCALE = 2  # 2x Supersampling for Retina broadcast sharpness


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Arial or fallback font."""
    font_names = ["arialbd.ttf" if bold else "arial.ttf", "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "seguiemj.ttf"]
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size)
        except IOError:
            continue
    return ImageFont.load_default()


def clean_and_prepare_logo(img: Image.Image) -> Image.Image:
    """Ensure logo is RGBA, transparent, and auto-cropped of empty margins."""
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

    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    return img


def resize_logo_proportional(img: Image.Image, max_w: int, max_h: int) -> tuple[Image.Image, int, int]:
    """Proportionally resize logo to fit within max_w x max_h preserving aspect ratio without distortion."""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img, max_w, max_h
    ratio = min(max_w / w, max_h / h)
    new_w = max(1, int(round(w * ratio)))
    new_h = max(1, int(round(h * ratio)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    return resized, new_w, new_h


def generate_league_table_image(
    standings: list[dict] | None = None,
    form_map: dict[str, list[str]] | None = None,
    division_name: str | None = None,
    division_id: int | None = None,
) -> io.BytesIO:
    """
    Generate a 2x supersampled, high-res graphic image of the league table.
    Returns io.BytesIO PNG buffer.

    division_id selects the division accent colour; without it the theme falls
    back to division_name and then to the neutral default.
    """
    # division_id здесь не только про цвет: если данные не передали, тянуть их надо
    # тем же срезом, иначе получается таблица в цветах дивизиона с чужими строками.
    if standings is None:
        standings = database.get_standings(division_id=division_id)
    if form_map is None:
        form_map = database.get_teams_recent_form(limit=5, division_id=division_id)

    # 1x Base Dimensions
    width_1x = 1120
    row_height_1x = 48
    table_top_1x = 130
    num_rows = len(standings) if standings else 16
    slots = config.get_eurocup_slots(division_id if division_id is not None else division_name)
    ucl_places = slots.get("ucl_places", 0)
    uel_places = slots.get("uel_places", 0)
    footer_height_1x = 110 if ucl_places > 0 else 90
    height_1x = table_top_1x + (num_rows * row_height_1x) + footer_height_1x

    # 2x Scaled Canvas Dimensions
    width = width_1x * SCALE
    height = height_1x * SCALE
    row_height = row_height_1x * SCALE
    table_top = table_top_1x * SCALE

    # Colors
    bg_color           = (20, 20, 22)         # #141416
    row_bg_1           = (26, 26, 30)         # #1A1A1E
    row_bg_2           = (20, 20, 22)         # #141416
    header_text_color  = (156, 163, 175)   # #9CA3AF
    primary_text_color = (255, 255, 255)
    muted_text_color   = (209, 213, 219)

    # Акцент дивизиона. Позиционные цвета строк (зона вылета, лидер, форма)
    # остаются семантическими и теме не подчиняются.
    theme = resolve_theme(division_id=division_id, division_name=division_name)
    accent_color = theme.accent

    # Canvas
    img = Image.new("RGBA", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # 2x Fonts
    font_title      = load_font(22 * SCALE, bold=True)
    font_subtitle   = load_font(14 * SCALE)
    font_col_header = load_font(13 * SCALE, bold=True)
    font_row_text   = load_font(15 * SCALE, bold=False)
    font_row_bold   = load_font(15 * SCALE, bold=True)
    font_footer     = load_font(13 * SCALE)
    font_badge      = load_font(12 * SCALE, bold=True)

    # Header
    title_str = f"СЕЗОН 2 • {division_name.upper()}" if division_name else "ТУРНИРНАЯ ТАБЛИЦА"
    subtitle_str = f"Турнирная таблица дивизиона {division_name}" if division_name else "Standings"
    draw.text((35 * SCALE, 25 * SCALE), title_str, fill=accent_color, font=font_title)
    draw.text((35 * SCALE, 58 * SCALE), subtitle_str, fill=header_text_color, font=font_subtitle)
    draw_division_badge(draw, theme, width - 35 * SCALE, 25 * SCALE, font_badge, SCALE)

    # Column X offsets (scaled)
    col_x = {
        "place": 35 * SCALE,
        "team": 95 * SCALE,
        "P": 390 * SCALE,
        "M": 460 * SCALE,
        "W": 530 * SCALE,
        "T": 600 * SCALE,
        "L": 670 * SCALE,
        "GF": 740 * SCALE,
        "GA": 810 * SCALE,
        "GD": 880 * SCALE,
        "%": 950 * SCALE,
        "form": 1020 * SCALE
    }

    # Column Headers
    y_hdr = table_top - 28 * SCALE
    draw.text((col_x["place"], y_hdr), "Standings", fill=header_text_color, font=font_col_header)
    for col in ["P", "M", "W", "T", "L", "GF", "GA", "GD", "%"]:
        draw.text((col_x[col], y_hdr), col, fill=header_text_color, font=font_col_header, anchor="mm")
    draw.text((col_x["form"] + 30 * SCALE, y_hdr), "Latest Results", fill=header_text_color, font=font_col_header, anchor="mm")

    # Separator line
    draw.line([(30 * SCALE, table_top - 10 * SCALE), (width - 30 * SCALE, table_top - 10 * SCALE)], fill=(45, 45, 52), width=1 * SCALE)

    # Rows
    y_curr = table_top
    for i, s in enumerate(standings, 1):
        bg = row_bg_1 if i % 2 == 1 else row_bg_2
        draw.rectangle([(30 * SCALE, y_curr), (width - 30 * SCALE, y_curr + row_height - 2 * SCALE)], fill=bg)

        # Eurocup qualification zone stripe on the left edge
        zone_color = None
        if ucl_places and i <= ucl_places:
            zone_color = (59, 130, 246)   # #3B82F6 UCL Blue
        elif uel_places and i <= ucl_places + uel_places:
            zone_color = (249, 115, 22)   # #F97316 UEL Orange

        if zone_color:
            draw.rectangle([(30 * SCALE, y_curr), (34 * SCALE, y_curr + row_height - 2 * SCALE)], fill=zone_color)

        y_center = y_curr + (row_height // 2)

        # Place number
        place_str = str(i)
        place_color = (147, 197, 253) if (ucl_places and i <= ucl_places) else ((253, 186, 116) if (uel_places and i <= ucl_places + uel_places) else primary_text_color)
        draw.text((col_x["place"] + 10 * SCALE, y_center), place_str, fill=place_color, font=font_row_bold, anchor="mm")

        # Team Logo with White Circular Container Badge
        team_name = s.get("team_name") or f"Команда {i}"
        logo_filename = get_team_logo_filename(team_name) or "default.png"
        logo_path = os.path.join(LOGOS_DIR, logo_filename)

        badge_diameter = 30 * SCALE
        badge_x = col_x["team"]
        badge_y = y_center - (badge_diameter // 2)

        # White circle background
        draw.ellipse([badge_x, badge_y, badge_x + badge_diameter, badge_y + badge_diameter], fill=(255, 255, 255))

        # Fit emblem centered proportionally without distortion
        if os.path.exists(logo_path):
            try:
                raw_logo = Image.open(logo_path)
                clean_logo = clean_and_prepare_logo(raw_logo)
                inner_size = 24 * SCALE
                logo_img, lw, lh = resize_logo_proportional(clean_logo, inner_size, inner_size)
                offset_x = badge_x + ((badge_diameter - lw) // 2)
                offset_y = badge_y + ((badge_diameter - lh) // 2)
                img.paste(logo_img, (offset_x, offset_y), logo_img)
            except Exception:
                pass

        # Team Name
        draw.text((col_x["team"] + 42 * SCALE, y_center), team_name, fill=primary_text_color, font=font_row_bold, anchor="lm")

        # Stat Values
        p = s.get("points", 0)
        w = s.get("wins", 0)
        t = s.get("draws", 0)
        l = s.get("losses", 0)
        m = s.get("played", w + t + l)
        gf = s.get("goals_scored", 0)
        ga = s.get("goals_conceded", 0)
        gd = gf - ga
        rating = (p / (m * 3) * 100.0) if m > 0 else 0.0
        rating_str = f"{rating:.1f}"

        draw.text((col_x["P"], y_center), str(p), fill=primary_text_color, font=font_row_bold, anchor="mm")
        draw.text((col_x["M"], y_center), str(m), fill=muted_text_color, font=font_row_text, anchor="mm")
        draw.text((col_x["W"], y_center), str(w), fill=muted_text_color, font=font_row_text, anchor="mm")
        draw.text((col_x["T"], y_center), str(t), fill=muted_text_color, font=font_row_text, anchor="mm")
        draw.text((col_x["L"], y_center), str(l), fill=muted_text_color, font=font_row_text, anchor="mm")
        draw.text((col_x["GF"], y_center), str(gf), fill=muted_text_color, font=font_row_text, anchor="mm")
        draw.text((col_x["GA"], y_center), str(ga), fill=muted_text_color, font=font_row_text, anchor="mm")

        gd_str = f"+{gd}" if gd > 0 else str(gd)
        draw.text((col_x["GD"], y_center), gd_str, fill=muted_text_color, font=font_row_text, anchor="mm")
        draw.text((col_x["%"], y_center), rating_str, fill=muted_text_color, font=font_row_text, anchor="mm")

        # Form Dots (5 dots)
        team_n = s.get("team_name", "").lower().strip()
        user_form = form_map.get(team_n, []) if team_n else []
        dots = (['E'] * (5 - len(user_form))) + user_form[-5:]

        dot_radius = 5 * SCALE
        start_x = col_x["form"]
        for d_idx, res in enumerate(dots):
            dx = start_x + (d_idx * 16 * SCALE)
            dy = y_center
            if res == 'W':
                fill_color = (34, 197, 94)   # Green #22C55E
            elif res == 'L':
                fill_color = (239, 68, 68)   # Red #EF4444
            elif res == 'D':
                fill_color = (156, 163, 175) # Gray #9CA3AF
            else:
                fill_color = (55, 65, 81)    # Muted dark #374151

            draw.ellipse([dx - dot_radius, dy - dot_radius, dx + dot_radius, dy + dot_radius], fill=fill_color)

        y_curr += row_height

    # Footer Legend
    y_footer = y_curr + 20 * SCALE
    legend_parts = [
        ("P", "Points"), ("M", "Matches"), ("W", "Wins"), ("T", "Ties"),
        ("L", "Losses"), ("GF", "Goals for"), ("GA", "Goals against"),
        ("GD", "Goals difference"), ("%", "Rating")
    ]

    x_leg = 35 * SCALE
    for code, desc in legend_parts:
        draw.text((x_leg, y_footer), code, fill=primary_text_color, font=font_row_bold)
        x_leg += draw.textlength(code, font=font_row_bold) + 4 * SCALE
        draw.text((x_leg, y_footer), desc, fill=header_text_color, font=font_footer)
        x_leg += draw.textlength(desc, font=font_footer) + 20 * SCALE

    # Eurocup zones legend
    if ucl_places > 0:
        y_euro = y_footer + 24 * SCALE
        x_euro = 35 * SCALE

        # UCL indicator & label
        draw.rectangle([(x_euro, y_euro + 3 * SCALE), (x_euro + 10 * SCALE, y_euro + 13 * SCALE)], fill=(59, 130, 246))
        x_euro += 15 * SCALE
        ucl_txt = f"1–{ucl_places} Лига Чемпионов"
        draw.text((x_euro, y_euro), ucl_txt, fill=(147, 197, 253), font=font_footer)
        x_euro += draw.textlength(ucl_txt, font=font_footer) + 20 * SCALE

        # UEL indicator & label
        draw.rectangle([(x_euro, y_euro + 3 * SCALE), (x_euro + 10 * SCALE, y_euro + 13 * SCALE)], fill=(249, 115, 22))
        x_euro += 15 * SCALE
        uel_txt = f"{ucl_places + 1}–{ucl_places + uel_places} Лига Европы"
        draw.text((x_euro, y_euro), uel_txt, fill=(253, 186, 116), font=font_footer)
        x_euro += draw.textlength(uel_txt, font=font_footer) + 24 * SCALE

        # Start info note
        note_txt = "⭐️ Еврокубки — после 15 тура (после ТО)"
        draw.text((x_euro, y_euro), note_txt, fill=(245, 158, 11), font=font_footer)

    # Resample down from 2x scale to 1x scale using LANCZOS
    resampled_img = img.resize((width_1x, height_1x), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    resampled_img.save(buffer, format="PNG", quality=95)
    buffer.seek(0)
    return buffer




generate_table = generate_league_table_image


if __name__ == "__main__":
    buf = generate_league_table_image()
    with open("test_league_table.png", "wb") as f:
        f.write(buf.getvalue())
    print("✓ test_league_table.png generated successfully with 2x supersampling!")
