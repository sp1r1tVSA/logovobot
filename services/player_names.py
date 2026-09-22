"""
services/player_names.py

Unified footballer name normalization and deduplication engine.
Provides deterministic normalization:
- Unicode NFKC + NFKD diacritic removal (accents, umlauts, tildes)
- European / Scandinavian / Turkish special characters (ø, æ, ß, œ, ł, đ, ı)
- Standardized hyphen and whitespace collapsing
- Cyrillic-to-Latin transliteration
- Footballer alias and token matching (e.g. 'Vini Jr' <-> 'Vinicius Junior')
"""

import re
import unicodedata

SPECIAL_CHAR_MAP: dict[str, str] = {
    'ø': 'o', 'Ø': 'O',
    'æ': 'ae', 'Æ': 'AE',
    'œ': 'oe', 'Œ': 'OE',
    'ß': 'ss',
    'ł': 'l', 'Ł': 'L',
    'đ': 'd', 'Đ': 'D',
    # Turkish dotless i has no decomposition, so NFKD would keep it and 'Yıldız'
    # would never meet 'YILDIZ'. Dotted 'İ' needs no entry: NFKD splits off its dot.
    'ı': 'i',
}

CYR_LAT_MAP: dict[str, str] = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'c',
    'ч': 'ch', 'ш': 'sh', 'щ': 'sh', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu',
    'я': 'ya',
}

ALIAS_TOKEN_MAP: dict[str, str] = {
    "vini": "vinicius",
    "jr": "junior",
    "младший": "junior",
    "жуниор": "junior",
    "leo": "lionel",
}


def normalize_player_name_key(name: str | None) -> str:
    """
    Produce a canonical normalized key for player name comparison and database indexing:
    1. Unicode NFKC normalization.
    2. Convert all hyphens/dashes, slashes, periods, and punctuation to spaces.
    3. Map Scandinavian and European ligatures (ø -> o, æ -> ae, etc.).
    4. Strip combining diacritical marks via NFKD decomposition.
    5. Transliterate Cyrillic to Latin.
    6. Lowercase, strip non-alphanumeric characters.
    7. Collapse multiple spaces into a single space.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    # Standardize unicode dashes, slashes, dots, underscores to space
    s = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uFE58\uFE63\uFF0D\.\-_'/]+", " ", s)
    # Map special European characters
    for k, v in SPECIAL_CHAR_MAP.items():
        s = s.replace(k, v)
    # Strip diacritics via NFKD
    decomposed = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    # Transliterate Cyrillic
    res = []
    for ch in s:
        low = ch.lower()
        res.append(CYR_LAT_MAP.get(low, low))
    s = "".join(res)
    # Strip non-alphanumeric except space
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Alias for backwards compatibility with services.ai.squad_recognizer
normalize_footballer_name = normalize_player_name_key


def is_same_footballer(name1: str, name2: str) -> bool:
    """
    Check if two names refer to the same footballer in the context of a single club's roster.
    Considers:
    - Exact normalized key equivalence
    - The same key with different word breaks ('Aldawsari' <-> 'Al Dawsari')
    - Common nicknames/aliases ('vini' <-> 'vinicius', 'jr' <-> 'junior')
    - Surname-only vs Full Name within the club ('Mbappé' <-> 'Kylian Mbappé')
    - Suffix matching ('Alexander-Arnold' <-> 'Trent Alexander-Arnold')
    """
    norm1 = normalize_player_name_key(name1)
    norm2 = normalize_player_name_key(name2)
    if not norm1 or not norm2:
        return False
    if norm1 == norm2:
        return True
    # Same letters, different word breaks: 'ALDAWSARI' <-> 'Al Dawsari'
    if norm1.replace(" ", "") == norm2.replace(" ", ""):
        return True

    toks1 = [ALIAS_TOKEN_MAP.get(t, t) for t in norm1.split()]
    toks2 = [ALIAS_TOKEN_MAP.get(t, t) for t in norm2.split()]
    if toks1 == toks2:
        return True

    # Check if single-token surname matches surname of multi-token name
    if len(toks1) == 1 and len(toks2) > 1:
        if len(toks1[0]) >= 4 and toks1[0] == toks2[-1]:
            return True
        if len(toks1[0]) >= 6 and toks1[0] == toks2[0]:
            return True
    elif len(toks2) == 1 and len(toks1) > 1:
        if len(toks2[0]) >= 4 and toks2[0] == toks1[-1]:
            return True
        if len(toks2[0]) >= 6 and toks2[0] == toks1[0]:
            return True

    # Suffix matching (e.g. "Alexander-Arnold" vs "Trent Alexander-Arnold")
    if len(toks1) >= 2 and len(toks2) > len(toks1):
        if toks2[-len(toks1):] == toks1:
            return True
    elif len(toks2) >= 2 and len(toks1) > len(toks2):
        if toks1[-len(toks2):] == toks2:
            return True

    return False
