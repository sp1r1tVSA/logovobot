import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database


class TestMergePlayerStats(unittest.TestCase):
    """scripts/merge_player_stats.py: два имени одного игрока → одно имя в событиях и коронах."""

    def _seed(self):
        with database.transaction() as conn:
            c = conn.cursor()
            # the test DB is shared by the whole module, so every test starts from a clean slate
            for table in ("match_events", "squad_players", "matches"):
                c.execute(f"DELETE FROM {table}")
            ids = []
            for rnd in (1, 2, 3):
                c.execute(
                    "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, "
                    "player2_score, status, tournament_type) VALUES (?, 'Бетис', 'Севилья', 3, 0, "
                    "'confirmed', 'league')", (rnd,))
                ids.append(c.lastrowid)
            ev = [(ids[0], "EZZALZULI", "Бетис", "goal", 4), (ids[1], "EZZALZULI", "Бетис", "goal", 2),
                  (ids[1], "ABDE", "Бетис", "goal", 3), (ids[2], "ABDE", "Бетис", "goal", 2),
                  (ids[2], "ABDE", "Бетис", "assist", 1), (ids[2], "ABDE", "Севилья", "goal", 1)]
            for m, name, team, kind, n in ev:
                c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) "
                          "VALUES (?, ?, ?, ?, ?)", (m, name, team, kind, n))
            c.execute("UPDATE matches SET mvp_player = 'ABDE' WHERE id = ?", (ids[2],))
            c.execute("INSERT INTO squad_players (team_name, player_name, position, norm_name, norm_team_name) "
                      "VALUES ('Бетис', 'EZZALZULI', NULL, 'ezzalzuli', 'бетис')")
            c.execute("INSERT INTO squad_players (team_name, player_name, position, norm_name, norm_team_name) "
                      "VALUES ('Бетис', 'ABDE', 'RW', 'abde', 'бетис')")
        return ids

    def _goals(self, name, team="Бетис"):
        with database.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(count), 0) AS n FROM match_events "
                "WHERE player_name = ? AND team_name = ? AND event_type = 'goal'", (name, team)).fetchone()
        return row["n"]

    def test_plan_is_read_only_and_counts_both_names(self):
        self._seed()
        plan = database.plan_player_merge(["abde"], "EZZALZULI", team_name="Бетис")
        self.assertEqual([(i["old_name"], i["goals"], i["assists"]) for i in plan["events"]],
                         [("ABDE", 5, 1)])
        self.assertEqual(len(plan["mvp"]), 1)
        self.assertEqual(plan["squad"], [])  # состав — только с include_squad
        self.assertEqual(self._goals("ABDE"), 5)

    def test_apply_folds_stats_and_is_idempotent(self):
        self._seed()
        plan = database.plan_player_merge(["ABDE"], "EZZALZULI", team_name="Бетис")
        self.assertEqual(database.apply_player_merge(plan), {"events": 3, "mvp": 1, "squad": 0})
        self.assertEqual(self._goals("EZZALZULI"), 11)
        self.assertEqual(self._goals("ABDE"), 0)
        # Тёзка в другом клубе не тронут из-за --club.
        self.assertEqual(self._goals("ABDE", "Севилья"), 1)
        with database.transaction() as conn:
            mvp = [r["mvp_player"] for r in conn.execute("SELECT mvp_player FROM matches ORDER BY id")]
        self.assertEqual(mvp, [None, None, "EZZALZULI"])
        plan = database.plan_player_merge(["ABDE"], "EZZALZULI", team_name="Бетис")
        self.assertEqual((plan["events"], plan["mvp"]), ([], []))

    def test_without_club_scope_every_club_is_merged(self):
        self._seed()
        plan = database.plan_player_merge(["ABDE"], "EZZALZULI")
        self.assertEqual({i["team_name"] for i in plan["events"]}, {"Бетис", "Севилья"})

    def test_squad_duplicate_is_dropped_and_position_kept(self):
        self._seed()
        plan = database.plan_player_merge(["ABDE"], "EZZALZULI", team_name="Бетис", include_squad=True)
        self.assertEqual([(i["old_name"], i["action"]) for i in plan["squad"]], [("ABDE", "drop")])
        self.assertEqual(database.apply_player_merge(plan)["squad"], 1)
        with database.transaction() as conn:
            rows = conn.execute("SELECT player_name, position FROM squad_players ORDER BY id").fetchall()
        self.assertEqual([(r["player_name"], r["position"]) for r in rows], [("EZZALZULI", "RW")])

    def test_squad_rename_when_target_absent(self):
        self._seed()
        with database.transaction() as conn:
            conn.execute("DELETE FROM squad_players WHERE player_name = 'EZZALZULI'")
        plan = database.plan_player_merge(["ABDE"], "EZZALZULI", team_name="Бетис", include_squad=True)
        self.assertEqual(plan["squad"][0]["action"], "rename")
        database.apply_player_merge(plan)
        with database.transaction() as conn:
            row = conn.execute("SELECT player_name, norm_name FROM squad_players").fetchone()
        self.assertEqual((row["player_name"], row["norm_name"]), ("EZZALZULI", "ezzalzuli"))

    def test_same_name_is_rejected(self):
        with self.assertRaises(ValueError):
            database.plan_player_merge(["ezzalzuli"], "EZZALZULI")


if __name__ == "__main__":
    unittest.main()
