"""
tests/test_cup_series_markets.py

Роспись общего кубка: рынки игры и рынки серии целиком.

Серия как объект рынка — строка-заголовок в `matches`, «счёт» которой равен
победам в серии. Решение держится на одном обещании: для неё не нужно ни одного
нового правила расчёта. `market_settler` уже умеет сравнивать `1x2` (`2 > 0` →
прошёл первый), `correct_score` (`cs_2_1` против «2:1») и `total_goals`
(«будет ли третья игра» = ТБ2.5 по счёту серии). Поэтому тест проверяет не
только состав исходов, но и то, что действующий расчётчик выносит им правильные
вердикты.

Второй блок — `apply_market_spec`: общая для лиги и кубка запись спеки в
`markets`/`market_selections`. Исход с ценой `None` (вероятность 0.0) в неё не
попадает, потому что `odds_value` — NOT NULL.
"""

import os
import tempfile
import unittest

import database
from services import odds_engine
from services.market_settler import evaluate_market_selection
from services.poisson_odds import TARGET_MARGIN

STAGE = "1/64"
GAME_KEYS = {"1x2", "total_goals", "btts", "individual_total_1", "individual_total_2", "handicap"}
HEADER_KEYS = {"1x2", "correct_score", "total_goals"}


class CupMarketsTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Markets Season', 'active')")
            self.season = cursor.lastrowid
        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        database.provision_cup_stage_line(STAGE, season_id=self.season)
        ok, message = database.open_cup_stage_bets(stage_id)
        self.assertTrue(ok, message)
        rows = database.get_cup_stage_matches(STAGE, season_id=self.season)
        self.game_id = [r for r in rows if not r["is_series_header"]][0]["match_id"]
        self.header_id = [r for r in rows if r["is_series_header"]][0]["match_id"]
        odds_engine.generate_cup_match_markets(self.game_id, "Клуб А", "Клуб Б", season_id=self.season)
        odds_engine.generate_cup_series_markets(self.header_id, "Клуб А", "Клуб Б", season_id=self.season)

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def _markets(self, match_id):
        return {m["market_key"]: {s["selection_key"]: float(s["odds_value"]) for s in m["selections"]}
                for m in odds_engine.get_match_markets(match_id)}

    def _implied(self, odds_map, keys):
        return sum(1.0 / odds_map[k] for k in keys)

    # --- состав росписи ----------------------------------------------------

    def test_01_game_market_set(self):
        markets = self._markets(self.game_id)
        self.assertEqual(set(markets) , GAME_KEYS)
        self.assertNotIn("double_chance", markets, "1X/X2 не наступают, 12 наступает всегда")

    def test_02_game_has_no_draw_selection_anywhere(self):
        markets = self._markets(self.game_id)
        self.assertEqual(set(markets["1x2"]), {"p1", "p2"})
        for selections in markets.values():
            for key in ("x", "draw", "1x", "x2", "12"):
                self.assertNotIn(key, selections)

    def test_03_header_market_set(self):
        markets = self._markets(self.header_id)
        self.assertEqual(set(markets), HEADER_KEYS)
        self.assertEqual(set(markets["1x2"]), {"p1", "p2"})
        self.assertEqual(set(markets["correct_score"]), {"cs_2_0", "cs_2_1", "cs_1_2", "cs_0_2"})
        self.assertEqual(set(markets["total_goals"]), {"over_2.5", "under_2.5"})

    # --- цены --------------------------------------------------------------

    def test_04_every_complete_market_carries_the_margin(self):
        game = self._markets(self.game_id)
        self.assertAlmostEqual(self._implied(game["1x2"], ("p1", "p2")), TARGET_MARGIN, places=2)
        self.assertAlmostEqual(
            self._implied(game["total_goals"], ("over_2.5", "under_2.5")), TARGET_MARGIN, places=2)
        self.assertAlmostEqual(
            self._implied(game["handicap"], ("h1_minus_1.5", "h2_plus_1.5")), TARGET_MARGIN, places=2)

        header = self._markets(self.header_id)
        self.assertAlmostEqual(self._implied(header["1x2"], ("p1", "p2")), TARGET_MARGIN, places=2)
        self.assertAlmostEqual(
            self._implied(header["correct_score"], ("cs_2_0", "cs_2_1", "cs_1_2", "cs_0_2")),
            TARGET_MARGIN, places=2)
        self.assertAlmostEqual(
            self._implied(header["total_goals"], ("over_2.5", "under_2.5")), TARGET_MARGIN, places=2)

    def test_05_third_game_price_is_the_series_total(self):
        """«Будет ли третья игра» — обычный `total_goals` поверх счёта серии."""
        header = self._markets(self.header_id)
        self.assertGreater(header["total_goals"]["over_2.5"], 1.0)
        self.assertAlmostEqual(
            self._implied(header["total_goals"], ("over_2.5", "under_2.5")), TARGET_MARGIN, places=2)

    def test_06_repricing_with_an_unchanged_model_keeps_the_line(self):
        first = self._markets(self.game_id)
        odds_engine.generate_cup_match_markets(self.game_id, "Клуб А", "Клуб Б", season_id=self.season)
        self.assertEqual(first, self._markets(self.game_id),
                         "повторный показ линии не должен шагать к той же модели")

    # --- расчёт серии действующими правилами ------------------------------

    def test_07_series_score_settles_with_existing_rules(self):
        cases = [
            (2, 0, {"1x2": {"p1": "won", "p2": "lost"},
                    "correct_score": {"cs_2_0": "won", "cs_2_1": "lost", "cs_1_2": "lost", "cs_0_2": "lost"},
                    "total_goals": {"over_2.5": "lost", "under_2.5": "won"}}),
            (2, 1, {"1x2": {"p1": "won", "p2": "lost"},
                    "correct_score": {"cs_2_0": "lost", "cs_2_1": "won", "cs_1_2": "lost", "cs_0_2": "lost"},
                    "total_goals": {"over_2.5": "won", "under_2.5": "lost"}}),
            (1, 2, {"1x2": {"p1": "lost", "p2": "won"},
                    "correct_score": {"cs_2_0": "lost", "cs_2_1": "lost", "cs_1_2": "won", "cs_0_2": "lost"},
                    "total_goals": {"over_2.5": "won", "under_2.5": "lost"}}),
            (0, 2, {"1x2": {"p1": "lost", "p2": "won"},
                    "correct_score": {"cs_2_0": "lost", "cs_2_1": "lost", "cs_1_2": "lost", "cs_0_2": "won"},
                    "total_goals": {"over_2.5": "lost", "under_2.5": "won"}}),
        ]
        for s1, s2, expected in cases:
            for market_key, selections in expected.items():
                for selection_key, verdict in selections.items():
                    got = evaluate_market_selection(market_key, selection_key, s1, s2, "finished")
                    self.assertEqual(got, verdict,
                                     f"{market_key}/{selection_key} при счёте серии {s1}:{s2}")

    def test_08_cancelled_series_voids_every_leg(self):
        for market_key, selection_key in (("1x2", "p1"), ("correct_score", "cs_2_0"), ("total_goals", "over_2.5")):
            self.assertEqual(
                evaluate_market_selection(market_key, selection_key, 2, 0, "cancelled"), "voided")

    # --- общая запись спеки ------------------------------------------------

    def test_09_apply_market_spec_skips_unpriceable_selections(self):
        spec = [("test_market", "Тест", "main", 1, [
            ("a", "A", 1.8),
            ("b", "B", None),
        ])]
        odds_engine.apply_market_spec(self.game_id, spec)
        markets = self._markets(self.game_id)
        self.assertEqual(set(markets["test_market"]), {"a"},
                         "вероятность 0.0 не вставляется в NOT NULL `odds_value`")

    def test_10_cup_markets_are_written_once_per_selection(self):
        with database.transaction() as conn:
            rows = conn.cursor().execute(
                "SELECT mk.match_id, mk.market_key, ms.selection_key, COUNT(*) AS n "
                "FROM markets mk JOIN market_selections ms ON ms.market_id = mk.id "
                "GROUP BY mk.match_id, mk.market_key, ms.selection_key HAVING n > 1"
            ).fetchall()
        self.assertEqual([dict(r) for r in rows], [])


if __name__ == "__main__":
    unittest.main()
