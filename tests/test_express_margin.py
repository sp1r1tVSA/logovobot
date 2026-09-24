"""
tests/test_express_margin.py

Two brakes on coin inflow from expresses:

* express margin — an express pays its odds product minus `express_margin_pct`
  (3% by default) for every leg after the first. The rate is stored on the bet,
  so settlement and cashout use the rate the coupon was accepted at, and bets
  from before the margin (NULL) keep the pure product;
* identical-coupon cap — coupons with the same set of selections share one
  `max_payout` between them, so a max-payout express cannot be multiplied by
  placing it again (bets #692/#693).
"""

import os
import tempfile
import unittest

import database
import services.settlement_engine as settlement_engine
from services.betting_limits import BettingLimitsService
from services.cashout_engine import calculate_cashout_offer
from services.risk_engine import RiskEngine


class TestExpressOddMath(unittest.TestCase):
    def test_single_has_no_margin(self):
        self.assertEqual(database.express_odd([2.5], 3), 2.5)

    def test_margin_per_extra_leg(self):
        # 2 × 2 × 2 = 8, three legs → two extra legs → 8 × 0.97² = 7.5272
        self.assertEqual(database.express_odd([2.0, 2.0, 2.0], 3), 7.53)

    def test_zero_or_none_is_pure_product(self):
        self.assertEqual(database.express_odd([2.0, 3.0], 0), 6.0)
        self.assertEqual(database.express_odd([2.0, 3.0], None), 6.0)

    def test_margin_is_clamped(self):
        capped = database.express_odd([2.0, 2.0], database.MAX_EXPRESS_MARGIN_PCT)
        self.assertEqual(database.express_odd([2.0, 2.0], 99), capped)
        self.assertEqual(database.express_odd([2.0, 2.0], -5), 4.0)

    def test_winning_coupon_never_pays_less_than_stake(self):
        self.assertEqual(database.express_odd([1.01, 1.01, 1.01, 1.01], 20), 1.01)

    def test_selection_identity_normalizes_outcome_aliases(self):
        self.assertEqual(database.selection_identity(5, " P1 "), database.selection_identity(5, "p1"))
        self.assertEqual(database.selection_identity(5, "p1", 77), ("s", 77))


class _BetFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.user_id = 950001
        self.division_id = 1
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'margin_user', 'user')",
                (self.user_id,)
            )
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Season Margin', 'active')")
            self.season_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) VALUES (?, 1, ?, 0, 1)",
                (self.division_id, self.season_id)
            )
        self.match_ids = [self._add_match(t1, t2) for t1, t2 in
                          (("Лидс", "Ренн"), ("Брест", "Монако"), ("Лион", "Ницца"))]
        database.get_or_create_wallet(self.user_id)
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 90000 WHERE user_id = ?", (self.user_id,))

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass

    def _add_match(self, t1, t2, odd=2.00):
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO matches (division_id, season_id, round_number, player1_team, player2_team, status)
                VALUES (?, ?, 1, ?, ?, 'pending')
            """, (self.division_id, self.season_id, t1, t2))
            match_id = cursor.lastrowid
            cursor.execute("""
                INSERT INTO markets (match_id, market_key, market_name, status)
                VALUES (?, '1x2', 'Match Winner', 'open')
            """, (match_id,))
            cursor.execute("""
                INSERT INTO market_selections (market_id, selection_key, selection_name, odds_value, status, odds_version)
                VALUES (?, 'p1', 'Home Win', ?, 'active', 1)
            """, (cursor.lastrowid, odd))
        return match_id

    def _slip(self, *match_ids):
        return [{"match_id": m, "outcome": "p1"} for m in match_ids]

    def _bet(self, bet_id):
        with database.transaction() as conn:
            return conn.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,)).fetchone()

    def _set_margin(self, pct):
        BettingLimitsService.set_limit("global", 0, "express_margin_pct", pct)


class TestExpressMarginPlacement(_BetFixture):
    def test_express_stores_margin_and_reduced_odd(self):
        ok, bet_id = database.place_user_bet(self.user_id, 100, self._slip(*self.match_ids))
        self.assertTrue(ok, bet_id)
        bet = self._bet(bet_id)
        self.assertEqual(bet["express_margin_pct"], 3)
        self.assertAlmostEqual(bet["total_odd"], 7.53)
        self.assertEqual(bet["potential_win"], 753)

    def test_single_is_untouched(self):
        ok, bet_id = database.place_user_bet(self.user_id, 100, self._slip(self.match_ids[0]))
        self.assertTrue(ok, bet_id)
        bet = self._bet(bet_id)
        self.assertIsNone(bet["express_margin_pct"])
        self.assertEqual(bet["potential_win"], 200)

    def test_panel_setting_changes_the_margin(self):
        self._set_margin(0)
        ok, bet_id = database.place_user_bet(self.user_id, 100, self._slip(*self.match_ids[:2]))
        self.assertTrue(ok, bet_id)
        self.assertEqual(self._bet(bet_id)["potential_win"], 400)

    def test_payout_cap_uses_the_margined_odd(self):
        # 2 × 2 × 0.97 = 3.88 → 10 000 / 3.88 = 2577
        decision = RiskEngine.evaluate_bet(
            user_id=self.user_id, amount=2600, selections=self._slip(*self.match_ids[:2]),
            division_id=self.division_id,
        )
        self.assertEqual(decision.reason, "MAX_PAYOUT")
        self.assertEqual(decision.max_allowed_stake, 2577)


class TestExpressMarginSettlement(_BetFixture):
    def _settle_all(self):
        for match_id in self.match_ids:
            settlement_engine.settle_match_predictions(match_id, 2, 0, "finished")

    def test_express_pays_with_the_stored_margin(self):
        ok, bet_id = database.place_user_bet(self.user_id, 100, self._slip(*self.match_ids))
        self.assertTrue(ok, bet_id)
        # Changing the setting later must not touch an accepted coupon.
        self._set_margin(10)
        self._settle_all()
        bet = self._bet(bet_id)
        self.assertEqual(bet["status"], "won")
        self.assertEqual(bet["actual_payout"], 753)

    def test_bet_without_margin_pays_pure_product(self):
        ok, bet_id = database.place_user_bet(self.user_id, 100, self._slip(*self.match_ids))
        self.assertTrue(ok, bet_id)
        with database.transaction() as conn:
            conn.execute("UPDATE user_bets SET express_margin_pct = NULL WHERE id = ?", (bet_id,))
        self._settle_all()
        self.assertEqual(self._bet(bet_id)["actual_payout"], 800)

    def test_voided_leg_drops_out_of_the_margin(self):
        ok, bet_id = database.place_user_bet(self.user_id, 100, self._slip(*self.match_ids))
        self.assertTrue(ok, bet_id)
        settlement_engine.settle_match_predictions(self.match_ids[0], 0, 0, "cancelled")
        for match_id in self.match_ids[1:]:
            settlement_engine.settle_match_predictions(match_id, 2, 0, "finished")
        bet = self._bet(bet_id)
        self.assertEqual(bet["status"], "won")
        # Two live legs: 2 × 2 × 0.97 = 3.88
        self.assertEqual(bet["actual_payout"], 388)


class TestIdenticalCouponCap(_BetFixture):
    def test_repeat_of_a_max_coupon_is_rejected(self):
        slip = self._slip(*self.match_ids[:2])
        ok, first = database.place_user_bet(self.user_id, 2577, slip)
        self.assertTrue(ok, first)
        ok, res = database.place_user_bet(self.user_id, 2577, slip)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "MAX_PAYOUT_EXCEEDED")

    def test_repeat_is_limited_to_what_is_left(self):
        slip = self._slip(*self.match_ids[:2])
        ok, first = database.place_user_bet(self.user_id, 1000, slip)  # 3 880
        self.assertTrue(ok, first)
        decision = RiskEngine.evaluate_bet(
            user_id=self.user_id, amount=2000, selections=list(reversed(slip)),
            division_id=self.division_id,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "MAX_PAYOUT")
        # (10 000 − 3 880) / 3.88 = 1577
        self.assertEqual(decision.max_allowed_stake, 1577)
        self.assertEqual(decision.details["identical_payout"], 3880)

    def test_different_set_is_not_affected(self):
        ok, first = database.place_user_bet(self.user_id, 2577, self._slip(*self.match_ids[:2]))
        self.assertTrue(ok, first)
        ok, second = database.place_user_bet(self.user_id, 2577, self._slip(*self.match_ids[1:]))
        self.assertTrue(ok, second)
        ok, third = database.place_user_bet(self.user_id, 1000, self._slip(*self.match_ids))
        self.assertTrue(ok, third)

    def test_settled_coupon_frees_the_cap(self):
        slip = self._slip(self.match_ids[0])
        ok, first = database.place_user_bet(self.user_id, 5000, slip)
        self.assertTrue(ok, first)
        with database.transaction() as conn:
            conn.execute("UPDATE user_bets SET status = 'lost' WHERE id = ?", (first,))
        ok, second = database.place_user_bet(self.user_id, 5000, slip)
        self.assertTrue(ok, second)

    def test_legacy_coupon_is_ignored(self):
        slip = self._slip(self.match_ids[0])
        ok, first = database.place_user_bet(self.user_id, 5000, slip)
        self.assertTrue(ok, first)
        with database.transaction() as conn:
            conn.execute("UPDATE user_bets SET legacy_limits = 1 WHERE id = ?", (first,))
        ok, second = database.place_user_bet(self.user_id, 5000, slip)
        self.assertTrue(ok, second)


class TestCashoutMargin(unittest.TestCase):
    def test_express_factor_is_applied(self):
        items = [
            {"status": "pending", "odd": 2.0, "current_odd": 2.0},
            {"status": "pending", "odd": 2.0, "current_odd": 2.0},
        ]
        plain = calculate_cashout_offer(100, 400, items, margin=0.0)
        margined = calculate_cashout_offer(100, 388, items, margin=0.0, express_margin_pct=3)
        self.assertEqual(plain, (True, 100, None))
        self.assertEqual(margined, (True, 97, None))


if __name__ == "__main__":
    unittest.main()
