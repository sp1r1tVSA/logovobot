"""
Пособие при нулевом балансе: 200 🪙 раз в 7 дней, только без открытых купонов.

 A. `database.get_bailout_status` / `claim_bailout` — условия и причины отказа;
 B. кулдаун считается по журналу `coin_transactions` ('bailout');
 C. два одновременных запроса выдают пособие один раз;
 D. REST: `GET/POST /api/wallet/bailout` и поле `bailout` в bootstrap.
"""
import hashlib
import hmac
import itertools
import json
import os
import sys
import threading
import time
import urllib.parse
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database
from api.server import create_app

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_ids = itertools.count(975001)


def _new_user(balance: int = 0) -> int:
    """Игрок с кошельком и заданным балансом."""
    uid = next(_ids)
    database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = ? WHERE user_id = ?", (balance, uid))
    return uid


def _insert_pending(table: str, uid: int, status: str = "pending") -> None:
    """Купон без родительских строк: FK временно выключаем на этом соединении."""
    sql = {
        "user_bets": (
            "INSERT INTO user_bets (user_id, bet_type, amount, total_odd, potential_win, status, created_at)"
            " VALUES (?, 'single', 10, 2.0, 20, ?, datetime('now', '+3 hours'))"
        ),
        "outright_bets": (
            "INSERT INTO outright_bets (user_id, market_id, selection_id, amount, odd, potential_win, status, created_at)"
            " VALUES (?, 1, 1, 10, 2.0, 20, ?, datetime('now', '+3 hours'))"
        ),
        "irl_bets": (
            "INSERT INTO irl_bets (user_id, irl_match_id, outcome, amount, odd, potential_win, status, created_at)"
            " VALUES (?, 1, 'home', 10, 2.0, 20, ?, datetime('now', '+3 hours'))"
        ),
    }[table]
    conn = database.get_connection()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(sql, (uid, status))
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _age_bailout(uid: int, days: float) -> None:
    """Сдвинуть выданное пособие в прошлое, как будто оно взято `days` дней назад."""
    with database.transaction() as conn:
        conn.execute(
            "UPDATE coin_transactions SET created_at = datetime('now', '+3 hours', ?)"
            " WHERE user_id = ? AND transaction_type = 'bailout'",
            (f"-{days} days", uid),
        )


def _balance(uid: int) -> int:
    return database.get_or_create_wallet(uid)["balance"]


class TestBailoutRules:

    def test_zero_balance_without_bets_is_eligible(self):
        uid = _new_user(0)
        status = database.get_bailout_status(uid)
        assert status["eligible"] is True
        assert status["reason"] is None
        assert status["amount"] == config.BAILOUT_AMOUNT == 200

    def test_claim_adds_coins_and_writes_ledger_row(self):
        uid = _new_user(0)
        result = database.claim_bailout(uid)
        assert result["granted"] is True
        assert _balance(uid) == 200
        with database.transaction() as conn:
            rows = conn.execute(
                "SELECT amount, balance_after FROM coin_transactions"
                " WHERE user_id = ? AND transaction_type = 'bailout'", (uid,)
            ).fetchall()
        assert [(r["amount"], r["balance_after"]) for r in rows] == [(200, 200)]

    def test_positive_balance_is_refused(self):
        uid = _new_user(1)
        result = database.claim_bailout(uid)
        assert result["granted"] is False
        assert result["reason"] == "balance"
        assert _balance(uid) == 1

    def test_open_match_coupon_blocks(self):
        for table in ("user_bets", "irl_bets"):
            uid = _new_user(0)
            _insert_pending(table, uid)
            result = database.claim_bailout(uid)
            assert result["granted"] is False, table
            assert result["reason"] == "open_bets", table
            assert _balance(uid) == 0, table

    def test_pending_outright_bet_does_not_block(self):
        uid = _new_user(0)
        _insert_pending("outright_bets", uid)
        assert database.get_bailout_status(uid)["eligible"] is True
        assert database.claim_bailout(uid)["granted"] is True
        assert _balance(uid) == 200

    def test_settled_coupons_do_not_block(self):
        uid = _new_user(0)
        for status in ("lost", "won", "refunded"):
            _insert_pending("user_bets", uid, status=status)
        assert database.claim_bailout(uid)["granted"] is True

    def test_other_players_coupon_does_not_block(self):
        other = _new_user(500)
        _insert_pending("user_bets", other)
        uid = _new_user(0)
        assert database.claim_bailout(uid)["granted"] is True

    def test_second_claim_within_cooldown_is_refused(self):
        uid = _new_user(0)
        assert database.claim_bailout(uid)["granted"] is True
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 0 WHERE user_id = ?", (uid,))
        result = database.claim_bailout(uid)
        assert result["granted"] is False
        assert result["reason"] == "cooldown"
        assert result["next_available_at"]
        assert _balance(uid) == 0

    def test_claim_is_available_again_after_the_cooldown(self):
        uid = _new_user(0)
        assert database.claim_bailout(uid)["granted"] is True
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 0 WHERE user_id = ?", (uid,))
        _age_bailout(uid, config.BAILOUT_COOLDOWN_DAYS + 0.1)
        assert database.get_bailout_status(uid)["eligible"] is True
        assert database.claim_bailout(uid)["granted"] is True
        assert _balance(uid) == 200

    def test_cooldown_has_not_ended_just_before_seven_days(self):
        uid = _new_user(0)
        assert database.claim_bailout(uid)["granted"] is True
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 0 WHERE user_id = ?", (uid,))
        _age_bailout(uid, config.BAILOUT_COOLDOWN_DAYS - 0.1)
        assert database.get_bailout_status(uid)["reason"] == "cooldown"

    def test_concurrent_claims_pay_once(self):
        uid = _new_user(0)
        results = []
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            results.append(database.claim_bailout(uid)["granted"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1
        assert _balance(uid) == 200


def make_init_data(user_id: int, token: str = TEST_BOT_TOKEN) -> str:
    user = {"id": user_id, "first_name": "Bailout", "username": f"bailout_{user_id}"}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class TestBailoutApi(AioHTTPTestCase):

    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_token = config.TOKEN
        self._orig_rate_limit = config.API_RATE_LIMIT_ENABLED
        config.TOKEN = TEST_BOT_TOKEN
        config.API_RATE_LIMIT_ENABLED = False
        await super().asyncSetUp()

    async def asyncTearDown(self):
        config.TOKEN = self._orig_token
        config.API_RATE_LIMIT_ENABLED = self._orig_rate_limit
        await super().asyncTearDown()

    def _headers(self, uid: int) -> dict:
        return {"X-Telegram-Init-Data": make_init_data(uid)}

    async def test_anonymous_is_401(self):
        for method in ("GET", "POST"):
            resp = await self.client.request(method, "/api/wallet/bailout")
            self.assertEqual(401, resp.status, method)

    async def test_restricted_user_is_403_and_gets_nothing(self):
        uid = _new_user(0)
        with mock.patch("api.routes_wallet.check_user_access", return_value=False):
            resp = await self.client.request("POST", "/api/wallet/bailout", headers=self._headers(uid))
        self.assertEqual(403, resp.status)
        self.assertEqual(0, _balance(uid))

    async def test_get_reports_status(self):
        uid = _new_user(0)
        resp = await self.client.request("GET", "/api/wallet/bailout", headers=self._headers(uid))
        self.assertEqual(200, resp.status)
        body = await resp.json()
        self.assertTrue(body["bailout"]["eligible"])

    async def test_post_grants_then_refuses_with_409(self):
        uid = _new_user(0)
        resp = await self.client.request("POST", "/api/wallet/bailout", headers=self._headers(uid))
        self.assertEqual(200, resp.status)
        body = await resp.json()
        self.assertEqual(200, body["amount"])
        self.assertEqual(200, body["bailout"]["balance"])
        self.assertNotIn("granted", body["bailout"])

        resp = await self.client.request("POST", "/api/wallet/bailout", headers=self._headers(uid))
        self.assertEqual(409, resp.status)
        body = await resp.json()
        self.assertEqual("bailout_unavailable", body["error"])
        self.assertEqual("balance", body["bailout"]["reason"])
        self.assertEqual(200, _balance(uid))

    async def test_post_with_open_coupon_is_409(self):
        uid = _new_user(0)
        _insert_pending("user_bets", uid)
        resp = await self.client.request("POST", "/api/wallet/bailout", headers=self._headers(uid))
        self.assertEqual(409, resp.status)
        body = await resp.json()
        self.assertEqual("open_bets", body["bailout"]["reason"])
        self.assertEqual(0, _balance(uid))

    async def test_bootstrap_carries_bailout_only_at_zero(self):
        broke = _new_user(0)
        rich = _new_user(500)
        resp = await self.client.request("GET", "/api/bootstrap", headers=self._headers(broke))
        user = (await resp.json())["user"]
        self.assertTrue(user["bailout"]["eligible"])
        resp = await self.client.request("GET", "/api/bootstrap", headers=self._headers(rich))
        user = (await resp.json())["user"]
        self.assertIsNone(user["bailout"])

    def test_route_is_guarded_against_parallel_duplicates(self):
        from api.rate_limiter import is_sensitive
        self.assertTrue(is_sensitive("/api/wallet/bailout", "POST"))
