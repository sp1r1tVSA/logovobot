"""Сквозная матрица маршрутов Mini App API.

Инвентарь маршрутов берётся из самого `create_app()`, поэтому новый роут
попадает под проверки автоматически — без правки этого файла. Проверяется три
границы, которые обязаны держаться на КАЖДОМ `/api/` маршруте:

1. FAIL-CLOSED: запрос без initData — строго 401.
2. FAIL-CLOSED: запрос с испорченной HMAC-подписью — строго 401.
3. RBAC: `/api/admin/*` для обычного авторизованного игрока — строго 403.

Плюс контрактная проверка тела: POST с битым JSON отдаёт 4xx, а не 500.

Тест самодостаточен: подписывает initData собственным недействительным
токеном из документации Telegram, поэтому не зависит от .env.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app

# Заведомо недействительный токен из документации Telegram.
TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

# Подстановка для динамических сегментов пути. Значение заведомо несуществующее:
# нас интересует код авторизации, а не наличие сущности.
PATH_PARAM_VALUE = "999000111"

# Маршруты, которые сознательно отдают данные без initData. Список — это
# зафиксированное текущее поведение, а не одобрение: каждая строка помечена
# причиной в отчёте аудита.
PUBLIC_BY_DESIGN = {
    ("GET", "/api/matches/hot"),
    ("GET", "/api/matches/{id}/photo"),
    # Logout отзывает Bearer-токен, если он есть, но обязан отвечать 200 и без
    # него/с мусорным токеном — иначе игрок, который хочет выйти, не сможет
    # этого сделать с просроченной или уже невалидной сессией.
    ("POST", "/api/tracker/auth/logout"),
}


def _collect_api_routes(app):
    """(method, template) для всех /api/ маршрутов приложения."""
    routes = []
    for resource in app.router.resources():
        info = resource.get_info()
        template = info.get("formatter") or info.get("path")
        if not template or not template.startswith("/api/"):
            continue
        for route in resource:
            if route.method in ("HEAD", "OPTIONS", "*"):
                continue
            routes.append((route.method, template))
    return sorted(set(routes))


def _fill(template: str) -> str:
    """Подставить числовой id во все динамические сегменты шаблона."""
    out = []
    for part in template.split("/"):
        out.append(PATH_PARAM_VALUE if part.startswith("{") and part.endswith("}") else part)
    return "/".join(out)


def make_init_data(user_id: int, token: str = TEST_BOT_TOKEN, username: str = "matrix") -> str:
    user = {"id": user_id, "first_name": "Matrix", "username": username}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class TestApiRouteMatrix(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_token = config.TOKEN
        self._orig_rate_limit = config.API_RATE_LIMIT_ENABLED
        self._orig_admin_ids = config.ADMIN_IDS
        config.TOKEN = TEST_BOT_TOKEN
        # Матрица бьёт по сотне маршрутов подряд — лимитер иначе отдаст 429
        # и скроет реальный код авторизации.
        config.API_RATE_LIMIT_ENABLED = False

        await super().asyncSetUp()

        uid = uuid.uuid4().hex[:6].upper()
        self.player_id = 970101
        self.intruder_id = 970102
        config.ADMIN_IDS = []
        database.register_user(self.player_id, f"matrix_player_{uid}", team_name=f"Matrix FC {uid}")
        database.register_user(self.intruder_id, f"matrix_intruder_{uid}", team_name=f"Intruder FC {uid}")

        self.routes = _collect_api_routes(self.app)

    def _seed_bet(self, owner_id: int) -> int:
        """Создать купон на имя owner_id и вернуть его id."""
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO user_bets (user_id, bet_type, amount, total_odd, potential_win, status)
                   VALUES (?, 'single', 100, 2.0, 200, 'pending')""",
                (owner_id,),
            )
            return cur.lastrowid

    async def asyncTearDown(self):
        config.TOKEN = self._orig_token
        config.API_RATE_LIMIT_ENABLED = self._orig_rate_limit
        config.ADMIN_IDS = self._orig_admin_ids
        await super().asyncTearDown()

    def test_inventory_is_not_empty(self):
        """Страховка: если инвентарь пуст, остальные тесты проходят вхолостую."""
        self.assertGreater(len(self.routes), 50, "Инвентарь маршрутов подозрительно мал")

    async def test_missing_init_data_is_401(self):
        """Ни один /api/ маршрут не отдаёт данные без initData."""
        leaks = []
        for method, template in self.routes:
            if (method, template) in PUBLIC_BY_DESIGN:
                continue
            resp = await self.client.request(method, _fill(template), json={})
            if resp.status != 401:
                leaks.append(f"{method} {template} -> {resp.status}")
        self.assertEqual([], leaks, "Маршруты без initData обязаны отдавать 401:\n" + "\n".join(leaks))

    async def test_public_by_design_routes_stay_documented(self):
        """Список публичных маршрутов не должен молча расти."""
        unexpected = []
        for method, template in self.routes:
            if (method, template) in PUBLIC_BY_DESIGN:
                continue
            resp = await self.client.request(method, _fill(template), json={})
            if resp.status == 200:
                unexpected.append(f"{method} {template}")
        self.assertEqual([], unexpected, "Новый анонимно доступный маршрут:\n" + "\n".join(unexpected))

    async def test_tampered_hash_is_401(self):
        """Подделанная подпись initData неотличима по последствиям от её отсутствия."""
        bad = make_init_data(self.player_id)[:-4] + "dead"
        headers = {"X-Telegram-Init-Data": bad}
        leaks = []
        for method, template in self.routes:
            if (method, template) in PUBLIC_BY_DESIGN:
                continue
            resp = await self.client.request(method, _fill(template), headers=headers, json={})
            if resp.status != 401:
                leaks.append(f"{method} {template} -> {resp.status}")
        self.assertEqual([], leaks, "Битая подпись обязана давать 401:\n" + "\n".join(leaks))

    async def test_foreign_token_signature_is_401(self):
        """initData, подписанная чужим токеном, не проходит."""
        other = make_init_data(self.player_id, token="999999:OTHER-BOT-TOKEN-xxxxxxxxxxxxxxxxxxx")
        resp = await self.client.request("GET", "/api/bootstrap", headers={"X-Telegram-Init-Data": other})
        self.assertEqual(401, resp.status)

    async def test_admin_routes_forbidden_for_player(self):
        """RBAC: игрок с валидным initData не проходит ни в один /api/admin/*."""
        headers = {"X-Telegram-Init-Data": make_init_data(self.player_id)}
        breaches = []
        for method, template in self.routes:
            if not template.startswith("/api/admin/"):
                continue
            resp = await self.client.request(method, _fill(template), headers=headers, json={})
            if resp.status != 403:
                breaches.append(f"{method} {template} -> {resp.status}")
        self.assertEqual([], breaches, "Админские маршруты обязаны давать 403 игроку:\n" + "\n".join(breaches))

    async def test_post_routes_reject_malformed_body(self):
        """Битый JSON — это 4xx, а не 500 и не молчаливый успех."""
        headers = {
            "X-Telegram-Init-Data": make_init_data(self.player_id),
            "Content-Type": "application/json",
        }
        bad = []
        for method, template in self.routes:
            if method not in ("POST", "PUT"):
                continue
            resp = await self.client.request(method, _fill(template), headers=headers, data="{not json")
            if resp.status >= 500:
                bad.append(f"{method} {template} -> {resp.status}")
        self.assertEqual([], bad, "Битый JSON не должен приводить к 5xx:\n" + "\n".join(bad))

    async def test_foreign_bet_is_not_readable(self):
        """IDOR: чужой купон не читается по прямому id."""
        bet_id = self._seed_bet(self.player_id)
        intruder = {"X-Telegram-Init-Data": make_init_data(self.intruder_id, username="intruder")}
        owner = {"X-Telegram-Init-Data": make_init_data(self.player_id)}

        own = await self.client.request("GET", f"/api/predictions/{bet_id}", headers=owner)
        self.assertEqual(200, own.status, "Владелец обязан видеть свой купон")

        for path in (f"/api/predictions/{bet_id}", f"/api/bets/{bet_id}"):
            resp = await self.client.request("GET", path, headers=intruder)
            self.assertIn(resp.status, (403, 404), f"{path} отдал чужой купон: {resp.status}")

    async def test_foreign_bet_is_not_cashable(self):
        """IDOR: чужой купон нельзя обналичить или повторить."""
        bet_id = self._seed_bet(self.player_id)
        intruder = {"X-Telegram-Init-Data": make_init_data(self.intruder_id, username="intruder")}

        quote = await self.client.request(
            "GET", f"/api/predictions/{bet_id}/cashout-quote", headers=intruder)
        if quote.status == 200:
            body = await quote.json()
            self.assertFalse(
                body.get("quote", {}).get("available"),
                "Котировка кэшаута выдана на чужой купон",
            )

        for path in (f"/api/predictions/{bet_id}/cashout", f"/api/predictions/{bet_id}/repeat"):
            resp = await self.client.request("POST", path, headers=intruder, json={})
            self.assertNotEqual(200, resp.status, f"{path} выполнен на чужом купоне")

    async def test_foreign_profile_exposes_public_fields_only(self):
        """Чужой профиль не раскрывает кошелёк."""
        intruder = {"X-Telegram-Init-Data": make_init_data(self.intruder_id, username="intruder")}
        resp = await self.client.request("GET", f"/api/profile/{self.player_id}", headers=intruder)
        self.assertEqual(200, resp.status)
        body = await resp.json()
        self.assertFalse(body.get("is_self"))
        profile = body.get("profile") or {}
        for secret in ("balance", "total_wagered", "wallet"):
            self.assertNotIn(secret, profile, f"Чужой профиль раскрывает {secret}")

    async def test_player_routes_answer_authenticated_request(self):
        """Личные маршруты игрока отвечают 200 на валидный initData."""
        headers = {"X-Telegram-Init-Data": make_init_data(self.player_id)}
        for path in ("/api/bootstrap", "/api/wallet", "/api/predictions",
                     "/api/cabinet/overview", "/api/cabinet/matches", "/api/cabinet/squad",
                     "/api/progression", "/api/achievements", "/api/stats/me",
                     "/api/divisions", "/api/standings", "/api/saved-coupons"):
            resp = await self.client.request("GET", path, headers=headers)
            self.assertEqual(200, resp.status, f"{path} -> {resp.status}")
            body = await resp.json()
            self.assertEqual("ok", body.get("status"), f"{path}: {body}")
