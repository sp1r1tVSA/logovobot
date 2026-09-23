"""
tests/test_cup_line.py

Линия этапа общего кубка: провижининг строк, выставление и чтение.

Здесь проверяются три решения, которые иначе разъедутся молча.

Первое — квоты лиги в кубке нет. `CENTRAL_MATCHES_PER_ROUND` отбирает четыре
статусные пары тура; сетка кубка уже отобрана жеребьёвкой, и «не статусный» матч
в ней — это выбывший клуб, а не лишняя строка.

Второе — строка-заголовок серии не должна становиться матчем. Она живёт в
`matches` намеренно (иначе для рынков на серию пришлось бы заводить вторую схему
`markets`), но без ID участников и без имён клубов: на эти два ключа завязаны
«Мои матчи», расписание, приёмка отчётов и история клуба.

Третье — кубок не попадает в `bet_markets`: `odd_x` там NOT NULL, а ничьей в
кубке нет, и каждая читалка тайлов джойнит `rounds`.
"""

import os
import tempfile
import unittest

import database
from services import betting_engine, odds_engine

STAGE = "1/64"


class CupLineTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Line Season', 'active')")
            self.season = cursor.lastrowid

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    # --- helpers -----------------------------------------------------------

    def _seed_stage(self, pairs, open_line=True):
        database.create_cup_series(STAGE, pairs, season_id=self.season)
        stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        database.provision_cup_stage_line(STAGE, season_id=self.season)
        if open_line:
            ok, message = database.open_cup_stage_bets(stage_id)
            self.assertTrue(ok, message)
        return stage_id

    def _one_pair(self, open_line=True):
        return self._seed_stage([("Клуб А", "Клуб Б")], open_line=open_line)

    def _rows(self):
        return database.get_cup_stage_matches(STAGE, season_id=self.season)

    def _match_row(self, match_id):
        with database.transaction() as conn:
            row = conn.cursor().execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        return dict(row)

    def _selection_keys(self, match_id, market_key):
        markets = {m["market_key"]: m for m in odds_engine.get_match_markets(match_id)}
        target = markets.get(market_key)
        return {s["selection_key"] for s in target["selections"]} if target else set()

    # --- 1-2. провижининг ---------------------------------------------------

    def test_01_provision_creates_three_games_and_one_header_per_series(self):
        pairs = [("Клуб 1", "Клуб 2"), ("Клуб 3", "Клуб 4")]
        database.create_cup_series(STAGE, pairs, season_id=self.season)
        stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]

        report = database.provision_cup_stage_line(STAGE, season_id=self.season)
        self.assertEqual(report["series"], 2)
        self.assertEqual(report["created_games"], 6, "Bo3: по три игры на серию")
        self.assertEqual(report["created_headers"], 2)

        rows = self._rows()
        self.assertEqual(len(rows), 8)
        for series_id in {r["series_id"] for r in rows}:
            series_rows = [r for r in rows if r["series_id"] == series_id]
            headers = [r for r in series_rows if r["is_series_header"]]
            games = [r for r in series_rows if not r["is_series_header"]]
            self.assertEqual(len(headers), 1)
            self.assertEqual(sorted(r["game_num_in_series"] for r in games), [1, 2, 3])
        self.assertEqual(database.count_cup_stage_matches(stage_id), 8)

    def test_02_provision_is_idempotent(self):
        self._one_pair(open_line=False)
        before = sorted(r["match_id"] for r in self._rows())
        again = database.provision_cup_stage_line(STAGE, season_id=self.season)
        self.assertEqual(again["created_games"], 0)
        self.assertEqual(again["created_headers"], 0)
        self.assertEqual(sorted(r["match_id"] for r in self._rows()), before,
                         "повторный вызов не должен двигать состав линии")
        # Заголовок идемпотентен и поштучно: линия пересобирается на каждом показе.
        header_id = [r for r in self._rows() if r["is_series_header"]][0]["match_id"]
        series_id = [r for r in self._rows() if r["is_series_header"]][0]["series_id"]
        self.assertEqual(database.create_cup_series_header(series_id), header_id)
        self.assertEqual(len([r for r in self._rows() if r["is_series_header"]]), 1)

    def test_03_provision_requires_the_stage_row(self):
        with self.assertRaises(ValueError):
            database.provision_cup_stage_line("1/32", season_id=self.season)

    # --- 3-4. форма строки-заголовка ---------------------------------------

    def test_04_header_row_carries_no_players_and_no_teams(self):
        self._one_pair()
        header = [r for r in self._rows() if r["is_series_header"]][0]
        row = self._match_row(header["match_id"])
        self.assertEqual(row["is_series_header"], 1)
        self.assertIsNone(row["player1_id"])
        self.assertIsNone(row["player2_id"])
        self.assertIsNone(row["player1_team"])
        self.assertIsNone(row["player2_team"])
        self.assertIsNone(row["game_num_in_series"])
        self.assertEqual(row["division_id"], database.CUP_DIVISION_SENTINEL)
        self.assertEqual(row["round_number"], -1)
        self.assertEqual(row["tournament_type"], "cup")
        # Игры серии, наоборот, привязаны к тренерам — их принимают в кабинете.
        game = [r for r in self._rows() if not r["is_series_header"]][0]
        self.assertEqual(self._match_row(game["match_id"])["game_num_in_series"], 1)

    def test_05_header_is_invisible_in_my_matches(self):
        with database.transaction() as conn:
            cursor = conn.cursor()
            for tid, team in ((7001, "Клуб А"), (7002, "Клуб Б")):
                cursor.execute(
                    "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                    "VALUES (?, ?, ?, 'player', datetime('now', '+3 hours'))",
                    (tid, f"coach_{tid}", team)
                )
        self._one_pair()
        rows = self._rows()
        game_ids = {r["match_id"] for r in rows if not r["is_series_header"]}
        header_id = [r for r in rows if r["is_series_header"]][0]["match_id"]

        mine = {m["id"] for m in database.get_active_matches(7001)}
        self.assertTrue(game_ids <= mine, "все три игры серии видны тренеру")
        self.assertNotIn(header_id, mine, "заголовок серии — не матч тренера")

    def test_06_cup_never_enters_the_legacy_tile_line(self):
        self._one_pair()
        betting_engine.generate_stage_markets(STAGE, season_id=self.season)
        cup_ids = {r["match_id"] for r in self._rows()}
        with database.transaction() as conn:
            rows = conn.cursor().execute("SELECT match_id FROM bet_markets").fetchall()
        self.assertEqual([r["match_id"] for r in rows], [],
                         "для кубка в bet_markets нечего делать: odd_x там NOT NULL")
        self.assertFalse(cup_ids & {r["match_id"] for r in database.get_active_bet_markets()})

    # --- 5-7. состав и цены линии ------------------------------------------

    def test_07_stage_line_has_no_central_match_quota(self):
        pairs = [(f"Клуб {i}A", f"Клуб {i}B") for i in range(6)]
        self._seed_stage(pairs)
        selected = betting_engine.select_stage_matches(STAGE, season_id=self.season)
        self.assertEqual(len(selected), 6 * 4, "шесть серий = 18 игр + 6 заголовков")
        self.assertGreater(len(selected), betting_engine.CENTRAL_MATCHES_PER_ROUND)
        priced = betting_engine.generate_stage_markets(STAGE, season_id=self.season)
        self.assertEqual(len(priced), len(selected))

    def test_08_closed_line_prices_nothing(self):
        self._seed_stage([("Клуб А", "Клуб Б")], open_line=False)
        self.assertEqual(betting_engine.generate_stage_markets(STAGE, season_id=self.season), [])
        with database.transaction() as conn:
            self.assertEqual(conn.cursor().execute("SELECT COUNT(*) FROM markets").fetchone()[0], 0)

    def test_09_starting_the_stage_closes_and_freezes_the_line(self):
        stage_id = self._one_pair()
        betting_engine.generate_stage_markets(STAGE, season_id=self.season)
        with database.transaction() as conn:
            self.assertGreater(
                conn.cursor().execute("SELECT COUNT(*) FROM markets WHERE status = 'open'").fetchone()[0], 0
            )

        ok, message = database.start_cup_stage(stage_id)
        self.assertTrue(ok, message)

        with database.transaction() as conn:
            open_markets = conn.cursor().execute(
                "SELECT COUNT(*) FROM markets WHERE status = 'open'"
            ).fetchone()[0]
            locked = conn.cursor().execute(
                "SELECT COUNT(*) FROM market_selections WHERE status = 'active'"
            ).fetchone()[0]
        self.assertEqual(open_markets, 0, "стартованный этап не продаётся")
        self.assertEqual(locked, 0, "исходы заперты тем же переходом, что и у лиги")
        self.assertEqual(betting_engine.generate_stage_markets(STAGE, season_id=self.season), [],
                         "пересчёт не возвращает в линию закрытый этап")
        with database.transaction() as conn:
            self.assertEqual(
                conn.cursor().execute("SELECT COUNT(*) FROM markets WHERE status = 'open'").fetchone()[0], 0
            )

    def test_10_line_reader_groups_games_under_their_header(self):
        self._seed_stage([("Клуб А", "Клуб Б"), ("Клуб В", "Клуб Г")])
        betting_engine.generate_stage_markets(STAGE, season_id=self.season)
        line = database.get_cup_stage_line(STAGE, season_id=self.season)
        self.assertEqual(len(line["series"]), 2)
        self.assertEqual(line["stage"]["bets_open"], 1)
        first = line["series"][0]
        self.assertEqual(first["team1_name"], "Клуб А")
        self.assertEqual(len(first["games"]), 3)
        header = first["header"]
        self.assertTrue(header["is_series_header"])
        self.assertTrue(header["is_line"])
        self.assertEqual(header["team1_name"], "Клуб А", "клубы заголовка берутся из cup_series")
        for key in ("p1", "p2", "tb25", "tm25"):
            self.assertIn(key, header["odds"])
        for game in first["games"]:
            self.assertNotIn("x", game["odds"])
            self.assertEqual(game["team1_name"], "Клуб А")

    def test_11_finished_matches_drop_out_of_the_line(self):
        self._seed_stage([("Клуб А", "Клуб Б")])
        betting_engine.generate_stage_markets(STAGE, season_id=self.season)
        game = [r for r in self._rows() if not r["is_series_header"]][0]["match_id"]
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET status = 'confirmed', player1_score = 2, player2_score = 1 WHERE id = ?",
                (game,)
            )
        line = database.get_cup_stage_line(STAGE, season_id=self.season)
        self.assertNotIn(game, [g["match_id"] for g in line["series"][0]["games"]])
        self.assertNotIn(game, [r["match_id"] for r in
                               betting_engine.select_stage_matches(STAGE, season_id=self.season)])

    # --- 8. ники тренеров под клубами ---------------------------------------

    def test_12_series_carry_coach_usernames(self):
        from api import routes_cup

        with database.transaction() as conn:
            cursor = conn.cursor()
            # Регистр и пробелы в team_name не должны ломать сопоставление:
            # LOWER() в SQLite кириллицу не складывает.
            for tid, team, username in ((7101, " клуб а ", "coach_a"), (7102, "Клуб В", " ")):
                cursor.execute(
                    "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                    "VALUES (?, ?, ?, 'player', datetime('now', '+3 hours'))",
                    (tid, username, team)
                )
        stage_id = self._seed_stage([("Клуб А", "Клуб Б"), ("Клуб В", "Клуб Г")])
        stage = database.get_cup_stage_by_id(stage_id)

        for series in (routes_cup._load_bracket(stage), routes_cup._load_line(stage)["series"]):
            by_team = {s["team1_name"]: s for s in series}
            self.assertEqual(by_team["Клуб А"]["team1_username"], "coach_a")
            self.assertIsNone(by_team["Клуб А"]["team2_username"], "у клуба нет тренера")
            self.assertIsNone(by_team["Клуб В"]["team1_username"], "пустой ник не выводится")


if __name__ == "__main__":
    unittest.main()
