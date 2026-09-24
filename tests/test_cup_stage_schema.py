"""
tests/test_cup_stage_schema.py

Схема общего кубка (миграция 022).

Проверяется то, что молча ломается в проде и не видно по остальному набору тестов:

 1. `cup_stages` есть, колонки называются так же, как в `rounds`, и UNIQUE
    (season_id, stage) действительно один-на-один.
 2. Повторный `init_db()` не роняет миграцию и не дублирует guard-строку.
 3. `cup_series` получила stage_id/winner_source, а (stage_id, series_num) уникальна (027) —
    иначе повторная жеребьёвка удваивает этап.
 4. `matches` получила cup_winner_team и stage_id.
 5. Кубковый матч не берёт `division_id IS NULL`: NULL в проекте читается как
    «дивизион 1», и sentinel 0 — то, что отсекает кубок от лиговых поверхностей.
 6. Дубли в `cup_series` не удаляются: миграция просто не применяется (тот же
    договор, что у predictions).
"""

import os
import sqlite3
import tempfile
import unittest

import database


STAGE = "1/64"


class CupSchemaTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO seasons (name, status) VALUES ('Cup Schema Season', 'active')")
            self.season = c.lastrowid

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def _columns(self, table):
        with database.transaction() as conn:
            rows = conn.cursor().execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1]: r for r in rows}

    def _stage_row(self, stage=STAGE):
        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT * FROM cup_stages WHERE season_id = ? AND stage = ?",
                (self.season, stage)
            ).fetchone()
        return dict(row) if row else None


class TestCupStagesTable(CupSchemaTestCase):

    def test_01_stage_columns_mirror_rounds(self):
        """Предикат гейта один, потому что имена колонок у него одни.

        Если `cup_stages` переименует `is_open`/`bets_open`/`deadline`, общий
        `_evaluate_gate_row` начнёт читать NULL и молча откажет (или, что хуже,
        пропустит) — этот тест ломается первым.
        """
        cup_cols = self._columns("cup_stages")
        shared = {"is_open", "bets_open", "deadline"}
        self.assertTrue(shared <= set(cup_cols), f"в `cup_stages` нет колонок: {shared - set(cup_cols)}")
        for name in shared:
            self.assertIn(name, self._columns("rounds"))

    def test_02_stage_row_defaults_are_closed(self):
        """Новый этап не принимает ставок и не играется, пока админ не скажет."""
        database.create_cup_stage(STAGE, season_id=self.season)
        row = self._stage_row()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_open"], 0)
        self.assertEqual(row["bets_open"], 0)
        self.assertIsNone(row["deadline"])
        self.assertEqual(row["stage_order"], database.CUP_STAGE_ORDER[STAGE])

    def test_03_stage_is_unique_per_season(self):
        database.create_cup_stage(STAGE, season_id=self.season)
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.cursor().execute(
                    "INSERT INTO cup_stages (season_id, stage, stage_order, created_at) "
                    "VALUES (?, ?, 1, datetime('now', '+3 hours'))",
                    (self.season, STAGE)
                )

    def test_04_same_stage_name_in_other_season_is_allowed(self):
        """Кубок сезонный: «1/64» прошлого сезона не мешает текущему."""
        database.create_cup_stage(STAGE, season_id=self.season)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO seasons (name, status) VALUES ('Cup Schema Season 2', 'active')")
            other = c.lastrowid
        stage_id = database.create_cup_stage(STAGE, season_id=other)
        self.assertNotEqual(stage_id, self._stage_row()["id"])
        self.assertIsNotNone(self._stage_row())


class TestMigration022(CupSchemaTestCase):

    def test_05_guard_row_present(self):
        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (database.MIGRATION_022_CUP_GENERAL,)
            ).fetchone()
        self.assertIsNotNone(row, "миграция 022 не зафиксирована в schema_migrations")

    def test_06_re_running_migration_is_noop(self):
        """ALTER на уже добавленные колонки не должен ронять init_db()."""
        before = self._columns("cup_series")
        with database.transaction() as conn:
            self.assertTrue(database._ensure_cup_schema(conn.cursor()))
            conn.cursor().execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                (database.MIGRATION_022_CUP_GENERAL,)
            )
        with database.transaction() as conn:
            self.assertTrue(database._ensure_cup_schema(conn.cursor()))
        database.init_db()
        self.assertEqual(set(before), set(self._columns("cup_series")))

    def test_07_duplicates_block_the_index_and_delete_nothing(self):
        """Правило как у predictions: поверх дублей индекс не создаётся, строки целые."""
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM schema_migrations WHERE version = ?", (database.MIGRATION_022_CUP_GENERAL,))
            # Индекс убирается первым: с ним дубли в таблицу просто не лягут,
            # а сценарий миграции — как раз про базу, где дубли уже есть.
            c.execute("DROP INDEX IF EXISTS idx_cup_series_stage_num_unique")
            c.execute("DELETE FROM cup_series")
            for team2 in ("Клуб А", "Клуб Б"):
                c.execute(
                    "INSERT INTO cup_series (stage, series_num, team1_name, team2_name) "
                    "VALUES (?, 1, ?, ?)",
                    (STAGE, "Клуб В", team2)
                )

        with database.transaction() as conn:
            self.assertFalse(database._ensure_cup_schema(conn.cursor()))
            c = conn.cursor()
            self.assertEqual(c.execute("SELECT COUNT(*) FROM cup_series").fetchone()[0], 2)
            self.assertIsNone(
                c.execute("SELECT 1 FROM schema_migrations WHERE version = ?",
                          (database.MIGRATION_022_CUP_GENERAL,)).fetchone()
            )
            self.assertIsNone(
                c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_cup_series_stage_num_unique'")
                  .fetchone()
            )


class TestCupSeriesAndMatchColumns(CupSchemaTestCase):

    def test_08_series_has_stage_and_winner_source(self):
        cols = self._columns("cup_series")
        self.assertIn("stage_id", cols)
        self.assertIn("winner_source", cols)

    def test_09_series_num_unique_within_stage(self):
        """С миграции 027 ключ — (stage_id, series_num): одноимённые этапы пяти
        кубков дивизионов не должны мешать друг другу, а внутри этапа номер один."""
        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.cursor().execute(
                    "INSERT INTO cup_series (stage, series_num, team1_name, team2_name, stage_id) "
                    "VALUES (?, 1, 'Клуб В', 'Клуб Г', ?)",
                    (STAGE, stage_id)
                )

    def test_10_match_has_winner_and_stage_columns(self):
        cols = self._columns("matches")
        self.assertIn("cup_winner_team", cols)
        self.assertIn("stage_id", cols)

    def test_11_cup_match_never_lands_in_division_one(self):
        """`division_id IS NULL` = дивизион 1, поэтому у кубка там sentinel.

        Проверка не косвенная: матч создаётся через единственный путь записи
        (`create_cup_series_match`), а дальше спрашивается то, что на этом матче
        иначе сломалось бы — лиговый гейт и таблица дивизиона 1.
        """
        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        series = database.get_cup_bracket(STAGE, season_id=self.season)[0]
        match_id = database.create_cup_series_match(series["id"])

        with database.transaction() as conn:
            c = conn.cursor()
            match = dict(c.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone())
        self.assertEqual(match["division_id"], database.CUP_DIVISION_SENTINEL)
        self.assertNotEqual(match["division_id"], 1)
        self.assertEqual(match["tournament_type"], "cup")
        self.assertEqual(match["round_number"], -1)
        self.assertIsNotNone(match["stage_id"])

        # Леговая строка тура с тем же «номером» матчу не подставится: у sentinel-
        # дивизиона строки в `rounds` нет и быть не может.
        with database.transaction() as conn:
            allowed, reason, _msg = database.evaluate_round_betting_gate(
                conn.cursor(), match["round_number"], match["division_id"], self.season,
                match_id=match_id,
            )
        self.assertFalse(allowed)
        self.assertEqual(reason, "ROUND_NOT_FOUND")

        # ...и сыгранный кубковый матч не попадает в таблицу дивизиона 1.
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET status = 'confirmed', player1_score = 2, player2_score = 1, "
                "cup_winner_team = 'Клуб А' WHERE id = ?",
                (match_id,)
            )
        for row in database.get_standings(division_id=1, season_id=self.season):
            self.assertEqual(row["played"], 0, f"кубковый матч попал в таблицу Д1: {row}")


class TestCupStageLadder(unittest.TestCase):

    def test_12_stage_ladder_is_consistent(self):
        self.assertEqual(len(database.CUP_STAGES), len(set(database.CUP_STAGES)))
        self.assertEqual(
            [database.CUP_STAGE_ORDER[s] for s in database.CUP_STAGES],
            list(range(1, len(database.CUP_STAGES) + 1)),
        )
        self.assertEqual(database.CUP_STAGE_ORDER["1/64"], 1)
        self.assertEqual(database.CUP_STAGE_ORDER["final"], len(database.CUP_STAGES))

    def test_13_unique_index_name_is_the_one_the_migration_creates(self):
        """Константа используется и в тесте выше, и в миграции — сверяем имя."""
        self.assertEqual(database.CUP_SERIES_UNIQUE_INDEX, "idx_cup_series_stage_num_unique")
