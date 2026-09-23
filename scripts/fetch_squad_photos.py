"""
scripts/fetch_squad_photos.py

Перекачивает фотографии игроков из `squad_players`, опознавая каждого игрока
**внутри состава его клуба**, а не поиском по имени во всём мире.

Зачем отдельный скрипт, а не `services/graphics/player_photos.py`. Фетчер бота
ищет игрока по имени у провайдеров: состав читается OCR из русифицированного FC,
там одни фамилии заглавными («MENDY», «MARTÍNEZ», «DAVID»), и такой запрос
регулярно приводил чужого футболиста — отсюда и неверные лица на карточках.
Состав же у участников реальный (Transfermarkt на 24.09.2026), поэтому клуб
здесь — не подсказка, а ограничение: сначала берём ростер клуба целиком и
сопоставляем фамилию только с ним. «MENDY» в Аль-Ахли — это Édouard Mendy и
никто другой; «MARTÍNEZ» в Интере с позицией ST — Lautaro, а не вратарь Josep.

Конвейер:

1. **Личность** — ростер клуба из FotMob (`/api/data/teams?id=`): полное имя,
   дата рождения, позиция, номер, id. Покрывает и Саудовскую лигу, и MLS,
   и тайскую Бурираму.
2. **Сопоставление** — тиры EXACT → все токены → фамилия → токен → fuzzy,
   строго внутри ростера. Ничья разводится позицией из БД, затем — известностью
   (рейтинг/трансферная стоимость), и только с большим отрывом. Неразведённая
   ничья — это отказ: пустая карточка восстановима, чужое лицо нет.
3. **Фото** — каскад по качеству, где **каждый источник проверяется на совпадение
   личности**: TheSportsDB (500px вырезка, сверка по клубу или дате рождения) →
   SoFIFA (360px рендер EA FC, сверка по клубу в строке поиска) → FotMob
   (192px, личность гарантирована id — крайний случай).

Файлы пишутся ровно туда, откуда их читает бот — `player_photos.get_cached_photo_path`
(`assets/players/<игрок>_<клуб>.png`), поэтому после прогона ничего доустанавливать
не нужно. Рядом кладётся `_photo_manifest.json`: что за игрок опознан, каким
источником, с какой уверенностью — по нему видно, что проверять руками.

    # посмотреть план, ничего не скачивая
    python scripts/fetch_squad_photos.py --db ../server_league.db --dry-run

    # прогон по одному клубу с контактным листом для глазной проверки
    python scripts/fetch_squad_photos.py --db ../server_league.db --club Арсенал --contact-sheet

    # полный прогон на сервере
    venv/bin/python3 scripts/fetch_squad_photos.py --contact-sheet --clear-media-cache
"""

import os
import re
import io
import sys
import json
import time
import sqlite3
import difflib
import logging
import argparse
import unicodedata
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# Скрипт запускают и на сервере, где системный python — без Pillow: там бот
# живёт в venv. Перезапускаемся в нём сами, как делает refresh_all_player_cards.
for _venv_py in (
    os.path.join(BASE_DIR, "venv", "bin", "python3"),
    os.path.join(BASE_DIR, "venv", "bin", "python"),
    os.path.join(BASE_DIR, ".venv", "bin", "python3"),
    os.path.join(BASE_DIR, ".venv", "bin", "python"),
    os.path.join(BASE_DIR, "venv", "Scripts", "python.exe"),
):
    if os.path.isfile(_venv_py) and os.path.abspath(sys.executable) != os.path.abspath(_venv_py):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            os.execv(_venv_py, [_venv_py] + sys.argv)

try:
    from PIL import Image, ImageDraw
except ImportError:
    print(
        "\n❌ Pillow не найден в текущем интерпретаторе.\n"
        "   Запустите через venv бота:  venv/bin/python3 scripts/fetch_squad_photos.py\n"
    )
    sys.exit(1)

from services.graphics import player_photos

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("squad_photos")

MANIFEST_PATH = os.path.join(player_photos.PHOTOS_DIR, "_photo_manifest.json")
CONTACT_SHEET_DIR = os.path.join(BASE_DIR, "assets", "contact_sheets")

# --------------------------------------------------------------------------
# Провайдеры
# --------------------------------------------------------------------------

FOTMOB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.fotmob.com/",
}
SOFIFA_HEADERS = player_photos.SOFIFA_HEADERS
STD_HEADERS = {"User-Agent": FOTMOB_HEADERS["User-Agent"], "Accept": "*/*"}

FOTMOB_TEAM_API = "https://www.fotmob.com/api/data/teams?id={tid}"
FOTMOB_SUGGEST_API = "https://apigw.fotmob.com/searchapi/suggest?term={term}"
FOTMOB_IMAGE = "https://images.fotmob.com/image_resources/playerimages/{pid}.png"
TSDB_SEARCH = "https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p={name}"
SOFIFA_SEARCH = "https://sofifa.com/players?keyword={name}"
SOFIFA_RENDER = "https://cdn.sofifa.net/players/{a}/{b}/{v}_" + player_photos.SOFIFA_RENDER_SIZE + ".png"

# Рендеры лежат по номеру издания игры. Свежее издание идёт первым: у игрока,
# добавленного в этом сезоне, рендера прошлого издания просто нет. Второй номер —
# тот, на который настроен фетчер бота.
SOFIFA_VERSIONS = tuple(dict.fromkeys(("27", player_photos.SOFIFA_FC_VERSION)))

# id клубов FotMob зафиксированы, а не ищутся при каждом прогоне: поиск по
# короткому названию легко отдаёт женскую или молодёжную команду («Arsenal (W)»,
# «Jong Ajax»), и тогда весь состав опознаётся мимо. Клуб вне карты ищется
# по названию, но результат проверяется по доле совпавших игроков.
CLUB_FOTMOB_IDS = {
    # DIV_1
    "Лидс": 8463,
    "Ренн": 9851,
    "Ницца": 9831,
    "Нэшвилл": 915807,
    "Порту": 9773,
    "Вест Хэм": 8654,
    "Вольфсбург": 8721,
    "Фиорентина": 8535,
    "Лацио": 8543,
    "Марсель": 8592,
    "Лилль": 8639,
    "Айнтрахт": 9810,
    "Майнц": 9905,
    "Бернли": 8191,
    "Будё Глимт": 8402,
    "Кельн": 8722,
    # DIV_2
    "Вулверхэмптон": 8602,
    "Бурирам": 165243,
    "Валенсия": 10267,
    "Сельта": 9910,
    "Ривер Плейт": 10076,
    "Аякс": 8593,
    "Спортинг": 9768,
    "Монако": 9829,
    "Бенфика": 9772,
    "Фулхэм": 9879,
    "Хоффенхайм": 8226,
    "Ланс": 8588,
    "Аль-Кадисия": 101919,
    "Торино": 9804,
    "Лос Анджелес": 867280,
    "ПСВ": 8640,
    # DIV_3
    "Сандерленд": 8472,
    "Ноттингем Форест": 10203,
    "Реал Сосьедад": 8560,
    "Париж": 6379,
    "Фенербахче": 8695,
    "Комо": 10171,
    "Брентфорд": 9937,
    "Кристал Пэлас": 9826,
    "Аль-Ахли": 2530,
    "Лион": 9748,
    "Борнмут": 8678,
    "Аль-Иттихад": 8577,
    "Трабзонспор": 9752,
    "Вильярреал": 10205,
    "Штутгарт": 10269,
    "Болонья": 9857,
    # DIV_4
    "Байя": 7877,
    "Милан": 8564,
    "Боруссия Дортмунд": 9789,
    "Интер Милан": 8636,
    "Брайтон": 10204,
    "Байер": 8178,
    "Лейпциг": 178475,
    "Эвертон": 8668,
    "Аталанта": 8524,
    "Астон Вилла": 10252,
    "Бешикташ": 10188,
    "Интер Майами": 960720,
    "Бетис": 8603,
    "Аль-Хиляль": 2529,
    "Ньюкасл": 10261,
    "Атлетик Бильбао": 8315,
    # DIV_5
    "Арсенал": 9825,
    "Манчестер Сити": 8456,
    "Манчестер Юнайтед": 10260,
    "Тоттенхэм": 8586,
    "Атлетико Мадрид": 9906,
    "Барселона": 8634,
    "Реал Мадрид": 8633,
    "Бавария": 9823,
    "Ливерпуль": 8650,
    "Челси": 8455,
    "Наполи": 9875,
    "Ювентус": 9885,
    "Рома": 8686,
    "ПСЖ": 9847,
    "Галатасарай": 8637,
    "Аль-Наср": 101918,
}

# Игровые прозвища, под которыми игрок не находится ни в ростере, ни в поиске:
# в FC он «VINI JR.», у провайдеров — «Vinícius Júnior». Ключ нормализован
# (`_norm`), значение — имя, каким его знают провайдеры.
PLAYER_NAME_OVERRIDES = {
    "vini jr": "Vinicius Junior",
    "savinho": "Savio",
    "c ronaldo": "Cristiano Ronaldo",
}

# Позиции из БД — из игры; у FotMob свой словарь. Сводим к группам: точное
# совпадение кода весит больше, группа — меньше, и это разводит однофамильцев
# («MARTÍNEZ» ST — Lautaro, а не вратарь Josep).
POSITION_GROUPS = {
    "GK": "GK",
    "CB": "DEF", "RB": "DEF", "LB": "DEF", "RWB": "DEF", "LWB": "DEF", "DEF": "DEF",
    "CDM": "MID", "DM": "MID", "CM": "MID", "CAM": "MID", "AM": "MID",
    "LM": "MID", "RM": "MID", "MID": "MID",
    "LW": "ATT", "RW": "ATT", "ST": "ATT", "CF": "ATT", "FW": "ATT", "ATT": "ATT",
}

# Токены, которые в названиях клубов не несут смысла при сверке
# («Al Ahli SFC» ↔ «Al Ahli», «VfB Stuttgart» ↔ «Stuttgart»).
# Namely: «real», «united» и «city» здесь намеренно **отсутствуют** — это не шум,
# а единственное, чем Реал Мадрид отличается от Реал Сосьедад, а Манчестер Сити
# от Манчестер Юнайтед.
CLUB_NOISE_TOKENS = {
    "fc", "cf", "sc", "afc", "ac", "as", "ss", "ssc", "sfc", "cd", "rc", "rcd",
    "ud", "sd", "club", "de", "the", "vfb", "vfl", "tsg", "fsv", "bsc", "calcio",
    "cp", "sad", "futbol", "football",
}

MIN_PIXELS_STRICT = 250   # планка для основных источников (как у player_photos)
MIN_PIXELS_LAST = 150     # FotMob отдаёт 192px — крайний источник, но личность точная
MIN_BYTES = 3000

REQUEST_PAUSE = 0.25      # вежливая пауза между запросами к провайдерам


# --------------------------------------------------------------------------
# Утилиты имён
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Нижний регистр без диакритики и пунктуации: «ÉDER MILITÃO» → «eder militao»."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    for src, dst in player_photos.TRANSLIT_LATIN.items():
        text = text.replace(src, dst)
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def _tokens(text: str) -> list[str]:
    """Значимые части имени; односимвольные инициалы («C. RONALDO») отбрасываются."""
    return [t for t in _norm(text).split() if len(t) > 1]


def _club_tokens(name: str) -> set[str]:
    return {t for t in _norm(name).split() if len(t) > 2 and t not in CLUB_NOISE_TOKENS}


def _clubs_match(a: str, b: str) -> bool:
    """
    Один ли это клуб. Требуется совпадение значимых токенов **целиком**, с
    поправкой на написание («Bayern München» ↔ «bayern-munchen»).

    Пересечения по одному токену недостаточно, и это главное: Реал Мадрид и
    Реал Сосьедад, Манчестеры Сити и Юнайтед, Интеры Милан и Майами делят
    первое слово. Строгая сверка иногда откажет там, где клуб тот же (у
    провайдера другое имя) — игрок тогда просто скатится на источник ниже;
    нестрогая молча подсунет однофамильца из соседнего клуба.
    """
    ta, tb = _club_tokens(a), _club_tokens(b)
    if not ta or not tb or len(ta) != len(tb):
        return False
    for token in ta:
        if not any(t == token or difflib.SequenceMatcher(None, t, token).ratio() >= 0.8 for t in tb):
            return False
    return True


def _provider_query_name(db_name: str, identity: dict | None = None) -> str:
    """Имя, под которым игрока ищут у провайдеров."""
    if identity and identity.get("name"):
        return identity["name"]
    return PLAYER_NAME_OVERRIDES.get(_norm(db_name), db_name)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _request(url: str, headers: dict, timeout: int = 15, retries: int = 3) -> bytes | None:
    """GET с повторами: провайдеры регулярно рвут TLS на длинных прогонах."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            if attempt == retries - 1:
                logger.debug(f"  request failed {url}: {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def _request_json(url: str, headers: dict, timeout: int = 15) -> dict | None:
    raw = _request(url, headers, timeout=timeout)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        logger.debug(f"  bad JSON from {url}: {e}")
        return None


# --------------------------------------------------------------------------
# Шаг 1. Ростер клуба
# --------------------------------------------------------------------------

def fetch_club_roster(club_ru: str) -> tuple[int | None, str, list[dict]]:
    """
    Возвращает `(fotmob_id, английское имя клуба, список игроков)`.

    Игрок: `{id, name, dob, positions, number, rating, value}`.
    """
    tid = CLUB_FOTMOB_IDS.get(club_ru)
    if tid is None:
        tid, resolved = _search_club_id(club_ru)
        if tid is None:
            return None, "", []
        logger.warning(f"  клуб '{club_ru}' не в CLUB_FOTMOB_IDS, поиск дал '{resolved}' (id={tid})")

    data = _request_json(FOTMOB_TEAM_API.format(tid=tid), FOTMOB_HEADERS, timeout=20)
    if not data:
        return tid, "", []

    club_en = (data.get("details") or {}).get("name") or ""
    players: list[dict] = []
    for group in (data.get("squad") or {}).get("squad") or []:
        if group.get("title") == "coach":
            continue
        for member in group.get("members") or []:
            players.append({
                "id": member.get("id"),
                "name": member.get("name") or "",
                "dob": (member.get("dateOfBirth") or "")[:10],
                "positions": [p.strip().upper() for p in (member.get("positionIdsDesc") or "").split(",") if p.strip()],
                "number": member.get("shirtNumber"),
                "rating": member.get("rating") or 0,
                "value": member.get("transferValue") or 0,
                "club_en": club_en,
            })
    return tid, club_en, players


def _search_club_id(club_ru: str) -> tuple[int | None, str]:
    """Запасной путь для клуба вне карты: поиск с отсевом женских и молодёжных команд."""
    try:
        from services.graphics.table_generator import TEAM_LOGO_MAP
        query = (TEAM_LOGO_MAP.get(club_ru) or "")[:-4].replace("_", " ") or club_ru
    except Exception:
        query = club_ru

    data = _request_json(FOTMOB_SUGGEST_API.format(term=urllib.parse.quote(query)), FOTMOB_HEADERS)
    for suggest in (data or {}).get("teamSuggest") or []:
        for option in suggest.get("options") or []:
            name = (option.get("text") or "").split("|")[0]
            if "(W)" in name or re.search(r"\bU\d\d\b|\bII\b|^Jong |\bB$", name):
                continue
            tid = option.get("payload", {}).get("id")
            if tid:
                return int(tid), name
    return None, ""


# --------------------------------------------------------------------------
# Шаг 2. Сопоставление игрока из БД с ростером
# --------------------------------------------------------------------------

TIER_EXACT, TIER_ALL_TOKENS, TIER_SURNAME, TIER_TOKEN, TIER_FUZZY = 50, 40, 30, 20, 10


def _name_tier(db_name: str, roster_name: str) -> int:
    """Насколько сильно имя из БД совпало с именем из ростера. 0 — не совпало."""
    db_norm, roster_norm = _norm(db_name), _norm(roster_name)
    if not db_norm or not roster_norm:
        return 0
    if db_norm == roster_norm:
        return TIER_EXACT

    db_tokens, roster_tokens = _tokens(db_name), _tokens(roster_name)
    if not db_tokens or not roster_tokens:
        return 0
    if set(db_tokens) <= set(roster_tokens):
        return TIER_ALL_TOKENS
    # Фамилия — последний токен у обоих. Совпадение фамилий весит больше, чем
    # совпадение с именем: «BRADLEY» в Ливерпуле — это Conor Bradley, а не
    # Bradley Barcola.
    if db_tokens[-1] == roster_tokens[-1]:
        return TIER_SURNAME
    if db_tokens[-1] in roster_tokens:
        return TIER_TOKEN
    ratio = difflib.SequenceMatcher(None, db_tokens[-1], roster_tokens[-1]).ratio()
    if ratio >= 0.87 and len(db_tokens[-1]) >= 5:
        return TIER_FUZZY
    return 0


def _position_score(db_position: str | None, roster_positions: list[str]) -> int:
    if not db_position or not roster_positions:
        return 0
    db_position = db_position.strip().upper()
    if db_position in roster_positions:
        return 3
    db_group = POSITION_GROUPS.get(db_position)
    if db_group and any(POSITION_GROUPS.get(p) == db_group for p in roster_positions):
        return 1
    return 0


def _is_clearly_more_prominent(first: dict, second: dict) -> bool:
    """
    Один из однофамильцев — очевидно основной игрок?

    Последний разводящий признак, и намеренно грубый: рейтинг сезона есть только
    у играющих, а разрыв трансферной стоимости втрое — это основа против дубля.
    Близкие значения ничего не решают, и тогда мы честно отказываемся.
    """
    if first["rating"] and not second["rating"]:
        return True
    if first["value"] and second["value"] and first["value"] >= second["value"] * 3:
        return True
    if first["value"] and not second["value"]:
        return True
    return False


def match_in_roster(db_name: str, db_position: str | None, roster: list[dict]) -> tuple[dict | None, str]:
    """
    Ищет игрока из БД в ростере клуба. Возвращает `(игрок | None, объяснение)`.

    Неразведённая ничья — отказ: пустая карточка восстановима, чужое лицо нет.
    """
    query_name = _provider_query_name(db_name)
    scored: list[tuple[int, int, dict]] = []
    for player in roster:
        tier = max(_name_tier(db_name, player["name"]), _name_tier(query_name, player["name"]))
        if tier:
            scored.append((tier, _position_score(db_position, player["positions"]), player))

    if not scored:
        return None, "не найден в ростере"

    scored.sort(key=lambda item: (-item[0], -item[1], -(item[2]["rating"] or 0)))
    best_tier, best_pos, best = scored[0]
    if len(scored) == 1:
        return best, f"tier={best_tier}"

    second_tier, second_pos, second = scored[1]
    if (best_tier, best_pos) > (second_tier, second_pos):
        return best, f"tier={best_tier} pos={best_pos}"
    if _is_clearly_more_prominent(best, second):
        return best, f"tier={best_tier} по известности (второй: {second['name']})"
    return None, "однофамильцы: " + " / ".join(p["name"] for _, _, p in scored[:3])


def search_player_globally(db_name: str, club_en: str) -> tuple[dict | None, str]:
    """
    Запасной путь, когда игрока нет в ростере: поиск по всей базе FotMob с
    обязательной сверкой клуба. Ростеры отстают от трансферов, а клуб в составе
    участника реальный — поэтому совпадение клуба здесь и есть подтверждение.
    """
    query = _provider_query_name(db_name)
    terms = [query]
    tokens = _tokens(query)
    if len(tokens) > 1:
        terms.append(tokens[-1])

    for term in terms:
        data = _request_json(FOTMOB_SUGGEST_API.format(term=urllib.parse.quote(term)), FOTMOB_HEADERS)
        for suggest in (data or {}).get("squadMemberSuggest") or []:
            for option in suggest.get("options") or []:
                payload = option.get("payload") or {}
                if payload.get("isCoach"):
                    continue
                found_name = (option.get("text") or "").split("|")[0]
                team_name = payload.get("teamName") or ""
                if not _clubs_match(team_name, club_en):
                    continue
                if not _name_tier(db_name, found_name) and not _name_tier(query, found_name):
                    continue
                return {
                    "id": payload.get("id"),
                    "name": found_name,
                    "dob": "",
                    "positions": [],
                    "number": None,
                    "rating": 0,
                    "value": 0,
                    "club_en": team_name,
                }, f"глобальный поиск, клуб совпал ({team_name})"
        time.sleep(REQUEST_PAUSE)
    return None, "не найден и в глобальном поиске"


# --------------------------------------------------------------------------
# Шаг 3. Источники фотографий (каждый — со сверкой личности)
# --------------------------------------------------------------------------

def _tsdb_candidates(identity: dict) -> list[tuple[str, str]]:
    """
    TheSportsDB: 500px вырезка. Личность сверяется по имени И (клубу ИЛИ дате
    рождения) — тёзки в базе есть, и без сверки это тот же промах, что у бота.
    """
    name = identity["name"]
    data = _request_json(TSDB_SEARCH.format(name=urllib.parse.quote(name)), STD_HEADERS)
    found: list[tuple[str, str]] = []
    for player in (data or {}).get("player") or []:
        position = str(player.get("strPosition") or "").lower()
        if "manager" in position or "coach" in position:
            continue
        if not _name_tier(name, player.get("strPlayer") or ""):
            continue
        club_ok = _clubs_match(player.get("strTeam") or "", identity.get("club_en") or "")
        dob_ok = bool(identity.get("dob")) and (player.get("dateBorn") or "")[:10] == identity["dob"]
        if not (club_ok or dob_ok):
            logger.debug(f"    TheSportsDB: '{player.get('strPlayer')}' отклонён "
                         f"(клуб '{player.get('strTeam')}' ≠ '{identity.get('club_en')}')")
            continue
        for key in ("strCutout", "strRender", "strThumb"):
            if player.get(key):
                found.append((f"TheSportsDB/{key[3:].lower()}", player[key]))
        break
    return found


def _sofifa_candidates(identity: dict) -> list[tuple[str, str]]:
    """
    SoFIFA: официальный рендер EA FC. Сверяемся по ссылкам в строке, а не по её
    тексту: в тексте имя сокращено («B. White»), а в ссылках лежат полные слаги —
    `/player/231936/benjamin-white/` и `/team/1/arsenal-fc/`.
    """
    # Поиск там нечёткий и по полному имени легко промахивается («Ben White» →
    # «B. Whiteman»), поэтому вторым заходом ищем по фамилии: однофамильцев
    # разведёт та же колонка клуба.
    queries = [identity["name"]]
    name_tokens = _tokens(identity["name"])
    if len(name_tokens) > 1:
        queries.append(name_tokens[-1])

    for query in queries:
        raw = _request(SOFIFA_SEARCH.format(name=urllib.parse.quote(query)), SOFIFA_HEADERS)
        if not raw:
            continue
        html = raw.decode("utf-8", "replace")
        for row in re.findall(r"<tr>.*?</tr>", html, re.S):
            player_link = re.search(r"/player/(\d+)/([a-z0-9-]+)/", row)
            team_link = re.search(r"/team/\d+/([a-z0-9-]+)/", row)
            if not player_link or not team_link:
                continue
            found_name = player_link.group(2).replace("-", " ")
            if not _name_tier(identity["name"], found_name):
                continue
            if not _clubs_match(team_link.group(1).replace("-", " "), identity.get("club_en") or ""):
                logger.debug(f"    SoFIFA: '{found_name}' отклонён "
                             f"(клуб '{team_link.group(1)}' ≠ '{identity.get('club_en')}')")
                continue
            pid = player_link.group(1).zfill(6)
            return [(f"SoFIFA/FC{v}", SOFIFA_RENDER.format(a=pid[:3], b=pid[3:6], v=v))
                    for v in SOFIFA_VERSIONS]
        time.sleep(REQUEST_PAUSE)
    return []


def _fotmob_candidates(identity: dict) -> list[tuple[str, str]]:
    """FotMob: всего 192px, зато личность гарантирована id из ростера."""
    if not identity.get("id"):
        return []
    return [("FotMob", FOTMOB_IMAGE.format(pid=identity["id"]))]


PHOTO_SOURCES = (
    ("TheSportsDB", _tsdb_candidates),
    ("SoFIFA", _sofifa_candidates),
    ("FotMob", _fotmob_candidates),
)


def _inspect_image(data: bytes, min_pixels: int) -> tuple[bool, int, int, bool]:
    """`(годится, ширина, высота, есть ли прозрачность)`."""
    if not data or len(data) < MIN_BYTES:
        return False, 0, 0, False
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            has_alpha = "A" in im.getbands() or "transparency" in im.info
    except Exception:
        return False, 0, 0, False
    return width >= min_pixels and height >= min_pixels, width, height, has_alpha


def download_photo(identity: dict) -> dict | None:
    """
    Качает лучшее доступное фото опознанного игрока.

    Прозрачная вырезка важнее порядка источников: плоский портрет от первого
    источника не перекрывает вырезку у следующего (так же, как в фетчере бота).
    """
    flat_fallback: dict | None = None
    last_resort: dict | None = None

    for source_name, collect in PHOTO_SOURCES:
        try:
            candidates = collect(identity)
        except Exception as e:
            logger.debug(f"    {source_name} упал: {e}")
            continue
        for label, url in candidates:
            headers = SOFIFA_HEADERS if source_name == "SoFIFA" else STD_HEADERS
            data = _request(url, headers, timeout=20, retries=2)
            min_pixels = MIN_PIXELS_LAST if source_name == "FotMob" else MIN_PIXELS_STRICT
            ok, width, height, has_alpha = _inspect_image(data, min_pixels)
            if not ok:
                continue
            record = {"source": label, "url": url, "width": width, "height": height,
                      "alpha": has_alpha, "data": data}
            if has_alpha and width >= MIN_PIXELS_STRICT:
                return record
            if source_name == "FotMob":
                last_resort = last_resort or record
            elif flat_fallback is None:
                flat_fallback = record
            time.sleep(REQUEST_PAUSE)
    return flat_fallback or last_resort


# --------------------------------------------------------------------------
# Данные и отчёты
# --------------------------------------------------------------------------

def load_squads(db_path: str, only_club: str | None = None) -> dict[str, list[tuple[str, str]]]:
    if not os.path.isabs(db_path):
        db_path = os.path.normpath(os.path.join(BASE_DIR, db_path))
    if not os.path.exists(db_path):
        logger.error(f"База не найдена: {db_path}")
        return {}

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT team_name, player_name, position FROM squad_players "
            "WHERE player_name IS NOT NULL AND player_name != '' ORDER BY team_name, id"
        ).fetchall()
    finally:
        conn.close()

    squads: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if only_club and row["team_name"].strip().lower() != only_club.strip().lower():
            continue
        squads.setdefault(row["team_name"], []).append((row["player_name"], row["position"]))
    return squads


def load_manifest() -> dict:
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(manifest: dict) -> None:
    os.makedirs(player_photos.PHOTOS_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)


def build_contact_sheet(club_ru: str, entries: list[dict]) -> str | None:
    """
    Лист с лицами и подписями для глазной проверки: одного взгляда хватает,
    чтобы увидеть чужого футболиста в составе.
    """
    entries = [e for e in entries if e.get("path") and os.path.exists(e["path"])]
    if not entries:
        return None

    # Шрифт берём тот же, что и карточки бота: у шрифта PIL по умолчанию нет
    # кириллицы, и названия клубов вышли бы квадратами.
    from services.graphics.table_generator import load_font
    title_font, name_font, meta_font = load_font(16, bold=True), load_font(13), load_font(11)

    cell, pad, caption_h, columns = 180, 10, 40, 6
    rows = (len(entries) + columns - 1) // columns
    width = columns * (cell + pad) + pad
    height = rows * (cell + caption_h + pad) + pad + 30
    sheet = Image.new("RGB", (width, height), (24, 26, 32))
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, pad), f"{club_ru} — {len(entries)} фото", fill=(235, 235, 235), font=title_font)

    for index, entry in enumerate(entries):
        col, row = index % columns, index // columns
        x = pad + col * (cell + pad)
        y = 30 + pad + row * (cell + caption_h + pad)
        try:
            with Image.open(entry["path"]) as im:
                im = im.convert("RGBA")
                im.thumbnail((cell, cell))
                box = Image.new("RGBA", (cell, cell), (40, 43, 52, 255))
                box.paste(im, ((cell - im.width) // 2, (cell - im.height) // 2), im)
                sheet.paste(box.convert("RGB"), (x, y))
        except Exception:
            draw.rectangle([x, y, x + cell, y + cell], fill=(70, 40, 40))
        draw.text((x, y + cell + 2), (entry["db_name"] or "")[:26], fill=(255, 255, 255), font=name_font)
        draw.text((x, y + cell + 16), (entry.get("identity") or "")[:30], fill=(150, 200, 255), font=meta_font)
        draw.text((x, y + cell + 28), (entry.get("source") or "")[:30], fill=(140, 140, 140), font=meta_font)

    os.makedirs(CONTACT_SHEET_DIR, exist_ok=True)
    out_path = os.path.join(CONTACT_SHEET_DIR, f"{player_photos._slugify(club_ru)}.png")
    sheet.save(out_path)
    return out_path


def purge_unidentified(entries: list[dict], dry_run: bool) -> int:
    """
    Убирает старые файлы тех, кого опознать не удалось.

    Их фото клал прежний фетчер — глобальным поиском по фамилии, то есть ровно
    тем способом, который и приносил чужие лица. Раз личность не подтверждена,
    файл подозрителен: силуэт вместо фото честнее, чем незнакомый футболист.
    Удаляем и версию с клубом, и безклубную — вторая работает запасной у
    `get_photo_path`, иначе бы она и осталась на карточке.
    """
    removed = 0
    for entry in entries:
        if entry.get("status") != "unidentified":
            continue
        for path in (player_photos.get_cached_photo_path(entry["db_name"], entry["club"]),
                     player_photos.get_cached_photo_path(entry["db_name"], None)):
            if not os.path.isfile(path):
                continue
            if dry_run:
                logger.info(f"  [dry-run] удалил бы {os.path.basename(path)}")
            else:
                os.remove(path)
                logger.info(f"  удалён подозрительный файл: {os.path.basename(path)}")
            removed += 1
    return removed


def clear_telegram_media_cache(db_path: str, dry_run: bool) -> int:
    """Старые карточки лежат в Telegram по file_id — без сброса бот отдаст их же."""
    if not os.path.isabs(db_path):
        db_path = os.path.normpath(os.path.join(BASE_DIR, db_path))
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM telegram_media_cache").fetchone()[0]
        if dry_run:
            logger.info(f"  [dry-run] очистил бы telegram_media_cache: {count} записей")
            return count
        conn.execute("DELETE FROM telegram_media_cache")
        conn.commit()
        logger.info(f"  очищен telegram_media_cache: {count} записей")
        return count
    except Exception as e:
        logger.error(f"  не смог очистить telegram_media_cache: {e}")
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Основной прогон
# --------------------------------------------------------------------------

def process_club(club_ru: str, players: list[tuple[str, str]], manifest: dict,
                 force: bool, dry_run: bool) -> list[dict]:
    logger.info(f"=== {club_ru} ({len(players)} игроков) ===")
    tid, club_en, roster = fetch_club_roster(club_ru)
    if not roster:
        logger.error(f"  ростер не получен (id={tid}) — клуб пропущен")
        return [{"club": club_ru, "db_name": name, "status": "no_roster"} for name, _ in players]

    logger.info(f"  ростер FotMob: {club_en} (id={tid}), {len(roster)} игроков")
    entries: list[dict] = []

    for db_name, position in players:
        key = f"{club_ru}|{db_name}"
        target = player_photos.get_cached_photo_path(db_name, club_ru)
        previous = manifest.get(key) or {}

        # Старый кэш писал фетчер бота — там и лежат чужие лица, поэтому файл сам
        # по себе не повод пропустить игрока. Пропускаем только то, что этот
        # скрипт уже опознал и скачал.
        if not force and previous.get("status") == "ok" and os.path.exists(target):
            entries.append({**previous, "club": club_ru, "db_name": db_name, "path": target})
            continue

        identity, why = match_in_roster(db_name, position, roster)
        if not identity:
            fallback_identity, fallback_why = search_player_globally(db_name, club_en)
            if fallback_identity:
                identity, why = fallback_identity, fallback_why

        if not identity:
            logger.warning(f"  ✗ {db_name} ({position}): {why}")
            entries.append({"club": club_ru, "db_name": db_name, "position": position,
                            "status": "unidentified", "reason": why})
            continue

        label = f"{db_name} ({position}) → {identity['name']}"
        if dry_run:
            logger.info(f"  [dry-run] {label} [{why}]")
            entries.append({"club": club_ru, "db_name": db_name, "position": position,
                            "identity": identity["name"], "status": "planned", "reason": why})
            continue

        photo = download_photo(identity)
        if not photo:
            logger.warning(f"  ✗ {label}: фото не нашлось ни у одного источника")
            entries.append({"club": club_ru, "db_name": db_name, "position": position,
                            "identity": identity["name"], "status": "no_photo", "reason": why})
            continue

        os.makedirs(player_photos.PHOTOS_DIR, exist_ok=True)
        with open(target, "wb") as f:
            f.write(photo["data"])

        entry = {
            "club": club_ru, "club_en": club_en, "db_name": db_name, "position": position,
            "identity": identity["name"], "fotmob_id": identity.get("id"),
            "dob": identity.get("dob"), "match": why,
            "source": photo["source"], "url": photo["url"],
            "size": f"{photo['width']}x{photo['height']}", "alpha": photo["alpha"],
            "status": "ok", "path": target,
        }
        manifest[f"{club_ru}|{db_name}"] = {k: v for k, v in entry.items() if k != "path"}
        entries.append(entry)
        logger.info(f"  ✓ {label} — {photo['source']} {photo['width']}x{photo['height']}"
                    f"{'' if photo['alpha'] else ' (без прозрачности)'}")
        time.sleep(REQUEST_PAUSE)

    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Скачивает фотографии игроков, опознавая их внутри состава клуба.")
    parser.add_argument("--db", default="league.db",
                        help="путь к SQLite-базе (по умолчанию league.db в корне проекта)")
    parser.add_argument("--club", help="прогнать один клуб, например --club Арсенал")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, кого и как опознали — ничего не скачивать")
    parser.add_argument("--force", action="store_true",
                        help="перекачать даже то, что уже записано в манифест")
    parser.add_argument("--contact-sheet", action="store_true",
                        help="собрать по клубу лист с лицами для глазной проверки")
    parser.add_argument("--purge-unidentified", action="store_true",
                        help="удалить старые фото тех, кого опознать не удалось — они от прежнего фетчера")
    parser.add_argument("--clear-media-cache", action="store_true",
                        help="очистить telegram_media_cache, чтобы бот перерисовал карточки")
    args = parser.parse_args()

    squads = load_squads(args.db, args.club)
    if not squads:
        logger.error("Игроков не найдено — проверьте --db и --club.")
        return

    total = sum(len(v) for v in squads.values())
    logger.info(f"Клубов: {len(squads)}, игроков: {total}"
                f"{' — DRY RUN, ничего не пишем' if args.dry_run else ''}")

    manifest = load_manifest()
    all_entries: list[dict] = []
    for club_ru, players in squads.items():
        entries = process_club(club_ru, players, manifest, args.force, args.dry_run)
        all_entries.extend(entries)
        if not args.dry_run:
            save_manifest(manifest)
            if args.contact_sheet:
                sheet = build_contact_sheet(club_ru, entries)
                if sheet:
                    logger.info(f"  контактный лист: {sheet}")

    ok = [e for e in all_entries if e.get("status") == "ok"]
    planned = [e for e in all_entries if e.get("status") == "planned"]
    unidentified = [e for e in all_entries if e.get("status") == "unidentified"]
    no_photo = [e for e in all_entries if e.get("status") == "no_photo"]
    low_res = [e for e in ok if e.get("source", "").startswith("FotMob")]

    logger.info("=" * 60)
    if args.dry_run:
        logger.info(f"• Опознано и готово к загрузке: {len(planned)}")
    else:
        logger.info(f"• Скачано/уже есть: {len(ok)}")
        by_source: dict[str, int] = {}
        for entry in ok:
            by_source[entry.get("source", "?")] = by_source.get(entry.get("source", "?"), 0) + 1
        for source, count in sorted(by_source.items(), key=lambda x: -x[1]):
            logger.info(f"    {source}: {count}")
        if low_res:
            logger.info(f"• Низкое разрешение (192px, FotMob): {len(low_res)}")
    logger.info(f"• Не опознаны: {len(unidentified)}")
    for entry in unidentified:
        logger.info(f"    {entry['club']}: {entry['db_name']} — {entry.get('reason')}")
    if no_photo:
        logger.info(f"• Опознаны, но фото нет: {len(no_photo)}")
        for entry in no_photo:
            logger.info(f"    {entry['club']}: {entry['db_name']} → {entry.get('identity')}")

    if args.purge_unidentified and unidentified:
        logger.info(f"• Удаление старых фото у неопознанных: {purge_unidentified(all_entries, args.dry_run)} файлов")

    if args.clear_media_cache and not args.dry_run:
        clear_telegram_media_cache(args.db, args.dry_run)

    if not args.dry_run:
        logger.info(f"• Манифест: {MANIFEST_PATH}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
