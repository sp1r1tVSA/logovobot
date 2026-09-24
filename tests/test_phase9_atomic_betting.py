"""
tests/test_phase9_atomic_betting.py

Phase 9 — Atomic Bet Placement, Concurrency & Idempotency Test Suite.
Verifies single & express bets, invalid leg rejection, concurrent overdraft prevention
(1000 balance, 800+800 race), and strict idempotency payload integrity.
"""

import concurrent.futures
import os
import tempfile
import unittest
import database


class TestPhase9AtomicBetting(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.user_id = 950001
        self.match1_id = 950011
        self.match2_id = 950012

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'atomic_user', 'user')", (self.user_id,))
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Atomic Season', 'active')")
            self.season_id = cursor.lastrowid

            cursor.execute("INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) VALUES (1, 1, ?, 0, 1)", (self.season_id,))

            # Match 1: open
            cursor.execute("""
                INSERT INTO matches (id, division_id, season_id, round_number, player1_team, player2_team, status)
                VALUES (?, 1, ?, 1, 'PSG', 'Marseille', 'open')
            """, (self.match1_id, self.season_id))
            cursor.execute("INSERT INTO markets (id, match_id, market_key, market_name, status) VALUES (951, ?, 'match_result', 'Match Winner', 'open')", (self.match1_id,))
            cursor.execute("""
                INSERT INTO market_selections (id, market_id, selection_key, selection_name, odds_value, status, odds_version)
                VALUES (9511, 951, 'home', 'PSG', 1.80, 'active', 1)
            """)

            # Match 2: open
            cursor.execute("""
                INSERT INTO matches (id, division_id, season_id, round_number, player1_team, player2_team, status)
                VALUES (?, 1, ?, 1, 'Bayern', 'Dortmund', 'open')
            """, (self.match2_id, self.season_id))
            cursor.execute("INSERT INTO markets (id, match_id, market_key, market_name, status) VALUES (952, ?, 'match_result', 'Match Winner', 'open')", (self.match2_id,))
            cursor.execute("""
                INSERT INTO market_selections (id, market_id, selection_key, selection_name, odds_value, status, odds_version)
                VALUES (9521, 952, 'home', 'Bayern', 2.00, 'active', 1)
            """)

        database.get_or_create_wallet(self.user_id)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_wallets SET balance = 1000 WHERE user_id = ?", (self.user_id,))

    def tearDown(self):
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def test_p9_atom_01_single_bet_atomic_deduction(self):
        """P9-ATOM-01: Normal valid single bet placement deducts balance and records transaction."""
        success, bet_res = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match1_id, "market_id": 951, "selection_id": 9511, "outcome": "home", "odds": 1.80}],
            idempotency_key="atom-single-1"
        )
        self.assertTrue(success)
        bet_id = bet_res

        # Wallet balance should be 900
        wallet = database.get_or_create_wallet(self.user_id)
        self.assertEqual(wallet["balance"], 900)

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,))
            bet = cursor.fetchone()
            self.assertEqual(bet["user_id"], self.user_id)
            self.assertEqual(bet["amount"], 100)
            self.assertAlmostEqual(bet["total_odd"], 1.80)

            # Transaction logged
            cursor.execute("SELECT * FROM coin_transactions WHERE user_id = ? AND transaction_type = 'bet_placed'", (self.user_id,))
            tx = cursor.fetchone()
            self.assertIsNotNone(tx)
            self.assertEqual(tx["amount"], -100)

    def test_p9_atom_02_express_bet_placement(self):
        """P9-ATOM-02: Valid express bet across multiple matches calculates combined odds correctly."""
        success, bet_res = database.place_user_bet(
            user_id=self.user_id,
            amount=200,
            selections=[
                {"match_id": self.match1_id, "market_id": 951, "selection_id": 9511, "outcome": "home", "odds": 1.80},
                {"match_id": self.match2_id, "market_id": 952, "selection_id": 9521, "outcome": "home", "odds": 2.00}
            ],
            idempotency_key="atom-express-1"
        )
        self.assertTrue(success)
        bet_id = bet_res

        wallet = database.get_or_create_wallet(self.user_id)
        self.assertEqual(wallet["balance"], 800)

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,))
            bet = cursor.fetchone()
            self.assertEqual(bet["bet_type"], "express")
            # 1.80 × 2.00 × 0.97 — надбавка на экспресс за вторую ногу
            self.assertAlmostEqual(bet["total_odd"], 3.49)

    def test_p9_atom_03_express_with_invalid_leg_rejected(self):
        """P9-ATOM-03: Express bet with one suspended leg is rejected with zero debit."""
        # Suspend market 952
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE markets SET status = 'suspended' WHERE id = 952")

        success, err = database.place_user_bet(
            user_id=self.user_id,
            amount=200,
            selections=[
                {"match_id": self.match1_id, "market_id": 951, "selection_id": 9511, "outcome": "home", "odds": 1.80},
                {"match_id": self.match2_id, "market_id": 952, "selection_id": 9521, "outcome": "home", "odds": 2.00}
            ],
            idempotency_key="atom-express-fail"
        )
        self.assertFalse(success)

        # Wallet must remain unchanged (1000)
        wallet = database.get_or_create_wallet(self.user_id)
        self.assertEqual(wallet["balance"], 1000)

    def test_p9_atom_04_concurrent_overdraft_race(self):
        """P9-ATOM-04: Concurrent overdraft race (1000 balance, 800+800 bets in threads)."""
        results = []

        def submit_bet(key_suffix):
            return database.place_user_bet(
                user_id=self.user_id,
                amount=800,
                selections=[{"match_id": self.match1_id, "market_id": 951, "selection_id": 9511, "outcome": "home", "odds": 1.80}],
                idempotency_key=f"race-{key_suffix}"
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(submit_bet, "1")
            f2 = executor.submit(submit_bet, "2")
            results = [f1.result(), f2.result()]

        success_count = sum(1 for s, _ in results if s)
        fail_count = sum(1 for s, _ in results if not s)

        self.assertEqual(success_count, 1, "Exactly one bet must succeed")
        self.assertEqual(fail_count, 1, "Exactly one bet must fail due to insufficient funds")

        # Balance must never drop below 0 (1000 - 800 = 200)
        wallet = database.get_or_create_wallet(self.user_id)
        self.assertEqual(wallet["balance"], 200)
        self.assertGreaterEqual(wallet["balance"], 0)

    def test_p9_atom_05_idempotent_duplicate_submission(self):
        """P9-ATOM-05: Submitting identical key and payload returns existing bet_id without extra debit."""
        key = "idemp-key-001"
        payload = [{"match_id": self.match1_id, "market_id": 951, "selection_id": 9511, "outcome": "home", "odds": 1.80}]

        s1, res1 = database.place_user_bet(self.user_id, 100, payload, idempotency_key=key)
        self.assertTrue(s1)

        wallet_after_first = database.get_or_create_wallet(self.user_id)["balance"]
        self.assertEqual(wallet_after_first, 900)

        # Second submission with same key & payload
        s2, res2 = database.place_user_bet(self.user_id, 100, payload, idempotency_key=key)
        self.assertTrue(s2)
        self.assertEqual(res1, res2)

        # Balance must still be 900 (no duplicate debit)
        wallet_after_second = database.get_or_create_wallet(self.user_id)["balance"]
        self.assertEqual(wallet_after_second, 900)

    def test_p9_atom_06_idempotency_key_reused_with_different_payload(self):
        """P9-ATOM-06: Submitting same key with modified stake/payload is rejected."""
        key = "idemp-key-002"
        payload1 = [{"match_id": self.match1_id, "market_id": 951, "selection_id": 9511, "outcome": "home", "odds": 1.80}]
        payload2 = [{"match_id": self.match2_id, "market_id": 952, "selection_id": 9521, "outcome": "home", "odds": 2.00}]

        s1, res1 = database.place_user_bet(self.user_id, 100, payload1, idempotency_key=key)
        self.assertTrue(s1)

        # Same key, different payload
        s2, res2 = database.place_user_bet(self.user_id, 100, payload2, idempotency_key=key)
        self.assertFalse(s2)
        self.assertIsInstance(res2, dict)
        self.assertEqual(res2.get("error"), "IDEMPOTENCY_KEY_REUSED")


if __name__ == "__main__":
    unittest.main()
