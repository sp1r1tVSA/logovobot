import os
import sys
import tempfile
import sqlite3
import datetime
import unittest

# Ensure logovobot root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database


class TestDebtLifecycle(unittest.TestCase):
    def setUp(self):
        """Create a temporary isolated SQLite database for each test."""
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.tf.name
        self.tf.close()

        self.orig_config_path = config.DB_PATH
        self.orig_database_path = database.DB_PATH

        config.DB_PATH = self.temp_db_path
        database.DB_PATH = self.temp_db_path
        database.init_db()

    def tearDown(self):
        """Restore original paths and cleanup temp db."""
        config.DB_PATH = self.orig_config_path
        database.DB_PATH = self.orig_database_path
        try:
            os.remove(self.temp_db_path)
        except Exception:
            pass

    def test_debt_reminders_table_and_stages(self):
        """Test recording and checking debt lifecycle stages."""
        match_id = 999
        with database.transaction() as conn:
            conn.execute("INSERT INTO matches (id, round_number, status) VALUES (?, 1, 'pending')", (match_id,))

        self.assertFalse(database.has_debt_stage(match_id, "deadline_passed"))
        self.assertFalse(database.has_debt_stage(match_id, "warn_24h"))

        database.record_debt_stage(match_id, "deadline_passed")
        self.assertTrue(database.has_debt_stage(match_id, "deadline_passed"))
        self.assertFalse(database.has_debt_stage(match_id, "warn_24h"))

        database.record_debt_stage(match_id, "warn_24h")
        self.assertTrue(database.has_debt_stage(match_id, "warn_24h"))

    def test_debt_12h_cycle_reminders(self):
        """Test 12h cycle reminder timestamp tracking."""
        match_id = 101
        with database.transaction() as conn:
            conn.execute("INSERT INTO matches (id, round_number, status) VALUES (?, 1, 'pending')", (match_id,))

        self.assertIsNone(database.get_last_debt_12h_reminder(match_id))

        database.record_debt_12h_reminder(match_id)
        last_dt = database.get_last_debt_12h_reminder(match_id)
        self.assertIsNotNone(last_dt)
        self.assertIsInstance(last_dt, datetime.datetime)

    def test_apply_debt_played_reward(self):
        """Test reward for clearing debt matches (-1 warn, 0 stays 0)."""
        user_id = 777123

        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (?, 'tester', 'Arsenal', 2)",
                (user_id,)
            )

        # 1. First played debt match: 2 -> 1
        new_cnt, unwarned = database.apply_debt_played_reward(user_id, round_number=5)
        self.assertEqual(new_cnt, 1)
        self.assertTrue(unwarned)
        self.assertEqual(database.get_user_warn_count(user_id), 1)

        # 2. Second played debt match: 1 -> 0
        new_cnt, unwarned = database.apply_debt_played_reward(user_id, round_number=6)
        self.assertEqual(new_cnt, 0)
        self.assertTrue(unwarned)
        self.assertEqual(database.get_user_warn_count(user_id), 0)

        # 3. Third played debt match when already 0: 0 -> 0
        new_cnt, unwarned = database.apply_debt_played_reward(user_id, round_number=7)
        self.assertEqual(new_cnt, 0)
        self.assertFalse(unwarned)
        self.assertEqual(database.get_user_warn_count(user_id), 0)

    def test_overdue_is_measured_from_the_round_deadline(self):
        """There is no global start gate: the round's own deadline is the whole clock."""
        past_dl = (datetime.datetime.now() - datetime.timedelta(hours=50)).strftime("%d.%m.%Y %H:%M")

        with database.transaction() as conn:
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (111, 'u1', 'Real Madrid', 0)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (222, 'u2', 'Barcelona', 0)")
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (1, 1, ?)", (past_dl,))
            conn.execute("INSERT INTO matches (id, round_number, player1_id, player2_id, player1_team, player2_team, status) VALUES (10, 1, 111, 222, 'Real Madrid', 'Barcelona', 'pending')")

        self.assertTrue(database.is_match_overdue(10))

        overdue = database.get_detailed_overdue_matches()
        self.assertEqual(len(overdue), 1)
        self.assertAlmostEqual(overdue[0]["hours_overdue"], 50.0, delta=1.0)

    def test_recent_warn_rate_limit(self):
        """Test has_user_been_warned_recently helper."""
        user_id = 555666
        with database.transaction() as conn:
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (?, 'warned_u', 'Porto', 1)", (user_id,))
            conn.execute("INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) VALUES (?, NULL, 'Test warn', 'WARN_ADD', CURRENT_TIMESTAMP)", (user_id,))

        self.assertTrue(database.has_user_been_warned_recently(user_id, hours=20.0))
        self.assertFalse(database.has_user_been_warned_recently(999999, hours=20.0))

    def test_admin_reset_and_restore(self):
        """Test global reset and restore user team."""
        with database.transaction() as conn:
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (10, 'u1', NULL, 4)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (20, 'u2', 'Ajax', 3)")
            conn.execute("INSERT INTO matches (id, round_number, status) VALUES (1, 1, 'pending')")
            conn.execute("INSERT INTO debt_reminders (match_id, stage) VALUES (1, 'warn_24h')")

        affected = database.admin_reset_all_warns_and_debts()
        self.assertEqual(affected, 2)
        self.assertEqual(database.get_user_warn_count(10), 0)
        # Restore user 10 club
        database.restore_user_team(10, "ПСВ")
        user10 = database.get_user(10)
        self.assertEqual(user10["team_name"], "ПСВ")
        self.assertEqual(user10["warn_count"], 0)

    def test_closed_rounds_and_flexible_dates(self):
        """Test overdue detection for closed rounds, flexible date formats, and future unopened rounds."""
        with database.transaction() as conn:
            # User 1 and User 2
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (301, 'user301', 'Ливерпуль', 0)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (302, 'user302', 'Манчестер Сити', 0)")

            # Past closed round 1 with expired deadline
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (1, 0, '15.08.2026 12:00')")
            conn.execute("INSERT INTO matches (id, round_number, player1_id, player2_id, player1_team, player2_team, status) VALUES (501, 1, 301, 302, 'Ливерпуль', 'Манчестер Сити', 'pending')")

            # Open round 2 without a deadline — legacy state, not a debt: a debt
            # needs a deadline to be late against (services.debt_policy)
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (2, 1, NULL)")
            conn.execute("INSERT INTO matches (id, round_number, player1_id, player2_id, player1_team, player2_team, status) VALUES (502, 2, 301, 302, 'Ливерпуль', 'Манчестер Сити', 'pending')")

            # Future unopened round 25 (is_open=0, deadline=NULL) - MUST BE IGNORED!
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (25, 0, NULL)")
            conn.execute("INSERT INTO matches (id, round_number, player1_id, player2_id, player1_team, player2_team, status) VALUES (525, 25, 301, 302, 'Ливерпуль', 'Манчестер Сити', 'pending')")

        overdue = database.get_detailed_overdue_matches()
        match_ids = {m["id"] for m in overdue}
        self.assertEqual(match_ids, {501})

        by_id = {m["id"]: m for m in overdue}
        self.assertGreater(by_id[501]["hours_overdue"], 24.0)

        # Test find_user_by_team
        u = database.find_user_by_team("ливерпуль")
        self.assertIsNotNone(u)
        self.assertEqual(u["telegram_id"], 301)

        # Test count_user_remaining_debts
        self.assertEqual(database.count_user_remaining_debts(301), 1)
        self.assertEqual(database.count_user_remaining_debts(302), 1)
        self.assertEqual(database.count_user_remaining_debts(999), 0)

    def test_debt_played_reward_cross_round_and_stages(self):
        """Test that playing any debt match clears warn regardless of which round originated the warn."""
        user_id = 401
        with database.transaction() as conn:
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (?, 'debt_player', 'Челси', 0)", (user_id,))
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (402, 'opp1', 'Арсенал', 0)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, warn_count) VALUES (403, 'opp2', 'Тоттенхэм', 0)")
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (1, 1, '01.01.2026 12:00')")
            conn.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (2, 1, '02.01.2026 12:00')")
            conn.execute("INSERT INTO matches (id, round_number, player1_id, player2_id, player1_team, player2_team, status) VALUES (601, 1, 401, 402, 'Челси', 'Арсенал', 'pending')")
            conn.execute("INSERT INTO matches (id, round_number, player1_id, player2_id, player1_team, player2_team, status) VALUES (602, 2, 401, 403, 'Челси', 'Тоттенхэм', 'pending')")
            # Record stage on match 601
            conn.execute("INSERT INTO debt_reminders (match_id, stage) VALUES (601, 'warn_24h')")

        # Give warn for round 1
        database.add_warn(user_id, None, "Авто-варн: просрочка 24ч по 1 туру")
        self.assertEqual(database.get_user_warn_count(user_id), 1)

        # Verify is_match_overdue recognizes recorded debt stage
        self.assertTrue(database.is_match_overdue(601))
        self.assertTrue(database.is_match_overdue(602))

        # User plays match 602 (Round 2) -> should successfully remove the warn from Round 1!
        new_cnt, unwarned = database.apply_debt_played_reward(user_id, round_number=2)
        self.assertTrue(unwarned)
        self.assertEqual(new_cnt, 0)
        self.assertEqual(database.get_user_warn_count(user_id), 0)


if __name__ == "__main__":
    unittest.main()

