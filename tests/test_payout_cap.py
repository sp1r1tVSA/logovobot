"""
tests/test_payout_cap.py

Payout cap for new bets: one coupon (single or the whole express) wins at most
10,000 🪙, so the max stake is 10,000 / odd; a player's open (pending) bets may
carry at most 35,000 🪙 of potential win. Bets already in user_bets when the cap
shipped (migration 014, legacy_limits = 1) keep the old rules: they settle in
full and do not take up the open-exposure limit.
"""

import os
import tempfile
import unittest

import database
import services.settlement_engine as settlement_engine
from services.betting_limits import DEFAULT_MAX_OPEN_EXPOSURE, DEFAULT_MAX_PAYOUT
from services.risk_engine import RiskEngine


class TestPayoutCap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.user_id = 940001
        self.division_id = 1

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'cap_user', 'user')",
                (self.user_id,)
            )
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Season Cap', 'active')")
            season_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) VALUES (?, 1, ?, 0, 1)",
                (self.division_id, season_id)
            )
            self.match_ids = []
            for t1, t2 in (("Лидс", "Ренн"), ("Брест", "Монако"), ("Лион", "Ницца"), ("Ланс", "Лилль")):
                cursor.execute("""
                    INSERT INTO matches (division_id, season_id, round_number, player1_team, player2_team, status)
                    VALUES (?, ?, 1, ?, ?, 'pending')
                """, (self.division_id, season_id, t1, t2))
                match_id = cursor.lastrowid
                self.match_ids.append(match_id)
                cursor.execute("""
                    INSERT INTO markets (match_id, market_key, market_name, status)
                    VALUES (?, '1x2', 'Match Winner', 'open')
                """, (match_id,))
                cursor.execute("""
                    INSERT INTO market_selections (market_id, selection_key, selection_name, odds_value, status, odds_version)
                    VALUES (?, 'p1', 'Home Win', 2.00, 'active', 1)
                """, (cursor.lastrowid,))

        database.get_or_create_wallet(self.user_id)
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 90000 WHERE user_id = ?", (self.user_id,))

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass

    def _slip(self, *match_ids):
        return [{"match_id": m, "outcome": "p1"} for m in (match_ids or self.match_ids[:1])]

    def _insert_old_bet(self, potential_win=17722, amount=100):
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_bets (user_id, bet_type, amount, total_odd, potential_win, status)
                VALUES (?, 'single', ?, ?, ?, 'pending')
            """, (self.user_id, amount, round(potential_win / amount, 2), potential_win))
            bet_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status) VALUES (?, ?, 'p1', ?, 'pending')",
                (bet_id, self.match_ids[0], round(potential_win / amount, 2))
            )
        return bet_id

    def _rerun_legacy_migration(self):
        """Simulate deploying the cap onto a database that already holds bets."""
        with database.transaction() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = '014_payout_cap_legacy_bets'")
        database.init_db()

    def _legacy_flag(self, bet_id):
        with database.transaction() as conn:
            return conn.execute("SELECT legacy_limits FROM user_bets WHERE id = ?", (bet_id,)).fetchone()[0]

    # ─── Defaults ────────────────────────────────────────────────────────────
    def test_defaults(self):
        self.assertEqual(DEFAULT_MAX_PAYOUT, 10_000)
        self.assertEqual(DEFAULT_MAX_OPEN_EXPOSURE, 35_000)
        self.assertEqual(database._MAX_PAYOUT, 10_000)

    # ─── Per-bet cap ─────────────────────────────────────────────────────────
    def test_single_over_cap_is_rejected_with_max_stake(self):
        ok, res = database.place_user_bet(self.user_id, 5001, self._slip())
        self.assertFalse(ok)
        self.assertEqual(res["error"], "MAX_PAYOUT_EXCEEDED")
        self.assertEqual(res["max_allowed_stake"], 5000)
        self.assertEqual(res["max_payout"], 10_000)

    def test_single_exactly_at_cap_is_accepted(self):
        ok, bet_id = database.place_user_bet(self.user_id, 5000, self._slip())
        self.assertTrue(ok, bet_id)
        with database.transaction() as conn:
            row = conn.execute("SELECT potential_win, legacy_limits FROM user_bets WHERE id = ?", (bet_id,)).fetchone()
        self.assertEqual(row["potential_win"], 10_000)
        self.assertEqual(row["legacy_limits"], 0)

    def test_express_cap_applies_to_the_whole_coupon(self):
        # Express 2.00 × 2.00 × 0.97 = 3.88: the cap is 10,000 for the coupon, not per leg.
        decision = RiskEngine.evaluate_bet(
            user_id=self.user_id, amount=2600, selections=self._slip(*self.match_ids[:2]),
            division_id=self.division_id,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "MAX_PAYOUT")
        self.assertEqual(decision.max_allowed_stake, 2577)

        ok, bet_id = database.place_user_bet(self.user_id, 2577, self._slip(*self.match_ids[:2]))
        self.assertTrue(ok, bet_id)

    # ─── Open-exposure limit ─────────────────────────────────────────────────
    def test_bets_fill_the_open_limit(self):
        # Four different coupons: 10,000 + 10,000 + 10,000 + 5,000 = 35,000.
        # (Repeating a coupon would hit the identical-coupon cap instead.)
        for match_id in self.match_ids[:3]:
            self.assertTrue(database.place_user_bet(self.user_id, 5000, self._slip(match_id))[0])
        self.assertTrue(database.place_user_bet(self.user_id, 2500, self._slip(self.match_ids[3]))[0])
        self.assertEqual(database.get_user_open_exposure(self.user_id), 35_000)

        # A new set of selections, so only the open limit can stop it.
        ok, res = database.place_user_bet(self.user_id, 10, self._slip(*self.match_ids[:2]))
        self.assertFalse(ok)
        self.assertEqual(res["error"], "EXPOSURE_LIMIT")

    # ─── Bets placed before the cap ──────────────────────────────────────────
    def test_existing_bets_are_marked_legacy_on_deploy(self):
        old_bet = self._insert_old_bet()
        self._rerun_legacy_migration()
        self.assertEqual(self._legacy_flag(old_bet), 1)
        self.assertEqual(database.get_user_open_exposure(self.user_id), 0)

    def test_legacy_bet_does_not_block_new_bets(self):
        self._insert_old_bet()
        self._rerun_legacy_migration()
        self.assertTrue(database.place_user_bet(self.user_id, 5000, self._slip(self.match_ids[0]))[0])
        self.assertTrue(database.place_user_bet(self.user_id, 5000, self._slip(self.match_ids[1]))[0])

    def test_legacy_bet_still_pays_out_in_full(self):
        old_bet = self._insert_old_bet(potential_win=17722, amount=100)
        self._rerun_legacy_migration()
        settlement_engine.settle_match_predictions(self.match_ids[0], 2, 0, "finished")
        with database.transaction() as conn:
            row = conn.execute("SELECT status, actual_payout FROM user_bets WHERE id = ?", (old_bet,)).fetchone()
        self.assertEqual(row["status"], "won")
        self.assertEqual(row["actual_payout"], 17722)

    def test_migration_runs_once(self):
        database.init_db()
        ok, bet_id = database.place_user_bet(self.user_id, 100, self._slip())
        self.assertTrue(ok, bet_id)
        database.init_db()
        self.assertEqual(self._legacy_flag(bet_id), 0)


if __name__ == "__main__":
    unittest.main()
