import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# Load env variables from project root
load_dotenv(PROJECT_ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def _get_admin_ids() -> list[int]:
    admins_raw = os.getenv("ADMIN_IDS", "")
    ids = []
    for x in admins_raw.split(","):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    return ids

ADMIN_IDS = _get_admin_ids()
_env_db_path = os.getenv("LEAGUE_SQLITE_PATH", "league.db")
DB_PATH = str(PROJECT_ROOT / _env_db_path) if not os.path.isabs(_env_db_path) else _env_db_path
def _get_gemini_api_keys() -> list[str]:
    keys_raw = os.getenv("GEMINI_API_KEY", "")
    return [k.strip() for k in keys_raw.split(",") if k.strip()]

GEMINI_API_KEYS = _get_gemini_api_keys()
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""

def _get_gemini_chat_keys() -> list[str]:
    keys_raw = os.getenv("GEMINI_CHAT_API_KEY", "")
    return [k.strip() for k in keys_raw.split(",") if k.strip()]

GEMINI_CHAT_API_KEYS = _get_gemini_chat_keys()
GEMINI_CHAT_API_KEY = GEMINI_CHAT_API_KEYS[0] if GEMINI_CHAT_API_KEYS else ""
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

def _get_gemini_models() -> list[str]:
    raw = os.getenv("GEMINI_MODELS", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
    ]

def _get_gemini_chat_models() -> list[str]:
    raw = os.getenv("GEMINI_CHAT_MODELS", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
    ]

GEMINI_MODELS = _get_gemini_models()
GEMINI_CHAT_MODELS = _get_gemini_chat_models()
# ─── OpenRouter: ИИ-прогноз во вкладке панели Logovo.bet ──────────────────────
# Без ключа вкладка работает по вероятностям линии. OPENROUTER_MODEL — одна
# модель или несколько через запятую: бесплатные (:free и stealth/*) пропадают
# и упираются в квоту, тогда пробуется следующая.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "qwen/qwen3.8-27b:free,stealth/space-bunny-alpha,google/gemma-4-31b-it:free,openrouter/free",
).strip()
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
# ─── Phase 8: Real Sports Provider Configuration ──────────────────────────────
SPORTS_PROVIDER = os.getenv("SPORTS_PROVIDER", "auto").strip()
SPORTS_API_KEY = os.getenv("SPORTS_API_KEY", os.getenv("APISPORTS_KEY", "")).strip()
APISPORTS_KEY = SPORTS_API_KEY  # Backward compatibility
SPORTS_API_BASE_URL = os.getenv("SPORTS_API_BASE_URL", "https://v3.football.api-sports.io").strip()
SPORTS_TIMEOUT_SECONDS = float(os.getenv("SPORTS_TIMEOUT_SECONDS", "10.0"))
SPORTS_CACHE_TTL_SECONDS = int(os.getenv("SPORTS_CACHE_TTL_SECONDS", "30"))
SPORTS_LIVE_POLL_SECONDS = int(os.getenv("SPORTS_LIVE_POLL_SECONDS", "15"))
SPORTS_MAX_RETRIES = int(os.getenv("SPORTS_MAX_RETRIES", "3"))
SPORTS_RATE_LIMIT_RPM = int(os.getenv("SPORTS_RATE_LIMIT_RPM", "60"))

# Stale data protection thresholds
LIVE_DATA_STALE_AFTER_SECONDS = int(os.getenv("LIVE_DATA_STALE_AFTER_SECONDS", "120"))
LIVE_DATA_EXPIRED_AFTER_SECONDS = int(os.getenv("LIVE_DATA_EXPIRED_AFTER_SECONDS", "300"))

# ─── Phase 6: Smart Notifications Service (В разработке - отключено) ─────────
SMART_NOTIFICATIONS_ENABLED = os.getenv("SMART_NOTIFICATIONS_ENABLED", "0").lower() in ("1", "true", "yes")

def _get_group_id() -> int | None:
    group_raw = os.getenv("TELEGRAM_GROUP_ID", "").strip()
    if not group_raw:
        return None
    try:
        return int(group_raw)
    except ValueError:
        return None

GROUP_ID = _get_group_id()

MAX_WARNS_LIMIT = 4

# Сколько туров дивизиона могут быть открыты одновременно. «Одновременно» здесь
# считается строго по дедлайну: тур занимает слот, пока `deadline > now()`, и
# освобождает его сам, без ручного закрытия админом (`is_open` остаётся 1).
# Держит конвейер линии Logovo.bet «два через два»: на два текущих тура ставки
# закрыты, на два следующих выставляется ранняя линия.
MAX_OPEN_ROUNDS_PER_DIVISION: int = 2

# Регламент долгов. Все этапы долга отсчитываются от его `escalate_at`
# (см. services/debt_policy.py и services/debt_lifecycle.py):
# escalate_at = момент, когда матч стал долгом + grace_hours + DEBT_ESCALATION_HOURS.
# grace_hours ≠ 0 только при досрочном закрытии тура — это остаток до дедлайна,
# округлённый вверх до часа.
DEBT_REMINDER_INTERVAL_HOURS: int = 12        # ЛС-напоминание о долге
DEBT_SOFT_WARNING_HOURS: int = 24             # мягкое предупреждение за N ч до эскалации
DEBT_ESCALATION_HOURS: int = 48               # срок отыгрыша, затем карточка вердикта админу
DEBT_REESCALATION_INTERVAL_HOURS: int = 24    # повтор карточки, пока вердикт не вынесен
DEBT_GLOBAL_ESCALATION_DELAY_HOURS: int = 48  # после первой эскалации — ещё и глобальным админам
# Вехи напоминаний о дедлайне открытого тура, часы до дедлайна.
ROUND_DEADLINE_REMINDER_HOURS: tuple[int, ...] = (72, 66, 60, 54, 48, 42, 36, 30, 24, 18, 12, 6, 1)

# Logovo.bet: стартовый баланс нового кошелька (🪙). Единственный источник истины —
# схема user_wallets.balance, get_or_create_wallet() и приветственный бонус
# coin_transactions('welcome_bonus') берут сумму отсюда.
INITIAL_WALLET_BALANCE = 677

# Долгосрочные ставки (outright): момент по Москве, с которого приём закрыт, —
# для дивизионов, кубков дивизионов и бомбардиров. Общий кубок закрывается
# по-своему, с началом 1/4 финала. Пустое значение — без срока. Расчёт и
# пересчёт линии после срока не останавливаются.
OUTRIGHT_BETS_CLOSE_AT = os.getenv("OUTRIGHT_BETS_CLOSE_AT", "2026-09-30 18:00:00").strip()

# Составы дивизионов сезона 2026/27, по 16 клубов в каждом. Ключ — divisions.code.
# Это сид-состав: фактический участник появляется в users.team_name, когда тренер
# регистрируется. Места, которым нужен реальный состав, обязаны спрашивать users;
# отсюда берётся только канон имён и состав по умолчанию для пустой БД.
DIVISION_CLUBS: dict[str, list[str]] = {
    "DIV_1": [
        "Лидс", "Ренн", "Ницца", "Нэшвилл",
        "Порту", "Вест Хэм", "Вольфсбург", "Фиорентина",
        "Лацио", "Марсель", "Лилль", "Айнтрахт",
        "Майнц", "Бернли", "Будё Глимт", "Кельн",
    ],
    "DIV_2": [
        "Вулверхэмптон", "Бурирам", "Валенсия", "Сельта",
        "Ривер Плейт", "Аякс", "Спортинг", "Монако",
        "Бенфика", "Фулхэм", "Хоффенхайм", "Ланс",
        "Аль-Кадисия", "Торино", "Лос Анджелес", "ПСВ",
    ],
    "DIV_3": [
        "Сандерленд", "Ноттингем Форест", "Реал Сосьедад", "Париж",
        "Фенербахче", "Комо", "Брентфорд", "Кристал Пэлас",
        "Аль-Ахли", "Лион", "Борнмут", "Аль-Иттихад",
        "Трабзонспор", "Вильярреал", "Штутгарт", "Болонья",
    ],
    "DIV_4": [
        "Байя", "Милан", "Боруссия Дортмунд", "Интер Милан",
        "Брайтон", "Байер", "Лейпциг", "Эвертон",
        "Аталанта", "Астон Вилла", "Бешикташ", "Интер Майами",
        "Бетис", "Аль-Хиляль", "Ньюкасл", "Атлетик Бильбао",
    ],
    "DIV_5": [
        "Арсенал", "Манчестер Сити", "Манчестер Юнайтед", "Тоттенхэм",
        "Атлетико Мадрид", "Барселона", "Реал Мадрид", "Бавария",
        "Ливерпуль", "Челси", "Наполи", "Ювентус",
        "Рома", "ПСЖ", "Галатасарай", "Аль-Наср",
    ],
}

# Предсезонный рейтинг участников сезона 2026/27 — от самого слабого к самому сильному.
# Ключ — divisions.code, как в DIVISION_CLUBS; позиция в списке и есть ранг: первый
# элемент — 1 (слабейший), последний — 16 (сильнейший).
#
# Запись «@login» — телеграм-логин тренера, сверяется с users.username без учёта регистра
# и без «@». Запись без «@» — имя клуба из DIVISION_CLUBS: так заводятся участники без
# тега, которых можно опознать только по клубу.
#
# Это сид, а не таблица: рейтинг задаёт стартовую статусность пары, дальше её перебивают
# сыгранные туры (см. services/betting_engine.select_top_round_matches).
DIVISION_PLAYER_SEEDS: dict[str, list[str]] = {
    "DIV_1": [
        "@Saharokk8830", "@curseedoeleo", "@TheFlakeSo", "@typeuw",
        "@ArtemPalagin", "@Snikers2121", "@Nukolaich", "@Rostyslav07",
        "@Maximilian4", "@Doakkk", "@qweasdzxc22819", "@brando055",
        # Двое участников без тега опознаются только по клубу: убиватор и Мандарин.
        "@Serghe1KO", "Марсель", "Кельн", "@ch1lyx",
    ],
    "DIV_2": [
        "@Vladimir_5500", "@govorigde", "@Davtyan_55", "@sulassll",
        "@Artem53824", "@saymino1", "@mitixfc", "@lvckri",
        "@Turolen", "@Tonyloki57", "@umbra_mind", "@Forzainternationale",
        "@GeorgiyKostenko", "@virkilainen", "@Prizrakks", "@vtrrgyg",
    ],
    "DIV_3": [
        "@XTrent20", "@TarEgiazaryan", "@Shotik_UA", "@perdun_1337",
        "@sergeynobody1", "@Artilawyer", "@kirillchuk_927", "@azs5652",
        "@Rodza20", "@Ghoust_tag", "@Daimond_Highlight", "@Acidonchik_95",
        "@sayvvel", "@Dr_Wh11te", "@aidarreezz", "@LazyMaxxAA",
    ],
    "DIV_4": [
        "@Nixan23", "@Flasin5", "@epl_l", "@k1nkyua",
        # В «falIingapart» заглавная I, а не строчная l — записано дословно, как прислано.
        "@MemoryYouSs", "@Komarik97", "@kostya94petrik", "@falIingapart",
        "@Leon_2515", "@Lyubimov_Aleksandr", "@ARTIKggvp", "@t3miy",
        "@sp1r1tVSA", "@pdsnvk", "@tshmrrr", "@ReiZekk",
    ],
    "DIV_5": [
        "@Fede_15r", "@tornike07", "@Kurilril5", "@MAGMDV_77",
        "@Rusasf", "@joraknaz", "@ArsenalSte", "@Daot1",
        "@agosv", "@Diktator_new", "@ilia575", "@zazz_33117",
        "@vitasmachiha", "@lsmaksimmn", "@mms_op", "@Kadyr_42",
    ],
}

# Канонические имена всех клубов лиги — источник истины для club_registry.resolve_team_name.
# Плоский срез DIVISION_CLUBS: резолверу дивизион не важен, имена уникальны глобально
# (idx_users_team_name_unique). Клуб, которого здесь нет, резолвится сам в себя —
# это безопасно, но его опечатки из OCR никуда не схлопнутся, поэтому каждый новый
# клуб обязан попадать в DIVISION_CLUBS.
CLUB_REGISTRY: list[str] = [club for clubs in DIVISION_CLUBS.values() for club in clubs]

# Класс дивизиона для общего кубка, в тех же s-поинтах силы, что считает
# services/betting_engine._get_team_strength_score (нейтраль 10.0). Д1 — сильнейший,
# Д5 — слабейший. Шаг 1.5 = «разница в один дивизион равна преимуществу своего поля»:
# services/poisson_odds.calculate_match_lambdas переводит силу в ожидаемые голы как
# exp(0.32 * (s1 - s2) / 10), а home_advantage там 0.05, то есть поле = 1.56 поинта.
#
# Среднее обязано быть нулевым: pace_factor в той же формуле берёт СУММУ сил, и
# глобальный сдвиг вверх поднял бы голевую планку — тоталы и ОЗ в кубке перестали бы
# сопоставляться с лигой на тех же клубах.
#
# Это ручной вес, а не измеренный: до общего кубка между дивизиона не сыграно ни
# одного матча, а ELO в team_ratings наращивались в каждом дивизионе независимо от
# 1500, так что разница средних ELO сейчас не измеряет ничего. Первая фактическая
# выборка появится как раз в 1/64 — после неё вес стоит пересмотреть.
CUP_DIVISION_CLASS: dict[str, float] = {
    "DIV_1": 3.0,
    "DIV_2": 1.5,
    "DIV_3": 0.0,
    "DIV_4": -1.5,
    "DIV_5": -3.0,
}

MAX_MATCH_GOALS = 50

# Telegram Mini App Configuration
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8080").strip()
API_PORT = int(os.getenv("API_PORT", "8080"))
API_HOST = os.getenv("API_HOST", "0.0.0.0").strip()

# Global Lockdown Mode: true = accessible only to Global Admins; false = regular operation
def is_global_lockdown_enabled() -> bool:
    """Return True if global lockdown mode is enabled via LOGOVO_LOCKDOWN environment variable."""
    return os.getenv("LOGOVO_LOCKDOWN", "false").strip().lower() in ("true", "1", "yes")

is_lockdown_enabled = is_global_lockdown_enabled
LOGOVO_LOCKDOWN = is_global_lockdown_enabled()


def is_dev_auth_bypass_enabled() -> bool:
    """
    Разрешён ли обход валидации initData («mock_admin_<id>») для локальной отладки.

    Читается динамически, как и lockdown: тесты и локальный запуск меняют флаг
    без перезапуска процесса. В продакшене переменная не выставляется никогда.
    """
    return os.getenv("ALLOW_DEV_AUTH_BYPASS", "").strip().lower() in ("1", "true", "yes")


# Mini App API: защита от флуда и спам-атак.
# Идентификация по user_id из валидированного initData, для анонимных — по IP.
# ─── Integrity Engine: детектор договорных матчей ────────────────────────────
# Только наблюдение: ставки не блокируются и не замораживаются, дела видны
# исключительно супер-админам на экране /admin_bets.
INTEGRITY_ENABLED = os.getenv("INTEGRITY_ENABLED", "true").strip().lower() in ("true", "1", "yes")
# Ставки мельче этого порога не анализируются: договорняк ради 100 монет
# бессмыслен, а шум от мелких ставок топит реальные сигналы.
INTEGRITY_MIN_STAKE = int(os.getenv("INTEGRITY_MIN_STAKE", "500"))
# С этого балла дело дублируется в risk_alerts как SUSPICIOUS_ACTIVITY.
INTEGRITY_ALERT_THRESHOLD = int(os.getenv("INTEGRITY_ALERT_THRESHOLD", "70"))


API_RATE_LIMIT_ENABLED = os.getenv("API_RATE_LIMIT_ENABLED", "true").strip().lower() in ("true", "1", "yes")
API_RATE_LIMIT_READ_RPM = int(os.getenv("API_RATE_LIMIT_READ_RPM", "60"))
API_RATE_LIMIT_WRITE_RPM = int(os.getenv("API_RATE_LIMIT_WRITE_RPM", "20"))
API_RATE_LIMIT_ADMIN_RPM = int(os.getenv("API_RATE_LIMIT_ADMIN_RPM", "120"))
API_RATE_LIMIT_ANON_RPM = int(os.getenv("API_RATE_LIMIT_ANON_RPM", "30"))
# Минимальный интервал между двумя чувствительными мутациями одного пользователя.
API_SENSITIVE_MIN_INTERVAL = float(os.getenv("API_SENSITIVE_MIN_INTERVAL", "2.0"))

# X-Forwarded-For подделывается кем угодно, если сервер смотрит в интернет напрямую,
# поэтому доверяем заголовку только при явном включении (за nginx/Cloudflare).
API_TRUST_PROXY_HEADERS = os.getenv("API_TRUST_PROXY_HEADERS", "false").strip().lower() in ("true", "1", "yes")


# Logovo Tracker: мобильное приложение live-трансляции матчей (api/routes_tracker.py).
# Одноразовый ПИН из бота живёт 10 минут — столько нужно, чтобы дойти до телефона.
TRACKER_PIN_TTL_SECONDS = int(os.getenv("TRACKER_PIN_TTL_SECONDS", "600"))
# Сессия устройства протухает после суток без запросов: матч длится минуты,
# а забытый на чужом телефоне токен — нет.
TRACKER_SESSION_TTL_SECONDS = int(os.getenv("TRACKER_SESSION_TTL_SECONDS", "86400"))
# Приложение шлёт тики каждые несколько секунд, поэтому обычный write-лимит
# (API_RATE_LIMIT_WRITE_RPM) ему не подходит — у трекера свой бюджет.
API_RATE_LIMIT_TRACKER_RPM = int(os.getenv("API_RATE_LIMIT_TRACKER_RPM", "180"))
# Кадр плашки события: 2 МБ с запасом хватает на скриншот телефона в JPEG.
TRACKER_MAX_SCREENSHOT_BYTES = int(os.getenv("TRACKER_MAX_SCREENSHOT_BYTES", str(2 * 1024 * 1024)))
# Распознавание фамилии с кадра — отключено по умолчанию для экономии лимитов Gemini API.
TRACKER_OCR_ENABLED = os.getenv("TRACKER_OCR_ENABLED", "false").strip().lower() in ("true", "1", "yes")
# Бэкдор для ручного теста без реального ПИН-кода из бота (коды 7777/0000 и
# мок-профиль 777777). Только для локальной разработки — выключен по
# умолчанию, включается явным флагом и никогда не должен быть true в проде.
TRACKER_DEV_PIN_ENABLED = os.getenv("TRACKER_DEV_PIN_ENABLED", "false").strip().lower() in ("true", "1", "yes")

