"""
tests/test_phase6_analytics.py

Tests for Phase 6G & 6H: Bettor Analytics, Division Leaderboards & Recommendations.
Ensures:
1. Strict ROI formula: ROI is None (NULL) when total_staked == 0, never 0.0.
2. Win rate is None when settled bets == 0.
3. Accurate profit, average odds, favorite/best/worst market, and recent form.
4. Capper leaderboard enforces MIN_LEADERBOARD_BETS (filters out 1-bet flukes).
5. Strict division and season isolation for leaderboard rankings.
6. Hot Matches scoring function and personalized recommendations.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from services.analytics_service import get_capper_leaderboard, get_user_betting_analytics
from services.recommendation_engine import get_hot_matches, get_user_recommendations


class TestPhase6Analytics(unittest.TestCase):

    def setUp(self) -> None:
        database.init_db()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bet_items WHERE bet_id IN (SELECT id FROM user_bets WHERE user_id >= 778000)")
            cursor.execute("DELETE FROM user_bets WHERE user_id >= 778000")
            cursor.execute("DELETE FROM user_wallets WHERE user_id >= 778000")
            cursor.execute("DELETE FROM user_progression WHERE user_id >= 778000")
            cursor.execute("DELETE FROM user_achievements WHERE user_id >= 778000")
            cursor.execute("DELETE FROM favorites WHERE user_id >= 778000")
            cursor.execute("DELETE FROM division_admins WHERE user_id >= 778000")
            cursor.execute("DELETE FROM admin_audit_log WHERE admin_id >= 778000")
            cursor.execute("DELETE FROM bet_items WHERE match_id >= 99400 OR match_id IN (SELECT id FROM matches WHERE player1_id >= 778000 OR player2_id >= 778000)")
            cursor.execute("DELETE FROM odds_movement WHERE match_id >= 99400 OR match_id IN (SELECT id FROM matches WHERE player1_id >= 778000 OR player2_id >= 778000)")
            cursor.execute("DELETE FROM live_match_states WHERE match_id >= 99400 OR match_id IN (SELECT id FROM matches WHERE player1_id >= 778000 OR player2_id >= 778000)")
            cursor.execute("DELETE FROM match_events WHERE match_id >= 99400 OR match_id IN (SELECT id FROM matches WHERE player1_id >= 778000 OR player2_id >= 778000)")
            cursor.execute("DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id >= 99400 OR match_id IN (SELECT id FROM matches WHERE player1_id >= 778000 OR player2_id >= 778000))")
            cursor.execute("DELETE FROM markets WHERE match_id >= 99400 OR match_id IN (SELECT id FROM matches WHERE player1_id >= 778000 OR player2_id >= 778000)")
            cursor.execute("DELETE FROM bet_markets WHERE match_id >= 99400 OR match_id IN (SELECT id FROM matches WHERE player1_id >= 778000 OR player2_id >= 778000)")
            cursor.execute("DELETE FROM matches WHERE id >= 99400 OR player1_id >= 778000 OR player2_id >= 778000")
            cursor.execute("DELETE FROM rounds WHERE round_number = 8 AND division_id IN (1, 2)")
            cursor.execute("DELETE FROM users WHERE telegram_id >= 778000")

            # Seed test users in different divisions
            # User 1: Division 1 (Active bettor)
            cursor.execute("""
                INSERT INTO users (telegram_id, username, division_id, team_name)
                VALUES (778001, 'capper_one', 1, 'ТестПорту')
            """)
            cursor.execute("INSERT INTO user_wallets (user_id, balance, total_wagered, total_won) VALUES (778001, 5000, 0, 0)")

            # User 2: Division 1 (1-bet fluke with 100% ROI)
            cursor.execute("""
                INSERT INTO users (telegram_id, username, division_id, team_name)
                VALUES (778002, 'fluke_bettor', 1, 'ТестБенфика')
            """)
            cursor.execute("INSERT INTO user_wallets (user_id, balance, total_wagered, total_won) VALUES (778002, 1000, 0, 0)")

            # User 3: Division 2 (Division isolation test)
            cursor.execute("""
                INSERT INTO users (telegram_id, username, division_id, team_name)
                VALUES (778003, 'div2_bettor', 2, 'ТестАякс')
            """)
            cursor.execute("INSERT INTO user_wallets (user_id, balance, total_wagered, total_won) VALUES (778003, 2000, 0, 0)")

            # User 4: Zero bets user (for ROI = None check)
            cursor.execute("""
                INSERT INTO users (telegram_id, username, division_id)
                VALUES (778004, 'newbie_zero_bets', 1)
            """)
            cursor.execute("INSERT INTO user_wallets (user_id, balance, total_wagered, total_won) VALUES (778004, 500, 0, 0)")

            # Seed test matches
            cursor.execute("INSERT OR REPLACE INTO rounds (round_number, division_id, season_id, is_open, bets_open) VALUES (8, 1, 1, 0, 1)")
            cursor.execute("INSERT OR REPLACE INTO rounds (round_number, division_id, season_id, is_open, bets_open) VALUES (8, 2, 1, 0, 1)")

            cursor.execute("""
                INSERT INTO matches (
                    id, season_id, division_id, round_number,
                    player1_team, player2_team, status, player1_score, player2_score
                ) VALUES
                (99401, 1, 1, 8, 'ТестПорту', 'Спортинг', 'open', 0, 0),
                (99402, 1, 1, 8, 'ТестБенфика', 'Брага', 'live', 1, 0),
                (99403, 1, 2, 8, 'ТестАякс', 'Фейеноорд', 'open', 0, 0)
            """)
            # Recommendations and hot matches only offer matches of the open line
            cursor.execute("""
                INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, is_active)
                SELECT id, round_number, player1_team, player2_team, 2.0, 3.2, 3.5, 1
                FROM matches WHERE id IN (99401, 99402, 99403)
            """)

            # Seed user 1 bets (6 settled bets: 4 won, 1 lost, 1 voided)
            # Won bets: 100 stake * 2.0 = 200 (profit +100 each, total +400)
            for i in range(4):
                cursor.execute("""
                    INSERT INTO user_bets (id, user_id, bet_type, amount, potential_win, total_odd, status, actual_payout, settled_at)
                    VALUES (?, 778001, 'single', 100, 200, 2.0, 'won', 200, CURRENT_TIMESTAMP)
                """, (994001 + i,))
                cursor.execute("""
                    INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status)
                    VALUES (?, 99401, 'p1', 2.0, 'won')
                """, (994001 + i,))

            # Lost bet: 100 stake (loss -100)
            cursor.execute("""
                INSERT INTO user_bets (id, user_id, bet_type, amount, potential_win, total_odd, status, actual_payout, settled_at)
                VALUES (994005, 778001, 'single', 100, 250, 2.5, 'lost', 0, CURRENT_TIMESTAMP)
            """)
            cursor.execute("""
                INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status)
                VALUES (994005, 99401, 'x', 2.5, 'lost')
            """)

            # Refunded bet: 100 stake (refund 100)
            cursor.execute("""
                INSERT INTO user_bets (id, user_id, bet_type, amount, potential_win, total_odd, status, actual_payout, settled_at)
                VALUES (994006, 778001, 'single', 100, 180, 1.8, 'refunded', 100, CURRENT_TIMESTAMP)
            """)
            cursor.execute("""
                INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status)
                VALUES (994006, 99401, 'tb25', 1.8, 'refunded')
            """)

            # Seed user 2 bet (ONLY 1 bet won: 100 stake -> 300 payout)
            cursor.execute("""
                INSERT INTO user_bets (id, user_id, bet_type, amount, potential_win, total_odd, status, actual_payout, settled_at)
                VALUES (994010, 778002, 'single', 100, 300, 3.0, 'won', 300, CURRENT_TIMESTAMP)
            """)
            cursor.execute("""
                INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status)
                VALUES (994010, 99402, 'p1', 3.0, 'won')
            """)

            # Seed user 3 bets in Division 2 (6 settled bets)
            for i in range(6):
                cursor.execute("""
                    INSERT INTO user_bets (id, user_id, bet_type, amount, potential_win, total_odd, status, actual_payout, settled_at)
                    VALUES (?, 778003, 'single', 100, 150, 1.5, 'won', 150, CURRENT_TIMESTAMP)
                """, (994020 + i,))
                cursor.execute("""
                    INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status)
                    VALUES (?, 99403, 'p1', 1.5, 'won')
                """, (994020 + i,))

    def tearDown(self) -> None:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bet_items WHERE bet_id IN (SELECT id FROM user_bets WHERE user_id >= 778000)")
            cursor.execute("DELETE FROM user_bets WHERE user_id >= 778000")
            cursor.execute("DELETE FROM user_wallets WHERE user_id >= 778000")
            cursor.execute("DELETE FROM favorites WHERE user_id >= 778000")
            cursor.execute("DELETE FROM users WHERE telegram_id >= 778000")
            cursor.execute("DELETE FROM markets WHERE match_id >= 99400")
            cursor.execute("DELETE FROM matches WHERE id >= 99400")

    def test_zero_staked_user_returns_none_roi(self) -> None:
        """A user with 0 staked bets must return roi_pct=None and win_rate_pct=None, NEVER 0.0!"""
        stats = get_user_betting_analytics(778004)
        self.assertEqual(stats["total_staked"], 0)
        self.assertEqual(stats["settled_predictions"], 0)
        # Critical assertion
        self.assertIsNone(stats["roi_pct"])
        self.assertIsNone(stats["win_rate_pct"])

    def test_user_betting_analytics_calculation(self) -> None:
        """Verify calculations for user 1 (6 bets: 4 won, 1 lost, 1 void)."""
        stats = get_user_betting_analytics(778001)
        self.assertEqual(stats["settled_predictions"], 6)
        self.assertEqual(stats["won_predictions"], 4)
        self.assertEqual(stats["lost_predictions"], 1)
        self.assertEqual(stats["void_predictions"], 1)
        self.assertEqual(stats["total_staked"], 600)

        # Total payout = 4 * 200 + 100 (void refund) = 900
        self.assertEqual(stats["total_payout"], 900)
        # Net profit = 900 - 600 = +300
        self.assertEqual(stats["net_profit"], 300)
        # ROI = (300 / 600) * 100 = 50.0%
        self.assertEqual(stats["roi_pct"], 50.0)
        # Win rate = (4 / 6) * 100 = 66.7%
        self.assertEqual(stats["win_rate_pct"], 66.7)
        # Recent form has 5 items
        self.assertEqual(len(stats["recent_form"]), 5)
        # Favorite market was 'p1' (4 picks)
        self.assertEqual(stats["favorite_market"], "P1")

    def test_capper_leaderboard_min_bets_threshold(self) -> None:
        """User with only 1 bet (user 778002) is excluded by MIN_LEADERBOARD_BETS=5."""
        leaders = get_capper_leaderboard(min_bets=5)
        user_ids = [l["user_id"] for l in leaders]
        self.assertIn(778001, user_ids)  # Has 6 bets
        self.assertNotIn(778002, user_ids)  # Only 1 bet: excluded!

        # If min_bets is lowered to 1, fluke user appears
        leaders_all = get_capper_leaderboard(min_bets=1)
        all_user_ids = [l["user_id"] for l in leaders_all]
        self.assertIn(778002, all_user_ids)

    def test_capper_leaderboard_division_isolation(self) -> None:
        """Division leaderboard strictly isolates bettors from that division."""
        # Division 1 query
        div1_leaders = get_capper_leaderboard(division_id=1, min_bets=5)
        div1_ids = [l["user_id"] for l in div1_leaders]
        self.assertIn(778001, div1_ids)
        self.assertNotIn(778003, div1_ids)  # User 3 is Division 2

        # Division 2 query
        div2_leaders = get_capper_leaderboard(division_id=2, min_bets=5)
        div2_ids = [l["user_id"] for l in div2_leaders]
        self.assertIn(778003, div2_ids)
        self.assertNotIn(778001, div2_ids)  # User 1 is Division 1

    def test_hot_matches_ranking(self) -> None:
        """Live match receives higher hotness score than non-live match."""
        hot = get_hot_matches(division_id=1, limit=5)
        self.assertGreaterEqual(len(hot), 2)
        # Match 99402 is LIVE -> must be top ranked over scheduled match 99401
        self.assertEqual(hot[0]["id"], 99402)
        self.assertTrue(hot[0]["is_live"])
        self.assertGreater(hot[0]["hot_score"], hot[1]["hot_score"])

    def test_user_recommendations_explainable(self) -> None:
        """Recommendations provide clear reasoning matching user's favorite club / division."""
        recs = get_user_recommendations(778001, limit=5)
        self.assertGreaterEqual(len(recs), 1)
        # Top recommendation should match user's team 'ТестПорту'
        top_rec = recs[0]
        self.assertEqual(top_rec["match_id"], 99401)
        self.assertIn("тестпорту", top_rec["reason"].lower())


if __name__ == "__main__":
    unittest.main()
