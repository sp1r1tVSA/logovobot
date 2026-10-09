"""tests/test_panel_irl_limits.py — Exhaustive tests for IRL betting limits in panel and placement."""

import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import datetime, timedelta

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from services.betting_limits import BettingLimitsService, IRL_LIMIT_KEYS
from time_utils import now_msk, today_msk_str

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
ADMIN_ID = 888001
PLAYER_1 = 888002
PLAYER_2 = 888003


def _init_data(user_id: int) -> str:
    user = {"id": user_id, "first_name": "TestUser", "username": f"user{user_id}"}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def _make_user(uid: int, username: str, balance: int = 50000):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                     (uid, username))
    database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = ? WHERE user_id = ?", (balance, uid))


def _insert_match(mid: int, kickoff_offset_hours: int = 2) -> int:
    kickoff = now_msk() + timedelta(hours=kickoff_offset_hours)
    match_id, _ = database.create_irl_draft(
        f"fix_{mid}", 39, "Premier League", f"Team A {mid}", f"Team B {mid}", kickoff,
        2.0, 3.0, 4.0, bet_day=today_msk_str()
    )
    database.publish_irl_match(match_id)
    return match_id


class TestPanelIrlLimits(AioHTTPTestCase):
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
        with database.transaction() as conn:
            conn.execute("DELETE FROM irl_express_items")
            conn.execute("DELETE FROM irl_expresses")
            conn.execute("DELETE FROM irl_bets")
            conn.execute("DELETE FROM irl_matches")
            conn.execute("DELETE FROM risk_limits_config")
            conn.execute("DELETE FROM coin_transactions")
            conn.execute("DELETE FROM admin_audit_log")
        _make_user(ADMIN_ID, "admin_user", 100_000)
        _make_user(PLAYER_1, "player_one", 100_000)
        _make_user(PLAYER_2, "player_two", 100_000)

    def tearDown(self):
        config.ADMIN_IDS = self._orig_admins
        config.TOKEN = self._orig_token
        config.IRL_ENABLED = self._orig_irl
        super().tearDown()

    def _headers(self, user_id: int = ADMIN_ID) -> dict[str, str]:
        return {
            "X-Telegram-Init-Data": _init_data(user_id),
            "Content-Type": "application/json",
        }

    async def test_limits_endpoint_reports_irl_bounds_and_defaults(self):
        resp = await self.client.get("/api/admin/panel/limits", headers=self._headers(ADMIN_ID))
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")

        for key in IRL_LIMIT_KEYS:
            self.assertIn(key, data["system"], f"Missing {key} in system")
            self.assertIn(key, data["defaults"], f"Missing {key} in defaults")
            self.assertIn(key, data["bounds"], f"Missing {key} in bounds")
            low, high = data["bounds"][key]
            self.assertLess(low, high)

    async def test_set_and_reset_irl_limits_from_panel(self):
        # Set irl_max_bet
        resp = await self.client.post("/api/admin/panel/limits", headers=self._headers(ADMIN_ID), json={
            "scope_type": "global",
            "scope_id": 0,
            "limit_key": "irl_max_bet",
            "value": 2500,
        })
        self.assertEqual(resp.status, 200)

        sys_limits = BettingLimitsService.get_system_limits()
        self.assertEqual(sys_limits["irl_max_bet"], 2500)

        # Reset irl_max_bet
        resp = await self.client.post("/api/admin/panel/limits", headers=self._headers(ADMIN_ID), json={
            "scope_type": "global",
            "scope_id": 0,
            "limit_key": "irl_max_bet",
            "value": None,
        })
        self.assertEqual(resp.status, 200)

        sys_limits_reset = BettingLimitsService.get_system_limits()
        self.assertEqual(sys_limits_reset["irl_max_bet"], BettingLimitsService.get_default_limits()["irl_max_bet"])

    async def test_irl_min_bet_conflict_with_max_bet(self):
        # Setting min_bet higher than current max_bet fails
        sys_limits = BettingLimitsService.get_system_limits()
        resp = await self.client.post("/api/admin/panel/limits", headers=self._headers(ADMIN_ID), json={
            "scope_type": "global",
            "scope_id": 0,
            "limit_key": "irl_min_bet",
            "value": sys_limits["irl_max_bet"] + 100,
        })
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertEqual(data["error"], "limits_conflict")

    async def test_today_route_includes_irl_limits(self):
        resp = await self.client.get("/api/irl/today", headers=self._headers(PLAYER_1))
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("min_bet", data)
        self.assertIn("max_bet", data)
        self.assertIn("max_payout", data)
        self.assertIn("max_express_events", data)

    async def test_enforce_irl_min_and_max_bet(self):
        mid = _insert_match(901)
        BettingLimitsService.set_limit("global", 0, "irl_min_bet", 50)
        BettingLimitsService.set_limit("global", 0, "irl_max_bet", 500)

        # Under min_bet
        ok, res = database.place_irl_bet(PLAYER_1, mid, "home", 30)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "MIN_BET_NOT_REACHED")

        # Over max_bet
        ok, res = database.place_irl_bet(PLAYER_1, mid, "home", 600)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "MAX_BET_EXCEEDED")

        # Within bounds
        ok, res = database.place_irl_bet(PLAYER_1, mid, "home", 100)
        self.assertTrue(ok)

    async def test_enforce_irl_max_open_bets(self):
        m1 = _insert_match(902)
        m2 = _insert_match(903)
        m3 = _insert_match(904)

        BettingLimitsService.set_limit("global", 0, "irl_max_open_bets", 2)

        ok1, _ = database.place_irl_bet(PLAYER_1, m1, "home", 100)
        self.assertTrue(ok1)
        ok2, _ = database.place_irl_bet(PLAYER_1, m2, "home", 100)
        self.assertTrue(ok2)

        # Third bet hits open bets limit
        ok3, res3 = database.place_irl_bet(PLAYER_1, m3, "home", 100)
        self.assertFalse(ok3)
        self.assertEqual(res3["error"], "IRL_OPEN_BETS_LIMIT")

    async def test_enforce_irl_max_daily_stake(self):
        m1 = _insert_match(905)
        m2 = _insert_match(906)

        BettingLimitsService.set_limit("global", 0, "irl_max_daily_stake", 400)
        ok1, _ = database.place_irl_bet(PLAYER_1, m1, "home", 300)
        self.assertTrue(ok1)

        # 300 + 200 = 500 > 400
        ok2, res2 = database.place_irl_bet(PLAYER_1, m2, "home", 200)
        self.assertFalse(ok2)
        self.assertEqual(res2["error"], "IRL_DAILY_LIMIT")

    async def test_enforce_irl_max_express_events(self):
        m1 = _insert_match(907)
        m2 = _insert_match(908)
        m3 = _insert_match(909)
        m4 = _insert_match(910)

        BettingLimitsService.set_limit("global", 0, "irl_max_express_events", 3)

        legs = [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m2, "outcome": "home"},
            {"match_id": m3, "outcome": "home"},
            {"match_id": m4, "outcome": "home"},
        ]
        ok, res = database.place_irl_express(PLAYER_1, legs, 100)
        self.assertFalse(ok)
        self.assertEqual(res["error"], database.IRL_INVALID_EXPRESS_LEGS_ERROR)

    async def test_enforce_irl_match_and_global_exposure(self):
        m1 = _insert_match(911)
        # Match exposure limit
        BettingLimitsService.set_limit("global", 0, "irl_match_exposure_limit", 300)
        # odd = 2.0, amount = 200 => potential = 400 > 300
        ok, res = database.place_irl_bet(PLAYER_1, m1, "home", 200)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "IRL_MATCH_EXPOSURE_LIMIT")

        # Now raise match exposure, but restrict global exposure
        BettingLimitsService.set_limit("global", 0, "irl_match_exposure_limit", 10000)
        BettingLimitsService.set_limit("global", 0, "irl_global_exposure_limit", 300)
        ok2, res2 = database.place_irl_bet(PLAYER_1, m1, "home", 200)
        self.assertFalse(ok2)
        self.assertEqual(res2["error"], "IRL_GLOBAL_EXPOSURE_LIMIT")

    async def test_irl_max_payout_caps_potential_win(self):
        m1 = _insert_match(912)
        BettingLimitsService.set_limit("global", 0, "irl_max_payout", 250)
        # amount = 100, odd = 4.0 => uncapped = 400, capped = 250
        ok, res = database.place_irl_bet(PLAYER_1, m1, "away", 100)
        self.assertTrue(ok)
        self.assertEqual(res["potential_win"], 250)
        self.assertTrue(res["payout_capped"])
