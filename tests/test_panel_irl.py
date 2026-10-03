"""tests/test_panel_irl.py — тесты IRL-вкладки в админ-панели Mini App."""

import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from services.sports.models import MatchWinnerOdds, PrematchFixture
from time_utils import now_msk, today_msk_str

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ADMIN_ID = 777001
REGULAR_USER = 777002


def _init_data(user_id: int) -> str:
    user = {"id": user_id, "first_name": "Admin", "username": f"user{user_id}"}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class TestAdminPanelIrl(AioHTTPTestCase):
    async def get_application(self):
        return create_app()

    def setUp(self):
        super().setUp()
        self._orig_admins = config.ADMIN_IDS
        config.ADMIN_IDS = [ADMIN_ID]
        self._orig_token = config.TOKEN
        config.TOKEN = TEST_BOT_TOKEN
        self._orig_irl = config.IRL_ENABLED
        config.IRL_ENABLED = True
        self._orig_bm = config.IRL_BOOKMAKER_ID
        config.IRL_BOOKMAKER_ID = 4

        with database.transaction() as conn:
            conn.execute("DELETE FROM irl_bets")
            conn.execute("DELETE FROM irl_matches")
            conn.execute("DELETE FROM admin_audit_log")

    def tearDown(self):
        config.ADMIN_IDS = self._orig_admins
        config.TOKEN = self._orig_token
        config.IRL_ENABLED = self._orig_irl
        config.IRL_BOOKMAKER_ID = self._orig_bm
        super().tearDown()

    def _headers(self, user_id=ADMIN_ID):
        return {"X-Telegram-Init-Data": _init_data(user_id)}

    async def test_get_matches_and_days(self):
        # Создадим черновик и открытый матч
        kickoff = now_msk() + timedelta(hours=3)
        mid1, _ = database.create_irl_draft(
            "fix_1", 39, "Premier League", "Arsenal", "Chelsea", kickoff, 2.1, 3.4, 3.8,
            bet_day=today_msk_str()
        )
        mid2, _ = database.create_irl_draft(
            "fix_2", 140, "La Liga", "Real Madrid", "Barcelona", kickoff, 2.2, 3.5, 3.2,
            bet_day=today_msk_str()
        )
        database.publish_irl_match(mid2)

        resp = await self.client.get("/api/admin/panel/irl/matches", headers=self._headers())
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["matches"]), 2)
        self.assertIn(today_msk_str(), data["days"])

    async def test_publish_and_publish_all(self):
        kickoff = now_msk() + timedelta(hours=4)
        mid1, _ = database.create_irl_draft(
            "fix_pub1", 39, "Premier League", "Liverpool", "Everton", kickoff, 1.5, 4.2, 6.5,
            bet_day=today_msk_str()
        )
        mid2, _ = database.create_irl_draft(
            "fix_pub2", 39, "Premier League", "Man City", "Man Utd", kickoff, 1.8, 3.8, 4.5,
            bet_day=today_msk_str()
        )

        # Публикация одного
        resp = await self.client.post(f"/api/admin/panel/irl/matches/{mid1}/publish", headers=self._headers())
        self.assertEqual(resp.status, 200)
        m1 = database.get_irl_match(mid1)
        self.assertEqual(m1["status"], "open")

        # Публикация всех оставшихся
        resp = await self.client.post("/api/admin/panel/irl/matches/publish-all",
                                      headers=self._headers(), json={"day": today_msk_str()})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["published"], 1)
        m2 = database.get_irl_match(mid2)
        self.assertEqual(m2["status"], "open")

    async def test_settle_match(self):
        kickoff = now_msk() + timedelta(hours=2)
        mid, _ = database.create_irl_draft(
            "fix_settle", 39, "Premier League", "Arsenal", "Chelsea", kickoff, 2.0, 3.2, 3.8,
            bet_day=today_msk_str()
        )
        database.publish_irl_match(mid)

        # Ставка игрока
        database.get_or_create_wallet(REGULAR_USER)
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 1000 WHERE user_id = ?", (REGULAR_USER,))
        database.place_irl_bet(REGULAR_USER, mid, "home", 500, 2.0)

        # Расчёт через админку
        resp = await self.client.post(
            f"/api/admin/panel/irl/matches/{mid}/settle",
            headers=self._headers(),
            json={"result": "home", "home_goals": 2, "away_goals": 1}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["info"]["won"], 1)

        m = database.get_irl_match(mid)
        self.assertEqual(m["status"], "settled")
        self.assertEqual(m["home_goals"], 2)
        self.assertEqual(m["away_goals"], 1)

    async def test_cancel_match_refunds_bets(self):
        kickoff = now_msk() + timedelta(hours=2)
        mid, _ = database.create_irl_draft(
            "fix_can", 39, "Premier League", "Milan", "Inter", kickoff, 2.5, 3.1, 2.8,
            bet_day=today_msk_str()
        )
        database.publish_irl_match(mid)

        database.get_or_create_wallet(REGULAR_USER)
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 1000 WHERE user_id = ?", (REGULAR_USER,))
        database.place_irl_bet(REGULAR_USER, mid, "away", 400, 2.8)

        # Отмена через админку
        resp = await self.client.post(
            f"/api/admin/panel/irl/matches/{mid}/cancel",
            headers=self._headers(),
            json={"reason": "Матч перенесён из-за погоды"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["refunded"], 1)

        m = database.get_irl_match(mid)
        self.assertEqual(m["status"], "void")
        self.assertEqual(m["void_reason"], "Матч перенесён из-за погоды")

    async def test_match_bets(self):
        kickoff = now_msk() + timedelta(hours=2)
        mid, _ = database.create_irl_draft(
            "fix_bets", 39, "Premier League", "Bayern", "Dortmund", kickoff, 1.7, 4.0, 5.0,
            bet_day=today_msk_str()
        )
        database.publish_irl_match(mid)

        database.get_or_create_wallet(REGULAR_USER)
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 1000 WHERE user_id = ?", (REGULAR_USER,))
        database.place_irl_bet(REGULAR_USER, mid, "home", 300, 1.7)

        resp = await self.client.get(f"/api/admin/panel/irl/matches/{mid}/bets", headers=self._headers())
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["bets"]), 1)
        self.assertEqual(data["bets"][0]["amount"], 300)

    async def test_non_admin_forbidden(self):
        resp = await self.client.get("/api/admin/panel/irl/matches", headers=self._headers(REGULAR_USER))
        self.assertEqual(resp.status, 403)
