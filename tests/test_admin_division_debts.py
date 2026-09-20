"""Unit tests for division isolation in debts summary and overdue match resolution."""
import unittest
import uuid

import database
from handlers.admin import _build_debts_summary


class TestAdminDivisionDebts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.div_a_id = database.create_division(name=f"Debts Div A {uid}", code=f"DDA_{uid}")
        self.div_b_id = database.create_division(name=f"Debts Div B {uid}", code=f"DDB_{uid}")

        self.user_a1 = 98101
        self.user_a2 = 98102
        self.user_b1 = 98201
        self.user_b2 = 98202

        with database.transaction() as conn:
            c = conn.cursor()
            # Active season ID
            act = database.get_active_season()
            self.season_id = act["id"] if act else 1

            # Users in Division A
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'user_a1', 'Valencia', 'player', ?)",
                (self.user_a1, self.div_a_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'user_a2', 'Sevilla', 'player', ?)",
                (self.user_a2, self.div_a_id),
            )

            # Users in Division B
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'user_b1', 'Juventus', 'player', ?)",
                (self.user_b1, self.div_b_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'user_b2', 'Milan', 'player', ?)",
                (self.user_b2, self.div_b_id),
            )

            # Expired rounds in both divisions (overdue)
            c.execute(
                "INSERT OR REPLACE INTO rounds (round_number, is_open, deadline, division_id, season_id) VALUES (1, 1, '2020-01-01 00:00', ?, ?)",
                (self.div_a_id, self.season_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO rounds (round_number, is_open, deadline, division_id, season_id) VALUES (1, 1, '2020-01-01 00:00', ?, ?)",
                (self.div_b_id, self.season_id),
            )

            # Pending matches
            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id, season_id) "
                "VALUES (1, ?, ?, 'Valencia', 'Sevilla', 'pending', ?, ?)",
                (self.user_a1, self.user_a2, self.div_a_id, self.season_id),
            )
            self.match_a = c.lastrowid

            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id, season_id) "
                "VALUES (1, ?, ?, 'Juventus', 'Milan', 'pending', ?, ?)",
                (self.user_b1, self.user_b2, self.div_b_id, self.season_id),
            )
            self.match_b = c.lastrowid

    async def asyncTearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM matches WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM rounds WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?, ?, ?)", (self.user_a1, self.user_a2, self.user_b1, self.user_b2))
            c.execute("DELETE FROM divisions WHERE id IN (?, ?)", (self.div_a_id, self.div_b_id))

    async def test_build_debts_summary_isolated_to_division(self):
        """_build_debts_summary для дивизиона A не должен содержать долгов дивизиона B."""
        text, count = await _build_debts_summary(division_id=self.div_a_id, season_id=self.season_id)
        self.assertIsNotNone(text)
        self.assertGreaterEqual(count, 1)

        # Команды дивизиона A должны быть в сводке
        self.assertIn("Valencia", text)
        self.assertIn("Sevilla", text)

        # Команды дивизиона B не должны фигурировать в сводке дивизиона A
        self.assertNotIn("Juventus", text)
        self.assertNotIn("Milan", text)

    def test_find_user_by_team_scoped_by_division(self):
        """find_user_by_team с указанием division_id находит пользователя только в своем дивизионе."""
        user_a = database.find_user_by_team("Valencia", division_id=self.div_a_id)
        self.assertIsNotNone(user_a)
        self.assertEqual(user_a["telegram_id"], self.user_a1)

        # Поиск той же команды в чужом дивизионе должен вернуть None
        user_wrong_div = database.find_user_by_team("Valencia", division_id=self.div_b_id)
        self.assertIsNone(user_wrong_div)

    def test_get_detailed_overdue_matches_division_integrity(self):
        """get_detailed_overdue_matches гарантирует, что player1_id/player2_id соответствуют участникам дивизиона матча."""
        overdue = database.get_detailed_overdue_matches()
        matched_a = [m for m in overdue if m["id"] == self.match_a]
        self.assertEqual(len(matched_a), 1)
        m_a = matched_a[0]
        self.assertEqual(m_a["player1_id"], self.user_a1)
        self.assertEqual(m_a["player2_id"], self.user_a2)

        matched_b = [m for m in overdue if m["id"] == self.match_b]
        self.assertEqual(len(matched_b), 1)
        m_b = matched_b[0]
        self.assertEqual(m_b["player1_id"], self.user_b1)
        self.assertEqual(m_b["player2_id"], self.user_b2)


if __name__ == "__main__":
    unittest.main()
