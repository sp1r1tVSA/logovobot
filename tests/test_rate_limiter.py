"""
tests/test_rate_limiter.py

Ограничение частоты обращений к REST API Mini App.

Инварианты:
1. Превышение окна запросов даёт HTTP 429 с заголовком Retry-After.
2. После истечения окна доступ восстанавливается.
3. Чувствительные мутации (деньги, бонусы) требуют минимального интервала.
4. Параллельный дубль мутации (двойной тап) отбивается, а не уходит в гонку.
5. In-flight слот освобождается даже если обработчик упал с исключением.
6. Память не течёт: остывшие ключи вычищаются по TTL.
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api import rate_limiter
from api.rate_limiter import (
    InFlightRegistry,
    MinIntervalLimiter,
    SlidingWindowLimiter,
)
from api.server import create_app, rate_limit_middleware
from tests.test_auth_security import TEST_TOKEN, build_init_data


class TestSlidingWindowLimiter(AioHTTPTestCase):
    """Алгоритм окна в изоляции, без HTTP."""

    async def get_application(self):
        return web.Application()

    def test_allows_up_to_limit(self):
        limiter = SlidingWindowLimiter(window_seconds=60)
        for _ in range(5):
            allowed, _ = limiter.check("k", 5)
            self.assertTrue(allowed)

    def test_blocks_beyond_limit(self):
        limiter = SlidingWindowLimiter(window_seconds=60)
        for _ in range(5):
            limiter.check("k", 5)
        allowed, retry_after = limiter.check("k", 5)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_keys_are_independent(self):
        limiter = SlidingWindowLimiter(window_seconds=60)
        for _ in range(3):
            limiter.check("user:1", 3)
        allowed, _ = limiter.check("user:2", 3)
        self.assertTrue(allowed, "лимит одного пользователя не должен задевать другого")

    def test_window_slides_and_access_recovers(self):
        limiter = SlidingWindowLimiter(window_seconds=0.3)
        for _ in range(3):
            limiter.check("k", 3)
        self.assertFalse(limiter.check("k", 3)[0])
        time.sleep(0.35)
        self.assertTrue(limiter.check("k", 3)[0], "после истечения окна доступ обязан вернуться")

    def test_rejected_attempts_do_not_extend_the_block(self):
        """Флуд не должен бесконечно продлевать блокировку — иначе не разблокируешься."""
        limiter = SlidingWindowLimiter(window_seconds=0.3)
        limiter.check("k", 1)
        for _ in range(20):
            limiter.check("k", 1)
        time.sleep(0.35)
        self.assertTrue(limiter.check("k", 1)[0])

    def test_zero_limit_disables_check(self):
        limiter = SlidingWindowLimiter()
        for _ in range(50):
            self.assertTrue(limiter.check("k", 0)[0])

    def test_stale_keys_are_swept(self):
        """TTL-очистка: словарь не растёт бесконечно от разовых посетителей."""
        limiter = SlidingWindowLimiter(window_seconds=0.05)
        for i in range(200):
            limiter.check(f"ip:{i}", 10)
        self.assertEqual(len(limiter._hits), 200)

        time.sleep(0.1)
        limiter._last_sweep = time.monotonic() - 999
        limiter.check("fresh", 10)
        self.assertLessEqual(len(limiter._hits), 1)


class TestMinIntervalLimiter(AioHTTPTestCase):
    async def get_application(self):
        return web.Application()

    def test_second_call_within_interval_blocked(self):
        limiter = MinIntervalLimiter()
        self.assertTrue(limiter.check("k", 2.0)[0])
        allowed, retry_after = limiter.check("k", 2.0)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)

    def test_call_after_interval_allowed(self):
        limiter = MinIntervalLimiter()
        limiter.check("k", 0.2)
        time.sleep(0.25)
        self.assertTrue(limiter.check("k", 0.2)[0])

    def test_zero_interval_disables_check(self):
        limiter = MinIntervalLimiter()
        self.assertTrue(limiter.check("k", 0)[0])
        self.assertTrue(limiter.check("k", 0)[0])


class TestInFlightRegistry(AioHTTPTestCase):
    async def get_application(self):
        return web.Application()

    def test_second_acquire_denied_until_release(self):
        registry = InFlightRegistry()
        self.assertTrue(registry.acquire("k"))
        self.assertFalse(registry.acquire("k"))
        registry.release("k")
        self.assertTrue(registry.acquire("k"))

    def test_release_of_unknown_key_is_safe(self):
        registry = InFlightRegistry()
        registry.release("never-acquired")


class _RateLimitAppTestCase(AioHTTPTestCase):
    """Общая обвязка: лимиты включены, состояние сброшено, токен детерминирован."""

    def setUp(self):
        super().setUp()
        self._orig = {
            "enabled": config.API_RATE_LIMIT_ENABLED,
            "read": config.API_RATE_LIMIT_READ_RPM,
            "write": config.API_RATE_LIMIT_WRITE_RPM,
            "anon": config.API_RATE_LIMIT_ANON_RPM,
            "interval": config.API_SENSITIVE_MIN_INTERVAL,
            "token": config.TOKEN,
        }
        config.API_RATE_LIMIT_ENABLED = True
        config.TOKEN = TEST_TOKEN
        rate_limiter.reset_all()

    def tearDown(self):
        config.API_RATE_LIMIT_ENABLED = self._orig["enabled"]
        config.API_RATE_LIMIT_READ_RPM = self._orig["read"]
        config.API_RATE_LIMIT_WRITE_RPM = self._orig["write"]
        config.API_RATE_LIMIT_ANON_RPM = self._orig["anon"]
        config.API_SENSITIVE_MIN_INTERVAL = self._orig["interval"]
        config.TOKEN = self._orig["token"]
        rate_limiter.reset_all()
        super().tearDown()


class TestRateLimitMiddlewareOnRealApp(_RateLimitAppTestCase):
    """Middleware действительно подключён к боевому приложению."""

    async def get_application(self):
        database.init_db()
        return create_app()

    async def test_flood_of_reads_returns_429_with_retry_after(self):
        config.API_RATE_LIMIT_READ_RPM = 4
        headers = {"X-Telegram-Init-Data": build_init_data(user_id=881001)}

        statuses = []
        for _ in range(8):
            resp = await self.client.request("GET", "/api/wallet", headers=headers)
            statuses.append(resp.status)

        self.assertIn(429, statuses, "флуд обязан упереться в лимит")

        resp = await self.client.request("GET", "/api/wallet", headers=headers)
        self.assertEqual(resp.status, 429)
        self.assertIn("Retry-After", resp.headers)
        self.assertGreater(int(resp.headers["Retry-After"]), 0)

        body = await resp.json()
        self.assertEqual(body["error"], "rate_limit_exceeded")
        self.assertGreater(body["retry_after"], 0)

    async def test_limit_is_per_user_not_global(self):
        """Один флудер не должен закрывать API остальным."""
        config.API_RATE_LIMIT_READ_RPM = 3
        flooder = {"X-Telegram-Init-Data": build_init_data(user_id=881002)}
        bystander = {"X-Telegram-Init-Data": build_init_data(user_id=881003)}

        for _ in range(6):
            await self.client.request("GET", "/api/wallet", headers=flooder)

        resp = await self.client.request("GET", "/api/wallet", headers=bystander)
        self.assertNotEqual(resp.status, 429)

    async def test_access_restored_after_window_expires(self):
        config.API_RATE_LIMIT_READ_RPM = 2
        headers = {"X-Telegram-Init-Data": build_init_data(user_id=881004)}

        for _ in range(4):
            await self.client.request("GET", "/api/wallet", headers=headers)
        self.assertEqual(
            (await self.client.request("GET", "/api/wallet", headers=headers)).status, 429
        )

        # Окно скользящее: отматываем метки в прошлое вместо ожидания минуты.
        rate_limiter._read_limiter.window_seconds = 0.2
        time.sleep(0.25)

        resp = await self.client.request("GET", "/api/wallet", headers=headers)
        self.assertNotEqual(resp.status, 429, "после истечения окна доступ обязан вернуться")

    async def test_sensitive_mutation_requires_min_interval(self):
        config.API_SENSITIVE_MIN_INTERVAL = 30.0
        headers = {"X-Telegram-Init-Data": build_init_data(user_id=881005)}

        first = await self.client.request("POST", "/api/achievements/claim", headers=headers)
        self.assertNotEqual(first.status, 429)

        second = await self.client.request("POST", "/api/achievements/claim", headers=headers)
        self.assertEqual(second.status, 429)
        body = await second.json()
        self.assertEqual(body["error"], "too_fast")
        self.assertIn("Retry-After", second.headers)

    async def test_reads_are_not_blocked_by_write_interval(self):
        """Строгий интервал мутаций не должен мешать обычному чтению."""
        config.API_SENSITIVE_MIN_INTERVAL = 30.0
        config.API_RATE_LIMIT_READ_RPM = 30
        headers = {"X-Telegram-Init-Data": build_init_data(user_id=881006)}

        await self.client.request("POST", "/api/achievements/claim", headers=headers)
        resp = await self.client.request("GET", "/api/wallet", headers=headers)
        self.assertNotEqual(resp.status, 429)

    async def test_disabled_flag_turns_limiting_off(self):
        config.API_RATE_LIMIT_ENABLED = False
        config.API_RATE_LIMIT_READ_RPM = 1
        headers = {"X-Telegram-Init-Data": build_init_data(user_id=881007)}

        for _ in range(5):
            resp = await self.client.request("GET", "/api/wallet", headers=headers)
            self.assertNotEqual(resp.status, 429)

    async def test_static_assets_are_not_rate_limited(self):
        config.API_RATE_LIMIT_READ_RPM = 1
        for _ in range(5):
            resp = await self.client.request("GET", "/")
            self.assertNotEqual(resp.status, 429)


class TestDoubleClickProtection(_RateLimitAppTestCase):
    """
    Гонка на двойном тапе.

    Обработчик намеренно медленный, а минимальный интервал выключен — так
    проверяется именно in-flight блокировка, а не throttling по времени.
    """

    async def get_application(self):
        async def slow_mutation(request):
            await asyncio.sleep(0.3)
            return web.json_response({"status": "ok"})

        async def exploding_mutation(request):
            raise RuntimeError("handler exploded")

        app = web.Application(middlewares=[rate_limit_middleware])
        app.router.add_post("/api/achievements/claim", slow_mutation)
        app.router.add_post("/api/predictions", exploding_mutation)
        return app

    def setUp(self):
        super().setUp()
        config.API_SENSITIVE_MIN_INTERVAL = 0.0
        config.API_RATE_LIMIT_WRITE_RPM = 100
        config.API_RATE_LIMIT_ANON_RPM = 100

    async def test_parallel_duplicate_mutation_is_rejected(self):
        first, second = await asyncio.gather(
            self.client.request("POST", "/api/achievements/claim"),
            self.client.request("POST", "/api/achievements/claim"),
        )
        statuses = sorted([first.status, second.status])
        self.assertEqual(statuses, [200, 429], "ровно один параллельный дубль должен пройти")

        rejected = first if first.status == 429 else second
        body = await rejected.json()
        self.assertEqual(body["error"], "duplicate_request")
        self.assertIn("Retry-After", rejected.headers)

    async def test_sequential_mutations_are_allowed(self):
        """Дедупликация не должна ломать нормальный последовательный сценарий."""
        first = await self.client.request("POST", "/api/achievements/claim")
        self.assertEqual(first.status, 200)
        second = await self.client.request("POST", "/api/achievements/claim")
        self.assertEqual(second.status, 200)

    async def test_in_flight_slot_released_after_handler_error(self):
        """Упавший обработчик не должен навсегда заблокировать маршрут."""
        first = await self.client.request("POST", "/api/predictions")
        self.assertEqual(first.status, 500)

        second = await self.client.request("POST", "/api/predictions")
        self.assertEqual(second.status, 500, "слот обязан освобождаться в finally")
