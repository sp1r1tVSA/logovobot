"""
tests/test_phase9_cashout.py

Phase 9 — Dynamic Cashout Engine Test Suite.
Verifies cashout quotation formulas, suspended market guards, lost leg guards,
atomic execution, duplicate rejection, and zero duplicate payout on subsequent match settlement.
"""

import os
import tempfile
import unittest
import database
from services.cashout_engine import quote_cashout, execute_cashout, calculate_cashout_offer
from services.settlement_engine import settle_match_predictions


class TestPhase9Cashout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.user_id = 960001
        self.match_id = 960011

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'cashout_user', 'user')", (self.user_id,))
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cashout Season', 'active')")
            self.season_id = cursor.lastrowid
            cursor.execute("INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) VALUES (1, 1, ?, 0, 1)", (self.season_id,))

            cursor.execute("""
                INSERT INTO matches (id, division_id, season_id, round_number, player1_team, player2_team, status)
                VALUES (?, 1, ?, 1, 'Liverpool', 'Chelsea', 'open')
            """, (self.match_id, self.season_id))

            cursor.execute("INSERT INTO markets (id, match_id, market_key, market_name, status) VALUES (961, ?, 'match_result', 'Match Winner', 'open')", (self.match_id,))
            cursor.execute("""
                INSERT INTO market_selections (id, market_id, selection_key, selection_name, odds_value, status, odds_version)
                VALUES (9611, 961, 'home', 'Liverpool', 2.00, 'active', 1)
            """)

        database.get_or_create_wallet(self.user_id)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_wallets SET balance = 1000 WHERE user_id = ?", (self.user_id,))

    def tearDown(self):
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def test_p9_cash_01_quote_calculation_favorable_odds(self):
        """P9-CASH-01: Odds drop in favor of bet increases cashout offer above stake."""
        items = [{
            "status": "pending",
            "odds_at_placement": 2.00,
            "current_odd": 1.40,
            "market_status": "open",
            "sel_status": "active"
        }]
        available, offer, reason = calculate_cashout_offer(stake=100, potential_win=200, items=items)
        self.assertTrue(available)
        self.assertIsNone(reason)
        # fair = 100 * (2.0 / 1.4) = 142.85; with 8% margin: 142.85 * 0.92 = 131
        self.assertGreater(offer, 100)
        self.assertLessEqual(offer, 200)

    def test_p9_cash_02_quote_calculation_lost_leg(self):
        """P9-CASH-02: Lost leg makes cashout unavailable with 0 offer."""
        items = [{
            "status": "lost",
            "odds_at_placement": 2.00,
            "current_odd": 10.0,
            "market_status": "open",
            "sel_status": "active"
        }]
        available, offer, reason = calculate_cashout_offer(stake=100, potential_win=200, items=items)
        self.assertFalse(available)
        self.assertEqual(offer, 0)
        self.assertEqual(reason, "LEG_LOST")

    def test_p9_cash_03_cashout_execution_atomic_settlement(self):
        """P9-CASH-03: Executing cashout atomically settles bet, updates status, and credits wallet."""
        # 1. Place bet
        success, bet_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "market_id": 961, "selection_id": 9611, "outcome": "home", "odds": 2.00}],
            idempotency_key="cashout-bet-1"
        )
        self.assertTrue(success)
        self.assertEqual(database.get_or_create_wallet(self.user_id)["balance"], 900)

        # 2. Quote cashout
        quote = quote_cashout(user_id=self.user_id, bet_id=bet_id)
        self.assertTrue(quote["available"])
        offer_val = quote["offer"]
        self.assertGreater(offer_val, 0)

        # 3. Execute cashout
        ok, res = execute_cashout(user_id=self.user_id, bet_id=bet_id, idempotency_key="cashout-exec-1")
        self.assertTrue(ok)
        self.assertEqual(res["payout"], offer_val)

        # 4. Verify wallet balance credited
        new_balance = database.get_or_create_wallet(self.user_id)["balance"]
        self.assertEqual(new_balance, 900 + offer_val)

        # 5. Verify bet record
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,))
            bet = cursor.fetchone()
            self.assertEqual(bet["status"], "cashed_out")
            self.assertIsNotNone(bet["cashout_at"])
            self.assertIsNotNone(bet["settled_at"])
            self.assertEqual(bet["actual_payout"], offer_val)

            cursor.execute("SELECT * FROM coin_transactions WHERE reference_id = ? AND transaction_type = 'cashout'", (bet_id,))
            tx = cursor.fetchone()
            self.assertIsNotNone(tx)
            self.assertEqual(tx["amount"], offer_val)

    def test_p9_cash_03b_cashout_enqueues_detailed_notice(self):
        """Кэшаут кладёт в очередь одну подробную квитанцию в бот."""
        _, bet_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "market_id": 961, "selection_id": 9611, "outcome": "home", "odds": 2.00}],
            idempotency_key="cashout-notice-bet"
        )
        ok, res = execute_cashout(user_id=self.user_id, bet_id=bet_id)
        self.assertTrue(ok)
        self.assertEqual(res["stake"], 100)

        with database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_events WHERE user_id = ? AND event_type = 'BET_SETTLED'",
                (self.user_id,),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_event_id"], f"bet_{bet_id}")
        self.assertIn(f"Кэшаут по ставке #{bet_id}", rows[0]["title"])
        body = rows[0]["body"]
        self.assertIn("Liverpool — Chelsea", body)
        self.assertIn("Ставка: <b>100 🪙</b> × 2.00", body)
        self.assertIn("Возможный выигрыш: 200 🪙", body)
        self.assertIn(f"Кэшаут: <b>+{res['payout']} 🪙</b>", body)
        self.assertIn(f"Баланс: <b>{res['balance']:,} 🪙</b>".replace(",", " "), body)

        # Более поздний расчёт матча не должен слать второе уведомление.
        settle_match_predictions(self.match_id, 2, 0, "finished")
        with database.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM notification_events WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_p9_cash_04_duplicate_cashout_rejected(self):
        """P9-CASH-04: Second cashout attempt on already settled bet is cleanly rejected."""
        _, bet_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "market_id": 961, "selection_id": 9611, "outcome": "home", "odds": 2.00}],
            idempotency_key="cashout-bet-2"
        )
        ok1, res1 = execute_cashout(user_id=self.user_id, bet_id=bet_id, idempotency_key="dup-1")
        self.assertTrue(ok1)

        bal_after_first = database.get_or_create_wallet(self.user_id)["balance"]

        # Attempt second cashout
        ok2, res2 = execute_cashout(user_id=self.user_id, bet_id=bet_id, idempotency_key="dup-2")
        self.assertFalse(ok2)
        self.assertIsInstance(res2, dict)
        self.assertEqual(res2.get("error"), "ALREADY_SETTLED")

        # Balance remains unchanged
        self.assertEqual(database.get_or_create_wallet(self.user_id)["balance"], bal_after_first)

    def test_p9_cash_05_cashout_suspended_market_rejected(self):
        """P9-CASH-05: When market is suspended, cashout quote and execution are rejected."""
        _, bet_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "market_id": 961, "selection_id": 9611, "outcome": "home", "odds": 2.00}],
            idempotency_key="cashout-bet-3"
        )

        # Suspend market
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE markets SET status = 'suspended' WHERE id = 961")

        quote = quote_cashout(user_id=self.user_id, bet_id=bet_id)
        self.assertFalse(quote["available"])
        self.assertEqual(quote["reason"], "MARKET_SUSPENDED")

        ok, res = execute_cashout(user_id=self.user_id, bet_id=bet_id)
        self.assertFalse(ok)
        self.assertIn(res.get("error"), ("MARKET_UNAVAILABLE", "CASHOUT_UNAVAILABLE"))

    def test_p9_cash_06_post_cashout_settlement_skips(self):
        """P9-CASH-06: Match settlement skips cashed-out bets, preventing duplicate payouts."""
        _, bet_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "market_id": 961, "selection_id": 9611, "outcome": "home", "odds": 2.00}],
            idempotency_key="cashout-bet-4"
        )
        ok, res = execute_cashout(user_id=self.user_id, bet_id=bet_id)
        self.assertTrue(ok)
        offer_val = res["payout"]

        bal_before_match_settlement = database.get_or_create_wallet(self.user_id)["balance"]

        # Run settlement engine with 2:0 result (Home won)
        settle_res = settle_match_predictions(self.match_id, score1=2, score2=0)
        self.assertIsNotNone(settle_res)

        # Balance must remain identical (NO extra 200 coins paid!)
        bal_after_match_settlement = database.get_or_create_wallet(self.user_id)["balance"]
        self.assertEqual(bal_after_match_settlement, bal_before_match_settlement)


if __name__ == "__main__":
    unittest.main()
