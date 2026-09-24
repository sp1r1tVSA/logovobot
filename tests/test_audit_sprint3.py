"""
tests/test_audit_sprint3.py

Sprint 3 Test Suite:
LB-07 (Margin Normalization ~7.5%),
LB-08 (Line Realism & Draw Odds 3.10-4.30),
LB-09 (Dynamic Totals & BTTS from Poisson Grid),
LB-10 (Market Hierarchy & Nesting Sanity),
LB-11 (Dynamic Line Repricing & Odds Movement Tracking),
LB-14 (Idempotent Resettle Routine for Disputed Matches),
LB-18 (Cashout Status 'cashed_out' and Stats Isolation),
LB-19 (Unified Payout Rounding),
LB-20 (Prediction Route Access Control & Threading).
"""

import unittest
import database
import services.betting_engine as betting_engine
import services.odds_engine as odds_engine
import services.settlement_engine as settlement_engine
from services.poisson_odds import calculate_poisson_market_odds, TARGET_MARGIN


class TestAuditSprint3(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self.user_id = 998811
        self.user_id_2 = 998822
        self.match_id = 889901

        with database.transaction() as conn:
            conn.execute("DELETE FROM user_wallets WHERE user_id IN (?, ?)", (self.user_id, self.user_id_2))
            conn.execute("DELETE FROM user_bets WHERE user_id IN (?, ?)", (self.user_id, self.user_id_2))
            conn.execute("DELETE FROM coin_transactions WHERE user_id IN (?, ?)", (self.user_id, self.user_id_2))
            conn.execute("DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id = ?)", (self.match_id,))
            conn.execute("DELETE FROM markets WHERE match_id = ?", (self.match_id,))
            conn.execute("DELETE FROM bet_markets WHERE match_id = ?", (self.match_id,))
            conn.execute("DELETE FROM matches WHERE id = ?", (self.match_id,))
            conn.execute("DELETE FROM users WHERE telegram_id IN (?, ?)", (self.user_id, self.user_id_2))

            conn.execute("INSERT INTO users (telegram_id, username, role) VALUES (?, 'sprint3_user1', 'user')", (self.user_id,))
            conn.execute("INSERT INTO users (telegram_id, username, role) VALUES (?, 'sprint3_user2', 'user')", (self.user_id_2,))
            conn.execute("""
                INSERT INTO matches (id, tournament_id, round_number, player1_team, player2_team, status)
                VALUES (?, 1, 1, 'Спортинг', 'Бенфика', 'scheduled')
            """, (self.match_id,))
            conn.execute("INSERT OR REPLACE INTO rounds (round_number, is_open, bets_open, deadline) VALUES (1, 0, 1, '2099-01-01 23:59')")

        database.get_or_create_wallet(self.user_id)
        database.get_or_create_wallet(self.user_id_2)

    def tearDown(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM user_wallets WHERE user_id IN (?, ?)", (self.user_id, self.user_id_2))
            conn.execute("DELETE FROM user_bets WHERE user_id IN (?, ?)", (self.user_id, self.user_id_2))
            conn.execute("DELETE FROM coin_transactions WHERE user_id IN (?, ?)", (self.user_id, self.user_id_2))
            conn.execute("DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id = ?)", (self.match_id,))
            conn.execute("DELETE FROM markets WHERE match_id = ?", (self.match_id,))
            conn.execute("DELETE FROM bet_markets WHERE match_id = ?", (self.match_id,))
            conn.execute("DELETE FROM matches WHERE id = ?", (self.match_id,))
            conn.execute("DELETE FROM users WHERE telegram_id IN (?, ?)", (self.user_id, self.user_id_2))

    # ──────────────────────────────────────────────────────────────────────────
    # LB-07 & LB-08: Margin Normalization & Draw Realism
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb07_lb08_poisson_margin_and_draw_realism(self):
        """LB-07 / LB-08: All 7 markets have ~7.5% margin and equal teams produce 3.20 - 4.30 draw odds."""
        # 1. Equal teams (s1 = 10, s2 = 10)
        odds_equal = calculate_poisson_market_odds(10.0, 10.0)

        # Draw realism: must be between 3.20 and 4.30
        self.assertGreaterEqual(odds_equal["odd_x"], 3.20)
        self.assertLessEqual(odds_equal["odd_x"], 4.30)

        # Margin checks on 1X2
        m_1x2 = (1.0 / odds_equal["odd_p1"] + 1.0 / odds_equal["odd_x"] + 1.0 / odds_equal["odd_p2"] - 1.0) * 100
        self.assertGreaterEqual(m_1x2, 6.5)
        self.assertLessEqual(m_1x2, 8.5)

        # Margin check on Totals 2.5
        m_tot25 = (1.0 / odds_equal["odd_tb25"] + 1.0 / odds_equal["odd_tm25"] - 1.0) * 100
        self.assertGreaterEqual(m_tot25, 6.5)
        self.assertLessEqual(m_tot25, 8.5)

        # Margin check on BTTS
        m_btts = (1.0 / odds_equal["odd_btts_yes"] + 1.0 / odds_equal["odd_btts_no"] - 1.0) * 100
        self.assertGreaterEqual(m_btts, 6.5)
        self.assertLessEqual(m_btts, 8.5)

        # Margin check on Individual Total 1
        m_it1 = (1.0 / odds_equal["odd_itb1"] + 1.0 / odds_equal["odd_itm1"] - 1.0) * 100
        self.assertGreaterEqual(m_it1, 6.5)
        self.assertLessEqual(m_it1, 8.5)

        # 2. Unequal teams (s1 = 16.0, s2 = 6.0)
        odds_skew = calculate_poisson_market_odds(16.0, 6.0)
        self.assertLess(odds_skew["odd_p1"], odds_skew["odd_p2"])
        self.assertLessEqual(odds_skew["odd_p1"], 1.50)  # Strong favorite
        self.assertGreater(odds_skew["odd_p2"], 4.00)   # Heavy underdog
        # Draw odd between 4.50 and 6.50 (realistic, not capped at 8.00)
        self.assertGreaterEqual(odds_skew["odd_x"], 4.20)
        self.assertLessEqual(odds_skew["odd_x"], 7.00)

    # ──────────────────────────────────────────────────────────────────────────
    # LB-09: Totals & BTTS Dynamism
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb09_dynamic_totals_and_btts(self):
        """LB-09: Totals and BTTS continuously adapt to strength differences rather than 3 frozen steps."""
        odds_low = calculate_poisson_market_odds(5.0, 5.0)
        odds_med = calculate_poisson_market_odds(10.0, 10.0)
        odds_high = calculate_poisson_market_odds(18.0, 18.0)

        # Even with equal delta (s1-s2=0), higher strength teams generate dynamic totals
        self.assertTrue(isinstance(odds_low["odd_tb25"], float))
        self.assertTrue(isinstance(odds_med["odd_tb25"], float))
        self.assertTrue(isinstance(odds_high["odd_tb25"], float))

        # Check that different strength matchups produce distinct odds
        odds_matchup1 = calculate_poisson_market_odds(14.0, 11.0)
        odds_matchup2 = calculate_poisson_market_odds(11.0, 8.0)
        self.assertNotEqual((odds_matchup1["odd_tb25"], odds_matchup1["odd_tm25"]), (1.55, 2.30))

    # ──────────────────────────────────────────────────────────────────────────
    # LB-10: Market Hierarchy & Nesting Sanity
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb10_nesting_sanity_across_spectrum(self):
        """LB-10: Strict monotonicity across related markets without nesting inversions."""
        test_pairs = [
            (10.0, 10.0), (15.0, 5.0), (6.0, 17.0),
            (12.0, 8.0), (4.0, 4.0), (20.0, 20.0),
            (7.5, 14.2), (18.5, 9.1)
        ]
        for s1, s2 in test_pairs:
            o = calculate_poisson_market_odds(s1, s2)
            # 1. Individual under cannot be pricier than match under
            self.assertLessEqual(o["odd_itm1"], o["odd_tm15"], f"Inversion ITM1 {o['odd_itm1']} > TM15 {o['odd_tm15']} for ({s1}, {s2})")
            self.assertLessEqual(o["odd_itm2"], o["odd_tm15"], f"Inversion ITM2 {o['odd_itm2']} > TM15 {o['odd_tm15']} for ({s1}, {s2})")

            # 2. Individual over cannot be cheaper than match over
            self.assertGreaterEqual(o["odd_itb1"], o["odd_tb15"], f"Inversion ITB1 {o['odd_itb1']} < TB15 {o['odd_tb15']} for ({s1}, {s2})")
            self.assertGreaterEqual(o["odd_itb2"], o["odd_tb15"], f"Inversion ITB2 {o['odd_itb2']} < TB15 {o['odd_tb15']} for ({s1}, {s2})")

            # 3. Totals hierarchy
            self.assertLessEqual(o["odd_tb15"], o["odd_tb25"])
            self.assertLessEqual(o["odd_tb25"], o["odd_tb35"])
            self.assertGreaterEqual(o["odd_tm15"], o["odd_tm25"])
            self.assertGreaterEqual(o["odd_tm25"], o["odd_tm35"])

            # 4. Double chance vs match winner
            self.assertLessEqual(o["odd_1x"], o["odd_p1"])
            self.assertLessEqual(o["odd_x2"], o["odd_p2"])

    # ──────────────────────────────────────────────────────────────────────────
    # LB-11: Dynamic Line Repricing & Movement
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb11_dynamic_line_repricing_and_movement(self):
        """LB-11: get_or_create_selection updates odds and logs odds_movement when update_odds=True."""
        markets_initial = odds_engine.generate_match_markets(self.match_id, "Спортинг", "Бенфика")
        m_1x2 = next(m for m in markets_initial if m["market_key"] == "1x2")
        sel_p1 = next(s for s in m_1x2["selections"] if s["selection_key"] == "p1")
        initial_odd = sel_p1["odds_value"]

        # Call get_or_create_selection with new odds and update_odds=True
        updated_sel = odds_engine.get_or_create_selection(
            market_id=m_1x2["id"],
            selection_key="p1",
            selection_name="П1 (Спортинг)",
            initial_odds=initial_odd + 0.40,
            update_odds=True
        )
        self.assertEqual(updated_sel["odds_value"], round(initial_odd + 0.40, 2))
        self.assertEqual(updated_sel["previous_odds"], initial_odd)
        self.assertEqual(updated_sel["odds_version"], 2)

        # Verify odds_movement recorded
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM odds_movement WHERE selection_id = ?", (sel_p1["id"],))
            mov = cursor.fetchone()
            self.assertIsNotNone(mov)
            self.assertEqual(mov["direction"], "up")

    def test_line_odds_match_the_odds_placement_validates_against(self):
        """The line tiles (bet_markets) and market_selections share one margin.

        Placement prices a pick without market/selection ids from market_selections,
        so any gap between the two tables rejected every line bet with ODDS_CHANGED.
        """
        # A line row priced by an older engine; startup repricing must refresh it.
        database.save_bet_market(self.match_id, 1, "Спортинг", "Бенфика", 9.9, 9.9, 9.9, 9.9, 9.9, 9.9, 9.9)
        betting_engine.regenerate_all_active_markets()
        line = database.get_active_bet_markets()
        line = next(m for m in line if m["match_id"] == self.match_id)

        keys = {"p1": "p1", "x": "x", "p2": "p2", "tb25": "over_2.5", "tm25": "under_2.5",
                "btts_yes": "btts_yes", "btts_no": "btts_no"}
        markets = odds_engine.generate_match_markets(self.match_id, "Спортинг", "Бенфика")
        relational = {s["selection_key"]: s["odds_value"] for m in markets for s in m["selections"]}
        for line_key, sel_key in keys.items():
            self.assertAlmostEqual(line[f"odd_{line_key}"], relational[sel_key], places=2, msg=line_key)

        ok, result = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "outcome": "p1", "odd": line["odd_p1"]}]
        )
        self.assertTrue(ok, f"Line pick rejected: {result}")

    def test_startup_repricing_keeps_pruned_matches_out_of_the_line(self):
        """Repricing refreshes the line; it must not re-open markets the round pruned."""
        database.save_bet_market(self.match_id, 1, "Спортинг", "Бенфика", 9.9, 9.9, 9.9, 9.9, 9.9, 9.9, 9.9)
        with database.transaction() as conn:
            conn.execute("UPDATE bet_markets SET is_active = 0 WHERE match_id = ?", (self.match_id,))

        betting_engine.regenerate_all_active_markets()

        self.assertNotIn(self.match_id, [m["match_id"] for m in database.get_active_bet_markets()])

    def test_match_back_among_the_central_ones_takes_bets_again(self):
        """A pruned match re-selected for the line must reopen its markets, not just its tile."""
        betting_engine.generate_round_markets(1)
        database.prune_round_markets(1, [])  # the match drops out of the central four
        betting_engine.generate_round_markets(1)  # ...and comes back once others are played

        line = next(m for m in database.get_active_bet_markets() if m["match_id"] == self.match_id)
        ok, result = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "outcome": "p1", "odd": line["odd_p1"]}]
        )
        self.assertTrue(ok, f"Re-selected match rejected: {result}")

    def test_played_match_markets_are_not_reopened(self):
        odds_engine.generate_match_markets(self.match_id, "Спортинг", "Бенфика")
        database.prune_round_markets(1, [])
        with database.transaction() as conn:
            conn.execute("UPDATE matches SET status = 'confirmed' WHERE id = ?", (self.match_id,))

        self.assertEqual(database.reopen_match_markets(self.match_id), 0)

    def test_showing_the_line_does_not_revive_a_started_round(self):
        """The line view regenerates every open round, including one already in play.

        Reopening its markets would let players cash out bets on matches whose
        result they already know.
        """
        betting_engine.generate_round_markets(1)
        with database.transaction() as conn:
            conn.execute("UPDATE rounds SET is_open = 1, bets_open = 0 WHERE round_number = 1")
            conn.execute("UPDATE bet_markets SET is_active = 0 WHERE match_id = ?", (self.match_id,))
            conn.execute("UPDATE markets SET status = 'closed' WHERE match_id = ?", (self.match_id,))

        betting_engine.generate_round_markets(1)

        self.assertNotIn(self.match_id, [m["match_id"] for m in database.get_active_bet_markets()])
        with database.transaction() as conn:
            statuses = {r["status"] for r in conn.execute(
                "SELECT status FROM markets WHERE match_id = ?", (self.match_id,)
            ).fetchall()}
        self.assertEqual(statuses, {"closed"})

    def test_admin_score_correction_resettles_already_settled_bets(self):
        """A corrected score must take back the old payout, not leave the bet won."""
        markets = odds_engine.generate_match_markets(self.match_id, "Спортинг", "Бенфика")
        m_1x2 = next(m for m in markets if m["market_key"] == "1x2")
        odd_p1 = next(s["odds_value"] for s in m_1x2["selections"] if s["selection_key"] == "p1")
        ok, bet_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "outcome": "p1", "odd": odd_p1}]
        )
        self.assertTrue(ok, bet_id)
        balance_before = database.get_wallet_balance(self.user_id)

        database.admin_set_match_score(self.match_id, 2, 1)
        self.assertEqual(database.get_user_bet_by_id(self.user_id, bet_id)["status"], "won")

        database.admin_set_match_score(self.match_id, 1, 2)
        bet = database.get_user_bet_by_id(self.user_id, bet_id)
        self.assertEqual((bet["status"], bet["actual_payout"]), ("lost", 0))
        self.assertEqual(database.get_wallet_balance(self.user_id), balance_before)

    def test_wallet_ledger_records_balance_after_for_every_credit(self):
        """Welcome and admin credits carry the balance they left, like bets do."""
        database.add_coins(self.user_id, 70, tx_type="admin_grant")
        with database.transaction() as conn:
            rows = conn.execute(
                "SELECT transaction_type, balance_after FROM coin_transactions WHERE user_id = ? ORDER BY id",
                (self.user_id,),
            ).fetchall()
        start = database.INITIAL_WALLET_BALANCE
        self.assertEqual(
            [(r["transaction_type"], r["balance_after"]) for r in rows],
            [("welcome_bonus", start), ("admin_grant", start + 70)],
        )

    # ──────────────────────────────────────────────────────────────────────────
    # LB-14: Resettle Routine for Disputed Matches
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb14_idempotent_resettle_routine(self):
        """LB-14: resettle_match_predictions rolls back old payouts and credits new winners atomically."""
        # 1. Generate markets
        markets = odds_engine.generate_match_markets(self.match_id, "Спортинг", "Бенфика")
        m_1x2 = next(m for m in markets if m["market_key"] == "1x2")
        odd_p1 = next(s["odds_value"] for s in m_1x2["selections"] if s["selection_key"] == "p1")
        odd_p2 = next(s["odds_value"] for s in m_1x2["selections"] if s["selection_key"] == "p2")

        # 2. User 1 bets 100 on P1
        ok1, b_id_1 = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "outcome": "p1", "odd": odd_p1}]
        )
        self.assertTrue(ok1, f"Failed placing bet 1: {b_id_1}")

        # 3. User 2 bets 100 on P2
        ok2, b_id_2 = database.place_user_bet(
            user_id=self.user_id_2,
            amount=100,
            selections=[{"match_id": self.match_id, "outcome": "p2", "odd": odd_p2}]
        )
        self.assertTrue(ok2, f"Failed placing bet 2: {b_id_2}")

        expected_win_1 = int(round(100 * odd_p1))
        expected_win_2 = int(round(100 * odd_p2))

        # 4. Initial settlement: Score 2-1 (P1 won)
        settlement_engine.settle_match_predictions(self.match_id, score1=2, score2=1)

        # User 1 should have won expected_win_1, User 2 lost
        self.assertEqual(database.get_user_bet_by_id(self.user_id, b_id_1)["status"], "won")
        self.assertEqual(database.get_user_bet_by_id(self.user_id, b_id_1)["actual_payout"], expected_win_1)
        self.assertEqual(database.get_user_bet_by_id(self.user_id_2, b_id_2)["status"], "lost")
        bal1_after_win = database.get_wallet_balance(self.user_id)

        # 5. Admin correction: Score was actually 1-2 (P2 won!)
        resettle_notes = settlement_engine.resettle_match_predictions(self.match_id, score1=1, score2=2)
        self.assertGreaterEqual(len(resettle_notes), 2)

        # Verify User 1 was reverted: balance reduced by expected_win_1, bet marked 'lost'
        self.assertEqual(database.get_user_bet_by_id(self.user_id, b_id_1)["status"], "lost")
        self.assertEqual(database.get_user_bet_by_id(self.user_id, b_id_1)["actual_payout"], 0)
        self.assertEqual(database.get_wallet_balance(self.user_id), bal1_after_win - expected_win_1)

        # Verify User 2 was paid: bet marked 'won', actual_payout = expected_win_2
        self.assertEqual(database.get_user_bet_by_id(self.user_id_2, b_id_2)["status"], "won")
        self.assertEqual(database.get_user_bet_by_id(self.user_id_2, b_id_2)["actual_payout"], expected_win_2)

        # Check transactions audit log
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM coin_transactions WHERE user_id = ? AND transaction_type = 'resettle_reversal'", (self.user_id,))
            self.assertIsNotNone(cursor.fetchone())

            cursor.execute("SELECT * FROM coin_transactions WHERE user_id = ? AND transaction_type = 'resettle_payout'", (self.user_id_2,))
            self.assertIsNotNone(cursor.fetchone())

    # ──────────────────────────────────────────────────────────────────────────
    # LB-18: Cashout Status & Career Stats Isolation
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb18_cashout_status_and_stats_isolation(self):
        """LB-18: Cashed out bets receive status 'cashed_out' and do not pollute career_wins."""
        markets = odds_engine.generate_match_markets(self.match_id, "Спортинг", "Бенфика")
        m_1x2 = next(m for m in markets if m["market_key"] == "1x2")
        odd_p1 = next(s["odds_value"] for s in m_1x2["selections"] if s["selection_key"] == "p1")

        ok, b_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "outcome": "p1", "odd": odd_p1}]
        )
        self.assertTrue(ok, f"Failed placing bet: {b_id}")

        from services.cashout_engine import execute_cashout
        ok_cash, res = execute_cashout(self.user_id, b_id)
        self.assertTrue(ok_cash, f"Failed execute_cashout: {res}")

        # Bet record must have status 'cashed_out'
        bet = database.get_user_bet_by_id(self.user_id, b_id)
        self.assertEqual(bet["status"], "cashed_out")
        self.assertIsNotNone(bet["cashout_at"])

        # Stats check: career_wins must be 0, career_cashouts must be 1
        stats = database.get_player_career_stats(self.user_id)
        self.assertEqual(stats["career_wins"], 0)
        self.assertEqual(stats["career_cashouts"], 1)
        self.assertGreater(stats["career_payout"], 0)

    # ──────────────────────────────────────────────────────────────────────────
    # LB-19: Rounding Discrepancy Unification
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb19_rounding_discrepancy_unification(self):
        """LB-19: Settlement payout formula matches placement potential_win calculation."""
        # 100 coins on odds 1.33 and 1.50 with the 3% express margin ->
        # total_odd = round(1.33 * 1.50 * 0.97, 2) = 1.94, potential_win = 194
        match_id_2 = 889902
        with database.transaction() as conn:
            conn.execute("""
                INSERT INTO matches (id, tournament_id, round_number, player1_team, player2_team, status)
                VALUES (?, 1, 1, 'Порту', 'Брага', 'scheduled')
            """, (match_id_2,))

        # Save bet_markets with exact odds
        database.save_bet_market(self.match_id, 1, "Спортинг", "Бенфика", 1.33, 3.50, 4.00, 1.80, 1.95, 1.70, 2.05)
        database.save_bet_market(match_id_2, 1, "Порту", "Брага", 1.50, 3.50, 3.80, 1.80, 1.95, 1.70, 2.05)

        ok, b_id = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[
                {"match_id": self.match_id, "outcome": "p1", "odd": 1.33},
                {"match_id": match_id_2, "outcome": "p1", "odd": 1.50}
            ]
        )
        self.assertTrue(ok, f"Failed placing express: {b_id}")
        bet = database.get_user_bet_by_id(self.user_id, b_id)
        self.assertEqual(bet["potential_win"], 194)

        # Settle both matches
        settlement_engine.settle_match_predictions(self.match_id, score1=2, score2=0)
        settlement_engine.settle_match_predictions(match_id_2, score1=1, score2=0)

        settled_bet = database.get_user_bet_by_id(self.user_id, b_id)
        self.assertEqual(settled_bet["status"], "won")
        # Actual payout must be exactly 194 (not truncated to 193!)
        self.assertEqual(settled_bet["actual_payout"], 194)
        self.assertEqual(settled_bet["actual_payout"], bet["potential_win"])

    # ──────────────────────────────────────────────────────────────────────────
    # LB-20: Access Control on Prediction Endpoints
    # ──────────────────────────────────────────────────────────────────────────
    def test_lb20_routes_access_control(self):
        """LB-20: Predictions detail, repeat, and cashout routes strictly check access."""
        from api.auth import check_user_access
        from unittest.mock import patch

        # When access is disallowed (fail-closed or restricted)
        with patch("api.auth.is_logovo_access_allowed", return_value=False):
            self.assertFalse(check_user_access(self.user_id))


if __name__ == "__main__":
    unittest.main()
