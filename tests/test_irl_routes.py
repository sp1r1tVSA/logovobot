"""
tests/test_irl_routes.py

Ставки на реальные матчи через HTTP (`api/routes_irl.py`): витрина дня, приём
ставки и коды отказов, «мои ставки», флаг IRL_ENABLED и доступ без initData.

База — модульная из conftest; каждый тест начинает с чистых irl-таблиц.
"""

import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import timedelta

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from time_utils import now_msk, today_msk_str

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

BETTOR = 973001
OTHER = 973002


def _init_data(user_id: int) -> str:
    user = {"id": user_id, "first_name": "Irl", "username": f"irl{user_id}"}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def _wipe() -> None:
    with database.transaction() as conn:
        conn.execute("DELETE FROM irl_bets")
        conn.execute("DELETE FROM irl_matches")
        conn.execute("DELETE FROM betting_bans")
        conn.execute("DELETE FROM system_config WHERE key = 'betting_pause'")
    for uid in (BETTOR, OTHER):
        database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = 5000 WHERE user_id IN (?, ?)", (BETTOR, OTHER))


def _match(status: str = "open", hours: float = 3, fixture: str = "f1",
           home: str = "Arsenal", away: str = "Chelsea", odds=(2.0, 3.5, 4.0)) -> int:
    kickoff = now_msk() + timedelta(hours=hours)
    mid, _ = database.create_irl_draft(fixture, 39, "Premier League", home, away, kickoff,
                                       *odds, bet_day=today_msk_str())
    if status != "draft":
        with database.transaction() as conn:
            conn.execute("UPDATE irl_matches SET status = ? WHERE id = ?", (status, mid))
    return mid


class IrlRoutesCase(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig = (config.TOKEN, config.IRL_ENABLED, config.IRL_MAX_BET)
        config.TOKEN = TEST_BOT_TOKEN
        config.IRL_ENABLED = True
        config.IRL_MAX_BET = 1000
        await super().asyncSetUp()
        _wipe()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        config.TOKEN, config.IRL_ENABLED, config.IRL_MAX_BET = self._orig

    async def _call(self, method, path, user_id=BETTOR, body=None):
        headers = {"X-Telegram-Init-Data": _init_data(user_id)} if user_id else {}
        resp = await self.client.request(method, path, headers=headers, json=body)
        return resp.status, await resp.json()

    async def _bet(self, match_id, outcome="home", amount=100, user_id=BETTOR, **extra):
        return await self._call("POST", "/api/irl/bets", user_id,
                                {"match_id": match_id, "outcome": outcome, "amount": amount, **extra})

    # ── доступ и флаг ────────────────────────────────────────────────────────

    async def test_no_init_data_is_401_even_when_disabled(self):
        for flag in (True, False):
            config.IRL_ENABLED = flag
            for method, path in (("GET", "/api/irl/today"), ("GET", "/api/irl/bets/mine"),
                                 ("POST", "/api/irl/bets")):
                status, _ = await self._call(method, path, user_id=None, body={})
                self.assertEqual(status, 401, (flag, method, path))

    async def test_bootstrap_tells_the_mini_app_whether_to_show_the_chip(self):
        for flag in (True, False):
            config.IRL_ENABLED = flag
            status, body = await self._call("GET", "/api/bootstrap")
            self.assertEqual(status, 200)
            self.assertIs(body["user"]["irl_enabled"], flag)

    async def test_disabled_flag_gives_404(self):
        config.IRL_ENABLED = False
        mid = _match()
        for method, path in (("GET", "/api/irl/today"), ("GET", "/api/irl/bets/mine")):
            status, body = await self._call(method, path)
            self.assertEqual((status, body["error"]), (404, "irl_disabled"))
        status, body = await self._bet(mid)
        self.assertEqual((status, body["error"]), (404, "irl_disabled"))
        self.assertIsNone(database.get_user_irl_bet_for_match(BETTOR, mid))

    async def test_malformed_body_is_400(self):
        headers = {"X-Telegram-Init-Data": _init_data(BETTOR), "Content-Type": "application/json"}
        resp = await self.client.request("POST", "/api/irl/bets", headers=headers, data="{not json")
        self.assertEqual(resp.status, 400)
        status, _ = await self._call("POST", "/api/irl/bets", body=[1, 2])
        self.assertEqual(status, 400)

    # ── витрина дня ──────────────────────────────────────────────────────────

    async def test_today_lists_published_matches_only(self):
        open_id = _match("open", fixture="a")
        closed_id = _match("closed", hours=-1, fixture="b")
        _match("draft", fixture="c")
        status, body = await self._call("GET", "/api/irl/today")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["bet_day"], today_msk_str())
        self.assertEqual(body["max_bet"], 1000)
        self.assertIn("основного времени", body["note"])
        self.assertEqual(sorted(m["id"] for m in body["matches"]), sorted([open_id, closed_id]))
        by_id = {m["id"]: m for m in body["matches"]}
        self.assertTrue(by_id[open_id]["betting_open"])
        self.assertFalse(by_id[closed_id]["betting_open"])
        self.assertEqual(by_id[open_id]["odds"], {"home": 2.0, "draw": 3.5, "away": 4.0})
        self.assertIsNone(by_id[open_id]["my_bet"])

    async def test_today_shows_only_my_bet_and_hides_stakes_of_others(self):
        mid = _match()
        self.assertEqual((await self._bet(mid, "draw", 200))[0], 200)
        _, mine = await self._call("GET", "/api/irl/today")
        self.assertEqual(mine["matches"][0]["my_bet"]["outcome"], "draw")
        self.assertEqual(mine["matches"][0]["my_bet"]["amount"], 200)
        _, other = await self._call("GET", "/api/irl/today", OTHER)
        self.assertIsNone(other["matches"][0]["my_bet"])

    async def test_today_ignores_other_days(self):
        mid = _match()
        with database.transaction() as conn:
            conn.execute("UPDATE irl_matches SET bet_day = '2000-01-01' WHERE id = ?", (mid,))
        _, body = await self._call("GET", "/api/irl/today")
        self.assertEqual(body["matches"], [])

    async def test_empty_day_is_ok(self):
        status, body = await self._call("GET", "/api/irl/today")
        self.assertEqual((status, body["status"], body["matches"]), (200, "ok", []))

    async def test_settled_match_carries_result_and_score(self):
        mid = _match()
        await self._bet(mid, "home", 100)
        database.settle_irl_match(mid, "home", 2, 1)
        _, body = await self._call("GET", "/api/irl/today")
        m = body["matches"][0]
        self.assertEqual((m["status"], m["result"], m["home_goals"], m["away_goals"]), ("settled", "home", 2, 1))
        self.assertEqual(m["my_bet"]["status"], "won")

    # ── приём ставки ─────────────────────────────────────────────────────────

    async def test_bet_is_placed_and_wallet_charged(self):
        mid = _match()
        status, body = await self._bet(mid, "home", 100, odd=2.0)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["odd"], body["amount"], body["potential_win"], body["balance"]),
                         (2.0, 100, 200, 4900))
        self.assertEqual(database.get_or_create_wallet(BETTOR)["balance"], 4900)

    async def test_outcome_accepts_1x2_aliases(self):
        mid = _match()
        status, body = await self._bet(mid, "X", 50)
        self.assertEqual(status, 200, body)
        self.assertEqual(database.get_user_irl_bet_for_match(BETTOR, mid)["outcome"], "draw")

    async def test_second_bet_on_same_match_is_409(self):
        mid = _match()
        self.assertEqual((await self._bet(mid))[0], 200)
        status, body = await self._bet(mid, "away", 10)
        self.assertEqual((status, body["error"]), (409, "IRL_ALREADY_BET"))
        self.assertEqual(database.get_or_create_wallet(BETTOR)["balance"], 4900)

    async def test_other_player_may_bet_on_same_match(self):
        mid = _match()
        self.assertEqual((await self._bet(mid))[0], 200)
        self.assertEqual((await self._bet(mid, user_id=OTHER))[0], 200)

    async def test_stake_above_the_cap_is_rejected_without_charge(self):
        mid = _match()
        status, body = await self._bet(mid, "home", 1001)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["status"], "error")
        self.assertEqual(database.get_or_create_wallet(BETTOR)["balance"], 5000)

    async def test_bad_stake_and_outcome_are_400(self):
        mid = _match()
        for body in ({"match_id": mid, "outcome": "home", "amount": 0},
                     {"match_id": mid, "outcome": "home", "amount": "abc"},
                     {"match_id": mid, "outcome": "home", "amount": -5},
                     {"match_id": mid, "outcome": "nobody", "amount": 10},
                     {"match_id": mid, "amount": 10},
                     {"match_id": "x", "outcome": "home", "amount": 10},
                     {"outcome": "home", "amount": 10}):
            status, res = await self._call("POST", "/api/irl/bets", body=body)
            self.assertEqual(status, 400, (body, res))
        self.assertEqual(database.get_or_create_wallet(BETTOR)["balance"], 5000)

    async def test_unknown_match_is_400(self):
        status, body = await self._bet(999999)
        self.assertEqual((status, body["error"]), (400, "INVALID_SELECTION"))

    async def test_closed_draft_and_started_matches_reject_bets(self):
        closed = _match("closed", hours=-1, fixture="a")
        draft = _match("draft", fixture="b")
        started = _match("open", hours=-0.5, fixture="c")     # open, но время уже вышло
        for mid in (closed, draft, started):
            status, body = await self._bet(mid)
            self.assertEqual((status, body["error"]), (409, "IRL_BETTING_CLOSED"), mid)
        self.assertEqual(database.get_or_create_wallet(BETTOR)["balance"], 5000)

    async def test_stale_client_odd_is_409_with_new_price(self):
        mid = _match()
        status, body = await self._bet(mid, "home", 100, odd=1.5)
        self.assertEqual((status, body["error"]), (409, "ODDS_CHANGED"))
        self.assertEqual((body["old_odd"], body["new_odd"]), (1.5, 2.0))
        self.assertIsNone(database.get_user_irl_bet_for_match(BETTOR, mid))

    async def test_insufficient_balance_is_400(self):
        mid = _match()
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 50 WHERE user_id = ?", (BETTOR,))
        status, body = await self._bet(mid, "home", 100)
        self.assertEqual((status, body["error"]), (400, "INSUFFICIENT_BALANCE"))

    async def test_banned_player_is_403(self):
        mid = _match()
        database.set_betting_ban(BETTOR, 1, "test")
        status, body = await self._bet(mid)
        self.assertEqual((status, body["error"]), (403, "BETTING_BANNED"))
        self.assertEqual(database.get_or_create_wallet(BETTOR)["balance"], 5000)

    async def test_lockdown_blocks_regular_players(self):
        mid = _match()
        orig = config.is_global_lockdown_enabled
        config.is_global_lockdown_enabled = lambda: True
        try:
            status, body = await self._bet(mid)
        finally:
            config.is_global_lockdown_enabled = orig
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"], "LOGOVO_LOCKDOWN")
        self.assertIsNone(database.get_user_irl_bet_for_match(BETTOR, mid))

    # ── мои ставки ───────────────────────────────────────────────────────────

    async def test_mine_returns_own_bets_newest_first(self):
        a = _match(fixture="a", home="A", away="B")
        b = _match(fixture="b", home="C", away="D")
        await self._bet(a, "home", 100)
        await self._bet(b, "away", 50)
        await self._bet(a, "draw", 10, user_id=OTHER)
        status, body = await self._call("GET", "/api/irl/bets/mine")
        self.assertEqual(status, 200, body)
        self.assertEqual([x["irl_match_id"] for x in body["bets"]], [b, a])
        first = body["bets"][0]
        self.assertEqual((first["home"], first["away"], first["outcome"], first["status"]),
                         ("C", "D", "away", "pending"))
        self.assertNotIn("user_id", first)

    async def test_mine_is_empty_for_a_new_player(self):
        status, body = await self._call("GET", "/api/irl/bets/mine", OTHER)
        self.assertEqual((status, body["bets"]), (200, []))
