"""
tests/test_lockdown.py

Comprehensive test suite verifying the Global Lockdown Mode (LOGOVO_LOCKDOWN=true):
1. lockdown=false -> regular user works as before.
2. lockdown=true + regular user: Telegram command -> BLOCKED with lockdown message.
3. lockdown=true + regular user: callback query -> BLOCKED with alert and ApplicationHandlerStop.
4. lockdown=true + regular user: GET API -> 403 {"error": "LOGOVO_LOCKDOWN", ...}.
5. lockdown=true + regular user: POST API -> 403 {"error": "LOGOVO_LOCKDOWN", ...}.
6. lockdown=true + regular user: place bet -> BLOCKED before database change.
7. lockdown=true + regular user: cashout -> BLOCKED before database change.
8. lockdown=true + regular user: reward / bonus -> BLOCKED before database change.
9. lockdown=true + global admin: Telegram command and callback -> PASS.
10. lockdown=true + global admin: API (GET & POST) -> PASS (200 OK).
11. Verification that database remains unchanged when non-admin action is attempted during lockdown.
12. Verification that division admin (without global privileges) is strictly blocked during lockdown.
13. Verification that unauthenticated API request returns 401 Unauthorized, not 403 (strict auth order).
"""

import os
import sys
import time
import json
import hmac
import hashlib
import urllib.parse
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from telegram.ext import ApplicationHandlerStop

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import config
from config import is_global_lockdown_enabled
from handlers.base import is_global_admin, is_admin_user, is_logovo_access_allowed
from handlers import global_lockdown_guard
from api.server import create_app


def generate_mock_init_data(user_id: int, username: str = "kapper") -> str:
    token = config.TOKEN or "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    user_dict = {
        "id": user_id,
        "first_name": "TestUser",
        "username": username
    }
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_dict, separators=(",", ":"))
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    hash_val = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    data["hash"] = hash_val
    return urllib.parse.urlencode(data)


# -----------------------------------------------------------------------------
# Unit & Telegram Guard Tests
# -----------------------------------------------------------------------------

def test_01_lockdown_config_and_access_helpers(monkeypatch):
    """Scenario 1: Access allowed when lockdown is false; strictly guarded when true."""
    regular_user = 999101
    global_admin = 999102
    div_admin = 999103

    database.init_db()
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'player', 'player')", (regular_user,))
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'admin', 'admin')", (global_admin,))
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role, division_id) VALUES (?, 'divadmin', 'division_admin', 1)", (div_admin,))

    # When lockdown is false (or unset)
    monkeypatch.delenv("LOGOVO_LOCKDOWN", raising=False)
    assert is_global_lockdown_enabled() is False
    assert is_logovo_access_allowed(regular_user) is True
    assert is_logovo_access_allowed(div_admin) is True
    assert is_logovo_access_allowed(global_admin) is True

    # When lockdown is true
    monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
    assert is_global_lockdown_enabled() is True
    assert is_logovo_access_allowed(regular_user) is False
    assert is_logovo_access_allowed(div_admin) is False
    assert is_logovo_access_allowed(global_admin) is True


import asyncio

def test_02_telegram_command_blocked_for_regular_user(monkeypatch):
    """Scenario 2: Regular user Telegram command is blocked with lockdown text."""
    monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
    user_id = 999201

    update = MagicMock()
    update.effective_user.id = user_id
    update.callback_query = None
    update.effective_message.text = "/start"
    update.effective_message.reply_text = AsyncMock()
    update.effective_chat.type = "private"

    context = MagicMock()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(global_lockdown_guard(update, context))

    update.effective_message.reply_text.assert_awaited_once()
    call_args = update.effective_message.reply_text.await_args[0][0]
    assert "Logovo.bet временно закрыт" in call_args
    assert "Доступ разрешён только администраторам" in call_args


def test_03_telegram_callback_blocked_for_regular_user(monkeypatch):
    """Scenario 3: Regular user callback query is answered with alert and stops propagation."""
    monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
    user_id = 999301

    update = MagicMock()
    update.effective_user.id = user_id
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.effective_message = None

    context = MagicMock()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(global_lockdown_guard(update, context))

    update.callback_query.answer.assert_awaited_once()
    call_args, call_kwargs = update.callback_query.answer.await_args
    assert "Logovo.bet временно закрыт" in call_args[0]
    assert call_kwargs.get("show_alert") is True


def test_09_telegram_allowed_for_global_admin(monkeypatch):
    """Scenario 9: Global Admin passes Telegram lockdown guard without interruption."""
    monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
    admin_id = 999901
    monkeypatch.setattr(config, "ADMIN_IDS", [admin_id])

    # Command from admin
    update_cmd = MagicMock()
    update_cmd.effective_user.id = admin_id
    update_cmd.callback_query = None
    update_cmd.effective_message.text = "/start"
    update_cmd.effective_message.reply_text = AsyncMock()
    update_cmd.effective_chat.type = "private"

    context = MagicMock()

    # Must NOT raise ApplicationHandlerStop
    asyncio.run(global_lockdown_guard(update_cmd, context))
    update_cmd.effective_message.reply_text.assert_not_called()

    # Callback from admin
    update_cb = MagicMock()
    update_cb.effective_user.id = admin_id
    update_cb.callback_query = MagicMock()
    update_cb.callback_query.answer = AsyncMock()
    update_cb.effective_message = None

    asyncio.run(global_lockdown_guard(update_cb, context))
    update_cb.callback_query.answer.assert_not_called()


# -----------------------------------------------------------------------------
# Database Financial Layer Protection Tests
# -----------------------------------------------------------------------------

def test_06_and_11_place_bet_blocked_before_db(monkeypatch):
    """Scenario 6 & 11: Regular user cannot place bet during lockdown; DB is untouched."""
    monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
    user_id = 999601

    database.init_db()
    with database.transaction() as conn:
        conn.execute("DELETE FROM user_bets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (user_id,))
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'bet_user', 'player')", (user_id,))
        conn.execute("INSERT OR REPLACE INTO user_wallets (user_id, balance) VALUES (?, 1000)", (user_id,))

    # Attempt to place bet
    ok, res = database.place_user_bet(
        user_id=user_id,
        amount=200,
        selections=[{"match_id": 1, "outcome": "p1", "odd": 2.0}]
    )

    assert ok is False
    assert isinstance(res, dict)
    assert res.get("error") == "LOGOVO_LOCKDOWN"
    assert "Logovo.bet временно закрыт" in res.get("message", "")

    # Invariant: Wallet balance is completely unchanged
    wallet = database.get_or_create_wallet(user_id)
    assert wallet["balance"] == 1000

    # Invariant: No bets inserted
    with database.transaction() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM user_bets WHERE user_id = ?", (user_id,))
        count = cur.fetchone()[0]
        assert count == 0



def test_07_cashout_blocked_before_db(monkeypatch):
    """Scenario 7: Cashout calculation and execution blocked for regular user during lockdown."""
    monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
    user_id = 999701
    from services.cashout_engine import quote_cashout, execute_cashout

    # Quote
    quote = quote_cashout(user_id=user_id, bet_id=999)
    assert quote["available"] is False
    assert quote["reason"] == "LOGOVO_LOCKDOWN"

    # Execution
    ok, res = execute_cashout(user_id=user_id, bet_id=999)
    assert ok is False
    assert isinstance(res, dict)
    assert res.get("error") == "LOGOVO_LOCKDOWN"


def test_08_reward_blocked_before_db(monkeypatch):
    """Scenario 8: Achievement claiming blocked before DB mutation during lockdown."""
    monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
    user_id = 999801

    database.init_db()
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'bonus_user', 'player')", (user_id,))
        conn.execute("INSERT OR REPLACE INTO user_wallets (user_id, balance) VALUES (?, 500)", (user_id,))

    # Claim achievement reward blocked
    ok_a, msg_a, data_a = database.claim_achievement_reward(user_id, "ACH_FIRST_BET")
    assert ok_a is False
    assert "Logovo.bet временно закрыт" in msg_a


# -----------------------------------------------------------------------------
# REST API Middleware Integration Tests
# -----------------------------------------------------------------------------

class TestLockdownApi(AioHTTPTestCase):
    async def get_application(self):
        return create_app()

    def setUp(self):
        super().setUp()
        database.init_db()
        self.regular_user_id = 999401
        self.division_admin_id = 999402
        self.global_admin_id = 999403

        # Configure users in DB
        with database.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'reg_player', 'player')", (self.regular_user_id,))
            conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role, division_id) VALUES (?, 'div_admin', 'division_admin', 1)", (self.division_admin_id,))
            conn.execute("INSERT OR REPLACE INTO division_admins (user_id, division_id) VALUES (?, 1)", (self.division_admin_id,))
            conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'global_admin', 'admin')", (self.global_admin_id,))

        # Подменяем список глобальных админов и обязательно возвращаем обратно:
        # xdist с --dist loadfile отдаёт одному воркеру несколько файлов подряд,
        # в одном процессе, и незакрытая подмена роняет чужие тесты
        # (test_feature_access_fail_closed берёт ADMIN_IDS[0] на импорте).
        self._orig_admin_ids = list(config.ADMIN_IDS)
        self.addCleanup(setattr, config, "ADMIN_IDS", self._orig_admin_ids)
        config.ADMIN_IDS = [self.global_admin_id]

        self.reg_headers = {"X-Telegram-Init-Data": generate_mock_init_data(self.regular_user_id, "reg_player")}
        self.div_headers = {"X-Telegram-Init-Data": generate_mock_init_data(self.division_admin_id, "div_admin")}
        self.adm_headers = {"X-Telegram-Init-Data": generate_mock_init_data(self.global_admin_id, "global_admin")}

    def tearDown(self):
        super().tearDown()
        os.environ.pop("LOGOVO_LOCKDOWN", None)


    @unittest_run_loop
    async def test_lockdown_disabled_regular_user_allowed(self):
        """Scenario 1 (API): When lockdown=false, regular user has normal access."""
        os.environ["LOGOVO_LOCKDOWN"] = "false"
        resp = await self.client.get("/api/matches", headers=self.reg_headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data.get("status"), "ok")

    @unittest_run_loop
    async def test_lockdown_unauthenticated_request_returns_401_first(self):
        """Scenario 13: Order of validation -> unauthenticated request gets 401, not 403."""
        os.environ["LOGOVO_LOCKDOWN"] = "true"
        resp = await self.client.get("/api/matches")
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data.get("error"), "unauthorized")

    @unittest_run_loop
    async def test_lockdown_get_api_forbidden_for_regular_user(self):
        """Scenario 4: When lockdown=true, GET /api/* returns 403 LOGOVO_LOCKDOWN for regular user."""
        os.environ["LOGOVO_LOCKDOWN"] = "true"

        endpoints = [
            "/api/matches",
            "/api/bootstrap",
            "/api/wallet",
            "/api/leaderboard",
            "/api/progression"
        ]

        for ep in endpoints:
            resp = await self.client.get(ep, headers=self.reg_headers)
            self.assertEqual(resp.status, 403, f"Endpoint {ep} did not return 403")
            data = await resp.json()
            self.assertEqual(data.get("error"), "LOGOVO_LOCKDOWN")
            self.assertIn("Logovo.bet временно закрыт для пользователей", data.get("message", ""))

    @unittest_run_loop
    async def test_lockdown_post_api_forbidden_for_regular_user(self):
        """Scenario 5: When lockdown=true, POST /api/* returns 403 LOGOVO_LOCKDOWN for regular user."""
        os.environ["LOGOVO_LOCKDOWN"] = "true"

        # POST /api/predictions
        resp_pred = await self.client.post("/api/predictions", headers=self.reg_headers, json={
            "amount": 100,
            "selections": [{"match_id": 1, "outcome": "p1"}]
        })
        self.assertEqual(resp_pred.status, 403)
        data_pred = await resp_pred.json()
        self.assertEqual(data_pred.get("error"), "LOGOVO_LOCKDOWN")

        # POST /api/achievements/claim
        resp_ach = await self.client.post("/api/achievements/claim", headers=self.reg_headers)
        self.assertEqual(resp_ach.status, 403)
        data_ach = await resp_ach.json()
        self.assertEqual(data_ach.get("error"), "LOGOVO_LOCKDOWN")

        # POST /api/predictions/1/cashout
        resp_cashout = await self.client.post("/api/predictions/1/cashout", headers=self.reg_headers)
        self.assertEqual(resp_cashout.status, 403)
        data_cashout = await resp_cashout.json()
        self.assertEqual(data_cashout.get("error"), "LOGOVO_LOCKDOWN")

    @unittest_run_loop
    async def test_lockdown_division_admin_strictly_blocked(self):
        """Scenario 12: Division Admin is not a Global Admin and must be blocked with 403."""
        os.environ["LOGOVO_LOCKDOWN"] = "true"

        resp = await self.client.get("/api/matches", headers=self.div_headers)
        self.assertEqual(resp.status, 403)
        data = await resp.json()
        self.assertEqual(data.get("error"), "LOGOVO_LOCKDOWN")

    @unittest_run_loop
    async def test_lockdown_global_admin_api_allowed(self):
        """Scenario 10: Global Admin has full unrestricted access to API during lockdown."""
        os.environ["LOGOVO_LOCKDOWN"] = "true"

        # GET /api/matches
        resp_matches = await self.client.get("/api/matches", headers=self.adm_headers)
        self.assertEqual(resp_matches.status, 200)

        # GET /api/bootstrap
        resp_boot = await self.client.get("/api/bootstrap", headers=self.adm_headers)
        self.assertEqual(resp_boot.status, 200)
        data_boot = await resp_boot.json()
        self.assertEqual(data_boot.get("status"), "ok")

    @unittest_run_loop
    async def test_static_files_accessible_during_lockdown(self):
        """Static assets (HTML, CSS, JS) remain accessible so Mini App can show lockdown screen."""
        os.environ["LOGOVO_LOCKDOWN"] = "true"
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
