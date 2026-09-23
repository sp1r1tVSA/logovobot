"""
services/graphics/player_identity.py

Опознание игрока **внутри состава его клуба** и загрузка фото, проверенного на
совпадение личности. Общий код для фетчера бота (`player_photos.fetch_and_cache`)
и для массовой перезаливки `scripts/fetch_squad_photos.py`.

Зачем. Состав читается OCR из русифицированного FC, там одни фамилии заглавными
(«MENDY», «MARTÍNEZ», «BRADLEY»), и глобальный поиск по такой строке регулярно
приводил чужого футболиста. Состав же у участников реальный, поэтому клуб —
не подсказка, а ограничение:

1. **Ростер** клуба из FotMob (`/api/data/teams?id=`): полное имя, дата
   рождения, позиция, id.
2. **Сопоставление** строго внутри ростера — тиры EXACT → все токены →
   фамилия → токен → fuzzy. Ничья разводится позицией, затем известностью,
   и только с большим отрывом. Неразведённая ничья — отказ: пустая карточка
   восстановима, чужое лицо нет.
3. **Фото** — каскад по качеству, где каждый источник сверяется с личностью:
   TheSportsDB (клуб или дата рождения) → SoFIFA (клуб в строке поиска) →
   FotMob (192px, личность гарантирована id).

Модуль не пишет файлов: `download_photo` возвращает байты, а куда их положить,
решает вызывающий.
"""

import io
import re
import json
import time
import difflib
import logging
import threading
import unicodedata
import urllib.parse
import urllib.request

from services.graphics import player_photos

logger = logging.getLogger(__name__)

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
    "nacho fernandez": "Nacho",
    "abner vinicius": "Abner",
    "pedro goncalves": "Pote",
    "desmet": "De Smet",
    "gorrotxa": "Gorrotxategi",
    "al juwair": "Al Juwayr",
    "thekri": "Thakri",
    "al shanqeeti": "Al Shanqiti",
    "batagov": "Batahov",
    "doechi": "Doekhi",
    "boushal": "Bu Washl",
    "balobaid": "Saad Yaslam",
    "gamer": "Garner",
    "urion": "Centurion",
}

# Игроки, перешедшие в другой клуб в базе FotMob (или отсутствующие в актуальном ростере клуба),
# но играющие за этот клуб в турнире. Ключ: (название клуба в РФ, _norm(имя в БД)).
# Прямая привязка исключает ошибки сопоставления и гарантирует загрузку проверенного фото.
PINNED_PLAYERS: dict[tuple[str, str], dict] = {
    ("Аталанта", "bakker"): {
        "id": 891870, "name": "Mitchel Bakker", "dob": "2000-06-20", "positions": ["LB"],
    },
    ("Атлетик Бильбао", "boiro"): {
        "id": 1331249, "name": "Adama Boiro", "dob": "2002-06-22", "positions": ["LB"],
    },
    ("Атлетик Бильбао", "gorosabel"): {
        "id": 839893, "name": "Andoni Gorosabel", "dob": "1996-08-04", "positions": ["RB"],
    },
    ("Бешикташ", "hadziahmetovic"): {
        "id": 639558, "name": "Amir Hadžiahmetović", "dob": "1997-03-08", "positions": ["CDM"],
    },
    ("Бурирам", "toku"): {
        "id": 888720, "name": "Emmanuel Toku", "dob": "2000-07-10", "positions": ["CAM"],
    },
    ("Бурирам", "ko myeong seok"): {
        "id": 828259, "name": "Myeong-Seok Ko", "dob": "1997-01-29", "positions": ["CB"],
    },
    ("Вулверхэмптон", "arias"): {
        "id": 1023030, "name": "Jhon Arias", "dob": "1997-10-21", "positions": ["LW"],
    },
    ("Вулверхэмптон", "joao gomes"): {
        "id": 1174672, "name": "João Gomes", "dob": "2001-02-12", "positions": ["CM"],
    },
    ("Интер Майами", "allen"): {
        "id": 1340790, "name": "Noah Allen", "dob": "2004-04-28", "positions": ["CB"],
    },
    ("Майнц", "hong hyeon seok"): {
        "id": 925345, "name": "Hyun-Seok Hong", "dob": "1999-06-16", "positions": ["ST"],
    },
    ("Ренн", "seidu"): {
        "id": 1177779, "name": "Alidu Seidu", "dob": "2000-06-04", "positions": ["RB"],
    },
    ("Трабзонспор", "lundstram"): {
        "id": 429955, "name": "John Lundstram", "dob": "1994-02-18", "positions": ["CDM"],
    },
    ("Хоффенхайм", "akpoguma"): {
        "id": 353519, "name": "Kevin Akpoguma", "dob": "1995-04-19", "positions": ["LB"],
    },
    ("Эвертон", "patterson"): {
        "id": 1112684, "name": "Nathan Patterson", "dob": "2001-10-16", "positions": ["RB"],
    },
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
        from PIL import Image

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
# Опознание целиком: закреплённые → ростер → глобальный поиск со сверкой клуба
# --------------------------------------------------------------------------

def _pinned_identity(club_ru: str, db_name: str, position: str | None, club_en: str) -> dict | None:
    pinned = PINNED_PLAYERS.get((club_ru, _norm(db_name)))
    if not pinned:
        return None
    return {
        "id": pinned["id"],
        "name": pinned["name"],
        "dob": pinned.get("dob", ""),
        "positions": pinned.get("positions", [position] if position else []),
        "number": None,
        "rating": 7.0,
        "value": 1000000,
        "club_en": pinned.get("club_en", club_en),
    }


def identify_in_roster(db_name: str, position: str | None, club_ru: str,
                       club_en: str, roster: list[dict]) -> tuple[dict | None, str]:
    """
    Опознаёт игрока по уже полученному ростеру. Возвращает `(личность | None, объяснение)`.

    Глобальный поиск здесь — не откат к старому поведению: найденный игрок
    принимается только если его текущий клуб совпал с клубом участника.
    """
    pinned = _pinned_identity(club_ru, db_name, position, club_en)
    if pinned:
        return pinned, f"зафиксирован (id={pinned['id']})"

    identity, why = match_in_roster(db_name, position, roster)
    if identity:
        return identity, why
    fallback, fallback_why = search_player_globally(db_name, club_en)
    if fallback:
        return fallback, fallback_why
    return None, why


# Бот опознаёт игроков по одному — на каждую карточку и каждый состав. Ростер
# клуба за это время не меняется, поэтому держим его в памяти; неудачу — недолго,
# чтобы сбой сети не отключал клуб надолго, но и не долбил FotMob на каждой карточке.
ROSTER_TTL = 6 * 3600
ROSTER_FAILURE_TTL = 10 * 60

_roster_cache: dict[str, tuple[float, tuple[int | None, str, list[dict]]]] = {}
_roster_lock = threading.Lock()


def get_club_roster(club_ru: str) -> tuple[int | None, str, list[dict]]:
    """`fetch_club_roster` с кэшем в памяти процесса."""
    now = time.monotonic()
    with _roster_lock:
        cached = _roster_cache.get(club_ru)
        if cached and cached[0] > now:
            return cached[1]
        result = fetch_club_roster(club_ru)
        ttl = ROSTER_TTL if result[2] else ROSTER_FAILURE_TTL
        _roster_cache[club_ru] = (now + ttl, result)
        return result


# Ответ на «кто это» при полученном ростере живёт столько же, сколько ростер:
# карточка неопознанного игрока иначе повторяла бы глобальный поиск на каждой отрисовке.
_identity_cache: dict[tuple[str, str, str], tuple[float, tuple[dict | None, str]]] = {}


def clear_roster_cache() -> None:
    with _roster_lock:
        _roster_cache.clear()
        _identity_cache.clear()


def identify_player(db_name: str, club_ru: str,
                    position: str | None = None) -> tuple[dict | None, str, bool]:
    """
    Опознаёт игрока из состава клуба `club_ru`.

    Возвращает `(личность | None, объяснение, получен ли ростер)`. Третий флаг
    отличает «игрок не опознан» (ростер есть, совпадения нет — решение окончательное
    до смены ростера) от «клуб не удалось загрузить» (сбой сети или неизвестный клуб).
    """
    tid, club_en, roster = get_club_roster(club_ru)
    if not roster:
        pinned = _pinned_identity(club_ru, db_name, position, club_en)
        if pinned:
            return pinned, f"зафиксирован (id={pinned['id']})", False
        return None, f"ростер клуба не получен (id={tid})", False
    key = (club_ru, _norm(db_name), (position or "").strip().upper())
    now = time.monotonic()
    with _roster_lock:
        cached = _identity_cache.get(key)
    if cached and cached[0] > now:
        return (*cached[1], True)

    identity, why = identify_in_roster(db_name, position, club_ru, club_en, roster)
    with _roster_lock:
        _identity_cache[key] = (now + ROSTER_TTL, (identity, why))
    return identity, why, True
