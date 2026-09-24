"""
tests/test_audit_sprint2.py

Sprint 2 Test Suite:
- LB-03: MAX_DAILY_LOSS and MAX_OPEN_EXPOSURE limits enforcement.
- LB-05: Server-authoritative market net exposure calculation even without client market_id.
- LB-06: Server odds drift validation across all bets with client-supplied odd.
- LB-17: Outcome alias normalization (tb25 <-> over_2.5, btts_yes <-> yes).
- LB-23: Exception logging in BettingLimitsService.get_limit.
"""

import os
import tempfile
import unittest
import database
from services.betting_limits import BettingLimitsService
from services.risk_engine import RiskEngine


class TestAuditSprint2(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.user_id = 920001
        self.division_id = 1

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'sprint2_user', 'user')",
                (self.user_id,)
            )
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Season Sprint 2', 'active')")
            self.season_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) VALUES (?, 1, ?, 0, 1)",
                (self.division_id, self.season_id)
            )
            cursor.execute("""
                INSERT INTO matches (division_id, season_id, round_number, player1_team, player2_team, status)
                VALUES (?, ?, 1, 'Лидс', 'Ренн', 'pending')
            """, (self.division_id, self.season_id))
            self.match_id = cursor.lastrowid

            # Create relational market 1X2
            cursor.execute("""
                INSERT INTO markets (match_id, market_key, market_name, status)
                VALUES (?, '1x2', 'Match Winner', 'open')
            """, (self.match_id,))
            self.market_1x2_id = cursor.lastrowid
            cursor.execute("""
                INSERT INTO market_selections (market_id, selection_key, selection_name, odds_value, status, odds_version)
                VALUES (?, 'p1', 'Leeds Win', 2.00, 'active', 1)
            """, (self.market_1x2_id,))
            self.sel_p1_id = cursor.lastrowid

            # Create relational market Totals 2.5 with selection_key = 'over_2.5'
            cursor.execute("""
                INSERT INTO markets (match_id, market_key, market_name, status)
                VALUES (?, 'totals', 'Total Goals', 'open')
            """, (self.match_id,))
            self.market_totals_id = cursor.lastrowid
            cursor.execute("""
                INSERT INTO market_selections (market_id, selection_key, selection_name, odds_value, status, odds_version)
                VALUES (?, 'over_2.5', 'Over 2.5 Goals', 1.85, 'active', 1)
            """, (self.market_totals_id,))
            self.sel_tb25_id = cursor.lastrowid

            # Create legacy bet_markets for fallback
            cursor.execute("""
                INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, odd_tb25, odd_tm25, odd_btts_yes, odd_btts_no, is_active)
                VALUES (?, 1, 'Лидс', 'Ренн', 2.00, 3.20, 3.50, 1.85, 1.95, 1.70, 2.05, 1)
            """, (self.match_id,))

        database.get_or_create_wallet(self.user_id)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_wallets SET balance = 500000 WHERE user_id = ?", (self.user_id,))

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass

    # ─── LB-03: Daily Loss Limit ────────────────────────────────────────────────
    def test_01_max_daily_loss_enforced(self):
        """LB-03: User who accumulated losses reaching MAX_DAILY_LOSS is rejected."""
        # Set custom daily loss limit of 10,000
        BettingLimitsService.set_limit("user", self.user_id, "max_daily_loss", 10_000)

        # Simulate user lost 10,000 today
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_bets (user_id, amount, total_odd, potential_win, actual_payout, status, created_at, settled_at)
                VALUES (?, 10000, 2.0, 20000, 0, 'lost', datetime('now', '+3 hours'), datetime('now', '+3 hours'))
            """, (self.user_id,))

        # Placing a new bet must be rejected due to daily loss limit
        slip = [{"match_id": self.match_id, "outcome": "p1", "market_id": self.market_1x2_id, "selection_id": self.sel_p1_id}]
        ok, res = database.place_user_bet(self.user_id, 500, slip)
        self.assertFalse(ok)
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("error"), "DAILY_LOSS_LIMIT")

    def test_02_max_daily_loss_limited_when_partially_spent(self):
        """LB-03: User with partial daily loss is limited to remaining loss budget."""
        BettingLimitsService.set_limit("user", self.user_id, "max_daily_loss", 10_000)

        # Lost 8,000 today -> 2,000 remaining
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_bets (user_id, amount, total_odd, potential_win, actual_payout, status, created_at, settled_at)
                VALUES (?, 8000, 2.0, 16000, 0, 'lost', datetime('now', '+3 hours'), datetime('now', '+3 hours'))
            """, (self.user_id,))

        slip = [{"match_id": self.match_id, "outcome": "p1", "market_id": self.market_1x2_id, "selection_id": self.sel_p1_id}]
        # Betting 3,000 > remaining 2,000 -> LIMITED
        ok, res = database.place_user_bet(self.user_id, 3000, slip)
        self.assertFalse(ok)
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("error"), "DAILY_LOSS_LIMIT")
        self.assertEqual(res.get("max_allowed_stake"), 2000)

        # Betting 2,000 <= remaining 2,000 -> Allowed
        ok_valid, _ = database.place_user_bet(self.user_id, 2000, slip)
        self.assertTrue(ok_valid)

    # ─── LB-03: Open Exposure Limits ───────────────────────────────────────────
    def test_03_max_user_open_exposure_enforced(self):
        """LB-03: User whose pending bets reach MAX_OPEN_EXPOSURE is rejected."""
        # Default user open exposure is 20,000. Set custom 50,000 for test.
        BettingLimitsService.set_limit("user", self.user_id, "max_open_exposure", 50_000)

        # Pending bet with potential win 45,000
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_bets (user_id, amount, total_odd, potential_win, status, created_at)
                VALUES (?, 22500, 2.0, 45000, 'pending', datetime('now', '+3 hours'))
            """, (self.user_id,))

        # New bet with potential win 10,000 (stake 5,000 * odd 2.0 = 10,000) -> 45,000 + 10,000 = 55,000 > 50,000
        slip = [{"match_id": self.match_id, "outcome": "p1", "market_id": self.market_1x2_id, "selection_id": self.sel_p1_id}]
        ok, res = database.place_user_bet(self.user_id, 5000, slip)
        self.assertFalse(ok)
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("error"), "EXPOSURE_LIMIT")

    # ─── LB-05: Server-Authoritative Market Exposure Without Market ID ─────────
    def test_04_market_net_exposure_enforced_even_if_client_omits_market_id(self):
        """LB-05: Market net exposure check triggers even if client omits market_id from selection payload."""
        # Set market exposure limit to 5,000 (below the 10,000 payout cap,
        # so the market limit is what trips)
        BettingLimitsService.set_division_limits(
            division_id=self.division_id,
            limits={"market_exposure_limit": 5_000},
            updated_by=999
        )

        # Client submits payload WITHOUT market_id or selection_id
        # Server resolves market_id from database and checks server odds (2.00)
        # Stake 3,000 * 2.00 = 6,000 > 5,000 market exposure limit
        slip = [{"match_id": self.match_id, "outcome": "p1"}]
        decision = RiskEngine.evaluate_bet(
            user_id=self.user_id,
            amount=3000,
            selections=slip,
            division_id=self.division_id
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "EXPOSURE_LIMIT")

    # ─── LB-06: Odds Drift Validation On All Bets ──────────────────────────────
    def test_05_odds_drift_detected_on_legacy_selection_format(self):
        """LB-06: Client submitting stale odd without selection_id triggers ODDS_CHANGED."""
        # Server odd is 2.00, client passes 2.50 in legacy format
        slip = [{"match_id": self.match_id, "outcome": "p1", "odd": 2.50}]
        ok, res = database.place_user_bet(self.user_id, 100, slip)
        self.assertFalse(ok)
        self.assertIsInstance(res, dict)
        self.assertEqual(res.get("error"), "ODDS_CHANGED")
        self.assertEqual(res.get("old_odd"), 2.50)
        self.assertEqual(res.get("new_odd"), 2.00)

    def test_06_matching_odds_accepted_on_legacy_selection_format(self):
        """LB-06: Client submitting matching server odd is accepted."""
        slip = [{"match_id": self.match_id, "outcome": "p1", "odd": 2.00}]
        ok, res = database.place_user_bet(self.user_id, 100, slip)
        self.assertTrue(ok)
        self.assertIsInstance(res, int)

    # ─── LB-17: Outcome Key Aliases ────────────────────────────────────────────
    def test_07_outcome_key_aliases_tb25_and_over_2_5(self):
        """LB-17: 'tb25' resolves to relational 'over_2.5' seamlessly."""
        # Client sends 'tb25', DB relational market has 'over_2.5'
        slip = [{"match_id": self.match_id, "outcome": "tb25"}]
        decision = RiskEngine.evaluate_bet(
            user_id=self.user_id,
            amount=100,
            selections=slip,
            division_id=self.division_id
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.details.get("total_odd"), 1.85)

        # Place bet with 'tb25'
        ok, res = database.place_user_bet(self.user_id, 100, slip)
        self.assertTrue(ok)
        self.assertIsInstance(res, int)

    def test_08_outcome_key_aliases_btts_yes_and_yes(self):
        """LB-17: 'yes' resolves to legacy bet_markets 'odd_btts_yes'."""
        slip = [{"match_id": self.match_id, "outcome": "yes"}]
        decision = RiskEngine.evaluate_bet(
            user_id=self.user_id,
            amount=100,
            selections=slip,
            division_id=self.division_id
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.details.get("total_odd"), 1.70)

        ok, res = database.place_user_bet(self.user_id, 100, slip)
        self.assertTrue(ok)
        self.assertIsInstance(res, int)

    # ─── LB-23: Exception Logging in BettingLimitsService ──────────────────────
    def test_09_limits_service_logs_and_falls_back_on_db_error(self):
        """LB-23: get_limit safely falls back to default_value if DB query fails."""
        # Pass non-existent table or corrupted state
        val = BettingLimitsService.get_limit("corrupted_scope", -9999, "non_existent_key", default_value=42)
        self.assertEqual(val, 42)


if __name__ == "__main__":
    unittest.main()
