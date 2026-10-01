"""
Сгенерированное расписание должно быть именованным.

Матч связан с users по имени клуба (`LOWER(playerN_team) = LOWER(team_name)`),
а не по `playerN_id`, поэтому COALESCE(..., u.team_name, 'Команда 1') в читателях
сам себя не спасает: пустая колонка клуба ломает тот самый JOIN, из которого он
берёт запасное значение. Пока `batch_insert_matches` писал только id, весь тур
выходил как «Команда 1 — Команда 2»: без таблицы, без ников, с одинаковыми
коэффициентами на все пары.

Существующие тесты матчей вставляют строки с уже заполненным `playerN_team` и
поэтому эту форму не проверяли ни разу.
"""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


class GeneratedMatchesTestBase(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(name=f"GEN {self.uid}", code=f"GEN_{self.uid}")
        self.season_id = 1
        self.players = []
        for n in range(1, 5):
            tg_id = int(f"9{n}{int(self.uid, 16) % 100000:05d}")
            database.register_user(tg_id, f"coach{n}_{self.uid}", team_name=f"Клуб {n} {self.uid}")
            database.assign_user_division(tg_id, self.div_id)
            self.players.append(tg_id)

    def _team_of(self, tg_id: int) -> str:
        return dict(database.get_user(tg_id))["team_name"]


class TestBatchInsertStoresTeamNames(GeneratedMatchesTestBase):
    def test_generated_round_carries_club_names(self):
        p1, p2, p3, p4 = self.players
        database.batch_insert_matches(
            [(1, p1, p2), (1, p3, p4)], division_id=self.div_id, season_id=self.season_id
        )

        matches = database.get_matches_by_round(1, division_id=self.div_id, season_id=self.season_id)
        self.assertEqual(len(matches), 2)
        for m in matches:
            self.assertNotEqual(m["player1_team"], "Команда 1")
            self.assertNotEqual(m["player2_team"], "Команда 2")

        pairs = {(m["player1_team"], m["player2_team"]) for m in matches}
        self.assertEqual(
            pairs,
            {(self._team_of(p1), self._team_of(p2)), (self._team_of(p3), self._team_of(p4))},
        )

    def test_generated_round_resolves_players_back(self):
        """Ник и telegram_id читатели тоже достают через клуб — они не должны пропасть."""
        p1, p2 = self.players[0], self.players[1]
        database.batch_insert_matches([(2, p1, p2)], division_id=self.div_id, season_id=self.season_id)

        m = database.get_matches_by_round(2, division_id=self.div_id, season_id=self.season_id)[0]
        self.assertEqual(m["player1_id"], p1)
        self.assertEqual(m["player2_id"], p2)
        self.assertTrue(m["player1_nickname"])
        self.assertTrue(m["player2_nickname"])

    def test_get_match_sees_the_clubs(self):
        p1, p2 = self.players[0], self.players[1]
        database.batch_insert_matches([(3, p1, p2)], division_id=self.div_id, season_id=self.season_id)
        m_id = database.get_matches_by_round(3, division_id=self.div_id, season_id=self.season_id)[0]["id"]

        m = database.get_match(m_id)
        self.assertEqual(m["player1_team"], self._team_of(p1))
        self.assertEqual(m["player2_team"], self._team_of(p2))

    def test_standings_count_a_generated_match(self):
        """Таблица считается по playerN_team: без него подтверждённый матч не виден."""
        p1, p2 = self.players[0], self.players[1]
        database.batch_insert_matches([(4, p1, p2)], division_id=self.div_id, season_id=self.season_id)
        m_id = database.get_matches_by_round(4, division_id=self.div_id, season_id=self.season_id)[0]["id"]

        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET player1_score = 3, player2_score = 1, status = 'confirmed' WHERE id = ?",
                (m_id,)
            )

        table = {r["team_name"]: r for r in database.get_standings(division_id=self.div_id, season_id=self.season_id)}
        self.assertEqual(table[self._team_of(p1)]["points"], 3)
        self.assertEqual(table[self._team_of(p1)]["goals_scored"], 3)
        self.assertEqual(table[self._team_of(p2)]["played"], 1)

    def test_create_match_stores_team_names_too(self):
        p1, p2 = self.players[0], self.players[1]
        m_id = database.create_match(5, p1, p2, division_id=self.div_id)

        m = database.get_match(m_id)
        self.assertEqual(m["player1_team"], self._team_of(p1))
        self.assertEqual(m["player2_team"], self._team_of(p2))

    def test_coach_without_a_club_leaves_the_slot_empty(self):
        """Клуба нет — пишем NULL, а не выдуманное имя: подстановка склеила бы матчи."""
        orphan = int(f"98{int(self.uid, 16) % 100000:05d}")
        database.register_user(orphan, f"orphan_{self.uid}")
        database.assign_user_division(orphan, self.div_id)

        database.batch_insert_matches(
            [(6, self.players[0], orphan)], division_id=self.div_id, season_id=self.season_id
        )
        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT player1_team, player2_team FROM matches WHERE round_number = 6 AND division_id = ?",
                (self.div_id,)
            ).fetchone()
        self.assertEqual(row["player1_team"], self._team_of(self.players[0]))
        self.assertIsNone(row["player2_team"])


class TestBackfillMigration(GeneratedMatchesTestBase):
    def test_backfill_repairs_a_schedule_generated_before_the_fix(self):
        """Старые строки на боевой базе чинятся миграцией, без перегенерации тура."""
        p1, p2 = self.players[0], self.players[1]
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, status, division_id, season_id)"
                " VALUES (7, ?, ?, 'pending', ?, ?)",
                (p1, p2, self.div_id, self.season_id)
            )
            conn.cursor().execute(
                "DELETE FROM schema_migrations WHERE version = '013_backfill_match_team_names'"
            )

        # Проверяем саму колонку, а не get_matches_by_round: читатель теперь
        # добирает клуб ещё и по playerN_id, так что пустая колонка в нём не видна,
        # а остальные читатели (JOIN по имени клуба) без неё по-прежнему слепнут.
        self.assertEqual(self._stored_teams(7), (None, None))

        database.init_db()

        self.assertEqual(self._stored_teams(7), (self._team_of(p1), self._team_of(p2)))
        after = database.get_matches_by_round(7, division_id=self.div_id, season_id=self.season_id)[0]
        self.assertEqual(after["player1_team"], self._team_of(p1))
        self.assertEqual(after["player2_team"], self._team_of(p2))

    def _stored_teams(self, round_number: int) -> tuple:
        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT player1_team, player2_team FROM matches WHERE round_number = ? AND division_id = ?",
                (round_number, self.div_id)
            ).fetchone()
        return row["player1_team"], row["player2_team"]

    def test_backfill_does_not_touch_rows_that_already_have_a_club(self):
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team,"
                " status, division_id, season_id) VALUES (8, ?, ?, 'Ручной Клуб', 'Другой Клуб', 'pending', ?, ?)",
                (self.players[0], self.players[1], self.div_id, self.season_id)
            )
            conn.cursor().execute(
                "DELETE FROM schema_migrations WHERE version = '013_backfill_match_team_names'"
            )

        database.init_db()

        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT player1_team, player2_team FROM matches WHERE round_number = 8 AND division_id = ?",
                (self.div_id,)
            ).fetchone()
        self.assertEqual(row["player1_team"], "Ручной Клуб")
        self.assertEqual(row["player2_team"], "Другой Клуб")


if __name__ == "__main__":
    unittest.main()
