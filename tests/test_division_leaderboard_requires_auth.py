"""
Аудит 6a — дивизионный лидерборд обязан требовать initData.

`GET /api/leaderboard/division/{division_id}` (`api/routes_wallet.py`) отдаёт
`services/analytics_service.get_capper_leaderboard`, а это на каждого игрока:
`user_id`, `username`, `total_staked`, `total_payout`, `net_profit`, `roi_pct`.
До фикса маршрут был единственным в списке `PUBLIC_BY_DESIGN`, которому данные
о деньгах игроков отдавались анонимно, — при этом соседний
`GET /api/leaderboard/division` (без параметра в пути) и `GET /api/leaderboard`
авторизацию требовали.

Здесь проверяется граница, а не формат ответа (формат покрыт
`tests/test_phase6_analytics.py` на уровне сервиса):

 A. анонимно — строго 401 и ни одного поля с деньгами;
 B. испорченная HMAC-подпись — 401;
 C. подписанная, но чужим токеном — 401;
 D. авторизованный игрок — 200 и свои лидеры на месте (фикс не сломал доступ);
 E. `check_user_access == False` — 403 (тот же гейт, что у `/api/wallet`).

Инвентарь `tests/test_api_route_matrix.py` после удаления роута из
`PUBLIC_BY_DESIGN` требует 401 для него автоматически — этот файл добавляет
позитивную ветку и проверку тела.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import uuid
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database
from api.server import create_app

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
PATH = "/api/leaderboard/division/1"

# То же окно, что у матрицы: подписываем заведомо недействительным токеном из
# документации Telegram, чтобы файл не зависел от .env.
FOREIGN_TOKEN = "999999:ZZZ-aaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def make_init_data(user_id: int, token: str = TEST_BOT_TOKEN, username: str = "lb_tester") -> str:
    user = {"id": user_id, "first_name": "Leaderboard", "username": username}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class TestDivisionLeaderboardRequiresAuth(AioHTTPTestCase):

    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_token = config.TOKEN
        self._orig_rate_limit = config.API_RATE_LIMIT_ENABLED
        config.TOKEN = TEST_BOT_TOKEN
        config.API_RATE_LIMIT_ENABLED = False
        await super().asyncSetUp()

        uid = uuid.uuid4().hex[:6].upper()
        self.player_id = 974101
        database.register_user(self.player_id, f"lb_player_{uid}")
        # Лидерборд скоупнут по u.division_id — задаём его явно, как в бою.
        with database.transaction() as conn:
            c = conn.cursor()
            # Файловая БД живёт весь модуль, а asyncSetUp — на каждый тест:
            # без очистки счётчик ставок накапливался бы от теста к тесту.
            c.execute("DELETE FROM user_bets WHERE user_id = ?", (self.player_id,))
            c.execute(
                "UPDATE users SET division_id = 1 WHERE telegram_id = ?", (self.player_id,)
            )
            for _ in range(5):
                c.execute(
                    """INSERT INTO user_bets (user_id, bet_type, amount, total_odd, potential_win,
                                               status, created_at)
                       VALUES (?, 'single', 100, 2.0, 250, 'won', datetime('now', '+3 hours'))""",
                    (self.player_id,),
                )

    async def asyncTearDown(self):
        config.TOKEN = self._orig_token
        config.API_RATE_LIMIT_ENABLED = self._orig_rate_limit
        await super().asyncTearDown()

    def _auth_headers(self, user_id: int, token: str = TEST_BOT_TOKEN) -> dict:
        return {"X-Telegram-Init-Data": make_init_data(user_id, token=token)}

    # --- A. анонимный доступ ------------------------------------------------

    async def test_anonymous_request_is_401(self):
        resp = await self.client.request("GET", PATH)
        self.assertEqual(401, resp.status)
        body = await resp.json()
        self.assertEqual("error", body["status"])
        self.assertEqual("unauthorized", body.get("error"))
        self.assertNotIn("leaders", body)

    async def test_anonymous_body_carries_no_player_money_fields(self):
        resp = await self.client.request("GET", PATH)
        text = await resp.text()
        for field in ("total_staked", "total_payout", "net_profit", "roi_pct", "win_rate_pct"):
            self.assertNotIn(field, text, f"{field} утёк в анонимном ответе")

    # --- B/C. подпись -------------------------------------------------------

    async def test_tampered_signature_is_401(self):
        bad = make_init_data(self.player_id)[:-4] + "beef"
        resp = await self.client.request("GET", PATH, headers={"X-Telegram-Init-Data": bad})
        self.assertEqual(401, resp.status)

    async def test_foreign_token_signature_is_401(self):
        resp = await self.client.request(
            "GET", PATH, headers=self._auth_headers(self.player_id, token=FOREIGN_TOKEN)
        )
        self.assertEqual(401, resp.status)

    # --- D. легитимный доступ сохранён --------------------------------------

    async def test_authenticated_player_still_gets_the_leaderboard(self):
        resp = await self.client.request("GET", PATH, headers=self._auth_headers(self.player_id))
        self.assertEqual(200, resp.status)
        body = await resp.json()
        self.assertEqual("ok", body["status"])
        self.assertEqual(1, body["division_id"])
        leaders = body["leaders"]
        self.assertEqual(1, len(leaders), "Игрок с пятью рассчитанными ставками обязан быть в списке")
        row = leaders[0]
        self.assertEqual(self.player_id, row["user_id"])
        self.assertEqual(500, row["total_staked"])
        self.assertEqual(1250, row["total_payout"])
        self.assertEqual(750, row["net_profit"])

    async def test_wrong_division_scope_is_not_an_auth_failure(self):
        """Скоупинг не подменён: дивизион 2 того же игрока — пустой список, не 401."""
        resp = await self.client.request(
            "GET", "/api/leaderboard/division/2", headers=self._auth_headers(self.player_id)
        )
        self.assertEqual(200, resp.status)
        body = await resp.json()
        self.assertEqual([], body["leaders"])

    # --- E. feature-access гейт --------------------------------------------

    async def test_restricted_user_is_403(self):
        with mock.patch("api.routes_wallet.check_user_access", return_value=False):
            resp = await self.client.request(
                "GET", PATH, headers=self._auth_headers(self.player_id)
            )
        self.assertEqual(403, resp.status)
        body = await resp.json()
        self.assertEqual("access_restricted", body.get("error"))


if __name__ == "__main__":
    import unittest

    unittest.main()
