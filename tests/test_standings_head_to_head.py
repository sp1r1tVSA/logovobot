"""Личные встречи при равенстве очков.

Порядок таблицы: очки → личные встречи клубов, набравших поровну (мини-турнир:
очки, разница, забитые) → общая разница → забитые → победы. Личные встречи
учитываются только когда сыграны все матчи этих клубов между собой, иначе
середина сезона наказывает клуб, ещё не успевший сыграть с соперником.
"""
import unittest
import uuid

import database


def _row(name: str, points: int, gf: int, ga: int, wins: int = 0) -> dict:
    return {
        "team_name": name, "points": points, "goals_scored": gf,
        "goals_conceded": ga, "wins": wins,
    }


def _h2h(*results, unplayed: dict | None = None) -> dict:
    """results: (club1, goals1, club2, goals2); unplayed: {(a, b): n}."""
    h2h: dict = {}
    for t1, g1, t2, g2 in results:
        h2h.setdefault(frozenset((t1, t2)), {"results": [], "unplayed": 0})["results"].append((t1, g1, t2, g2))
    for (a, b), n in (unplayed or {}).items():
        h2h.setdefault(frozenset((a, b)), {"results": [], "unplayed": 0})["unplayed"] += n
    return h2h


def _names(rows: list[dict]) -> list[str]:
    return [r["team_name"] for r in rows]


class TestSortStandings(unittest.TestCase):

    def test_head_to_head_beats_goal_difference(self):
        rows = [_row("B", 6, 10, 2), _row("A", 6, 4, 3)]
        h2h = _h2h(("A", 1, "B", 0))
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["A", "B"])

    def test_points_still_come_first(self):
        rows = [_row("A", 3, 1, 0), _row("B", 6, 2, 1)]
        h2h = _h2h(("A", 1, "B", 0))
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["B", "A"])

    def test_unplayed_meeting_falls_back_to_goal_difference(self):
        rows = [_row("A", 6, 4, 3), _row("B", 6, 10, 2)]
        h2h = _h2h(("A", 1, "B", 0), unplayed={("A", "B"): 1})
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["B", "A"])

    def test_clubs_that_never_met_use_goal_difference(self):
        rows = [_row("A", 6, 4, 3), _row("B", 6, 10, 2)]
        self.assertEqual(_names(database.sort_standings(rows, {})), ["B", "A"])

    def test_two_legs_are_summed(self):
        # A 1:0 B, B 3:1 A — 3 очка у каждого, разница в личных +2 у B.
        rows = [_row("A", 9, 20, 5), _row("B", 9, 8, 6)]
        h2h = _h2h(("A", 1, "B", 0), ("B", 3, "A", 1))
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["B", "A"])

    def test_full_mini_league_tie_uses_overall(self):
        # По победе у каждого, разница и забитые в личных равны (2:2).
        rows = [_row("A", 5, 9, 1), _row("B", 5, 6, 4)]
        h2h = _h2h(("A", 0, "B", 1), ("B", 1, "A", 2))
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["A", "B"])

    def test_level_head_to_head_falls_back_to_overall(self):
        rows = [_row("A", 4, 3, 3), _row("B", 4, 7, 2)]
        h2h = _h2h(("A", 1, "B", 1))
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["B", "A"])

    def test_mini_league_is_reapplied_to_subgroups(self):
        # Четверо на одинаковых очках. Мини-турнир делит их на пары (A, B) и (C, D),
        # внутри каждой пары — снова личная встреча, а не общая разница.
        rows = [
            _row("C", 9, 20, 10),
            _row("B", 9, 15, 10),
            _row("A", 9, 10, 10),
            _row("D", 9, 10, 10),
        ]
        h2h = _h2h(
            ("A", 1, "B", 0), ("C", 1, "A", 0), ("A", 1, "D", 0),
            ("B", 1, "C", 0), ("B", 1, "D", 0),
            ("D", 1, "C", 0),
        )
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["A", "B", "D", "C"])

    def test_incomplete_pair_inside_a_group_disables_head_to_head(self):
        rows = [_row("C", 6, 9, 1), _row("A", 6, 3, 2), _row("B", 6, 4, 4)]
        h2h = _h2h(("A", 1, "B", 0), ("A", 1, "C", 0), unplayed={("B", "C"): 1})
        self.assertEqual(_names(database.sort_standings(rows, h2h)), ["C", "A", "B"])

    def test_full_tie_keeps_given_order(self):
        rows = [_row("A", 3, 2, 1, 1), _row("B", 3, 2, 1, 1)]
        self.assertEqual(_names(database.sort_standings(rows, {})), ["A", "B"])


class TestGetStandingsHeadToHead(unittest.TestCase):
    """Сквозной путь: матчи в БД → порядок `get_standings`."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        base = 970_000_000 + uuid.uuid4().int % 1_000_000
        self.division_id = database.create_division(name=f"H2H Дивизион {self.uid}", code=f"H2H_{self.uid}")
        self.x, self.y, self.z = f"Икс {self.uid}", f"Игрек {self.uid}", f"Зет {self.uid}"
        self.ids = {self.x: base, self.y: base + 1, self.z: base + 2}
        for club, tg_id in self.ids.items():
            database.register_user(tg_id, f"h2h_{tg_id}_{self.uid}", team_name=club)
            database.assign_user_division(tg_id, self.division_id)
        season = database.get_active_season()
        self.season_id = season["id"] if season else 1

        # X 1:0 Y, Y 5:0 Z → X и Y по 3 очка, у Y общая разница лучше (+4 против +1).
        self._add(1, self.x, self.y, 1, 0)
        self._add(2, self.y, self.z, 5, 0)

    def tearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM matches WHERE division_id = ?", (self.division_id,))
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?, ?)", tuple(self.ids.values()))
            c.execute("DELETE FROM divisions WHERE id = ?", (self.division_id,))

    def _add(self, rnd, t1, t2, s1=None, s2=None, status="confirmed"):
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, "
                "player1_score, player2_score, status, division_id, season_id, tournament_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'league')",
                (rnd, self.ids[t1], self.ids[t2], t1, t2, s1, s2, status, self.division_id, self.season_id),
            )

    def _order(self, **kw):
        return _names(database.get_standings(division_id=self.division_id, **kw))

    def test_winner_of_the_meeting_goes_above(self):
        self.assertEqual(self._order(), [self.x, self.y, self.z])

    def test_pending_return_leg_suspends_head_to_head(self):
        self._add(3, self.y, self.x, status="pending")
        self.assertEqual(self._order(), [self.y, self.x, self.z])

    def test_return_leg_beyond_the_snapshot_round_is_ignored(self):
        self._add(3, self.y, self.x, status="pending")
        self.assertEqual(self._order(up_to_round=2), [self.x, self.y, self.z])

    def test_cancelled_fixture_does_not_block_head_to_head(self):
        self._add(3, self.y, self.x, status="cancelled")
        self.assertEqual(self._order(), [self.x, self.y, self.z])

    def test_unplayed_fixture_is_not_counted_in_the_table(self):
        self._add(3, self.y, self.x, status="pending")
        rows = {r["team_name"]: r for r in database.get_standings(division_id=self.division_id)}
        self.assertEqual((rows[self.x]["played"], rows[self.y]["played"], rows[self.z]["played"]), (1, 2, 1))


if __name__ == "__main__":
    unittest.main()
