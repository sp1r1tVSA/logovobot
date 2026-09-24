"""
api/rate_limiter.py

In-memory sliding-window rate limiting and in-flight deduplication for the
Logovo.bet Mini App API.

Три независимых механизма:
  * SlidingWindowLimiter — сколько запросов в минуту допустимо (флуд, парсинг);
  * минимальный интервал между чувствительными мутациями (спам ставками);
  * InFlightRegistry — блокировка параллельного дубля одной и той же мутации
    (двойной тап по кнопке, который иначе уходит в гонку за балансом).

Состояние живёт в памяти процесса. Бот разворачивается одним `worker`, поэтому
шардировать лимиты не нужно; при переезде на несколько процессов этот модуль
меняется на Redis-бэкенд, а вызывающий код остаётся прежним.
"""

import hashlib
import logging
import time
from collections import deque

import config

logger = logging.getLogger(__name__)

# Реже этого не подметаем мёртвые ключи, чтобы не ходить по всему словарю на каждом запросе.
_SWEEP_INTERVAL_SECONDS = 60.0


class SlidingWindowLimiter:
    """
    Скользящее окно: для каждого ключа хранится очередь меток времени запросов,
    попавших в последние `window_seconds`. Устаревшие метки вытесняются лениво
    при обращении, а полностью остывшие ключи выбрасываются периодическим sweep.
    """

    def __init__(self, window_seconds: float = 60.0):
        self.window_seconds = window_seconds
        self._default_window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    def check(self, key: str, limit: int) -> tuple[bool, int]:
        """
        Зарегистрировать попытку обращения по ключу.

        Возвращает (allowed, retry_after_seconds). При отказе запрос НЕ
        записывается в окно — иначе непрерывный флуд бесконечно продлевал бы
        блокировку, и клиент никогда не дождался бы разблокировки.
        """
        if limit <= 0:
            return True, 0

        now = time.monotonic()
        self._maybe_sweep(now)

        bucket = self._hits.get(key)
        if bucket is None:
            bucket = deque()
            self._hits[key] = bucket

        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = max(1, int(bucket[0] + self.window_seconds - now) + 1)
            return False, retry_after

        bucket.append(now)
        return True, 0

    def _maybe_sweep(self, now: float) -> None:
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        cutoff = now - self.window_seconds
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()
        self.window_seconds = self._default_window
        self._last_sweep = time.monotonic()


class MinIntervalLimiter:
    """Не чаще одного чувствительного действия раз в `interval` секунд на ключ."""

    def __init__(self):
        self._last_seen: dict[str, float] = {}
        self._last_sweep = time.monotonic()

    def check(self, key: str, interval: float) -> tuple[bool, int]:
        if interval <= 0:
            return True, 0

        now = time.monotonic()
        self._maybe_sweep(now, interval)

        last = self._last_seen.get(key)
        if last is not None and (now - last) < interval:
            return False, max(1, int(interval - (now - last)) + 1)

        self._last_seen[key] = now
        return True, 0

    def _maybe_sweep(self, now: float, interval: float) -> None:
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        cutoff = now - max(interval, _SWEEP_INTERVAL_SECONDS)
        stale = [key for key, seen in self._last_seen.items() if seen <= cutoff]
        for key in stale:
            del self._last_seen[key]

    def reset(self) -> None:
        self._last_seen.clear()
        self._last_sweep = time.monotonic()


class InFlightRegistry:
    """
    Реестр мутаций, которые прямо сейчас обрабатываются сервером.

    Защищает от гонки на двойном тапе: пока первый POST /api/predictions ещё
    списывает баланс, второй такой же запрос того же пользователя отбивается,
    а не исполняется параллельно.
    """

    def __init__(self):
        self._active: set[str] = set()

    def acquire(self, key: str) -> bool:
        """Занять слот. False означает, что предыдущий запрос ещё в работе."""
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def release(self, key: str) -> None:
        self._active.discard(key)

    def reset(self) -> None:
        self._active.clear()


# Чувствительные мутации: деньги и награды. Для них действует
# минимальный интервал и защита от параллельного дубля.
SENSITIVE_PATH_MARKERS = (
    "/api/predictions",
    "/api/bets",
    "/api/achievements/claim",
    "/api/saved-coupons",
    "/api/favorites",
    "/api/cabinet/match-time",
)

# Мобильный трекер живёт по своим правилам: он шлёт тики каждые несколько секунд
# и авторизуется Bearer-токеном, а не initData.
TRACKER_PATH_PREFIX = "/api/tracker/"

_read_limiter = SlidingWindowLimiter()
_write_limiter = SlidingWindowLimiter()
_tracker_limiter = SlidingWindowLimiter()
_sensitive_interval = MinIntervalLimiter()
_in_flight = InFlightRegistry()


def reset_all() -> None:
    """Сбросить всё накопленное состояние. Нужно тестам для изоляции."""
    _read_limiter.reset()
    _write_limiter.reset()
    _tracker_limiter.reset()
    _sensitive_interval.reset()
    _in_flight.reset()


def is_sensitive(path: str, method: str) -> bool:
    if method == "GET":
        return False
    if path.startswith("/api/admin/"):
        return False
    return any(path.startswith(marker) for marker in SENSITIVE_PATH_MARKERS)


def client_ip(request) -> str:
    """
    IP клиента. X-Forwarded-For принимается только когда сервер объявлен стоящим
    за доверенным прокси — иначе заголовок подделывается и лимит по IP обходится
    одной строкой.
    """
    if config.API_TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    peer = getattr(request, "remote", None)
    return peer or "unknown"


def resolve_identity(request) -> tuple[str, bool]:
    """
    Определить, кого ограничиваем: (ключ, авторизован ли).

    Личность берётся только из валидированного initData. Подпись уже проверена
    криптографически, поэтому подменить чужой ключ и израсходовать чужой лимит
    нельзя. Для анонимных запросов остаётся IP.
    """
    from api.auth import extract_init_data, get_authenticated_user

    try:
        user_info = get_authenticated_user(extract_init_data(request))
        if user_info and user_info.get("id"):
            return f"user:{user_info['id']}", True
    except Exception:
        logger.exception("Rate limiter: failed to resolve identity, falling back to IP.")

    return f"ip:{client_ip(request)}", False


def tracker_identity(request) -> str:
    """
    Ключ лимита для мобильного трекера.

    Считается из хеша Bearer-токена: сам токен в ключи словаря не кладём, а
    проверять сессию здесь незачем — лимитер работает до обработчика и не должен
    зависеть от его хранилища. Запрос без токена всё равно отобьётся 401, но
    свой лимит он расходует по IP, иначе перебор токенов был бы бесплатным.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header[7:].strip()
        if token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
            return f"tracker:{digest}"
    return f"tracker-anon:{client_ip(request)}"


def check_request(request) -> tuple[bool, str, int]:
    """
    Проверить лимиты окна для запроса.

    Возвращает (allowed, error_code, retry_after). Вызывается до обработчика.
    """
    path = request.path
    method = request.method

    # Трекер считается отдельно и до общих веток: обычный write-бюджет (20/мин)
    # отрезал бы трансляцию уже на третьей минуте матча.
    if path.startswith(TRACKER_PATH_PREFIX):
        allowed, retry_after = _tracker_limiter.check(
            tracker_identity(request), config.API_RATE_LIMIT_TRACKER_RPM
        )
        if not allowed:
            return False, "rate_limit_exceeded", retry_after
        return True, "", 0

    identity, authenticated = resolve_identity(request)

    if method == "GET":
        if authenticated:
            limit = config.API_RATE_LIMIT_READ_RPM
        else:
            limit = config.API_RATE_LIMIT_ANON_RPM
        allowed, retry_after = _read_limiter.check(f"{identity}|read", limit)
        if not allowed:
            return False, "rate_limit_exceeded", retry_after
        return True, "", 0

    if path.startswith("/api/admin/"):
        limit = config.API_RATE_LIMIT_ADMIN_RPM
    elif authenticated:
        limit = config.API_RATE_LIMIT_WRITE_RPM
    else:
        limit = config.API_RATE_LIMIT_ANON_RPM

    allowed, retry_after = _write_limiter.check(f"{identity}|write", limit)
    if not allowed:
        return False, "rate_limit_exceeded", retry_after

    if is_sensitive(path, method):
        allowed, retry_after = _sensitive_interval.check(
            f"{identity}|{path}", config.API_SENSITIVE_MIN_INTERVAL
        )
        if not allowed:
            return False, "too_fast", retry_after

    return True, "", 0


def in_flight_key(request) -> str | None:
    """Ключ дедупликации для мутации, либо None если защита не нужна."""
    if not is_sensitive(request.path, request.method):
        return None
    identity, _ = resolve_identity(request)
    return f"{identity}|{request.method}|{request.path}"


def acquire_in_flight(key: str) -> bool:
    return _in_flight.acquire(key)


def release_in_flight(key: str) -> None:
    _in_flight.release(key)
