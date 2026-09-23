"""
player_photos.py

Fetches and caches player portrait photos.

Когда клуб известен (а из состава он известен всегда), игрок опознаётся внутри
ростера своего клуба — `services/graphics/player_identity.py` — и фото берётся
только у источника, сверенного с этой личностью. Не опознан — фото нет:
пустая карточка исправима, чужой футболист на ней — нет.

Без клуба остаётся прежний поиск по имени у провайдеров:
1. TheSportsDB API (500-700px transparent cutouts — best quality)
2. SoFIFA CDN (360px official EA FC renders — consistent, matches the game roster)
3. Wikipedia / Wikimedia Commons API (real portraits, last resort)
4. FotMob Search API (192px palette thumbnails — emergency fallback only)

Имена игроков приходят из OCR русифицированного FC, поэтому перед запросом
к провайдерам кириллическое имя разрешается в латиницу через межъязыковые
ссылки русской Википедии (см. `_resolve_latin_name`). Фото при этом
кэшируется под исходным именем, каким его прочитал OCR.

Transfermarkt и SofaScore проверены и отброшены: оба отдают 403/202 на
запросы без браузера (Cloudflare), стабильно использовать их нельзя.
"""

import io
import os
import re
import json
import time
import difflib
import logging
import threading
import unicodedata
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)

from pathlib import Path

# Project root directory (services/graphics -> services -> root)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR   = str(PROJECT_ROOT)
PHOTOS_DIR = str(PROJECT_ROOT / "assets" / "players")

# Latin ligatures and special character replacement
TRANSLIT_LATIN = {
    'ø': 'o', 'Ø': 'O',
    'æ': 'ae', 'Æ': 'AE',
    'œ': 'oe', 'Œ': 'OE',
    'ß': 'ss',
    'ł': 'l', 'Ł': 'L',
    'đ': 'd', 'Đ': 'D',
    'ð': 'd', 'Ð': 'D',
    'þ': 'th', 'Þ': 'TH',
    'ı': 'i', 'İ': 'I',
}

# Individual headers for API providers
FOTMOB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.fotmob.com/"
}

WIKI_HEADERS = {
    "User-Agent": "Logovobot/1.0 (https://t.me/logovobot; contact@logovo.bot)",
    "Accept": "application/json"
}

SOFIFA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # CDN отдаёт 403 без Referer; Accept без image/webp заставляет вернуть PNG, а не WebP.
    "Accept": "image/png,*/*",
    "Referer": "https://sofifa.com/"
}

STD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Версия EA FC, чьи рендеры тянем с SoFIFA, и максимальный доступный размер
# (480/540/720 отдают 404 — проверено).
SOFIFA_FC_VERSION = "26"
SOFIFA_RENDER_SIZE = "360"

# Порог похожести части имени и части заголовка статьи Википедии: 0.83 пропускает
# «Холанд»/«Холанн», при этом «Мбаппе»/«Месси» даёт только 0.36.
NAME_MATCH_RATIO = 0.8

# Ниже этих порогов картинка — заглушка или мусор, а не портрет игрока.
# Порог в пикселях совпадает с планкой `scripts/refresh_all_player_cards.py`:
# если фетчер примет то, что скрипт считает браком, каждый прогон будет качать
# один и тот же файл заново.
MIN_PHOTO_BYTES = 3000
MIN_PHOTO_PIXELS = 250

_photos_lock = threading.Lock()

# Разрешённые RU→EN имена держим на диске: Википедию достаточно спросить один раз.
NAME_MAP_PATH = str(PROJECT_ROOT / "assets" / "players" / "_name_map.json")
_name_map_lock = threading.Lock()
_name_map_cache: dict | None = None


PLAYER_NAME_ALIASES = {
    "oxl.-chamberlain": "Alex Oxlade-Chamberlain",
    "oxl. chamberlain": "Alex Oxlade-Chamberlain",
    "vítor carvalho": "Vitor Carvalho",
    "vitor carvalho": "Vitor Carvalho",
}


def _ensure_photos_dir() -> None:
    os.makedirs(PHOTOS_DIR, exist_ok=True)


def _normalize_name(name: str) -> str:
    """Убирает акценты/диакритику и нормализует спецсимволы латиницы."""
    clean_lower = name.lower().strip()
    if clean_lower in PLAYER_NAME_ALIASES:
        name = PLAYER_NAME_ALIASES[clean_lower]
    for k, v in TRANSLIT_LATIN.items():
        name = name.replace(k, v)
    nfkd_form = unicodedata.normalize('NFKD', name)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)]).strip()


def _slugify(name: str) -> str:
    """Convert player name to a safe filename slug."""
    clean = _normalize_name(name).lower()
    clean = re.sub(r"[^\w\s-]", "", clean)
    return re.sub(r"[\s]+", "_", clean)


def _has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[а-яёА-ЯЁ]", text or ""))


def _strip_parenthetical(name: str) -> str:
    """`Arthur Gomes (footballer, born 1997)` → `Arthur Gomes`."""
    return re.sub(r"\s*\([^)]*\)", "", name or "").strip()


def _name_tokens(name: str) -> list[str]:
    """Значимые части имени; инициалы и мусор вида «К.» отбрасываются."""
    cleaned = re.sub(r"[^\w\s-]", " ", _strip_parenthetical(name).lower())
    return [t for t in cleaned.split() if len(t) >= 4]


def _title_matches_name(ru_title: str, player_name: str) -> bool:
    """
    Статья описывает именно запрошенного футболиста?

    Клуб участника в игре почти никогда не совпадает с реальным клубом
    футболиста (игрок мог купить кого угодно), поэтому запрос вида
    «Мбаппе Порту футболист» спокойно возвращает статью про Месси — с нужной
    категорией и всем остальным. Требуем, чтобы хотя бы одна значимая часть
    запрошенного имени совпала с частью заголовка найденной статьи.

    Сравнение нечёткое: игра и Википедия транслитерируют по-разному — в игре
    «Холанд», в статье «Холанн».
    """
    tokens = _name_tokens(player_name)
    title_tokens = _name_tokens(ru_title)
    if not tokens or not title_tokens:
        return False

    for token in tokens:
        for title_token in title_tokens:
            if token in title_token or title_token in token:
                return True
            if difflib.SequenceMatcher(None, token, title_token).ratio() >= NAME_MATCH_RATIO:
                return True
    return False


def _load_name_map() -> dict:
    """Disk-backed RU→EN name cache, so Wikipedia is queried once per player."""
    global _name_map_cache
    if _name_map_cache is not None:
        return _name_map_cache
    try:
        with open(NAME_MAP_PATH, "r", encoding="utf-8") as f:
            _name_map_cache = json.load(f)
    except Exception:
        _name_map_cache = {}
    return _name_map_cache


def _save_name_map(mapping: dict) -> None:
    try:
        _ensure_photos_dir()
        with open(NAME_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception as e:
        logger.debug(f"[NameMap] Could not persist name map: {e}")


def _wiki_ru_to_en(player_name: str, team: str | None = None) -> tuple[str | None, bool]:
    """
    Разрешает русское имя футболиста в латинское через межъязыковые ссылки
    ru.wikipedia. Возвращает `(латинское имя | None, были ли сетевые ошибки)`.

    Сначала ищем по одному имени: клуб участника — это его клуб в игре, а не
    реальный клуб футболиста, так что как подсказка он чаще мешает, чем помогает.
    Запрос с клубом идёт второй попыткой — он выручает только на однофамильцах,
    чей реальный клуб совпал с игровым.
    """
    queries = [f"{player_name} футболист"]
    if team:
        queries.append(f"{player_name} {team} футболист")

    failed = False
    for query in queries:
        try:
            encoded = urllib.parse.quote(query)
            url = (
                "https://ru.wikipedia.org/w/api.php?action=query&list=search"
                f"&srsearch={encoded}&srlimit=3&format=json"
            )
            req = urllib.request.Request(url, headers=WIKI_HEADERS)
            with urllib.request.urlopen(req, timeout=5) as resp:
                hits = json.loads(resp.read().decode("utf-8")).get("query", {}).get("search", [])

            for hit in hits[:3]:
                title = urllib.parse.quote(hit["title"])
                ll_url = (
                    "https://ru.wikipedia.org/w/api.php?action=query"
                    f"&titles={title}&prop=langlinks|categories&lllang=en"
                    "&cllimit=50&format=json"
                )
                ll_req = urllib.request.Request(ll_url, headers=WIKI_HEADERS)
                with urllib.request.urlopen(ll_req, timeout=5) as ll_resp:
                    pages = json.loads(ll_resp.read().decode("utf-8")).get("query", {}).get("pages", {})
                for _, pdata in pages.items():
                    # Поиск легко выдаёт статью о самом клубе или о постороннем
                    # футболисте: нужна и категория про футболистов, и совпадение
                    # имени с заголовком статьи.
                    categories = " ".join(
                        (c.get("title") or "") for c in (pdata.get("categories") or [])
                    ).lower()
                    if "футболист" not in categories:
                        continue
                    if not _title_matches_name(pdata.get("title") or "", player_name):
                        continue
                    for link in pdata.get("langlinks") or []:
                        latin = _strip_parenthetical(link.get("*") or "")
                        if latin:
                            return latin, failed
        except Exception as e:
            failed = True
            logger.debug(f"[NameResolve] Error for '{query}': {e}")
    return None, failed


def _resolve_latin_name(player_name: str, team: str | None = None) -> str:
    """Return the Latin form used to query providers. Latin names pass through."""
    if not _has_cyrillic(player_name):
        return player_name

    key = f"{player_name}|{team}" if team else player_name
    mapping = _load_name_map()
    if key in mapping:
        return mapping[key] or player_name

    latin, failed = _wiki_ru_to_en(player_name, team)

    # Пустой результат кэшируем только если Википедия ответила и ничего не нашла.
    # Иначе разовый сбой сети навсегда прибил бы игрока к кириллическому поиску.
    if latin or not failed:
        with _name_map_lock:
            mapping = _load_name_map()
            mapping[key] = latin or ""
            _save_name_map(mapping)

    if latin:
        logger.info(f"[NameResolve] '{player_name}' ({team}) → '{latin}'")
        return latin

    logger.info(f"[NameResolve] Could not resolve '{player_name}' to Latin, querying as-is")
    return player_name


def get_cached_photo_path(player_name: str, disambiguator: str | None = None) -> str:
    slug = _slugify(player_name)
    if disambiguator:
        slug = f"{slug}_{_slugify(str(disambiguator))}"
    return os.path.join(PHOTOS_DIR, f"{slug}.png")


def is_cached(player_name: str, disambiguator: str | None = None) -> bool:
    path = get_cached_photo_path(player_name, disambiguator)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return True
    path_no_dis = get_cached_photo_path(player_name, None)
    return os.path.isfile(path_no_dis) and os.path.getsize(path_no_dis) > 0


def get_photo_path(player_name: str, disambiguator: str | None = None) -> str | None:
    """Check if photo exists on disk without network request."""
    if disambiguator:
        path = get_cached_photo_path(player_name, disambiguator)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    path = get_cached_photo_path(player_name, None)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    return None


def _get_fotmob_url(player_name: str) -> str | None:
    clean_name = _normalize_name(player_name)
    search_terms = [clean_name]
    if "-" in clean_name:
        search_terms.append(clean_name.replace("-", " "))
        search_terms.append(clean_name.split("-")[-1].strip())
    parts = clean_name.split()
    if len(parts) > 2:
        search_terms.append(f"{parts[0]} {parts[-1]}")
    if len(parts) >= 2 and parts[-1] not in search_terms:
        search_terms.append(parts[-1])

    for term in search_terms:
        encoded = urllib.parse.quote(term)
        url = f"https://apigw.fotmob.com/searchapi/suggest?term={encoded}"
        req = urllib.request.Request(url, headers=FOTMOB_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                squad = data.get("squadMemberSuggest", [])
                if squad and len(squad) > 0:
                    options = squad[0].get("options", [])
                    for opt in options:
                        payload = opt.get("payload", {})
                        if payload.get("isCoach"):
                            continue
                        player_id = payload.get("id")
                        if player_id:
                            return f"https://images.fotmob.com/image_resources/playerimages/{player_id}.png"
        except Exception as e:
            logger.debug(f"[FotMob] Error for '{term}': {e}")
    return None


def _get_sofifa_url(player_name: str) -> str | None:
    """
    Официальный рендер игрока из EA FC через CDN SoFIFA.

    Покрытие здесь ровно то же, что у самой игры, из которой читается состав,
    поэтому SoFIFA находит и молодых игроков, которых нет в TheSportsDB.
    """
    clean_name = _normalize_name(player_name)
    search_terms = [clean_name]
    parts = clean_name.split()
    if len(parts) > 2:
        search_terms.append(f"{parts[0]} {parts[-1]}")
    if len(parts) >= 2:
        search_terms.append(parts[-1])

    for term in search_terms:
        encoded = urllib.parse.quote(term)
        url = f"https://sofifa.com/players?keyword={encoded}"
        req = urllib.request.Request(url, headers=SOFIFA_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", "replace")
            ids = re.findall(r"/player/(\d+)/", html)
            if ids:
                pid = ids[0].zfill(6)
                return (
                    f"https://cdn.sofifa.net/players/{pid[:3]}/{pid[3:6]}/"
                    f"{SOFIFA_FC_VERSION}_{SOFIFA_RENDER_SIZE}.png"
                )
        except Exception as e:
            logger.debug(f"[SoFIFA] Error for '{term}': {e}")
    return None


def _get_thesportsdb_url(player_name: str) -> str | None:
    clean_name = _normalize_name(player_name).lower().strip()
    search_terms = [clean_name]
    if "-" in clean_name:
        search_terms.append(clean_name.replace("-", " "))
    parts = clean_name.split()
    if len(parts) > 2:
        search_terms.append(f"{parts[0]} {parts[-1]}")

    for term in search_terms:
        encoded = urllib.parse.quote(term)
        url = f"https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p={encoded}"
        req = urllib.request.Request(url, headers=STD_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                players = data.get("player")
                if players:
                    for p in players:
                        pos = str(p.get("strPosition") or "").lower()
                        if "manager" in pos or "coach" in pos:
                            continue
                        found_name = _normalize_name(p.get("strPlayer") or "").lower()
                        # Strict name matching
                        if clean_name not in found_name and found_name not in clean_name:
                            continue
                        # Prioritize transparent PNG cutouts (with torso & kit) over square thumbnails
                        cutout = p.get("strCutout") or p.get("strRender")
                        if cutout:
                            return cutout
                        thumb = p.get("strThumb")
                        if thumb:
                            return thumb
        except Exception as e:
            logger.debug(f"[TheSportsDB] Error for '{term}': {e}")
    return None


def _get_wikipedia_url(player_name: str) -> str | None:
    clean_name = _normalize_name(player_name)
    search_terms = [player_name, clean_name]
    for term in search_terms:
        encoded = urllib.parse.quote(term)
        url = f"https://en.wikipedia.org/w/api.php?action=query&titles={encoded}&prop=pageimages&format=json&pithumbsize=500"
        req = urllib.request.Request(url, headers=WIKI_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                pages = data.get("query", {}).get("pages", {})
                for _, pdata in pages.items():
                    if "thumbnail" in pdata:
                        return pdata["thumbnail"]["source"]
        except Exception as e:
            logger.debug(f"[Wikipedia] Error for '{term}': {e}")
    return None


def _photo_quality(data: bytes) -> str | None:
    """
    Оценивает скачанные байты: `"cutout"` — прозрачная вырезка, `"flat"` —
    годный, но непрозрачный портрет, `None` — заглушка или мусор.

    Заглушки «нет фото» и обрезки-марки провайдеры отдают с кодом 200, поэтому
    раньше они попадали в кэш как настоящие портреты. Прозрачность выделена
    отдельно: карточки рисуют игрока поверх фона, и плоский прямоугольник там
    выглядит заметно хуже вырезки.
    """
    if len(data) < MIN_PHOTO_BYTES:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            has_alpha = "A" in im.getbands() or "transparency" in im.info
    except Exception as e:
        logger.debug(f"[Download] Rejected undecodable image: {e}")
        return None
    if width < MIN_PHOTO_PIXELS or height < MIN_PHOTO_PIXELS:
        return None
    return "cutout" if has_alpha else "flat"


def _write_photo(dest_path: str, data: bytes) -> bool:
    try:
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except OSError as e:
        logger.warning(f"[Download] Could not write photo to {dest_path}: {e}")
        return False


def _fetch_photo_bytes(url: str, headers: dict | None = None) -> bytes | None:
    if headers is None:
        headers = STD_HEADERS
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read()
    except Exception as e:
        logger.debug(f"[Download] Error fetching from {url}: {e}")
        return None


def fetch_and_cache(player_name: str, team: str | None = None, force_refresh: bool = False,
                    position: str | None = None) -> str | None:
    """
    Fetch photo for player_name and cache it locally.

    С клубом — опознание в ростере клуба и никакого отката к глобальному поиску
    (`_fetch_identified`). Без клуба — поиск по имени (`_fetch_by_name`).
    `position` из состава разводит однофамильцев внутри ростера.
    """
    _ensure_photos_dir()
    cached = get_cached_photo_path(player_name, team)
    
    if not force_refresh:
        existing = get_photo_path(player_name, team)
        if existing:
            return existing

    with _photos_lock:
        if not force_refresh:
            # Double check inside lock
            existing = get_photo_path(player_name, team)
            if existing:
                return existing

        # Кириллицу из OCR разрешаем в латиницу, иначе провайдеры не находят
        # игрока вовсе и всё скатывается к худшему источнику.
        query_name = _resolve_latin_name(player_name, team)

        if team:
            return _fetch_identified(player_name, query_name, team, position, cached)
        return _fetch_by_name(player_name, query_name, cached)


def _fetch_identified(player_name: str, query_name: str, team: str,
                      position: str | None, dest_path: str) -> str | None:
    """
    Фото игрока, опознанного в ростере клуба `team`.

    Глобальный поиск по имени здесь намеренно не используется даже как запасной:
    именно он приводил чужие лица — «BRADLEY» из Ливерпуля становился Barcola.
    Если игрок не опознан, карточка остаётся с силуэтом.
    """
    from services.graphics import player_identity

    identity, why, _ = player_identity.identify_player(query_name, team, position)
    if not identity:
        logger.info(f"[Identity] '{player_name}' ({team}) не опознан: {why} — фото не ставим")
        return None

    photo = player_identity.download_photo(identity)
    if not photo:
        logger.info(f"[Identity] '{player_name}' ({team}) → {identity['name']}: "
                    f"фото нет ни у одного источника")
        return None

    if _write_photo(dest_path, photo["data"]):
        logger.info(f"[{photo['source']}] ✅ '{player_name}' ({team}) → {identity['name']} "
                    f"[{why}] {photo['width']}x{photo['height']}")
        return dest_path
    return None


def _fetch_by_name(player_name: str, query_name: str, cached: str) -> str | None:
    """Прежний поиск по имени у провайдеров — только когда клуб неизвестен."""
    # Порядок по качеству: 500-700px вырезки → 360px рендер EA FC →
    # реальный портрет из Википедии → 192px превью FotMob как крайний случай.
    providers = [
        ("TheSportsDB", _get_thesportsdb_url, STD_HEADERS),
        ("SoFIFA", _get_sofifa_url, SOFIFA_HEADERS),
        ("Wikipedia", _get_wikipedia_url, WIKI_HEADERS),
        ("FotMob", _get_fotmob_url, FOTMOB_HEADERS),
    ]

    # Вырезка важнее порядка провайдеров: плоский портрет от первого
    # источника не должен перекрывать вырезку у следующего. Плоскую
    # картинку запоминаем и пишем только если вырезки нет ни у кого —
    # иначе `refresh_all_player_cards` будет вечно пытаться её улучшить.
    fallback: tuple[str, bytes] | None = None

    for provider_name, get_url_func, p_headers in providers:
        photo_url = get_url_func(query_name)
        if not photo_url:
            continue
        data = _fetch_photo_bytes(photo_url, headers=p_headers)
        quality = _photo_quality(data) if data else None
        if quality == "cutout":
            if _write_photo(cached, data):
                logger.info(f"[{provider_name}] ✅ Downloaded cutout for '{player_name}'")
                return cached
            return None
        if quality == "flat" and fallback is None:
            fallback = (provider_name, data)
            logger.debug(f"[{provider_name}] Keeping opaque photo as a fallback.")
        elif quality is None:
            logger.debug(f"[{provider_name}] ⚠️ Photo URL found but file is unusable (404/403/заглушка).")

    if fallback:
        provider_name, data = fallback
        if _write_photo(cached, data):
            logger.info(f"[{provider_name}] ✅ Downloaded photo for '{player_name}' (без прозрачности)")
            return cached

    logger.info(f"No photo found for player '{player_name}' in any provider")
    return None


def get_player_photo(player_name: str, team: str | None = None, force_refresh: bool = False) -> str | None:
    """Convenience alias to fetch and return cached photo path on demand."""
    if not force_refresh:
        cached = get_photo_path(player_name, team)
        if cached:
            return cached
    return fetch_and_cache(player_name, team, force_refresh=force_refresh)


def fetch_all_players(players: list[str] | list[tuple]) -> dict[str, str | None]:
    """
    Bulk-fetch photos for a list of players.

    Элемент — имя, `(имя, клуб)` или `(имя, клуб, позиция)`.
    """
    results: dict[str, str | None] = {}
    for item in players:
        if isinstance(item, tuple):
            name, team, position = (tuple(item) + (None, None))[:3]
        else:
            name, team, position = item, None, None
        key = f"{name} ({team})" if team else name
        results[key] = fetch_and_cache(name, team, position=position)
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) > 1:
        p_name = sys.argv[1]
        t_name = sys.argv[2] if len(sys.argv) > 2 else None
        res = fetch_and_cache(p_name, t_name)
        if res:
            print(f"✅ Photo downloaded to: {res}")
        else:
            print(f"❌ Failed to fetch photo for '{p_name}'")
    else:
        print("Usage: python player_photos.py <Player Name> [Team Name]")