"""Club name registry and name resolution.

Canonical club names, the alias dictionary and fuzzy resolution live here rather
than in database.py: none of this touches SQL, and keeping it in the storage layer
forced every consumer of the registry to drag the database module along with it
(audit item P3-7).

Layering rule: this module sits BELOW database.py and must never import it.
It depends on config only, so importing it can never create a cycle.
"""
import difflib
import logging
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

logger = logging.getLogger(__name__)


# Алиасы под клубы, которые реально играют в сезоне. Канон алиаса обязан быть в
# config.CLUB_REGISTRY — алиас на клуб вне реестра отбрасывается при загрузке
# (get_orphan_aliases) и только засоряет аудит.
#
# Ключи сравниваются уже после normalize_team_name, поэтому пишем их сразу
# нормализованными: нижний регистр, 'э'/'ё' свёрнуты в 'е', дефисы — пробелы.
#
# Чего здесь сознательно нет: форм, которые подходят двум живым клубам сразу —
# «реал» (Мадрид/Сосьедад), «интер» (Милан/Майами), «манчестер» (Сити/Юнайтед),
# «мадрид» (Реал/Атлетико), «юнайтед» (МЮ/Ньюкасл), «пари»/«paris» (Париж/ПСЖ).
# Они обязаны не резолвиться: пустой ответ обратим, чужой канон — нет. Нет тут и
# «байа», «фенер», «фенербахе»: первое короче FUZZY_MIN_LEN и обязано молчать,
# остальные два уже ловят префиксный и фаззи тиры — алиас отобрал бы у них работу.
TEAM_ALIASES = {
    # ─── DIV_1 ──────────────────────────────────────────────────────────────
    "лидс юнайтед": "Лидс", "leeds": "Лидс", "leeds united": "Лидс",
    "rennes": "Ренн", "stade rennais": "Ренн", "стад ренн": "Ренн",
    "nice": "Ницца", "ogc nice": "Ницца", "ницца огс": "Ницца",
    "nashville": "Нэшвилл", "nashville sc": "Нэшвилл",
    # Порту
    "порту": "Порту", "порто": "Порту", "порт": "Порту", "португал": "Порту",
    "porto": "Порту", "portu": "Порту", "fc porto": "Порту", "фк порту": "Порту", "фк порто": "Порту",
    "west ham": "Вест Хэм", "west ham united": "Вест Хэм", "вест хем юнайтед": "Вест Хэм",
    "wolfsburg": "Вольфсбург", "vfl wolfsburg": "Вольфсбург",
    "fiorentina": "Фиорентина", "фиора": "Фиорентина", "виола": "Фиорентина",
    "lazio": "Лацио", "ss lazio": "Лацио",
    "marseille": "Марсель", "olympique marseille": "Марсель", "олимпик марсель": "Марсель",
    "lille": "Лилль", "losc": "Лилль",
    "айнтрахт франкфурт": "Айнтрахт", "франкфурт": "Айнтрахт",
    "eintracht": "Айнтрахт", "eintracht frankfurt": "Айнтрахт", "frankfurt": "Айнтрахт",
    "mainz": "Майнц", "mainz 05": "Майнц", "майнц 05": "Майнц",
    "burnley": "Бернли",
    # Будё Глимт. Ключи проходят через normalize_team_name, поэтому ё/ë, дефис и
    # слэш тут уже схлопнуты — латиница же своя, фаззи через алфавиты не работает.
    "буде глимт": "Будё Глимт", "будеглимт": "Будё Глимт", "буде": "Будё Глимт", "глимт": "Будё Глимт",
    "bodo glimt": "Будё Глимт", "bodoe glimt": "Будё Глимт", "bodo": "Будё Глимт", "glimt": "Будё Глимт",
    "koln": "Кельн", "cologne": "Кельн", "1 fc koln": "Кельн",

    # ─── DIV_2 ──────────────────────────────────────────────────────────────
    "вулвз": "Вулверхэмптон", "wolves": "Вулверхэмптон", "wolverhampton": "Вулверхэмптон",
    "buriram": "Бурирам", "buriram united": "Бурирам", "бурирам юнайтед": "Бурирам",
    "valencia": "Валенсия", "valencia cf": "Валенсия",
    "celta": "Сельта", "celta vigo": "Сельта", "сельта виго": "Сельта",
    # Ривер Плейт
    "ривер плейт": "Ривер Плейт", "ривер": "Ривер Плейт", "плейт": "Ривер Плейт", "ривера": "Ривер Плейт",
    "river plate": "Ривер Плейт", "river": "Ривер Плейт",
    # Аякс
    "аякс": "Аякс", "аякса": "Аякс", "аяксу": "Аякс", "аяксе": "Аякс",
    "ajax": "Аякс", "afc ajax": "Аякс",
    "аякс амстердам": "Аякс", "ajax amsterdam": "Аякс",
    # Спортинг
    "спортинг": "Спортинг", "спортнг": "Спортинг", "спортинга": "Спортинг", "спорт": "Спортинг",
    "sporting": "Спортинг", "sporting cp": "Спортинг", "спортинг лиссабон": "Спортинг",
    "monaco": "Монако", "as monaco": "Монако", "ас монако": "Монако",
    # Бенфика
    "бенфика": "Бенфика", "бенфику": "Бенфика", "бенфике": "Бенфика", "бенфики": "Бенфика", "бенфа": "Бенфика",
    "benfica": "Бенфика", "sl benfica": "Бенфика", "бенфика лиссабон": "Бенфика",
    "fulham": "Фулхэм",
    "hoffenheim": "Хоффенхайм", "tsg hoffenheim": "Хоффенхайм", "хофенхайм": "Хоффенхайм",
    "lens": "Ланс", "rc lens": "Ланс",
    "кадисия": "Аль-Кадисия", "al qadsiah": "Аль-Кадисия", "qadsiah": "Аль-Кадисия",
    "torino": "Торино", "torino fc": "Торино", "торо": "Торино",
    "lafc": "Лос Анджелес", "los angeles": "Лос Анджелес", "лафк": "Лос Анджелес",
    # ПСВ
    "псв": "ПСВ", "псв эйндховен": "ПСВ",
    "psv": "ПСВ", "psv eindhoven": "ПСВ",

    # ─── DIV_3 ──────────────────────────────────────────────────────────────
    "sunderland": "Сандерленд",
    "ноттингем": "Ноттингем Форест", "форест": "Ноттингем Форест", "ноттингем форрест": "Ноттингем Форест",
    "nottingham": "Ноттингем Форест", "nottingham forest": "Ноттингем Форест", "forest": "Ноттингем Форест",
    "сосьедад": "Реал Сосьедад", "реал сосиедад": "Реал Сосьедад",
    "sociedad": "Реал Сосьедад", "real sociedad": "Реал Сосьедад",
    "paris fc": "Париж", "париж фк": "Париж",
    "fenerbahce": "Фенербахче", "fener": "Фенербахче",
    "como": "Комо", "como 1907": "Комо",
    "brentford": "Брентфорд",
    "палас": "Кристал Пэлас", "кристал палас": "Кристал Пэлас",
    "palace": "Кристал Пэлас", "crystal palace": "Кристал Пэлас",
    "ахли": "Аль-Ахли", "al ahli": "Аль-Ахли",
    "lyon": "Лион", "olympique lyonnais": "Лион", "олимпик лион": "Лион",
    "bournemouth": "Борнмут",
    "иттихад": "Аль-Иттихад", "al ittihad": "Аль-Иттихад",
    "трабзон": "Трабзонспор", "trabzon": "Трабзонспор", "trabzonspor": "Трабзонспор",
    "вильяреал": "Вильярреал", "villarreal": "Вильярреал",
    "stuttgart": "Штутгарт", "vfb stuttgart": "Штутгарт",
    "bologna": "Болонья",

    # ─── DIV_4 ──────────────────────────────────────────────────────────────
    "баия": "Байя", "bahia": "Байя", "ec bahia": "Байя",
    "milan": "Милан", "ac milan": "Милан", "ац милан": "Милан",
    "дортмунд": "Боруссия Дортмунд", "боруссия": "Боруссия Дортмунд", "бвб": "Боруссия Дортмунд",
    "dortmund": "Боруссия Дортмунд", "borussia dortmund": "Боруссия Дортмунд", "bvb": "Боруссия Дортмунд",
    "интернационале": "Интер Милан", "inter milan": "Интер Милан", "internazionale": "Интер Милан",
    "brighton": "Брайтон", "brighton hove albion": "Брайтон",
    "леверкузен": "Байер", "байер леверкузен": "Байер", "байер 04": "Байер",
    "leverkusen": "Байер", "bayer": "Байер", "bayer leverkusen": "Байер",
    "leipzig": "Лейпциг", "rb leipzig": "Лейпциг", "рб лейпциг": "Лейпциг",
    "everton": "Эвертон",
    "atalanta": "Аталанта", "аталанта бергамо": "Аталанта",
    "вилла": "Астон Вилла", "астон вила": "Астон Вилла", "villa": "Астон Вилла", "aston villa": "Астон Вилла",
    "besiktas": "Бешикташ",
    "майами": "Интер Майами", "интер маями": "Интер Майами",
    "miami": "Интер Майами", "inter miami": "Интер Майами",
    "betis": "Бетис", "real betis": "Бетис", "реал бетис": "Бетис",
    "хиляль": "Аль-Хиляль", "аль хилаль": "Аль-Хиляль", "al hilal": "Аль-Хиляль",
    "newcastle": "Ньюкасл", "newcastle united": "Ньюкасл", "ньюкасл юнайтед": "Ньюкасл",
    "бильбао": "Атлетик Бильбао", "атлетик": "Атлетик Бильбао", "атлетик клуб": "Атлетик Бильбао",
    "bilbao": "Атлетик Бильбао", "athletic": "Атлетик Бильбао", "athletic bilbao": "Атлетик Бильбао",

    # ─── DIV_5 ──────────────────────────────────────────────────────────────
    "arsenal": "Арсенал",
    "ман сити": "Манчестер Сити", "сити": "Манчестер Сити",
    "man city": "Манчестер Сити", "manchester city": "Манчестер Сити",
    "мю": "Манчестер Юнайтед", "ман юнайтед": "Манчестер Юнайтед", "ман юнайтид": "Манчестер Юнайтед",
    "man utd": "Манчестер Юнайтед", "man united": "Манчестер Юнайтед",
    "manchester united": "Манчестер Юнайтед",
    "шпоры": "Тоттенхэм", "тоттенхем хотспур": "Тоттенхэм", "spurs": "Тоттенхэм", "tottenham": "Тоттенхэм",
    "атлетико": "Атлетико Мадрид", "атлети": "Атлетико Мадрид",
    "atletico": "Атлетико Мадрид", "atletico madrid": "Атлетико Мадрид",
    "барса": "Барселона", "барка": "Барселона",
    "barca": "Барселона", "barcelona": "Барселона", "fc barcelona": "Барселона",
    "real madrid": "Реал Мадрид",
    "мюнхен": "Бавария", "бавария мюнхен": "Бавария",
    # «Байерн» на одну букву отличается от живого «Байер» и без алиаса уходил к
    # нему по фаззи. Точный ключ снимает вопрос: тир алиасов идёт раньше фаззи.
    "байерн": "Бавария", "байерн мюнхен": "Бавария",
    "bayern": "Бавария", "bayern munich": "Бавария", "fc bayern": "Бавария",
    "liverpool": "Ливерпуль", "лфк": "Ливерпуль", "lfc": "Ливерпуль",
    "chelsea": "Челси",
    "napoli": "Наполи", "ssc napoli": "Наполи",
    "юве": "Ювентус", "juve": "Ювентус", "juventus": "Ювентус",
    "roma": "Рома", "as roma": "Рома", "ас рома": "Рома",
    "psg": "ПСЖ", "paris saint germain": "ПСЖ", "пари сен жермен": "ПСЖ",
    "гала": "Галатасарай", "gala": "Галатасарай", "galatasaray": "Галатасарай",
    "наср": "Аль-Наср", "аль насср": "Аль-Наср", "al nassr": "Аль-Наср", "al nasr": "Аль-Наср",
}

def normalize_team_name(name: str | None) -> str:
    """Normalize team name for fuzzy matching (handles ё/э/е, latin ë, hyphens, slashes, extra spaces)."""
    if not name:
        return ""
    s = str(name).lower()
    # 'э' сворачивается в 'е' наравне с 'ё': транслит из английского пишут и так
    # и так ('Фулхэм'/'Фулхем', 'Вест Хэм'/'Вест Хем', 'Эвертон'/'Евертон'), а
    # fuzzy вытягивает только длинные имена — 'фулхем' до порога не дотягивает.
    # Свёртка проверена на всём ростере: двух клубов с одним каноном она не даёт,
    # и это стережёт TestTotalityInvariant.
    s = s.replace("э", "е")
    # Replace variants of 'ё', latin 'ë' (\u00eb), 'ø', 'ö'
    s = s.replace("ё", "е").replace("\u00eb", "е").replace("ø", "o").replace("ö", "o")
    # Replace punctuation and separators
    s = re.sub(r"[\-_/\\.,]", " ", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def teams_match(team_a: str | None, team_b: str | None) -> bool:
    """Check whether two names refer to the same club.

    All the judgement lives in resolve_team_name_ex, on purpose. The previous
    version added its own substring and word-level tiers on top of the resolver,
    and those tiers carried the same defect: 'Расинг' is a substring of
    'Расинг Ланс' and shares a word with it, so two different clubs matched.

    Deciding "typo of the same club" versus "two similar clubs" is impossible
    without knowing the club list, so the registry is the only authority here.
    While the registry is incomplete this errs towards False: refusing to match
    is recoverable, silently merging two coaches' clubs is not.
    """
    if not team_a or not team_b:
        return False

    res_a = resolve_team_name_ex(team_a)
    res_b = resolve_team_name_ex(team_b)

    # Both sides landed on a registered club: the canonical names settle it.
    # This is also the guard against conflating distinct clubs — it now reads the
    # full registry instead of the 16 legacy names, and it runs first.
    if res_a.is_confident and res_b.is_confident:
        return res_a.canonical == res_b.canonical

    a_norm = normalize_team_name(team_a)
    b_norm = normalize_team_name(team_b)
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True

    # One side is a registered club, the other is not: they match only when the
    # unresolved side spells that club's canonical name outright.
    if res_a.is_confident and normalize_team_name(res_a.canonical) == b_norm:
        return True
    if res_b.is_confident and normalize_team_name(res_b.canonical) == a_norm:
        return True

    return False


# ---------------------------------------------------------------------------
# Club registry
#
# The canonical set of club names. Historically this was config.KPL_TEAMS — the
# 16 clubs of the pre-division era — which is why clubs outside those 16 used to
# collapse into each other. config.CLUB_REGISTRY is the single source of truth
# now, and there is deliberately no fallback: an empty registry must mean an
# empty registry, so that every name resolves to itself instead of collapsing
# into a stale canon.
# ---------------------------------------------------------------------------

_registry: tuple[str, ...] = ()
_registry_index: dict[str, str] = {}
_alias_index: dict[str, str] = {}
_joined_index: dict[str, str] = {}
_dropped_aliases: tuple[tuple[str, str, str], ...] = ()
_orphan_aliases: tuple[str, ...] = ()


def _load_canonical_names() -> tuple[str, ...]:
    """Read the canonical club list from config, newest setting first."""
    import config

    names = getattr(config, "CLUB_REGISTRY", None) or []

    seen: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        norm = normalize_team_name(name)
        if norm and norm not in seen:
            seen[norm] = name.strip()
    return tuple(seen.values())


def _validate_aliases(index: dict[str, str]) -> tuple[tuple[str, str, str], ...]:
    """Find aliases that point away from a club registered under that exact name.

    An alias like "расинг" -> "Расинг" is fine. An alias whose key IS the canonical
    name of a *different* registered club would silently rename that club, so it is
    reported here and ignored by the resolver.
    """
    dropped: list[tuple[str, str, str]] = []
    for alias, canonical in TEAM_ALIASES.items():
        owner = index.get(normalize_team_name(alias))
        if owner is None:
            continue
        if normalize_team_name(owner) != normalize_team_name(canonical):
            dropped.append((alias, canonical, owner))
    return tuple(dropped)


def _build_alias_index(index: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Map normalized alias -> canonical name, keeping only aliases that are safe.

    Two kinds are left out: aliases whose key is itself a registered club name (the
    EXACT tier owns those, and honouring the alias would rename a real club), and
    aliases pointing at a club that is not in the registry at all.
    """
    aliases: dict[str, str] = {}
    orphans: list[str] = []
    for alias, canonical in TEAM_ALIASES.items():
        a_norm = normalize_team_name(alias)
        if not a_norm or a_norm in index:
            continue
        owner = index.get(normalize_team_name(canonical))
        if owner is None:
            orphans.append(alias)
            continue
        aliases[a_norm] = owner
    return aliases, tuple(orphans)


def _build_joined_index(*sources: dict[str, str]) -> dict[str, str]:
    """Map space-free forms -> canonical name, for OCR that glues or splits words.

    Ambiguous keys are dropped: if two clubs collapse to the same space-free form,
    neither may win by accident.
    """
    joined: dict[str, str] = {}
    conflicting: set[str] = set()
    for source in sources:
        for key, canonical in source.items():
            glued = key.replace(" ", "")
            if not glued or glued == key:
                continue
            existing = joined.get(glued)
            if existing is not None and existing != canonical:
                conflicting.add(glued)
            else:
                joined[glued] = canonical
    for key in conflicting:
        joined.pop(key, None)
    return joined


def reload_registry() -> int:
    """Rebuild the registry index from config. Returns the number of clubs loaded.

    Call this after the club list changes. Kept explicit (rather than lazy) so the
    resolver stays free of I/O and of surprise rebuilds inside hot loops.
    """
    global _registry, _registry_index, _alias_index, _joined_index
    global _dropped_aliases, _orphan_aliases

    _registry = _load_canonical_names()
    _registry_index = {normalize_team_name(name): name for name in _registry}
    _dropped_aliases = _validate_aliases(_registry_index)
    _alias_index, _orphan_aliases = _build_alias_index(_registry_index)
    _joined_index = _build_joined_index(_registry_index, _alias_index)
    _resolve_cached.cache_clear()

    for alias, canonical, owner in _dropped_aliases:
        logger.warning(
            "Alias %r -> %r shadows registered club %r and will be ignored",
            alias, canonical, owner,
        )
    if _orphan_aliases:
        logger.debug(
            "%d aliases point at clubs outside the registry and are inactive",
            len(_orphan_aliases),
        )
    return len(_registry)


def get_registry() -> tuple[str, ...]:
    """All canonical club names, in registration order."""
    return _registry


def get_registry_index() -> dict[str, str]:
    """Normalized name -> canonical name, precomputed for the resolver."""
    return _registry_index


def get_dropped_aliases() -> tuple[tuple[str, str, str], ...]:
    """Aliases ignored because they shadow a registered club: (alias, target, owner)."""
    return _dropped_aliases


def is_registered(name: str | None) -> bool:
    """True when the name matches a registered club exactly (after normalization)."""
    return normalize_team_name(name) in _registry_index


def get_alias_index() -> dict[str, str]:
    """Normalized alias -> canonical name, for aliases active against this registry."""
    return _alias_index


def get_orphan_aliases() -> tuple[str, ...]:
    """Aliases inactive because their target club is not in the registry."""
    return _orphan_aliases


# ---------------------------------------------------------------------------
# Name resolution
#
# Tiers run in order and the first *unambiguous* one wins. Ambiguity anywhere
# stops resolution with method=NONE: letting a weaker tier settle what a stronger
# one called a tie is exactly how «Расинг Ланс» used to become «Расинг».
# ---------------------------------------------------------------------------

# Фаззи-тир нужен против опечаток OCR, а не против коротких похожих имён.
FUZZY_MIN_LEN = 5       # 'псж' против 'псв' даёт 0.667 — на трёх буквах фаззи бессмысленен
FUZZY_THRESHOLD = 0.87  # было 0.65: слишком низко, склеивало разные клубы
FUZZY_MARGIN = 0.07     # отрыв от второго кандидата; без него побеждал просто «наименее плохой»
PREFIX_MIN_LEN = 3      # префикс короче трёх букв подходит слишком многим

# Юридические формы и приставки: шум, а не часть имени. Отбрасываются, чтобы
# «Спортинг CP» и «ФК Порту» дошли до точного совпадения. Географические
# уточнения сюда не входят и входить не должны — именно они отличают
# «Расинг Сантандер» от «Расинг Ланс».
_NOISE_TOKENS = frozenset({
    "фк", "фс", "сп", "сц", "кф", "клуб",
    "fc", "sc", "sl", "cf", "ac", "afc", "cp", "club", "jrs",
})


class ResolveMethod(str, Enum):
    """Which tier produced the answer."""
    EXACT = "exact"      # совпадение с каноном реестра
    ALIAS = "alias"      # словарь TEAM_ALIASES
    JOINED = "joined"    # склейка токенов / отброшенный шум
    PREFIX = "prefix"    # единственный канон с таким префиксом
    FUZZY = "fuzzy"      # difflib, с запасом над вторым кандидатом
    NONE = "none"        # не разрешено — вход возвращается как есть


@dataclass(frozen=True, slots=True)
class TeamResolution:
    """Outcome of resolving a raw team name against the club registry."""
    raw: str
    canonical: str | None
    method: ResolveMethod
    confidence: float
    candidates: tuple[str, ...] = ()

    @property
    def is_confident(self) -> bool:
        return self.canonical is not None


def _alternate_forms(norm: str) -> list[str]:
    """Rewrites of the input worth a second lookup, in order of trustworthiness."""
    forms: list[str] = []
    tokens = norm.split()

    stripped = [t for t in tokens if t not in _NOISE_TOKENS]
    if stripped and len(stripped) != len(tokens):
        forms.append(" ".join(stripped))

    if len(tokens) > 1:
        forms.append("".join(tokens))
    if len(stripped) > 1 and len(stripped) != len(tokens):
        forms.append("".join(stripped))

    return [f for f in forms if f and f != norm]


def _fuzzy_scores(norm: str) -> list[tuple[str, float]]:
    """Best difflib ratio per canonical club, highest first."""
    scores: dict[str, float] = {}
    for candidate_norm, canonical in list(_registry_index.items()) + list(_alias_index.items()):
        ratio = difflib.SequenceMatcher(None, norm, candidate_norm).ratio()
        if ratio > scores.get(canonical, 0.0):
            scores[canonical] = ratio
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def resolve_team_name_ex(name: str | None) -> TeamResolution:
    """Resolve a raw team name, reporting how confident the answer is.

    Pure CPU: no SQL, no network. Callers run inside the event loop.
    Results are cached; reload_registry() drops the cache.
    """
    return _resolve_cached(str(name).strip() if name else "")


@lru_cache(maxsize=4096)
def _resolve_cached(raw: str) -> TeamResolution:
    """Tier walk for one already-stripped name. Only reload_registry() may invalidate."""
    norm = normalize_team_name(raw)
    if not norm:
        return TeamResolution(raw, None, ResolveMethod.NONE, 0.0)

    # 1. Точное совпадение с каноном. idx_users_team_name_unique гарантирует,
    #    что канон один, поэтому спорить тут не с чем.
    canonical = _registry_index.get(norm)
    if canonical is not None:
        return TeamResolution(raw, canonical, ResolveMethod.EXACT, 1.0)

    # 2. Словарь алиасов: транслит, склонения, опечатки OCR.
    canonical = _alias_index.get(norm)
    if canonical is not None:
        return TeamResolution(raw, canonical, ResolveMethod.ALIAS, 1.0)

    # 3. Те же справочники, но по переписанным формам: отброшенные «ФК»/«CP»
    #    и склейка токенов в обе стороны (OCR и слепляет слова, и рвёт их).
    for form in _alternate_forms(norm):
        canonical = _registry_index.get(form) or _alias_index.get(form)
        if canonical is not None:
            return TeamResolution(raw, canonical, ResolveMethod.JOINED, 1.0)

    for form in [norm] + _alternate_forms(norm):
        canonical = _joined_index.get(form.replace(" ", ""))
        if canonical is not None:
            return TeamResolution(raw, canonical, ResolveMethod.JOINED, 1.0)

    # 4. Префикс — но только если он ведёт ровно к одному клубу. Именно этот
    #    предохранитель не даёт «Расинг» угадаться при живых «Расинг Сантандер»
    #    и «Расинг Ланс».
    if len(norm) >= PREFIX_MIN_LEN:
        prefixed = sorted({
            canon for canon_norm, canon in _registry_index.items()
            if canon_norm.startswith(norm)
        })
        if len(prefixed) == 1:
            return TeamResolution(raw, prefixed[0], ResolveMethod.PREFIX, 1.0)
        if len(prefixed) > 1:
            return TeamResolution(raw, None, ResolveMethod.NONE, 0.0, tuple(prefixed))

    # 5. Фаззи — последний и самый слабый тир, под тремя предохранителями.
    if len(norm) < FUZZY_MIN_LEN:
        return TeamResolution(raw, None, ResolveMethod.NONE, 0.0)

    ranked = _fuzzy_scores(norm)
    if not ranked:
        return TeamResolution(raw, None, ResolveMethod.NONE, 0.0)

    best_canon, best_score = ranked[0]
    if best_score < FUZZY_THRESHOLD:
        return TeamResolution(raw, None, ResolveMethod.NONE, 0.0)

    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score - second_score < FUZZY_MARGIN:
        tied = tuple(canon for canon, score in ranked if best_score - score < FUZZY_MARGIN)
        return TeamResolution(raw, None, ResolveMethod.NONE, 0.0, tied)

    return TeamResolution(raw, best_canon, ResolveMethod.FUZZY, best_score)


def resolve_team_name(name: str | None) -> str:
    """Resolve a raw team name to its canonical form, or return it unchanged.

    Backwards-compatible wrapper: never returns an empty string for a non-empty
    input, so the existing `resolve_team_name(x) or x` call sites keep working.
    Use resolve_team_name_ex when you need to know whether it actually resolved.
    """
    if not name:
        return ""
    resolved = resolve_team_name_ex(name)
    return resolved.canonical if resolved.canonical is not None else str(name).strip()


# ---------------------------------------------------------------------------
# Human-typed club queries
#
# «Темшик долги Ренна» is not OCR output: people decline names («Валенсии»),
# drop the article («Кадисия»), and misspell. A miss here costs nothing worse
# than showing the wrong club's list, with the club named in the header, so the
# fuzzy bar sits lower than the OCR tiers. A near tie still asks back with
# suggestions instead of guessing. Nothing here merges or writes anything: it
# is for lookups only, never for deciding which club a match belongs to.
# ---------------------------------------------------------------------------

QUERY_FUZZY_THRESHOLD = 0.72  # «кадисия» против «аль кадисия» — 0.78
QUERY_FUZZY_MARGIN = 0.06
QUERY_SUGGEST_THRESHOLD = 0.5
QUERY_MIN_LEN = 4
QUERY_MAX_SUGGESTIONS = 3

# Падежные окончания, длинные первыми: «Валенсии» → «валенси», «Ренна» → «ренн».
_CASE_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему",
    "ой", "ей", "ом", "ем", "ах", "ях", "ов", "ев",
    "ы", "и", "а", "я", "у", "ю", "е",
)
# Приставка «Аль-» общая для пяти клубов, поэтому её часто опускают.
_ARTICLE_PREFIXES = ("аль ",)


@dataclass(frozen=True, slots=True)
class ClubQuery:
    """A club typed by a person: the club, or the closest names to ask back with."""
    canonical: str | None
    suggestions: tuple[str, ...] = ()


def _query_fuzzy_scores(norm: str) -> list[tuple[str, float]]:
    """Like `_fuzzy_scores`, but also against names without the «Аль-» article."""
    scores = dict(_fuzzy_scores(norm))
    for candidate_norm, canonical in _registry_index.items():
        for prefix in _ARTICLE_PREFIXES:
            if candidate_norm.startswith(prefix):
                ratio = difflib.SequenceMatcher(None, norm, candidate_norm[len(prefix):]).ratio()
                if ratio > scores.get(canonical, 0.0):
                    scores[canonical] = ratio
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def resolve_club_query(text: str | None) -> ClubQuery:
    """Find the club a person meant, tolerating declension and typos.

    Order: the strict resolver; the same with a case ending cut off; a looser
    fuzzy pass. An ambiguity at any step returns no club, only suggestions.
    """
    raw = str(text).strip() if text else ""
    norm = normalize_team_name(raw)
    if not norm:
        return ClubQuery(None)

    strict = resolve_team_name_ex(raw)
    if strict.canonical is not None:
        return ClubQuery(strict.canonical)

    # Склонение: отрезаем окончание у последнего слова и пробуем строгий резолвер.
    # Разные окончания могут привести к разным клубам — тогда переспрашиваем.
    stems: dict[str, None] = {}
    ambiguous: dict[str, None] = dict.fromkeys(strict.candidates)
    for ending in _CASE_ENDINGS:
        if norm.endswith(ending) and len(norm) - len(ending) >= PREFIX_MIN_LEN:
            stem = resolve_team_name_ex(norm[: -len(ending)])
            if stem.canonical is not None:
                stems[stem.canonical] = None
            else:
                ambiguous.update(dict.fromkeys(stem.candidates))  # «Реала» → «реал»
    if len(stems) == 1:
        return ClubQuery(next(iter(stems)))
    if stems:
        return ClubQuery(None, tuple(stems)[:QUERY_MAX_SUGGESTIONS])

    if ambiguous:
        return ClubQuery(None, tuple(ambiguous)[:QUERY_MAX_SUGGESTIONS])

    if len(norm) < QUERY_MIN_LEN:
        return ClubQuery(None)

    ranked = _query_fuzzy_scores(norm)
    if not ranked:
        return ClubQuery(None)
    best_canon, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score >= QUERY_FUZZY_THRESHOLD and best_score - second_score >= QUERY_FUZZY_MARGIN:
        return ClubQuery(best_canon)

    suggestions = tuple(
        canon for canon, score in ranked[:QUERY_MAX_SUGGESTIONS]
        if score >= QUERY_SUGGEST_THRESHOLD
    )
    return ClubQuery(None, suggestions)


reload_registry()
