"""
tests/test_phase6_security.py

Phase 6 Security, RBAC, IDOR & Integrity Tests:
1. HMAC Telegram authentication (signature validation, expired tokens, tampered data).
2. IDOR: Users cannot view or mutate other users' bets, analytics, or wallet.
3. RBAC: Global admin vs Division admin division isolation vs regular player 403.
4. Destructive Admin Safety: Result correction requires confirmation, explicit reason, and logs to both admin_audit_log and bet_audit_log.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from api.server import create_app
from config import TOKEN


def generate_valid_init_data(user_dict: dict, bot_token: str, auth_date: int | None = None) -> str:
    """Helper to generate cryptographically valid Telegram initData string."""
    if auth_date is None:
        auth_date = int(time.time())

    params = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_dict, separators=(",", ":"))
    }
    # Sort keys
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    hash_val = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params["hash"] = hash_val
    return urlencode(params)


class TestPhase6Security(AioHTTPTestCase):

    async def get_application(self) -> web.Application:
        return create_app()

    def setUp(self) -> None:
        super().setUp()
        database.init_db()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bet_items WHERE match_id >= 99600")
            cursor.execute("DELETE FROM user_bets WHERE user_id IN (779901, 779902, 779903, 779904)")
            cursor.execute("DELETE FROM user_wallets WHERE user_id IN (779901, 779902, 779903, 779904)")
            cursor.execute("DELETE FROM division_admins WHERE user_id IN (779902, 779903)")
            cursor.execute("DELETE FROM admin_audit_log WHERE admin_id IN (779901, 779902, 779903, 779904) OR target_id >= 99600")
            cursor.execute("DELETE FROM bet_audit_log WHERE actor_id IN (779901, 779902, 779903, 779904) OR entity_id >= 99600")
            cursor.execute("DELETE FROM users WHERE telegram_id IN (779901, 779902, 779903, 779904)")
            cursor.execute("DELETE FROM markets WHERE match_id >= 99600")
            cursor.execute("DELETE FROM matches WHERE id >= 99600")

            # Seed User 1: Regular player (id 779901)
            cursor.execute("INSERT INTO users (telegram_id, username, division_id) VALUES (779901, 'regular_player', 1)")
            cursor.execute("INSERT INTO user_wallets (user_id, balance) VALUES (779901, 1000)")

            # Seed User 2: Division 1 Admin (id 779902)
            cursor.execute("INSERT INTO users (telegram_id, username, division_id) VALUES (779902, 'div1_admin', 1)")
            cursor.execute("INSERT INTO division_admins (user_id, division_id) VALUES (779902, 1)")

            # Seed User 3: Victim player (id 779903)
            cursor.execute("INSERT INTO users (telegram_id, username, division_id) VALUES (779903, 'victim_player', 2)")
            cursor.execute("INSERT INTO user_wallets (user_id, balance) VALUES (779903, 5000)")

            # Seed Match 99601 (Division 1) and Match 99602 (Division 2)
            cursor.execute("""
                INSERT INTO matches (id, season_id, division_id, round_number, player1_team, player2_team, status)
                VALUES (99601, 1, 1, 1, 'Порту', 'Бенфика', 'live'),
                       (99602, 1, 2, 1, 'Аякс', 'ПСВ', 'live')
            """)

            # Seed Market in Div 1
            cursor.execute("INSERT INTO markets (id, match_id, market_key, market_name, category, status) VALUES (996001, 99601, '1x2', '1X2', 'main', 'open')")
            # Seed Market in Div 2
            cursor.execute("INSERT INTO markets (id, match_id, market_key, market_name, category, status) VALUES (996002, 99602, '1x2', '1X2', 'main', 'open')")

    def tearDown(self) -> None:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bet_items WHERE match_id >= 99600")
            cursor.execute("DELETE FROM user_bets WHERE user_id IN (779901, 779902, 779903, 779904)")
            cursor.execute("DELETE FROM user_wallets WHERE user_id IN (779901, 779902, 779903, 779904)")
            cursor.execute("DELETE FROM division_admins WHERE user_id IN (779902, 779903)")
            cursor.execute("DELETE FROM admin_audit_log WHERE admin_id IN (779901, 779902, 779903, 779904) OR target_id >= 99600")
            cursor.execute("DELETE FROM bet_audit_log WHERE actor_id IN (779901, 779902, 779903, 779904) OR entity_id >= 99600")
            cursor.execute("DELETE FROM users WHERE telegram_id IN (779901, 779902, 779903, 779904)")
            cursor.execute("DELETE FROM markets WHERE match_id >= 99600")
            cursor.execute("DELETE FROM matches WHERE id >= 99600")
        super().tearDown()

    @unittest_run_loop
    async def test_hmac_tampered_data_rejected(self) -> None:
        """Tampering with initData parameters must return 401 Unauthorized."""
        init_data = generate_valid_init_data({"id": 779901, "username": "player"}, TOKEN or "test_token")
        tampered = init_data.replace("779901", "779903")

        resp = await self.client.get("/api/wallet", headers={"X-Telegram-Init-Data": tampered})
        self.assertEqual(resp.status, 401)

    @unittest_run_loop
    async def test_hmac_expired_auth_date_rejected(self) -> None:
        """auth_date older than 24 hours must be rejected as expired."""
        old_time = int(time.time()) - (86400 * 2)  # 48 hours ago
        expired_init_data = generate_valid_init_data({"id": 779901, "username": "player"}, TOKEN or "test_token", auth_date=old_time)

        resp = await self.client.get("/api/wallet", headers={"X-Telegram-Init-Data": expired_init_data})
        self.assertEqual(resp.status, 401)

    @unittest_run_loop
    async def test_idor_user_cannot_access_other_user_analytics(self) -> None:
        """Profile analytics route strictly scopes to authenticated user id."""
        token = TOKEN or "test_token"
        init_data_user1 = generate_valid_init_data({"id": 779901, "username": "regular_player"}, token)

        # Seed bet for user 1 and user 3
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_bets (id, user_id, bet_type, amount, potential_win, total_odd, status)
                VALUES (996101, 779901, 'single', 100, 200, 2.0, 'pending'),
                       (996102, 779903, 'single', 500, 1500, 3.0, 'pending')
            """)

        resp = await self.client.get("/api/profile/analytics", headers={"X-Telegram-Init-Data": init_data_user1})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        # Assert strictly user 1 data is returned, never user 3
        self.assertEqual(data["analytics"]["user_id"], 779901)
        self.assertEqual(data["analytics"]["balance"], 1000)

    @unittest_run_loop
    async def test_rbac_player_blocked_from_admin_live_endpoints(self) -> None:
        """A regular player must be refused with 403 on admin live overview and actions."""
        token = TOKEN or "test_token"
        init_data_player = generate_valid_init_data({"id": 779901, "username": "regular_player"}, token)

        resp1 = await self.client.get("/api/admin/live/overview", headers={"X-Telegram-Init-Data": init_data_player})
        self.assertEqual(resp1.status, 403)

        resp2 = await self.client.post("/api/admin/live/markets/996001/suspend",
                                       headers={"X-Telegram-Init-Data": init_data_player},
                                       json={"reason": "Hacker attempt"})
        self.assertEqual(resp2.status, 403)

    @unittest_run_loop
    async def test_rbac_division_admin_scoped_isolation(self) -> None:
        """Division 1 admin can manage Div 1 markets, but gets 403 when trying to modify Div 2."""
        token = TOKEN or "test_token"
        init_data_div1_admin = generate_valid_init_data({"id": 779902, "username": "div1_admin"}, token)

        # 1. Div 1 admin suspending Div 1 market -> 200 OK
        resp1 = await self.client.post("/api/admin/live/markets/996001/suspend",
                                       headers={"X-Telegram-Init-Data": init_data_div1_admin},
                                       json={"reason": "Goal scored in Div 1"})
        self.assertEqual(resp1.status, 200)
        data1 = await resp1.json()
        self.assertEqual(data1["status"], "ok")

        # 2. Div 1 admin attempting to suspend Div 2 market -> 403 Forbidden!
        resp2 = await self.client.post("/api/admin/live/markets/996002/suspend",
                                       headers={"X-Telegram-Init-Data": init_data_div1_admin},
                                       json={"reason": "Unauthorized across divisions"})
        self.assertEqual(resp2.status, 403)

    @unittest_run_loop
    async def test_destructive_action_safety_and_audit(self) -> None:
        """Result correction requires explicit confirmation, reason, and writes audit logs."""
        token = TOKEN or "test_token"
        init_data_div1_admin = generate_valid_init_data({"id": 779902, "username": "div1_admin"}, token)

        # 1. Missing confirm -> 400
        resp1 = await self.client.post("/api/admin/live/matches/99601/correction",
                                       headers={"X-Telegram-Init-Data": init_data_div1_admin},
                                       json={"home_score": 2, "away_score": 0, "reason": "VAR"})
        self.assertEqual(resp1.status, 400)

        # 2. Missing reason -> 400
        resp2 = await self.client.post("/api/admin/live/matches/99601/correction",
                                       headers={"X-Telegram-Init-Data": init_data_div1_admin},
                                       json={"home_score": 2, "away_score": 0, "confirm": True})
        self.assertEqual(resp2.status, 400)

        # 3. Valid correction
        resp3 = await self.client.post("/api/admin/live/matches/99601/correction",
                                       headers={"X-Telegram-Init-Data": init_data_div1_admin},
                                       json={
                                           "home_score": 3,
                                           "away_score": 1,
                                           "reason": "VAR overturned goal at 88 min",
                                           "confirm": True
                                       })
        self.assertEqual(resp3.status, 200)
        data3 = await resp3.json()
        self.assertEqual(data3["status"], "ok")

        # 4. Verify DB state and audit logs
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT player1_score, player2_score FROM matches WHERE id = 99601")
            m_row = cursor.fetchone()
            self.assertEqual(m_row["player1_score"], 3)
            self.assertEqual(m_row["player2_score"], 1)

            # Check admin_audit_log
            cursor.execute("SELECT * FROM admin_audit_log WHERE target_type = 'match' AND target_id = 99601")
            audit_entry = cursor.fetchone()
            self.assertIsNotNone(audit_entry)
            self.assertEqual(audit_entry["action"], "match_result_correction")
            self.assertEqual(audit_entry["admin_id"], 779902)


class TestAdminMarketVoidIntegrity(AioHTTPTestCase):
    """Аудит 2a — аннулирование рынка обязано доходить до каждой затронутой ставки.

    До фикса `POST /api/admin/live/markets/{id}/void` возвращал деньги только
    ординарам (`bet_type = 'single'`), а pending-нога экспресса оставалась
    живой: `settle_match_predictions` читал рынки без фильтра статуса, решал
    эту ногу по счёту матча и попутно затирал `markets.status` из 'voided' в
    'settled'. Итог — проигрыш или выплата по официально аннулированному
    исходу. Теперь ноги разбирает `database.void_market`, а settlement
    аннулированные рынки не трогает.
    """

    BETTOR = 779905
    BETTOR2 = 779906
    DIV1_ADMIN = 779902
    DIV2_ADMIN = 779907

    async def get_application(self) -> web.Application:
        return create_app()

    def setUp(self) -> None:
        super().setUp()
        database.init_db()
        self._clean()

        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO users (telegram_id, username, division_id) VALUES (?, 'void_bettor', 1)", (self.BETTOR,))
            c.execute("INSERT INTO users (telegram_id, username, division_id) VALUES (?, 'void_bettor2', 1)", (self.BETTOR2,))
            c.execute("INSERT INTO users (telegram_id, username, division_id) VALUES (?, 'void_div1_admin', 1)", (self.DIV1_ADMIN,))
            c.execute("INSERT INTO division_admins (user_id, division_id) VALUES (?, 1)", (self.DIV1_ADMIN,))
            c.execute("INSERT INTO users (telegram_id, username, division_id) VALUES (?, 'void_div2_admin', 2)", (self.DIV2_ADMIN,))
            c.execute("INSERT INTO division_admins (user_id, division_id) VALUES (?, 2)", (self.DIV2_ADMIN,))
            for uid in (self.BETTOR, self.BETTOR2):
                database.get_or_create_wallet(uid)
                c.execute("UPDATE user_wallets SET balance = 20000 WHERE user_id = ?", (uid,))

            # Матч 99601 (Д1) — рынок, который будем аннулировать.
            # Матч 99603 (Д1) — живой матч второй ноги экспресса.
            # Матч 99602 (Д2) — для проверки скоупинга и отказа по settled.
            c.execute("""
                INSERT INTO matches (id, season_id, division_id, round_number, player1_team, player2_team, status)
                VALUES (99601, 1, 1, 1, 'Порту', 'Бенфика', 'live'),
                       (99603, 1, 1, 1, 'Брага', 'Портимоненсе', 'scheduled'),
                       (99602, 1, 2, 1, 'Аякс', 'ПСВ', 'live')
            """)
            c.execute("""
                INSERT INTO markets (id, match_id, market_key, market_name, category, status)
                VALUES (996001, 99601, '1x2', '1X2', 'main', 'open'),
                       (996004, 99601, 'total_goals', 'Total', 'main', 'open'),
                       (996003, 99603, '1x2', '1X2', 'main', 'open'),
                       (996002, 99602, '1x2', '1X2', 'main', 'open')
            """)
            c.execute("""
                INSERT INTO market_selections (id, market_id, selection_key, selection_name, odds_value, status)
                VALUES (996101, 996001, 'p1', 'П1', 2.0, 'active'),
                       (996104, 996004, 'over_2_5', 'ТБ2.5', 1.9, 'active'),
                       (996103, 996003, 'p1', 'П1', 2.0, 'active'),
                       (996102, 996002, 'p1', 'П1', 2.0, 'active')
            """)
            # Линия Д1 тура 1 открыта: без этого place_user_bet отсекает гейт раунда.
            c.execute(
                "INSERT OR IGNORE INTO rounds (round_number, division_id, season_id, is_open, bets_open, deadline) "
                "VALUES (1, 1, 1, 0, 1, NULL)"
            )
            c.execute(
                "UPDATE rounds SET is_open = 0, bets_open = 1, deadline = NULL "
                "WHERE round_number = 1 AND division_id = 1 AND season_id = 1"
            )

        token = TOKEN or "test_token"
        self.admin_headers = {"X-Telegram-Init-Data": generate_valid_init_data(
            {"id": self.DIV1_ADMIN, "username": "div1_admin"}, token)}
        self.div2_headers = {"X-Telegram-Init-Data": generate_valid_init_data(
            {"id": self.DIV2_ADMIN, "username": "void_div2_admin"}, token)}

    def tearDown(self) -> None:
        self._clean()
        super().tearDown()

    def _clean(self) -> None:
        with database.transaction() as conn:
            c = conn.cursor()
            ids = (self.BETTOR, self.BETTOR2, self.DIV1_ADMIN, self.DIV2_ADMIN)
            c.execute("DELETE FROM coin_transactions WHERE user_id IN (?,?,?,?,779901,779903)", ids)
            c.execute("DELETE FROM bet_items WHERE match_id >= 99600")
            c.execute("DELETE FROM user_bets WHERE user_id IN (?,?,?,?,779901,779903)", ids)
            c.execute("DELETE FROM user_wallets WHERE user_id IN (?,?)", (self.BETTOR, self.BETTOR2))
            c.execute("DELETE FROM division_admins WHERE user_id IN (?,?)", (self.DIV1_ADMIN, self.DIV2_ADMIN))
            c.execute("DELETE FROM bet_audit_log WHERE entity_id >= 99600 OR actor_id IN (?,?,?,?)", ids)
            c.execute("DELETE FROM admin_audit_log WHERE target_id >= 99600 OR admin_id IN (?,?,?,?)", ids)
            c.execute("DELETE FROM notification_events WHERE user_id IN (?,?,?)", (self.BETTOR, self.BETTOR2, 779901))
            c.execute("DELETE FROM market_selections WHERE id >= 996000")
            c.execute("DELETE FROM markets WHERE id >= 996000")
            c.execute("DELETE FROM bet_markets WHERE match_id >= 99600")
            c.execute("DELETE FROM matches WHERE id >= 99600")
            c.execute("DELETE FROM users WHERE telegram_id IN (?,?,?,?)", ids)

    # --- фабрики ----------------------------------------------------------

    def _place(self, user_id: int, amount: int, legs: list[tuple[int, int, int]]) -> int:
        """Поставить купон через единый владелец списания — database.place_user_bet."""
        selections = [{"match_id": m, "market_id": mk, "selection_id": s, "outcome": "p1"}
                      for m, mk, s in legs]
        ok, res = database.place_user_bet(user_id=user_id, amount=amount, selections=selections)
        self.assertTrue(ok, f"Купон обязан пройти везику: {res}")
        return res

    async def _void(self, market_id: int, headers: dict | None = None,
                    body: dict | None = None):
        payload = {"reason": "Technical issue", "confirm": True} if body is None else body
        return await self.client.post(f"/api/admin/live/markets/{market_id}/void",
                                      headers=headers if headers is not None else self.admin_headers,
                                      json=payload)

    def _bet(self, bet_id: int) -> dict:
        with database.transaction() as conn:
            return dict(conn.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,)).fetchone())

    def _legs(self, bet_id: int) -> dict:
        with database.transaction() as conn:
            rows = conn.execute("SELECT market_id, status FROM bet_items WHERE bet_id = ?", (bet_id,)).fetchall()
        return {r["market_id"]: r["status"] for r in rows}

    def _balance(self, user_id: int) -> int:
        return database.get_or_create_wallet(user_id)["balance"]

    def _market_status(self, market_id: int) -> str:
        with database.transaction() as conn:
            return conn.execute("SELECT status FROM markets WHERE id = ?", (market_id,)).fetchone()["status"]

    def _refund_rows(self, bet_id: int) -> list[dict]:
        with database.transaction() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT transaction_type, reference_type, amount FROM coin_transactions "
                "WHERE reference_id = ? AND reference_type = 'bet' AND transaction_type = 'admin_refund'",
                (bet_id,)).fetchall()]

    # --- 1. экспресс: нога аннулируется, купон живёт дальше ---------------

    @unittest_run_loop
    async def test_void_market_refunds_express_leg_and_keeps_coupon_pending(self):
        bet_id = self._place(self.BETTOR, 1000, [(99601, 996001, 996101), (99603, 996003, 996103)])
        self.assertEqual(self._balance(self.BETTOR), 19000)

        resp = await self._void(996001)
        self.assertEqual(200, resp.status)
        data = await resp.json()
        self.assertEqual(1, data["voided_legs"], "Аннулирована ровно одна нога")
        self.assertEqual(0, data["refunded_count"], "Купон с живой ногой не возвращается целиком")
        self.assertEqual(1, data["pending_coupons"])

        self.assertEqual(self._legs(bet_id)[996001], "refunded")
        self.assertEqual(self._legs(bet_id)[996003], "pending")
        self.assertEqual(self._bet(bet_id)["status"], "pending")
        self.assertEqual(self._balance(self.BETTOR), 19000, "Движения денег при живой ноге быть не должно")
        self.assertEqual(self._market_status(996001), "voided")

    # --- 2. деньги: settlement не платит и не проигрывает по voided-ноге --

    @unittest_run_loop
    async def test_express_with_voided_leg_pays_only_live_legs(self):
        bet_id = self._place(self.BETTOR, 1000, [(99601, 996001, 996101), (99603, 996003, 996103)])
        await self._void(996001)

        from services.settlement_engine import settle_match_predictions
        settle_match_predictions(99603, 2, 0, "finished")

        bet = self._bet(bet_id)
        self.assertEqual("won", bet["status"], "Купон обязан дожить до выигрыша, а не стать refund/lost")
        self.assertEqual(2000, bet["actual_payout"],
                         "Аннулированная нога даёт 1.00: выплата = стейк × коэффициент живой ноги")
        self.assertEqual(self._balance(self.BETTOR), 21000)

    @unittest_run_loop
    async def test_voiding_the_last_pending_leg_refunds_the_stake(self):
        bet_id = self._place(self.BETTOR, 1000, [(99601, 996001, 996101), (99603, 996003, 996103)])
        await self._void(996001)
        resp = await self._void(996003)
        data = await resp.json()

        self.assertEqual(1, data["refunded_count"], "Последняя аннулированная нога закрывает купон возвратом")
        bet = self._bet(bet_id)
        self.assertEqual("refunded", bet["status"])
        self.assertEqual(1000, bet["actual_payout"])
        self.assertEqual(20000, self._balance(self.BETTOR), "Стейк вернулся ровно один раз")
        rows = self._refund_rows(bet_id)
        self.assertEqual(1, len(rows), "Двойного возврата по одному купону быть не может")

    @unittest_run_loop
    async def test_single_bet_on_voided_market_is_still_refunded(self):
        """Регрессия на исходное поведение: ординар возвращаем и после переноса логики."""
        bet_id = self._place(self.BETTOR, 800, [(99601, 996001, 996101)])
        resp = await self._void(996001)
        data = await resp.json()

        self.assertEqual(1, data["refunded_count"])
        self.assertEqual("refunded", self._bet(bet_id)["status"])
        self.assertEqual(20000, self._balance(self.BETTOR), "Стейк ординара возвращается полностью")

    # --- 3. settlement обязан уважать статус voided -----------------------

    @unittest_run_loop
    async def test_settlement_preserves_voided_market_and_settles_sibling(self):
        await self._void(996001)
        from services.settlement_engine import settle_match_predictions
        settle_match_predictions(99601, 2, 0, "finished")

        self.assertEqual("voided", self._market_status(996001), "Счёт матча не имеет права «re-settle’ить аннулированный рынок")
        self.assertEqual("settled", self._market_status(996004), "Соседний рынок того же матча рассчитывается как раньше")
        with database.transaction() as conn:
            sel = conn.execute("SELECT status FROM market_selections WHERE id = 996101").fetchone()
        self.assertEqual("active", sel["status"], "Отборы аннулированного рынка не перемаркиваются расчётом")

    @unittest_run_loop
    async def test_resettle_does_not_resurrect_voided_leg(self):
        """Правка счёта (LB-14) тоже не должна оживлять аннулированную ногу."""
        bet_id = self._place(self.BETTOR, 1000, [(99601, 996001, 996101), (99603, 996003, 996103)])
        await self._void(996001)
        self._legs(bet_id)

        from services.settlement_engine import settle_match_predictions, resettle_match_predictions
        settle_match_predictions(99601, 2, 0, "finished")
        resettle_match_predictions(99601, 0, 3, "finished")

        self.assertEqual("refunded", self._legs(bet_id)[996001],
                         "Нога аннулированного рынка не получает won/lost после правки счёта")
        self.assertEqual("voided", self._market_status(996001))

    # --- 4. безопасность операции: идемпотентность, статусы, доступ --------

    @unittest_run_loop
    async def test_void_market_is_idempotent(self):
        bet_id = self._place(self.BETTOR, 800, [(99601, 996001, 996101)])
        first = await self._void(996001)
        self.assertEqual(200, first.status)
        balance_after_first = self._balance(self.BETTOR)

        second = await self._void(996001)
        self.assertEqual(200, second.status)
        data = await second.json()
        self.assertTrue(data["already_voided"])
        self.assertEqual(0, data["refunded_count"])
        self.assertEqual(balance_after_first, self._balance(self.BETTOR), "Повторный void не двигает деньги")
        self.assertEqual(1, len(self._refund_rows(bet_id)))

    @unittest_run_loop
    async def test_settled_market_cannot_be_voided(self):
        """Рассчитанный рынок аннулировать нельзя — это terminal-статус state machine."""
        from services.settlement_engine import settle_match_predictions
        settle_match_predictions(99603, 2, 0, "finished")
        self.assertEqual("settled", self._market_status(996003))

        resp = await self._void(996003)
        self.assertEqual(409, resp.status)
        self.assertEqual("settled", self._market_status(996003))

    @unittest_run_loop
    async def test_missing_market_fails_scoping_before_404(self):
        """Несуществующий рынок не проходит `_can_manage_market` (fail closed) → 403."""
        resp = await self._void(996999)
        self.assertEqual(403, resp.status)

    @unittest_run_loop
    async def test_confirmation_and_reason_are_required(self):
        no_confirm = await self._void(996001, body={"reason": "x"})
        self.assertEqual(400, no_confirm.status)
        no_reason = await self._void(996001, body={"confirm": True, "reason": "  "})
        self.assertEqual(400, no_reason.status)
        self.assertEqual("open", self._market_status(996001), "Отклонённый запрос не должен менять рынок")

    @unittest_run_loop
    async def test_void_requires_auth_and_market_ownership(self):
        anon = await self._void(996001, headers={})
        self.assertEqual(401, anon.status)
        foreign_admin = await self._void(996001, headers=self.div2_headers)
        self.assertEqual(403, foreign_admin.status)
        self.assertEqual("open", self._market_status(996001))

    # --- 5. аудит ---------------------------------------------------------

    @unittest_run_loop
    async def test_void_writes_bet_and_admin_audit_trails(self):
        bet_id = self._place(self.BETTOR, 800, [(99601, 996001, 996101)])
        await self._void(996001)

        with database.transaction() as conn:
            bet_actions = [r["action"] for r in conn.execute(
                "SELECT action FROM bet_audit_log WHERE entity_id = ? AND entity_type = 'bet'", (bet_id,))]
            market_actions = [r["action"] for r in conn.execute(
                "SELECT action FROM bet_audit_log WHERE entity_id = ? AND entity_type = 'market'", (996001,))]
            admin = conn.execute(
                "SELECT * FROM admin_audit_log WHERE target_type = 'market' AND target_id = 996001").fetchone()
            reason_row = conn.execute(
                "SELECT new_value FROM bet_audit_log WHERE entity_id = ? AND action = 'market_void_bet_refund'",
                (bet_id,)).fetchone()

        self.assertIn("bet_voided", bet_actions, "Возврат стейка обязан быть в аудите от имени канонического void_user_bet")
        self.assertIn("market_void_bet_refund", bet_actions)
        self.assertIn("market_voided", market_actions, "Переход статуса рынка ведёт state machine и пишет свой аудит")
        self.assertIsNotNone(admin)
        self.assertEqual("live_market_void", admin["action"])
        self.assertEqual(self.DIV1_ADMIN, admin["admin_id"])
        self.assertIn("Technical issue", reason_row["new_value"])

    # --- 6. аннулированный рынок закрыт для новых ставок ------------------

    @unittest_run_loop
    async def test_new_bet_on_voided_market_is_refused(self):
        await self._void(996001)
        ok, res = database.place_user_bet(
            user_id=self.BETTOR, amount=100,
            selections=[{"match_id": 99601, "market_id": 996001, "selection_id": 996101, "outcome": "p1"}],
        )
        self.assertFalse(ok, "Ставку на аннулированный рынок принимать нельзя")
        self.assertIn("приостановлен или закрыт", str(res))

    @unittest_run_loop
    async def test_voided_market_is_not_revived_by_legacy_bet_markets(self):
        """Дыра: relational-проверка пропускала 'voided', и код падал в legacy bet_markets.

        Матч обязан быть не 'live' — фолбэк в `bet_markets` закрыт для живых
        матчей условием `match_row["status"] != "live"` (database.py:9755).
        """
        await self._void(996003)
        with database.transaction() as conn:
            conn.execute("""
                INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, is_active)
                VALUES (99603, 1, 'Брага', 'Портимоненсе', 2.1, 3.0, 3.5, 1)
            """)

        ok, res = database.place_user_bet(
            user_id=self.BETTOR2, amount=100,
            selections=[{"match_id": 99603, "market_id": 996003, "selection_id": 996103, "outcome": "p1"}],
        )
        self.assertFalse(ok, f"legacy-фолбэк не должен оживлять аннулированный рынок: {res}")
        self.assertIn("приостановлен или закрыт", str(res))
        self.assertEqual(20000, self._balance(self.BETTOR2), "Списание не произошло")

    # --- 7. нога аннулированного рынка не участвует в кэшауте -------------

    def test_cashout_prices_voided_leg_as_one(self):
        """calculate_cashout_offer — чистая функция: 'refunded' нога даёт 1.00."""
        from services.cashout_engine import calculate_cashout_offer
        items = [
            {"status": "won", "odd": 2.0, "odds_at_placement": 2.0, "current_odd": 1.01},
            {"status": "refunded", "odd": 5.0, "odds_at_placement": 5.0, "current_odd": 1.01},
        ]
        available, offer, reason = calculate_cashout_offer(stake=100, potential_win=1000, items=items)
        self.assertTrue(available, reason)
        # Обе ноги вне игры → ratio_product = 1.00, оставка = 100 × (1 - 0.08).
        # Без скипа аннулированной ноги в предложение въелся бы коэффициент 5.0/1.01.
        self.assertEqual(92, offer, f"Аннулированная нога завысила кэшаут (reason={reason})")


if __name__ == "__main__":
    unittest.main()
