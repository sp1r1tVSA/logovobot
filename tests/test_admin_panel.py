"""
tests/test_admin_panel.py

Панель управления Logovo.bet: запрет ставок игроку, экстренная остановка
(глобальная и по дивизионам), ручная корректировка кошелька и разграничение
прав — админ дивизиона видит и трогает только свои дивизионы, игроки и лимиты
доступны лишь главным админам.

База — модульная из conftest: обработчики API ходят в неё из пула потоков,
поэтому подмена DB_PATH на время теста здесь не годится.
"""

import hashlib
import hmac
import json
import time
import unittest
import urllib.parse
from unittest import mock

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from services.ai import market_analysis

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

PLAYER = 971001
OTHER_PLAYER = 971002
GLOBAL_ADMIN = 971900
DIV2_ADMIN = 971901
ROLE_ADMIN = 971902  # users.role = 'admin', но не в ADMIN_IDS

# (match_id, market_id, selection_id, division_id) — None: матч без дивизиона.
MATCHES = {
    "div1": (971101, 971201, 971301, 1),
    "div2": (971102, 971202, 971302, 2),
    "nodiv": (971103, 971203, 971303, None),
}


def _seed() -> None:
    database.ensure_canonical_divisions()
    with database.transaction() as conn:
        cursor = conn.cursor()
        for uid, name in ((PLAYER, "panel_player"), (OTHER_PLAYER, "panel_other"),
                          (GLOBAL_ADMIN, "panel_global"), (DIV2_ADMIN, "panel_div2"),
                          (ROLE_ADMIN, "panel_role")):
            cursor.execute(
                "INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                (uid, name),
            )
        cursor.execute("SELECT id FROM seasons WHERE name = 'Panel Season'")
        row = cursor.fetchone()
        if row:
            season_id = row["id"]
        else:
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Panel Season', 'active')")
            season_id = cursor.lastrowid
            for div in (1, 2):
                cursor.execute(
                    "INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) "
                    "VALUES (?, 1, ?, 0, 1)",
                    (div, season_id),
                )
        for key, (match_id, market_id, selection_id, div) in MATCHES.items():
            cursor.execute("""
                INSERT OR IGNORE INTO matches (id, division_id, season_id, round_number, player1_team, player2_team, status)
                VALUES (?, ?, ?, 1, ?, ?, 'open')
            """, (match_id, div, season_id, f"Home {key}", f"Away {key}"))
            cursor.execute("""
                INSERT OR IGNORE INTO markets (id, match_id, market_key, market_name, status)
                VALUES (?, ?, 'match_result', 'Match Winner', 'open')
            """, (market_id, match_id))
            cursor.execute("""
                INSERT OR IGNORE INTO market_selections (id, market_id, selection_key, selection_name, odds_value, status, odds_version)
                VALUES (?, ?, 'home', ?, 1.50, 'active', 1)
            """, (selection_id, market_id, f"Home {key}"))

    for uid in (PLAYER, OTHER_PLAYER):
        database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM betting_bans")
        cursor.execute("DELETE FROM system_config WHERE key = 'betting_pause'")
        # Лимиты и настройки из панели — у каждого теста с чистого листа.
        cursor.execute("DELETE FROM risk_limits_config")
        # Купоны прошлых тестов упёрлись бы в лимит открытых купонов (12).
        cursor.execute(
            "UPDATE user_bets SET status = 'cancelled' WHERE status = 'pending' AND user_id IN (?, ?)",
            (PLAYER, OTHER_PLAYER),
        )
        cursor.execute("UPDATE markets SET status = 'open' WHERE id IN (?, ?, ?)",
                       tuple(m[1] for m in MATCHES.values()))
        cursor.execute(
            "UPDATE user_wallets SET balance = 100000, total_wagered = 0 WHERE user_id IN (?, ?)",
            (PLAYER, OTHER_PLAYER),
        )
    database.add_division_admin(2, DIV2_ADMIN)


_bet_counter = 0


def _place(user_id: int, *keys: str, amount: int = 100):
    global _bet_counter
    _bet_counter += 1
    selections = [
        {"match_id": MATCHES[k][0], "market_id": MATCHES[k][1], "selection_id": MATCHES[k][2],
         "outcome": "home", "odds": 1.50}
        for k in keys
    ]
    return database.place_user_bet(
        user_id=user_id, amount=amount, selections=selections,
        idempotency_key=f"panel-{user_id}-{_bet_counter}-{time.time_ns()}",
    )


def _error_code(result) -> str | None:
    ok, payload = result
    if ok:
        return None
    return payload.get("error") if isinstance(payload, dict) else str(payload)


class PanelCase(unittest.TestCase):
    def setUp(self):
        _seed()


class TestBettingBan(PanelCase):
    def test_ban_blocks_only_that_player(self):
        database.set_betting_ban(PLAYER, GLOBAL_ADMIN, "подозрение на договорняк")
        self.assertEqual(_error_code(_place(PLAYER, "div1")), "BETTING_BANNED")
        ok, _ = _place(OTHER_PLAYER, "div1")
        self.assertTrue(ok)

    def test_ban_message_carries_the_reason(self):
        database.set_betting_ban(PLAYER, GLOBAL_ADMIN, "мультиаккаунт")
        ok, payload = _place(PLAYER, "div1")
        self.assertFalse(ok)
        self.assertIn("мультиаккаунт", payload["message"])

    def test_lifting_the_ban_restores_betting(self):
        database.set_betting_ban(PLAYER, GLOBAL_ADMIN, "x")
        self.assertTrue(database.lift_betting_ban(PLAYER, GLOBAL_ADMIN))
        self.assertFalse(database.lift_betting_ban(PLAYER, GLOBAL_ADMIN))
        ok, _ = _place(PLAYER, "div1")
        self.assertTrue(ok)

    def test_ban_of_unknown_player_is_rejected(self):
        with self.assertRaises(ValueError):
            database.set_betting_ban(971999, GLOBAL_ADMIN, "x")

    def test_ban_is_audited(self):
        database.set_betting_ban(PLAYER, GLOBAL_ADMIN, "x")
        with database.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM bet_audit_log WHERE action = 'player_betting_banned' AND entity_id = ?",
                (PLAYER,),
            ).fetchone()
        self.assertGreaterEqual(row["c"], 1)


class TestBettingPause(PanelCase):
    def test_global_pause_blocks_everything(self):
        database.set_betting_pause(GLOBAL_ADMIN, True, reason="сбой линии")
        for key in MATCHES:
            with self.subTest(match=key):
                self.assertEqual(_error_code(_place(PLAYER, key)), "BETTING_PAUSED")
        database.set_betting_pause(GLOBAL_ADMIN, False)
        ok, _ = _place(PLAYER, "div1")
        self.assertTrue(ok)

    def test_division_pause_blocks_only_that_division(self):
        database.set_betting_pause(DIV2_ADMIN, True, division_id=2, reason="перенос тура")
        ok, payload = _place(PLAYER, "div2")
        self.assertFalse(ok)
        self.assertEqual(payload["error"], "BETTING_PAUSED")
        self.assertEqual(payload["division_id"], 2)
        ok, _ = _place(PLAYER, "div1")
        self.assertTrue(ok)

    def test_express_touching_a_paused_division_is_blocked(self):
        database.set_betting_pause(DIV2_ADMIN, True, division_id=2, reason="x")
        self.assertEqual(_error_code(_place(PLAYER, "div1", "div2")), "BETTING_PAUSED")

    def test_match_without_division_counts_as_division_one(self):
        database.set_betting_pause(GLOBAL_ADMIN, True, division_id=1, reason="x")
        self.assertEqual(_error_code(_place(PLAYER, "nodiv")), "BETTING_PAUSED")
        ok, _ = _place(PLAYER, "div2")
        self.assertTrue(ok)

    def test_pause_state_round_trips(self):
        database.set_betting_pause(GLOBAL_ADMIN, True, division_id=2, reason="причина")
        state = database.get_betting_pause()
        self.assertIsNone(state["global"])
        self.assertEqual(state["divisions"][2]["reason"], "причина")
        database.set_betting_pause(GLOBAL_ADMIN, False, division_id=2)
        self.assertEqual(database.get_betting_pause()["divisions"], {})

    def test_corrupt_pause_state_fails_closed(self):
        with database.transaction() as conn:
            conn.execute("REPLACE INTO system_config (key, value) VALUES ('betting_pause', '{not json')")
        self.assertEqual(_error_code(_place(PLAYER, "div1")), "BETTING_UNAVAILABLE")
        # Любая запись поверх испорченного состояния чинит его.
        database.set_betting_pause(GLOBAL_ADMIN, False)
        ok, _ = _place(PLAYER, "div1")
        self.assertTrue(ok)


class TestWalletAdjustment(PanelCase):
    def _wallet(self, uid=PLAYER):
        with database.transaction() as conn:
            return dict(conn.execute("SELECT * FROM user_wallets WHERE user_id = ?", (uid,)).fetchone())

    def test_credit_and_debit_move_the_balance_not_the_turnover(self):
        result = database.admin_adjust_wallet(PLAYER, 500, GLOBAL_ADMIN, "компенсация")
        self.assertEqual(result["new_balance"], 100500)
        self.assertEqual(result["transaction_type"], "admin_credit")
        result = database.admin_adjust_wallet(PLAYER, -1500, GLOBAL_ADMIN, "ошибка начисления")
        self.assertEqual(result["new_balance"], 99000)
        self.assertEqual(result["transaction_type"], "admin_debit")
        wallet = self._wallet()
        self.assertEqual(wallet["balance"], 99000)
        self.assertEqual(wallet["total_wagered"], 0)

    def test_transactions_are_recorded(self):
        database.admin_adjust_wallet(PLAYER, 250, GLOBAL_ADMIN, "бонус")
        txs = database.get_coin_transactions(PLAYER, limit=1)
        self.assertEqual(txs[0]["transaction_type"], "admin_credit")
        self.assertEqual(txs[0]["amount"], 250)
        self.assertEqual(txs[0]["balance_after"], 100250)

    def test_balance_never_goes_negative(self):
        with self.assertRaises(ValueError):
            database.admin_adjust_wallet(PLAYER, -100001, GLOBAL_ADMIN, "x")
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 10 WHERE user_id = ?", (PLAYER,))
        with self.assertRaises(ValueError):
            database.admin_adjust_wallet(PLAYER, -11, GLOBAL_ADMIN, "x")
        self.assertEqual(self._wallet()["balance"], 10)

    def test_invalid_input_is_rejected(self):
        for amount, reason in ((0, "x"), (True, "x"), (1.5, "x"), (100_001, "x"), (100, ""), (100, "   ")):
            with self.subTest(amount=amount, reason=reason):
                with self.assertRaises(ValueError):
                    database.admin_adjust_wallet(PLAYER, amount, GLOBAL_ADMIN, reason)
        self.assertEqual(self._wallet()["balance"], 100000)

    def test_unknown_player_is_rejected(self):
        with self.assertRaises(ValueError):
            database.admin_adjust_wallet(971999, 100, GLOBAL_ADMIN, "x")


class TestPlayersAndFeeds(PanelCase):
    def test_search_filters_banned_players(self):
        database.set_betting_ban(OTHER_PLAYER, GLOBAL_ADMIN, "x")
        banned = {p["user_id"] for p in database.search_betting_players(banned_only=True, limit=500)}
        self.assertIn(OTHER_PLAYER, banned)
        self.assertNotIn(PLAYER, banned)

    def test_search_by_name_and_id(self):
        found = database.search_betting_players("@PANEL_PLAYER", limit=500)
        self.assertEqual([p["user_id"] for p in found], [PLAYER])
        found = database.search_betting_players(str(OTHER_PLAYER), limit=500)
        self.assertEqual([p["user_id"] for p in found], [OTHER_PLAYER])

    def test_player_card(self):
        database.set_betting_ban(PLAYER, GLOBAL_ADMIN, "x")
        card = database.get_betting_player(PLAYER)
        self.assertEqual(card["username"], "panel_player")
        self.assertEqual(card["wallet"]["balance"], 100000)
        self.assertEqual(card["ban"]["reason"], "x")
        self.assertIsNone(database.get_betting_player(971999))

    def test_dashboard_counts_pending_stake(self):
        before = database.get_betting_dashboard()["totals"]
        ok, _ = _place(PLAYER, "div1", amount=300)
        self.assertTrue(ok)
        after = database.get_betting_dashboard()["totals"]
        self.assertEqual(after["pending_count"] - before["pending_count"], 1)
        self.assertEqual(after["pending_stake"] - before["pending_stake"], 300)

    def test_dashboard_scoped_to_division(self):
        before = database.get_betting_dashboard([2])["totals"]["pending_count"]
        ok, _ = _place(PLAYER, "div1")
        self.assertTrue(ok)
        self.assertEqual(database.get_betting_dashboard([2])["totals"]["pending_count"], before)

    def test_bet_feed_scoped_to_divisions(self):
        ok, bet_div1 = _place(PLAYER, "div1")
        self.assertTrue(ok)
        ok, bet_div2 = _place(PLAYER, "div2")
        self.assertTrue(ok)
        ok, bet_nodiv = _place(PLAYER, "nodiv")
        self.assertTrue(ok)
        ids = {b["id"] for b in database.get_all_bets(division_ids=[2], limit=500)[0]}
        self.assertIn(bet_div2, ids)
        self.assertNotIn(bet_div1, ids)
        ids = {b["id"] for b in database.get_all_bets(division_ids=[1], limit=500)[0]}
        self.assertIn(bet_div1, ids)
        self.assertIn(bet_nodiv, ids)
        bets, total = database.get_all_bets(division_ids=[], limit=500)
        self.assertEqual((bets, total), ([], 0))


def make_init_data(user_id: int) -> str:
    user = {"id": user_id, "first_name": "Panel", "username": f"u{user_id}"}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class TestPanelRoutes(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_token = config.TOKEN
        config.TOKEN = TEST_BOT_TOKEN
        config.ADMIN_IDS.append(GLOBAL_ADMIN)
        await super().asyncSetUp()
        _seed()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        config.TOKEN = self._orig_token

    async def _call(self, method: str, path: str, user_id: int, body: dict | None = None):
        headers = {"X-Telegram-Init-Data": make_init_data(user_id)}
        resp = await self.client.request(method, f"/api/admin/panel{path}", headers=headers,
                                         json=body if body is not None else None)
        return resp.status, await resp.json()

    async def test_plain_player_is_forbidden(self):
        status, _ = await self._call("GET", "/me", PLAYER)
        self.assertEqual(status, 403)

    async def test_missing_auth_is_unauthorized(self):
        resp = await self.client.get("/api/admin/panel/me")
        self.assertEqual(resp.status, 401)

    async def test_me_reports_scope(self):
        status, body = await self._call("GET", "/me", GLOBAL_ADMIN)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["is_global"])

    async def test_panel_is_limited_to_admin_ids(self):
        with database.transaction() as conn:
            conn.execute("UPDATE users SET role = 'admin' WHERE telegram_id = ?", (ROLE_ADMIN,))
        self.assertNotIn(ROLE_ADMIN, config.ADMIN_IDS)
        for user_id in (DIV2_ADMIN, ROLE_ADMIN):
            for method, path in (("GET", "/me"), ("GET", "/dashboard"), ("GET", "/markets"),
                                 ("GET", "/bets"), ("GET", "/limits")):
                with self.subTest(user=user_id, path=path):
                    status, body = await self._call(method, path, user_id)
                    self.assertEqual((status, body["error"]), (403, "forbidden"))

    async def test_bootstrap_shows_the_panel_button_to_admin_ids_only(self):
        for user_id, expected in ((GLOBAL_ADMIN, True), (DIV2_ADMIN, False), (PLAYER, False)):
            with self.subTest(user=user_id):
                resp = await self.client.get("/api/bootstrap",
                                             headers={"X-Telegram-Init-Data": make_init_data(user_id)})
                self.assertEqual(resp.status, 200, await resp.text())
                self.assertIs((await resp.json())["user"]["is_panel_admin"], expected)

    async def test_division_admin_cannot_touch_other_divisions(self):
        _, _, div1_selection, _ = MATCHES["div1"]
        div1_market = MATCHES["div1"][1]
        nodiv_market = MATCHES["nodiv"][1]
        ok, bet_id = _place(PLAYER, "div1")
        self.assertTrue(ok, bet_id)
        cases = [
            ("POST", f"/markets/{div1_market}/action", {"action": "suspend"}),
            ("POST", f"/markets/{nodiv_market}/action", {"action": "suspend"}),
            ("POST", f"/selections/{div1_selection}/odds", {"odds": 2.0}),
            ("GET", f"/bets/{bet_id}", None),
            ("POST", f"/bets/{bet_id}/void", {"confirm": True}),
            ("GET", "/bets?division_id=1", None),
            ("POST", "/pause", {"paused": True, "division_id": 1, "reason": "x"}),
            ("POST", "/pause", {"paused": True, "reason": "x"}),
            ("GET", "/players", None),
            ("GET", f"/players/{PLAYER}", None),
            ("POST", f"/players/{PLAYER}/adjust", {"amount": 100, "reason": "x"}),
            ("POST", f"/players/{PLAYER}/ban", {"reason": "x"}),
            ("POST", f"/players/{PLAYER}/unban", {}),
            ("POST", "/limits", {"scope_type": "global", "limit_key": "max_bet", "value": 500}),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                status, payload = await self._call(method, path, DIV2_ADMIN, body)
                self.assertEqual(status, 403, payload)
        self.assertEqual(database.get_betting_pause(), {"global": None, "divisions": {}})
        self.assertIsNone(database.get_betting_ban(PLAYER))

    async def test_division_admin_cannot_manage_even_own_division(self):
        market = MATCHES["div2"][1]
        status, body = await self._call("POST", f"/markets/{market}/action", DIV2_ADMIN, {"action": "suspend"})
        self.assertEqual(status, 403, body)
        status, body = await self._call("POST", "/pause", DIV2_ADMIN,
                                        {"paused": True, "division_id": 2, "reason": "перенос"})
        self.assertEqual(status, 403, body)
        self.assertEqual(database.get_betting_pause(), {"global": None, "divisions": {}})

    async def test_pause_requires_a_reason(self):
        status, body = await self._call("POST", "/pause", GLOBAL_ADMIN, {"paused": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "reason_required")

    async def test_void_requires_confirmation_and_reason(self):
        market = MATCHES["div1"][1]
        status, body = await self._call("POST", f"/markets/{market}/action", GLOBAL_ADMIN,
                                        {"action": "void", "reason": "x"})
        self.assertEqual((status, body["error"]), (400, "confirmation_required"))
        status, body = await self._call("POST", f"/markets/{market}/action", GLOBAL_ADMIN,
                                        {"action": "void", "confirm": True})
        self.assertEqual((status, body["error"]), (400, "reason_required"))

    async def test_odds_out_of_range_are_rejected(self):
        selection = MATCHES["div1"][2]
        for odds in (1.0, 1001, "abc", True):
            with self.subTest(odds=odds):
                status, _ = await self._call("POST", f"/selections/{selection}/odds", GLOBAL_ADMIN, {"odds": odds})
                self.assertEqual(status, 400)

    async def test_global_admin_bans_and_adjusts(self):
        status, body = await self._call("POST", f"/players/{PLAYER}/ban", GLOBAL_ADMIN, {"reason": "проверка"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["ban"]["reason"], "проверка")
        self.assertEqual(_error_code(_place(PLAYER, "div1")), "BETTING_BANNED")

        status, body = await self._call("POST", f"/players/{PLAYER}/unban", GLOBAL_ADMIN, {})
        self.assertEqual((status, body["lifted"]), (200, True))

        status, body = await self._call("POST", f"/players/{PLAYER}/adjust", GLOBAL_ADMIN,
                                        {"amount": -400, "reason": "штраф"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["new_balance"], 99600)

        status, body = await self._call("POST", f"/players/{PLAYER}/adjust", GLOBAL_ADMIN,
                                        {"amount": 100, "reason": ""})
        self.assertEqual(status, 400)

    async def test_global_admin_voids_a_bet_with_refund(self):
        ok, bet_id = _place(PLAYER, "div2", amount=700)
        self.assertTrue(ok, bet_id)
        status, body = await self._call("POST", f"/bets/{bet_id}/void", GLOBAL_ADMIN, {"reason": "x"})
        self.assertEqual((status, body["error"]), (400, "confirmation_required"))
        status, body = await self._call("POST", f"/bets/{bet_id}/void", GLOBAL_ADMIN,
                                        {"confirm": True, "reason": "ошибка линии"})
        self.assertEqual(status, 200, body)
        with database.transaction() as conn:
            balance = conn.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (PLAYER,)).fetchone()[0]
        self.assertEqual(balance, 100000)

    async def test_bet_endpoint_maps_ban_to_403(self):
        database.set_betting_ban(PLAYER, GLOBAL_ADMIN, "x")
        match_id, market_id, selection_id, _ = MATCHES["div1"]
        resp = await self.client.post(
            "/api/predictions",
            headers={"X-Telegram-Init-Data": make_init_data(PLAYER)},
            json={"amount": 100, "selections": [{"match_id": match_id, "market_id": market_id,
                                                 "selection_id": selection_id, "outcome": "home",
                                                 "odds": 1.5}]},
        )
        self.assertEqual(resp.status, 403, await resp.text())

    # ─── Старые эндпоинты: переход рынка и риск-алерты ─────────────────────

    async def _legacy(self, path: str, user_id: int, body: dict | None = None):
        headers = {"X-Telegram-Init-Data": make_init_data(user_id)}
        resp = await self.client.post(path, headers=headers, json=body if body is not None else {})
        return resp.status, await resp.json()

    def _balance(self, uid=PLAYER) -> int:
        with database.transaction() as conn:
            return conn.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,)).fetchone()[0]

    def _market_status(self, market_id) -> str:
        with database.transaction() as conn:
            return conn.execute("SELECT status FROM markets WHERE id = ?", (market_id,)).fetchone()[0]

    async def test_transition_to_voided_refunds_the_bets(self):
        market = MATCHES["div2"][1]
        ok, bet_id = _place(PLAYER, "div2", amount=500)
        self.assertTrue(ok, bet_id)
        self.assertEqual(self._balance(), 99500)

        status, body = await self._legacy(f"/api/admin/markets/{market}/transition", DIV2_ADMIN,
                                          {"new_status": "voided"})
        self.assertEqual(status, 400, body)
        self.assertEqual(self._market_status(market), "open")

        status, body = await self._legacy(f"/api/admin/markets/{market}/transition", DIV2_ADMIN,
                                          {"new_status": "voided", "reason": "матч отменён"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["market"]["new_status"], "voided")
        # На рынке могут висеть купоны соседних тестов — сверяем свой.
        refunded = {b["bet_id"]: b["stake"] for b in body["void"]["refunded_bets"]}
        self.assertEqual(refunded.get(bet_id), 500)
        self.assertEqual(self._balance(), 99500 + sum(
            b["stake"] for b in body["void"]["refunded_bets"] if b["user_id"] == PLAYER))
        with database.transaction() as conn:
            bet_status = conn.execute("SELECT status FROM user_bets WHERE id = ?", (bet_id,)).fetchone()[0]
        self.assertEqual(bet_status, "refunded")

        balance_after_void = self._balance()
        status, body = await self._legacy(f"/api/admin/markets/{market}/transition", DIV2_ADMIN,
                                          {"new_status": "voided", "reason": "ещё раз"})
        self.assertEqual((status, body["error"]), (409, "INVALID_TRANSITION"))
        self.assertEqual(self._balance(), balance_after_void)

    async def test_transition_cannot_settle_by_hand(self):
        market = MATCHES["div1"][1]
        for status_step in ("closed", "settled"):
            status, body = await self._legacy(f"/api/admin/markets/{market}/transition", GLOBAL_ADMIN,
                                              {"new_status": status_step})
            if status_step == "closed":
                self.assertEqual(status, 200, body)
        self.assertEqual((status, body["error"]), (409, "INVALID_TRANSITION"))
        self.assertEqual(self._market_status(market), "closed")

    def _alert(self, division_id):
        import services.risk_alerts as risk_alerts
        alert_id = risk_alerts.create_risk_alert(
            f"panel_test_{time.time_ns()}", "high", f"panel alert {time.time_ns()}",
            division_id=division_id,
        )
        self.assertIsNotNone(alert_id)
        return alert_id

    def _alert_status(self, alert_id) -> str:
        with database.transaction() as conn:
            return conn.execute("SELECT status FROM risk_alerts WHERE id = ?", (alert_id,)).fetchone()[0]

    async def test_division_admin_handles_only_own_alerts(self):
        foreign = {"div1": self._alert(1), "nodiv": self._alert(None)}
        for kind in ("ack", "resolve"):
            for key, alert_id in foreign.items():
                with self.subTest(kind=kind, alert=key):
                    status, body = await self._legacy(f"/api/admin/risk/alerts/{alert_id}/{kind}", DIV2_ADMIN)
                    self.assertEqual(status, 403, body)
                    self.assertEqual(self._alert_status(alert_id), "active")

        own = self._alert(2)
        status, body = await self._legacy(f"/api/admin/risk/alerts/{own}/ack", DIV2_ADMIN)
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertEqual(self._alert_status(own), "acknowledged")
        status, body = await self._legacy(f"/api/admin/risk/alerts/{own}/resolve", DIV2_ADMIN)
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertEqual(self._alert_status(own), "resolved")

    async def test_global_admin_handles_any_alert(self):
        alert_id = self._alert(None)
        status, body = await self._legacy(f"/api/admin/risk/alerts/{alert_id}/resolve", GLOBAL_ADMIN)
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertEqual(self._alert_status(alert_id), "resolved")

    async def test_missing_alert_is_not_found(self):
        status, body = await self._legacy("/api/admin/risk/alerts/987654321/ack", GLOBAL_ADMIN)
        self.assertEqual((status, body["error"]), (404, "not_found"))

    # ─── Лимиты и настройки экономики ──────────────────────────────────────

    async def _set_limit(self, key: str, value, scope_type: str = "global", scope_id: int = 0,
                         user_id: int = GLOBAL_ADMIN):
        return await self._call("POST", "/limits", user_id,
                                {"scope_type": scope_type, "scope_id": scope_id, "limit_key": key, "value": value})

    async def test_limits_report_settings_defaults_and_bounds(self):
        status, body = await self._call("GET", "/limits", GLOBAL_ADMIN)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["system"]["max_express_events"], database.MAX_EXPRESS_EVENTS)
        self.assertEqual(body["system"]["initial_balance"], database.INITIAL_WALLET_BALANCE)
        self.assertEqual(body["defaults"]["max_open_bets"], 12)
        self.assertEqual(body["bounds"]["max_express_events"], [database.MIN_EXPRESS_EVENTS, 50])

        self.assertNotIn("daily_bonus", body["system"])
        status, body = await self._set_limit("daily_bonus", 400)
        self.assertEqual((status, body["error"]), (400, "invalid_limit_key"))

        status, body = await self._set_limit("initial_balance", 900)
        self.assertEqual(status, 200, body)
        status, body = await self._call("GET", "/limits", GLOBAL_ADMIN)
        self.assertEqual(body["system"]["initial_balance"], 900)
        self.assertEqual(body["global_overrides"]["initial_balance"], 900)
        self.assertEqual(body["defaults"]["initial_balance"], database.INITIAL_WALLET_BALANCE)

        status, body = await self._set_limit("initial_balance", None)
        self.assertEqual(status, 200, body)
        self.assertEqual(database.get_initial_wallet_balance(), database.INITIAL_WALLET_BALANCE)

    async def test_express_cap_follows_the_panel(self):
        status, body = await self._set_limit("max_express_events", 2)
        self.assertEqual(status, 200, body)
        ok, payload = _place(PLAYER, "div1", "div2", "nodiv")
        self.assertFalse(ok)
        self.assertEqual(payload["error"], "MAX_EXPRESS_EVENTS_EXCEEDED")
        self.assertEqual(payload["max_events"], 2)
        ok, payload = _place(PLAYER, "div1", "div2")
        self.assertTrue(ok, payload)

        status, body = await self._set_limit("max_express_events", 3)
        self.assertEqual(status, 200, body)
        self.assertNotEqual(_error_code(_place(PLAYER, "div1", "div2", "nodiv")), "MAX_EXPRESS_EVENTS_EXCEEDED")

    async def test_starting_balance_follows_the_panel(self):
        newcomer = 971005
        with database.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, 'panel_new', 'user')",
                         (newcomer,))
            conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (newcomer,))
            conn.execute("DELETE FROM coin_transactions WHERE user_id = ?", (newcomer,))
        self.assertEqual((await self._set_limit("initial_balance", 1000))[0], 200)

        self.assertEqual(database.get_or_create_wallet(newcomer)["balance"], 1000)

    async def test_limit_values_are_bounded(self):
        cases = [("max_express_events", 1), ("max_express_events", 51), ("initial_balance", 0),
                 ("initial_balance", 1_000_001), ("max_open_bets", 1_001), ("max_bet", 0)]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                status, _ = await self._set_limit(key, value)
                self.assertEqual(status, 400)
        self.assertEqual(database.get_risk_limit_overrides("global", 0), {})

    async def test_settings_are_global_only(self):
        for key in ("max_express_events", "initial_balance"):
            for scope_type, scope_id in (("division", 2), ("user", PLAYER)):
                with self.subTest(key=key, scope=scope_type):
                    status, body = await self._set_limit(key, 5, scope_type, scope_id)
                    self.assertEqual((status, body["error"]), (400, "invalid_limit_key"))

    async def test_min_bet_cannot_exceed_max_bet(self):
        self.assertEqual((await self._set_limit("max_bet", 1000))[0], 200)
        status, body = await self._set_limit("min_bet", 1500)
        self.assertEqual((status, body["error"]), (400, "limits_conflict"))
        self.assertEqual((await self._set_limit("min_bet", 200))[0], 200)
        status, body = await self._set_limit("max_bet", 100)
        self.assertEqual((status, body["error"]), (400, "limits_conflict"))

    async def test_personal_limits_are_listed_for_global_admin_only(self):
        status, body = await self._set_limit("max_open_bets", 3, "user", PLAYER)
        self.assertEqual(status, 200, body)
        status, body = await self._call("GET", "/limits", GLOBAL_ADMIN)
        self.assertEqual(status, 200, body)
        entry = next(u for u in body["user_overrides"] if u["user_id"] == PLAYER)
        self.assertEqual(entry["username"], "panel_player")
        self.assertEqual(entry["limits"], {"max_open_bets": 3})

        status, body = await self._call("GET", "/limits", DIV2_ADMIN)
        self.assertEqual(status, 403, body)

    async def test_bet_type_bans_are_set_and_lifted_from_the_panel(self):
        status, body = await self._call("GET", "/limits", GLOBAL_ADMIN)
        self.assertEqual(status, 200, body)
        self.assertEqual({g["id"] for g in body["ban_groups"]}, set(database.BET_BAN_GROUPS))
        status, me = await self._call("GET", "/me", GLOBAL_ADMIN)
        self.assertNotIn("ban_result", me["limit_keys"]["division"])

        status, body = await self._set_limit("ban_result", 1, "division", 1)
        self.assertEqual(status, 200, body)
        self.assertEqual(_error_code(_place(OTHER_PLAYER, "div1")), "BET_TYPE_BANNED")
        self.assertIsNone(_error_code(_place(OTHER_PLAYER, "div2")))
        status, body = await self._call("GET", "/limits", GLOBAL_ADMIN)
        div1 = next(d for d in body["divisions"] if d["id"] == 1)
        self.assertEqual(div1["overrides"].get("ban_result"), 1)

        status, body = await self._set_limit("ban_result", None, "division", 1)
        self.assertEqual(status, 200, body)
        self.assertIsNone(_error_code(_place(OTHER_PLAYER, "div1")))

        status, body = await self._set_limit("ban_express", 1)
        self.assertEqual(status, 200, body)
        self.assertEqual(_error_code(_place(OTHER_PLAYER, "div1", "div2")), "EXPRESS_BANNED")

    async def test_bet_type_ban_values_and_scopes_are_checked(self):
        for value in (0, 2, 500):
            with self.subTest(value=value):
                status, _ = await self._set_limit("ban_result", value, "division", 1)
                self.assertEqual(status, 400)
        for key, scope_type, scope_id in (("ban_result", "user", PLAYER), ("ban_nonsense", "global", 0)):
            with self.subTest(key=key, scope=scope_type):
                status, body = await self._set_limit(key, 1, scope_type, scope_id)
                self.assertEqual((status, body["error"]), (400, "invalid_limit_key"))
        self.assertEqual(database.get_bet_bans(1), set())

    async def test_market_analysis_is_global_only(self):
        for user_id in (DIV2_ADMIN, PLAYER):
            with self.subTest(user=user_id):
                status, body = await self._call("GET", "/analysis", user_id)
                self.assertEqual((status, body["error"]), (403, "forbidden"))

    async def test_market_analysis_report(self):
        market_analysis.clear_cache()
        self.addCleanup(market_analysis.clear_cache)
        with mock.patch.object(config, "OPENROUTER_API_KEY", ""):
            status, body = await self._call("GET", "/analysis?days=7", GLOBAL_ADMIN)
            self.assertEqual(status, 200, body)
            self.assertEqual((body["status"], body["source"], body["error"]), ("ok", "rules", "no_key"))
            self.assertEqual((body["period_days"], body["cached"]), (7, False))
            for key in ("findings", "stats", "suggestions", "refresh_in"):
                self.assertIn(key, body)
            _, again = await self._call("GET", "/analysis?days=7", GLOBAL_ADMIN)
            self.assertTrue(again["cached"])

    async def test_market_analysis_rejects_other_periods(self):
        for days in ("5", "abc"):
            with self.subTest(days=days):
                status, body = await self._call("GET", f"/analysis?days={days}", GLOBAL_ADMIN)
                self.assertEqual((status, body["error"]), (400, "bad_period"))

    def test_migration_drops_the_daily_bonus_setting(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = ?",
                         (database.MIGRATION_025_DROP_DAILY_BONUS,))
            conn.execute("INSERT INTO risk_limits_config (scope_type, scope_id, limit_key, limit_value, updated_at)"
                         " VALUES ('global', 0, 'daily_bonus', 400, datetime('now', '+3 hours'))")
            conn.execute("INSERT INTO risk_limits_config (scope_type, scope_id, limit_key, limit_value, updated_at)"
                         " VALUES ('global', 0, 'max_express_events', 7, datetime('now', '+3 hours'))")
        database.init_db()
        self.assertEqual(database.get_risk_limit_overrides("global", 0), {"max_express_events": 7})
