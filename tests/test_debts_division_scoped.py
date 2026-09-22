import os
import sys
import tempfile
import datetime
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database


class TestDebtsAreDivisionScoped(unittest.TestCase):
    """Regression: opening rounds 1–2 in division 5 must not turn the same rounds
    of other divisions (still unopened there) into debts."""

    def setUp(self):
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.tf.name
        self.tf.close()

        self.orig_config_path = config.DB_PATH
        self.orig_database_path = database.DB_PATH
        config.DB_PATH = self.temp_db_path
        database.DB_PATH = self.temp_db_path
        database.init_db()

        now = datetime.datetime.now()
        future_dl = (now + datetime.timedelta(days=3)).strftime("%d.%m.%Y %H:%M")
        past_dl = (now - datetime.timedelta(days=1)).strftime("%d.%m.%Y %H:%M")

        with database.transaction() as conn:
            for div in (1, 5):
                conn.execute(
                    "INSERT OR IGNORE INTO divisions (id, name, code, tournament_id) VALUES (?, ?, ?, 1)",
                    (div, f"Дивизион {div}", f"DIV_{div}"),
                )
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id) VALUES (11, 'a', 'Бетис', 1)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id) VALUES (12, 'b', 'Бешикташ', 1)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id) VALUES (51, 'c', 'Брайтон', 5)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id) VALUES (52, 'd', 'Порту', 5)")

            # Division 5: rounds 1–2 opened with a future deadline
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (1, 1, ?, 5)", (future_dl,))
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (2, 1, ?, 5)", (future_dl,))
            # Division 1: the same rounds exist (betting line) but were never opened
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (1, 0, NULL, 1)")
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (2, 0, NULL, 1)")

            conn.execute("INSERT INTO matches (id, round_number, player1_team, player2_team, status, division_id) VALUES (101, 1, 'Бетис', 'Бешикташ', 'pending', 1)")
            conn.execute("INSERT INTO matches (id, round_number, player1_team, player2_team, status, division_id) VALUES (102, 2, 'Бешикташ', 'Бетис', 'pending', 1)")
            conn.execute("INSERT INTO matches (id, round_number, player1_team, player2_team, status, division_id) VALUES (501, 1, 'Брайтон', 'Порту', 'pending', 5)")
            conn.execute("INSERT INTO matches (id, round_number, player1_team, player2_team, status, division_id) VALUES (502, 2, 'Порту', 'Брайтон', 'pending', 5)")
        self.past_dl = past_dl

    def tearDown(self):
        config.DB_PATH = self.orig_config_path
        database.DB_PATH = self.orig_database_path
        try:
            os.remove(self.temp_db_path)
        except Exception:
            pass

    def test_tracker_ignores_unopened_rounds_of_other_divisions(self):
        ids = {m["id"] for m in database.get_detailed_overdue_matches()}
        self.assertEqual(ids, set())

    def test_debt_digest_ignores_unopened_rounds_of_other_divisions(self):
        ids = {m["id"] for m in database.get_all_unplayed_league_matches()}
        self.assertEqual(ids, set())

    def test_expired_deadline_is_debt_only_in_its_own_division(self):
        with database.transaction() as conn:
            conn.execute("UPDATE rounds SET deadline = ? WHERE division_id = 5 AND round_number = 1", (self.past_dl,))
        tracked = {m["id"] for m in database.get_detailed_overdue_matches()}
        digest = {m["id"] for m in database.get_all_unplayed_league_matches()}
        self.assertEqual(tracked, {501})
        self.assertEqual(digest, {501})

    def test_earlier_round_without_deadline_is_not_debt(self):
        """Раньше тур ниже максимального открытого считался долгом даже без
        дедлайна. Теперь долг — только от дедлайна или досрочного закрытия."""
        with database.transaction() as conn:
            conn.execute("UPDATE rounds SET is_open = 1 WHERE division_id = 1 AND round_number = 2")
            conn.execute("UPDATE rounds SET deadline = ? WHERE division_id = 1 AND round_number = 2",
                         ((datetime.datetime.now() + datetime.timedelta(days=3)).strftime("%d.%m.%Y %H:%M"),))
        ids = {m["id"] for m in database.get_detailed_overdue_matches()}
        self.assertEqual(ids, set())

    def test_closed_round_with_passed_deadline_is_debt(self):
        with database.transaction() as conn:
            conn.execute("UPDATE rounds SET deadline = ? WHERE division_id = 1 AND round_number = 1", (self.past_dl,))
        ids = {m["id"] for m in database.get_detailed_overdue_matches()}
        self.assertEqual(ids, {101})
        self.assertTrue(database.is_match_overdue(101))
        self.assertFalse(database.is_match_overdue(102))

if __name__ == "__main__":
    unittest.main()
