"""
tests/test_cup_strength.py

Цена пары общего кубка: сила клуба = сила в СВОЁМ дивизионе + надбавка за класс.

Лиговый индекс силы для кубка не годится дважды: `get_standings` отдаёт таблицу
одного дивизиона (в паре Д4×Д5 одного клуба в смеси просто нет), и нормирован он
внутри дивизиона, то есть «первый снизу Д5» и «первый снизу Д1» получили бы одну
силу. Здесь проверяются ровно те два свойства, ради которых заводится
`config.CUP_DIVISION_CLASS` и `CUP_HOME_ADVANTAGE = 0.0`: класс перевешивает, а
порядок клубов в паре — не перевешивает ничего.

Отдельный блок — арифметика Bo3: ничьей в кубке нет, поэтому вероятность выиграть
игру включает половину ничьей, а четыре счёта серии обязаны давать единицу.
"""

import os
import tempfile
import unittest

import config
import database
from services import cup_strength
from services.poisson_odds import TARGET_MARGIN


class CupStrengthTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Strength Season', 'active')")
            self.season = cursor.lastrowid
            cursor.execute("SELECT id, code FROM divisions")
            self.divisions = {r["code"]: r["id"] for r in cursor.fetchall()}

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def _club(self, team_name: str, div_code: str, telegram_id: int) -> str:
        """Клуб с тренером в дивизионе — единственный источник класса для резолвера."""
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT INTO users (telegram_id, username, team_name, role, division_id, registered_at) "
                "VALUES (?, ?, ?, 'player', ?, datetime('now', '+3 hours'))",
                (telegram_id, f"coach_{telegram_id}", team_name, self.divisions[div_code])
            )
        return team_name

    def _pair(self, div1: str, div2: str):
        """Пара двух клубов разных дивизионов (имена уникальны: `idx_users_team_name_unique`)."""
        t1 = self._club(f"Клуб {div1} A", div1, 1001)
        t2 = self._club(f"Клуб {div2} B", div2, 1002)
        return t1, t2

    # --- лестница классов ---------------------------------------------------

    def test_01_ladder_is_ordered_and_neutral_on_average(self):
        ladder = config.CUP_DIVISION_CLASS
        self.assertEqual(
            [ladder["DIV_1"], ladder["DIV_2"], ladder["DIV_3"], ladder["DIV_4"], ladder["DIV_5"]],
            [3.0, 1.5, 0.0, -1.5, -3.0],
            "Д1 — сильнейший, Д5 — слабейший, шаг одного дивизиона 1.5 s-поинта"
        )
        # Смещение всей лестницы вверх подняло бы голевую планку: `pace_factor`
        # берёт СУММУ сил, и тоталы кубка перестали бы сопоставляться с Лигой.
        self.assertAlmostEqual(sum(ladder.values()), 0.0, places=9)

    def test_02_unknown_division_gets_no_uplift(self):
        self.assertEqual(cup_strength.class_uplift(None), 0.0)
        self.assertEqual(cup_strength.class_uplift("DIV_9"), 0.0)

    def test_03_strength_uses_the_club_own_division_class(self):
        club = self._club("Клуб Д4", "DIV_4", 2001)
        self.assertEqual(
            cup_strength.cup_club_strength(club, season_id=self.season),
            10.0 + config.CUP_DIVISION_CLASS["DIV_4"],
            "Без сыгранных туров остаётся нейтраль лиги (10.0) плюс класс дивизиона"
        )

    # --- зеркальность пары: порядка клубов не существует --------------------

    def test_04_cross_division_pair_favours_the_higher_class(self):
        t4, t5 = self._pair("DIV_4", "DIV_5")
        priced = cup_strength.cup_match_odds(t4, t5, season_id=self.season)
        self.assertGreater(priced["p_game_1"], priced["p_game_2"])
        self.assertLess(priced["odds"]["p1"], priced["odds"]["p2"])

    def test_05_home_advantage_is_not_applied(self):
        """Клуб, написан слева, не становится фаворитом: пару задаёт жеребьёвка."""
        t1, t2 = self._pair("DIV_4", "DIV_4")
        forward = cup_strength.cup_match_odds(t1, t2, season_id=self.season)
        back = cup_strength.cup_match_odds(t2, t1, season_id=self.season)
        self.assertAlmostEqual(forward["p_game_1"], back["p_game_2"], places=6)
        self.assertAlmostEqual(forward["lambda1"], back["lambda2"], places=6)
        self.assertAlmostEqual(forward["p_game_1"], 0.5, places=3,
                               msg="Равные по классу клубы обязаны давать симметричную цену")

    # --- ничьей нет, но основное время остаётся основой --------------------

    def test_06_game_probabilities_sum_to_one(self):
        t1, t2 = self._pair("DIV_2", "DIV_5")
        priced = cup_strength.cup_match_odds(t1, t2, season_id=self.season)
        # Вероятности в ответе округлены до 4 знаков — складываем уже округлённые.
        self.assertAlmostEqual(priced["p_game_1"] + priced["p_game_2"], 1.0, places=3)
        self.assertAlmostEqual(
            priced["p_game_1"],
            priced["p_main_1"] + 0.5 * priced["p_main_x"],
            places=3,
            msg="Ничья основного времени разыгрывается послематчевыми как жребий"
        )

    def test_07_draw_is_not_priced_but_main_time_still_is(self):
        t1, t2 = self._pair("DIV_1", "DIV_3")
        priced = cup_strength.cup_match_odds(t1, t2, season_id=self.season)
        for key in ("x", "1x", "x2", "12"):
            self.assertNotIn(key, priced["odds"], "Ничьей в кубке нет, и её нельзя выставить")
        self.assertAlmostEqual(
            priced["p_main_1"] + priced["p_main_x"] + priced["p_main_2"], 1.0, places=6,
            msg="Тоталы, ОЗ и фора считаются по основному времени, где ничья есть"
        )

    def test_08_handicap_pairs_are_complements(self):
        """Ф2(+1.5) — дополнение к Ф1(-1.5), а не зеркальная Ф1(+1.5).

        `market_settler` считает Ф2 выигравшей при `s1 - s2 <= 1`. Если брать
        `s2 - s1 <= 1`, зеркальному клубу отдаётся цена соседнего рынка, и
        фаворит получает вторую фору дешевле, чем она стоит.
        """
        t1, t2 = self._pair("DIV_1", "DIV_5")
        odds = cup_strength.cup_match_odds(t1, t2, season_id=self.season)["odds"]
        for minus_key, plus_key in (("h1_minus_1.5", "h2_plus_1.5"), ("h2_minus_1.5", "h1_plus_1.5")):
            implied = 1.0 / odds[minus_key] + 1.0 / odds[plus_key]
            self.assertAlmostEqual(implied, TARGET_MARGIN, places=2,
                                   msg=f"{minus_key} + {plus_key} обязаны быть дополнением")

    def test_09_totals_and_btts_are_main_time_markets(self):
        t1, t2 = self._pair("DIV_3", "DIV_3")
        odds = cup_strength.cup_match_odds(t1, t2, season_id=self.season)["odds"]
        for over, under in (("tb15", "tm15"), ("tb25", "tm25"), ("tb35", "tm35")):
            implied = 1.0 / odds[over] + 1.0 / odds[under]
            self.assertAlmostEqual(implied, TARGET_MARGIN, places=2)
        implied = 1.0 / odds["btts_yes"] + 1.0 / odds["btts_no"]
        self.assertAlmostEqual(implied, TARGET_MARGIN, places=2)

    # --- развёртка Bo3 ------------------------------------------------------

    def test_10_series_scores_cover_the_series_once(self):
        t1, t2 = self._pair("DIV_4", "DIV_5")
        series = cup_strength.best_of_three_odds(t1, t2, season_id=self.season)
        probs = series["probs"]
        # Каждый `probs[*]` уже округлён до 4 знаков, поэтому сравнение — до 3.
        self.assertAlmostEqual(
            probs["series_2_0"] + probs["series_2_1"] + probs["series_1_2"] + probs["series_0_2"],
            1.0, places=3
        )
        self.assertAlmostEqual(probs["qualify_1"] + probs["qualify_2"], 1.0, places=3)
        self.assertAlmostEqual(
            probs["third_game"], probs["series_2_1"] + probs["series_1_2"], places=3,
            msg="Третья игра играется ровно в двух счетах — 2:1 и 1:2"
        )

    def test_11_series_markets_carry_the_margin(self):
        t1, t2 = self._pair("DIV_2", "DIV_4")
        series = cup_strength.best_of_three_odds(t1, t2, season_id=self.season)
        odds = series["odds"]
        self.assertAlmostEqual(
            1.0 / odds["p1"] + 1.0 / odds["p2"], TARGET_MARGIN, places=2
        )
        self.assertAlmostEqual(
            1.0 / odds["over_2.5"] + 1.0 / odds["under_2.5"], TARGET_MARGIN, places=2
        )
        scores = sum(1.0 / odds[k] for k in ("series_2_0", "series_2_1", "series_1_2", "series_0_2"))
        self.assertAlmostEqual(scores, TARGET_MARGIN, places=2)

    def test_12_impossible_outcome_has_no_price(self):
        # `market_selections.odds_value` — NOT NULL: нулевую вероятность отдавать
        # нечем, и выборка с `None` обязана выпадать из росписи, а не множиться.
        self.assertIsNone(cup_strength._odd(0.0, TARGET_MARGIN))
        # Достоверный исход дешевле потолка всё равно не продаётся.
        self.assertEqual(cup_strength._odd(1.0, TARGET_MARGIN), 1.01)

    def test_13_margin_is_applied_against_the_player(self):
        """Полное покрытие обязано давать overround `margin`, а не `1 / margin`.

        Перевёрнутая запись (`margin / p`) даёт сумму воображаемых вероятностей
        93%: каждый закрытый набор исходов возвращал бы игроку 107.5% ставки, и
        кубок стал бы убыточным независимо от того, как сыграны матчи.
        """
        t1, t2 = self._pair("DIV_1", "DIV_5")
        odds = cup_strength.cup_match_odds(t1, t2, season_id=self.season)["odds"]
        implied = 1.0 / odds["p1"] + 1.0 / odds["p2"]
        self.assertGreaterEqual(implied, 1.0)


if __name__ == "__main__":
    unittest.main()
