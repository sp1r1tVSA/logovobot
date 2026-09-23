"""Повторный скриншот не должен уезжать в неоткрытый тур.

Баг: результат матча принимали дважды. Первый раз он лёг в открытый тур, матч
стал `confirmed` с событиями и выпал из кандидатов. Второй скриншот той же пары
клубов скоринг раздал по остаткам — и выбрал расписание тура, который ещё не
открывали.

Правило: кандидатом может быть матч тура `open` или `closed`. Закрытый тур
проверке не подлежит — в нём остаются долги, и их доигрывают.
"""

import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


class TestLookupSkipsScheduledRounds(unittest.TestCase):
    def setUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.team1 = f"Тур A{uid}"
        self.team2 = f"Тур B{uid}"
        self.division_id = 1
        self.season_id = int(database.get_active_season() or 1)
        self.matches = {}

    def _round(self, round_number: int, is_open: int, deadline: str | None, status: str | None) -> int:
        """Матч пары клубов в туре с заданным состоянием. Возвращает match_id."""
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO matches (player1_team, player2_team, round_number, status, "
                "tournament_type, division_id, season_id) "
                "VALUES (?, ?, ?, 'pending', 'league', ?, ?)",
                (self.team1, self.team2, round_number, self.division_id, self.season_id)
            )
            match_id = cur.lastrowid
            cur.execute(
                "INSERT OR REPLACE INTO rounds (season_id, division_id, round_number, "
                "is_open, deadline, status) VALUES (?, ?, ?, ?, ?, ?)",
                (self.season_id, self.division_id, round_number, is_open, deadline, status)
            )
        self.matches[round_number] = match_id
        return match_id

    def _find(self, exclude_ids=None):
        return database.get_active_match_by_teams(
            self.team1, self.team2, division_id=self.division_id, exclude_ids=exclude_ids
        )

    def test_scheduled_round_is_not_a_candidate(self):
        self._round(11, is_open=0, deadline=None, status="scheduled")
        self.assertIsNone(self._find())

    def test_round_without_status_and_deadline_is_scheduled(self):
        """Строки, вставленные расписанием, идут без `status` — это тоже `scheduled`."""
        self._round(12, is_open=0, deadline=None, status=None)
        self.assertIsNone(self._find())

    def test_closed_round_stays_a_candidate(self):
        """В закрытом туре остаются долги — их доигрывают и вносят."""
        closed = self._round(13, is_open=0, deadline="2020-01-01 00:00:00", status="closed")
        found = self._find()
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], closed)

    def test_open_round_stays_a_candidate(self):
        open_match = self._round(14, is_open=1, deadline="2099-01-01 00:00:00", status="open")
        found = self._find()
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], open_match)

    def test_second_screenshot_does_not_fall_through_to_a_scheduled_round(self):
        """Ровно баг: открытый тур занят, следующий ещё не открывали."""
        open_match = self._round(15, is_open=1, deadline="2099-01-01 00:00:00", status="open")
        self._round(16, is_open=0, deadline=None, status="scheduled")

        first = self._find()
        self.assertEqual(first["id"], open_match)

        second = self._find(exclude_ids={open_match})
        self.assertIsNone(second, "Повторный результат ушёл в неоткрытый тур")

    def test_caption_cannot_force_a_scheduled_round(self):
        """«17 тур» в подписи не открывает тур."""
        self._round(17, is_open=0, deadline=None, status="scheduled")
        found = database.get_active_match_by_teams(
            self.team1, self.team2, caption="17 тур", division_id=self.division_id
        )
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
