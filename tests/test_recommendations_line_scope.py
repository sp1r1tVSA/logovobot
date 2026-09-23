"""
tests/test_recommendations_line_scope.py

Verifies that:
1. After schedule generation with line open on tours 1 and 2 (bets_open=1, is_open=0),
   recommendations only contain matches from tours 1 and 2, never from future unopened tours (3..30).
2. A player user sees only their matches from the open tours (e.g. 2 matches: tour 1 and tour 2).
3. Matches from closed/unopened tours are strictly excluded.
4. When the line advances to subsequent tours, recommendations follow the active line.
5. Neutral bettors see central games of the open tours only.
6. A match of an open tour that is not in the line (no active tile) is never offered.
"""

import unittest
import database
from services.recommendation_engine import get_user_recommendations, get_hot_matches


DIV_ID = 881
SEASON_ID = 881

USER_PLAYER = 779101
USER_OPP1 = 779102
USER_OPP2 = 779103
USER_BETTOR = 779104

TEAM_PLAYER = "Айнтрахт"
TEAM_OPP1 = "Майнц"
TEAM_OPP2 = "Вест Хэм"
TEAM_OTHER1 = "Ницца"
TEAM_OTHER2 = "Лион"


class TestRecommendationsLineScope(unittest.TestCase):

    def setUp(self) -> None:
        database.init_db()
        self._cleanup()

        with database.transaction() as conn:
            cursor = conn.cursor()

            # 1. Create test users
            cursor.execute(
                "INSERT INTO users (telegram_id, username, division_id, team_name) VALUES (?, 'eintracht_user', ?, ?)",
                (USER_PLAYER, DIV_ID, TEAM_PLAYER)
            )
            cursor.execute(
                "INSERT INTO users (telegram_id, username, division_id, team_name) VALUES (?, 'mainz_user', ?, ?)",
                (USER_OPP1, DIV_ID, TEAM_OPP1)
            )
            cursor.execute(
                "INSERT INTO users (telegram_id, username, division_id, team_name) VALUES (?, 'westham_user', ?, ?)",
                (USER_OPP2, DIV_ID, TEAM_OPP2)
            )
            cursor.execute(
                "INSERT INTO users (telegram_id, username, division_id, team_name) VALUES (?, 'neutral_bettor', ?, NULL)",
                (USER_BETTOR, DIV_ID)
            )

            for uid in (USER_PLAYER, USER_OPP1, USER_OPP2, USER_BETTOR):
                cursor.execute(
                    "INSERT INTO user_wallets (user_id, balance, total_wagered, total_won) VALUES (?, 1000, 0, 0)",
                    (uid,)
                )

            # 2. Create rounds: Tours 1 and 2 open for bets (bets_open=1, is_open=0)
            # Tours 3, 4, 10 are unopened (bets_open=0, is_open=0)
            for rn in range(1, 11):
                bets_open = 1 if rn in (1, 2) else 0
                cursor.execute("""
                    INSERT INTO rounds (round_number, division_id, season_id, is_open, bets_open, deadline)
                    VALUES (?, ?, ?, 0, ?, '01.01.2030 20:00')
                """, (rn, DIV_ID, SEASON_ID, bets_open))

            # 3. Create matches:
            # Tour 1: Player match + Other match
            cursor.execute("""
                INSERT INTO matches (id, round_number, division_id, season_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99801, 1, ?, ?, ?, ?, ?, ?, 'pending')
            """, (DIV_ID, SEASON_ID, USER_PLAYER, USER_OPP1, TEAM_PLAYER, TEAM_OPP1))

            cursor.execute("""
                INSERT INTO matches (id, round_number, division_id, season_id, player1_team, player2_team, status)
                VALUES (99802, 1, ?, ?, ?, ?, 'pending')
            """, (DIV_ID, SEASON_ID, TEAM_OTHER1, TEAM_OTHER2))

            # Tour 2: Player match + Other match
            cursor.execute("""
                INSERT INTO matches (id, round_number, division_id, season_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99803, 2, ?, ?, ?, ?, ?, ?, 'pending')
            """, (DIV_ID, SEASON_ID, USER_OPP2, USER_PLAYER, TEAM_OPP2, TEAM_PLAYER))

            cursor.execute("""
                INSERT INTO matches (id, round_number, division_id, season_id, player1_team, player2_team, status)
                VALUES (99804, 2, ?, ?, ?, ?, 'pending')
            """, (DIV_ID, SEASON_ID, TEAM_OTHER1, TEAM_OPP1))

            # Tour 3 (unopened): Player match
            cursor.execute("""
                INSERT INTO matches (id, round_number, division_id, season_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99805, 3, ?, ?, ?, ?, ?, ?, 'pending')
            """, (DIV_ID, SEASON_ID, USER_PLAYER, USER_OPP2, TEAM_PLAYER, TEAM_OPP2))

            # Tour 10 (late season, unopened): Player match
            cursor.execute("""
                INSERT INTO matches (id, round_number, division_id, season_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99806, 10, ?, ?, ?, ?, ?, ?, 'pending')
            """, (DIV_ID, SEASON_ID, USER_OPP1, USER_PLAYER, TEAM_OPP1, TEAM_PLAYER))

            # 4. Line tiles for every match: the round gate alone decides here;
            # a match of an open round without a tile is covered separately.
            for match_id, tour in ((99801, 1), (99802, 1), (99803, 2), (99804, 2), (99805, 3), (99806, 10)):
                cursor.execute("""
                    INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, is_active)
                    SELECT id, ?, player1_team, player2_team, 2.0, 3.2, 3.5, 1 FROM matches WHERE id = ?
                """, (tour, match_id))

    def tearDown(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bet_items WHERE bet_id IN (SELECT id FROM user_bets WHERE user_id >= 779100)")
            cursor.execute("DELETE FROM user_bets WHERE user_id >= 779100")
            cursor.execute("DELETE FROM user_wallets WHERE user_id >= 779100")
            cursor.execute("DELETE FROM user_progression WHERE user_id >= 779100")
            cursor.execute("DELETE FROM user_achievements WHERE user_id >= 779100")
            cursor.execute("DELETE FROM favorites WHERE user_id >= 779100")
            cursor.execute("DELETE FROM division_admins WHERE user_id >= 779100")
            cursor.execute("DELETE FROM admin_audit_log WHERE admin_id >= 779100")
            cursor.execute("DELETE FROM odds_movement WHERE match_id >= 99800")
            cursor.execute("DELETE FROM live_match_states WHERE match_id >= 99800")
            cursor.execute("DELETE FROM match_events WHERE match_id >= 99800")
            cursor.execute("DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id >= 99800)")
            cursor.execute("DELETE FROM markets WHERE match_id >= 99800")
            cursor.execute("DELETE FROM bet_markets WHERE match_id >= 99800")
            cursor.execute("DELETE FROM matches WHERE id >= 99800 OR division_id = ?", (DIV_ID,))
            cursor.execute("DELETE FROM rounds WHERE division_id = ?", (DIV_ID,))
            cursor.execute("DELETE FROM users WHERE telegram_id >= 779100")

    def test_preseason_line_shows_only_first_two_tours_for_player(self) -> None:
        """
        After schedule generation, recommendations must show ONLY matches
        from the user's first 2 open tours (matches 99801 and 99803).
        Matches from tours 3 and 10 must NOT appear.
        """
        recs = get_user_recommendations(
            user_id=USER_PLAYER,
            limit=5,
            division_id=DIV_ID,
            season_id=SEASON_ID
        )

        match_ids = [r["match_id"] for r in recs]
        round_numbers = [r["round_number"] for r in recs]

        # Must have exactly 2 matches (one from tour 1, one from tour 2)
        self.assertEqual(len(recs), 2)
        self.assertEqual(match_ids, [99801, 99803])
        self.assertEqual(round_numbers, [1, 2])

        # Must NOT contain tour 3 or tour 10 matches
        self.assertNotIn(99805, match_ids)
        self.assertNotIn(99806, match_ids)

        # Both must indicate player's club
        for r in recs:
            self.assertIn("Айнтрахт", r["reason"])

    def test_neutral_bettor_sees_central_games_from_open_tours_only(self) -> None:
        """Neutral bettor sees central matches from tours 1 and 2 only."""
        recs = get_user_recommendations(
            user_id=USER_BETTOR,
            limit=5,
            division_id=DIV_ID,
            season_id=SEASON_ID
        )

        match_ids = [r["match_id"] for r in recs]
        for r in recs:
            self.assertIn(r["round_number"], (1, 2))
        self.assertNotIn(99805, match_ids)
        self.assertNotIn(99806, match_ids)

    def test_line_advancement_shifts_recommendations(self) -> None:
        """When line shifts to tours 3 and 4, recommendations show tour 3 match."""
        with database.transaction() as conn:
            cursor = conn.cursor()
            # Finish tour 1 and 2 matches
            cursor.execute("UPDATE matches SET status = 'confirmed' WHERE id IN (99801, 99802, 99803, 99804)")
            # Close tour 1 and 2 bets, open tour 3 and 4 bets
            cursor.execute("UPDATE rounds SET bets_open = 0, is_open = 0 WHERE division_id = ? AND round_number IN (1, 2)", (DIV_ID,))
            cursor.execute("UPDATE rounds SET bets_open = 1, is_open = 0 WHERE division_id = ? AND round_number IN (3, 4)", (DIV_ID,))

        recs = get_user_recommendations(
            user_id=USER_PLAYER,
            limit=5,
            division_id=DIV_ID,
            season_id=SEASON_ID
        )

        match_ids = [r["match_id"] for r in recs]
        self.assertEqual(match_ids, [99805])
        self.assertEqual(recs[0]["round_number"], 3)
        self.assertIn("Айнтрахт", recs[0]["reason"])

    def test_open_round_match_outside_the_line_is_not_recommended(self) -> None:
        """Only the central pairs of an open round are in the line; the rest are not offered."""
        with database.transaction() as conn:
            conn.execute("DELETE FROM bet_markets WHERE match_id IN (99801, 99802)")

        neutral = [r["match_id"] for r in get_user_recommendations(
            user_id=USER_BETTOR, limit=5, division_id=DIV_ID, season_id=SEASON_ID)]
        self.assertEqual(neutral, [99803, 99804])

        # The player's own tour-1 match is not in the line either: only tour 2 is left.
        own = [r["match_id"] for r in get_user_recommendations(
            user_id=USER_PLAYER, limit=5, division_id=DIV_ID, season_id=SEASON_ID)]
        self.assertEqual(own, [99803])

        hot = {m["id"] for m in get_hot_matches(division_id=DIV_ID, season_id=SEASON_ID, limit=10)}
        self.assertEqual(hot, {99803, 99804})

    def test_hot_matches_scoped_to_open_line(self) -> None:
        """Hot matches also only pick from open line rounds."""
        hot = get_hot_matches(division_id=DIV_ID, season_id=SEASON_ID, limit=10)
        hot_rounds = [m["round_number"] for m in hot]
        for r_num in hot_rounds:
            self.assertIn(r_num, (1, 2))
        self.assertNotIn(3, hot_rounds)
        self.assertNotIn(10, hot_rounds)


if __name__ == "__main__":
    unittest.main()
