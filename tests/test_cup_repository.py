"""
tests/test_cup_repository.py

Репозиторий стадий и сетки общего кубка.

Сетка пишется один раз на сезон и дальше живёт в ней результат, поэтому валидация
вся — до первой записи: сетка, где один клуб стоит в двух парах, даёт этап, в котором
клуб одновременно выбывает и проходит, а чинится только переигрыванием матчей.

Отдельно проверяется канонизация имён: пары владельцем присылаются живым текстом
(«Ман Сити», «МЮ»), а `users.team_name`, линия и резолвер OCR работают с каноном из
`club_registry`. Два написания одного клуба в сетке — это два участника.
"""

import os
import tempfile
import unittest

import database


STAGE = "1/64"


class CupRepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO seasons (name, status) VALUES ('Cup Repo Season', 'active')")
            self.season = c.lastrowid

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def _series_rows(self, stage=STAGE):
        with database.transaction() as conn:
            rows = conn.cursor().execute(
                "SELECT * FROM cup_series WHERE stage = ? ORDER BY series_num", (stage,)
            ).fetchall()
        return [dict(r) for r in rows]


class TestCupStages(CupRepositoryTestCase):

    def test_01_unknown_stage_is_rejected(self):
        with self.assertRaises(ValueError):
            database.create_cup_stage("1/128", season_id=self.season)
        self.assertIsNone(database.get_cup_stage("1/128", season_id=self.season))

    def test_02_create_is_idempotent(self):
        first = database.create_cup_stage(STAGE, season_id=self.season)
        second = database.create_cup_stage(STAGE, season_id=self.season)
        self.assertEqual(first, second)
        self.assertEqual(len(database.list_cup_stages(season_id=self.season)), 1)

    def test_03_stage_order_follows_the_ladder(self):
        for stage in ("final", STAGE, "1/8"):
            database.create_cup_stage(stage, season_id=self.season)
        orders = [s["stage_order"] for s in database.list_cup_stages(season_id=self.season)]
        self.assertEqual(orders, sorted(orders), "этапы должны выдаваться в порядке игры")
        self.assertEqual([s["stage"] for s in database.list_cup_stages(season_id=self.season)],
                         [STAGE, "1/8", "final"])


class TestCupSeriesSeeding(CupRepositoryTestCase):

    def test_04_empty_pairs_rejected(self):
        with self.assertRaises(ValueError):
            database.create_cup_series(STAGE, [], season_id=self.season)
        self.assertEqual(database.list_cup_stages(season_id=self.season), [])

    def test_05_duplicate_club_across_pairs_rejected_and_nothing_written(self):
        with self.assertRaises(ValueError) as ctx:
            database.create_cup_series(
                STAGE, [("Клуб А", "Клуб Б"), ("Клуб В", "Клуб А")], season_id=self.season
            )
        self.assertIn("дважды", str(ctx.exception))
        self.assertEqual(self._series_rows(), [])
        # Отказ случился до первой записи — стадия не должна остаться заведённой.
        self.assertEqual(database.list_cup_stages(season_id=self.season), [])

    def test_06_same_club_twice_in_one_pair_rejected(self):
        with self.assertRaises(ValueError):
            database.create_cup_series(STAGE, [("Клуб А", "клуб а")], season_id=self.season)
        self.assertEqual(self._series_rows(), [])

    def test_07_aliases_are_canonicalised_before_the_write(self):
        ids = database.create_cup_series(
            STAGE, [("Ман Сити", "МЮ"), ("Бавария", "Реал Мадрид")], season_id=self.season
        )
        self.assertEqual(len(ids), 2)
        rows = self._series_rows()
        self.assertEqual(rows[0]["team1_name"], "Манчестер Сити")
        self.assertEqual(rows[0]["team2_name"], "Манчестер Юнайтед")
        self.assertEqual([r["series_num"] for r in rows], [1, 2])
        self.assertEqual(rows[1]["team1_name"], "Бавария")

    def test_08_alias_and_canon_of_one_club_count_as_one_participant(self):
        with self.assertRaises(ValueError) as ctx:
            database.create_cup_series(
                STAGE, [("Ман Сити", "Бавария"), ("Реал Мадрид", "Манчестер Сити")],
                season_id=self.season,
            )
        self.assertIn("дважды", str(ctx.exception))
        self.assertEqual(self._series_rows(), [])

    def test_09_second_seeding_of_the_same_stage_refused(self):
        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        with self.assertRaises(ValueError) as ctx:
            database.create_cup_series(STAGE, [("Клуб В", "Клуб Г")], season_id=self.season)
        self.assertIn("уже заведена", str(ctx.exception))
        self.assertEqual(len(self._series_rows()), 1)

    def test_10_series_created_closed_and_linked_to_stage(self):
        stage_id = database.create_cup_stage(STAGE, season_id=self.season)
        series_id, = database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        row = self._series_rows()[0]
        self.assertEqual(row["stage_id"], stage_id)
        self.assertEqual(row["status"], "active")
        self.assertEqual((row["team1_wins"], row["team2_wins"]), (0, 0))
        self.assertIsNone(row["winner_name"])

    def test_11_pair_shape_is_validated(self):
        with self.assertRaises(ValueError):
            database.create_cup_series(STAGE, [("Клуб А",)], season_id=self.season)
        with self.assertRaises(ValueError):
            database.create_cup_series(STAGE, [("Клуб А", "")], season_id=self.season)
        self.assertEqual(self._series_rows(), [])


class TestCupBracketReader(CupRepositoryTestCase):

    def test_12_bracket_is_empty_without_a_seeding(self):
        database.create_cup_stage(STAGE, season_id=self.season)
        self.assertEqual(database.get_cup_bracket(STAGE, season_id=self.season), [])

    def test_13_bracket_orders_by_series_num_not_by_id(self):
        with database.transaction() as conn:
            c = conn.cursor()
            stage_id = c.execute(
                "INSERT INTO cup_stages (season_id, stage, stage_order, created_at) "
                "VALUES (?, ?, 1, datetime('now', '+3 hours'))",
                (self.season, STAGE),
            ).lastrowid
            # 3-я серия вставлена первой: порядок читателя обязан задаваться номером.
            for num, t1 in ((3, "Клуб К"), (1, "Клуб А"), (2, "Клуб Г")):
                c.execute(
                    "INSERT INTO cup_series (stage, series_num, team1_name, team2_name, status, stage_id) "
                    "VALUES (?, ?, ?, 'Клуб Б', 'active', ?)",
                    (STAGE, num, t1, stage_id),
                )
        self.assertEqual(
            [s["team1_name"] for s in database.get_cup_bracket(STAGE, season_id=self.season)],
            ["Клуб А", "Клуб Г", "Клуб К"],
        )

    def test_14_bracket_of_another_season_is_not_visible(self):
        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO seasons (name, status) VALUES ('Cup Repo Season 2', 'active')")
            other = c.lastrowid
        self.assertEqual(database.get_cup_bracket(STAGE, season_id=other), [])


class TestCupSeriesMatches(CupRepositoryTestCase):

    def _one_series(self):
        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        return database.get_cup_bracket(STAGE, season_id=self.season)[0]

    def test_15_game_row_carries_the_cup_scope(self):
        series = self._one_series()
        match_id = database.create_cup_series_match(series["id"], game_num=1)
        with database.transaction() as conn:
            row = dict(conn.cursor().execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone())
        self.assertEqual(row["cup_series_id"], series["id"])
        self.assertEqual(row["cup_stage"], STAGE)
        self.assertEqual(row["game_num_in_series"], 1)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["player1_score"])

    def test_16_coaches_are_resolved_from_club_names(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO users (telegram_id, username, team_name, division_id, role) "
                "VALUES (410001, 'coach_a', 'Клуб А', 4, 'user')"
            )
        series = self._one_series()
        match_id = database.create_cup_series_match(series["id"])
        with database.transaction() as conn:
            row = dict(conn.cursor().execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone())
        self.assertEqual(row["player1_id"], 410001)
        self.assertIsNone(row["player2_id"], "у клуба без тренера id остаётся пустым, имя — обязательным")
        self.assertEqual(row["player2_team"], "Клуб Б")

    def test_17_game_numbers_are_unique_within_a_series(self):
        series = self._one_series()
        database.create_cup_series_match(series["id"], game_num=1)
        with self.assertRaises(ValueError):
            database.create_cup_series_match(series["id"], game_num=1)
        second = database.create_cup_series_match(series["id"], game_num=2)
        self.assertGreater(second, 0)

    def test_18_game_num_must_be_positive_and_series_must_exist(self):
        series = self._one_series()
        with self.assertRaises(ValueError):
            database.create_cup_series_match(series["id"], game_num=0)
        with self.assertRaises(ValueError):
            database.create_cup_series_match(999999, game_num=1)

    def test_19_match_count_feeds_the_line_opening_guard(self):
        series = self._one_series()
        stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        self.assertEqual(database.count_cup_stage_matches(stage_id), 0)
        database.create_cup_series_match(series["id"])
        self.assertEqual(database.count_cup_stage_matches(stage_id), 1)
        database.create_cup_series_match(series["id"], game_num=2)
        self.assertEqual(database.count_cup_stage_matches(stage_id), 2)


class TestCupSeriesCard(CupRepositoryTestCase):
    """Карточка серии в /cup: сама серия и её игры, без строки-заголовка."""

    def test_series_and_its_games(self):
        series_id = database.create_cup_series(STAGE, [("Бавария", "МЮ")], season_id=self.season)[0]
        database.provision_cup_stage_line(STAGE, season_id=self.season, games_per_series=2)

        series = database.get_cup_series(series_id)
        self.assertEqual((series["team1_name"], series["team2_name"]), ("Бавария", "Манчестер Юнайтед"))
        self.assertIsNotNone(series["stage_id"])

        games = database.get_cup_series_games(series_id)
        self.assertEqual([g["game_num_in_series"] for g in games], [1, 2])
        self.assertTrue(all(g["status"] == "pending" for g in games))
        self.assertIsNone(database.get_cup_series(series_id + 999))
