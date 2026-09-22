"""
tests/test_cup_bet_placement.py

Приём ставки на общий кубок: что проходит, что отклоняется и какой ценой.

Главная проверка — отказ `MARKET_NOT_OFFERED`. Ничьей в кубке нет, поэтому `x`
там не «закрытый рынок», а исход, которого не бывает. Отличать одно от другого
нужно и игроку, и Mini App: для лиги `x` без рынка — ошибка кэша, для кубка —
исход, который нельзя выиграть. Реализация одна (`database.cup_outcome_missing_error`)
и вызывается из двух слоёв — `RiskEngine` и `place_user_bet` — чтобы отказ не
зависел от того, через какую поверхность пришла ставка.

Отдельно закреплено, что кубок не пользуется legacy-`bet_markets`: если бы
фолбэк срабатывал, ничью можно было бы купить по цене из соседней таблицы.
"""

import os
import tempfile
import unittest

import database
from services import betting_engine, odds_engine
from services.betting_limits import BettingLimitsService
from services.risk_engine import RiskEngine

STAGE = "1/64"


class CupBetPlacementTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Bets Season', 'active')")
            self.season = cursor.lastrowid

        self.club_a, self.club_b = "Клуб А", "Клуб Б"
        self.club_c, self.club_d = "Клуб В", "Клуб Г"
        self.coach_a, self.coach_b, self.bettor = 8001, 8002, 8999
        with database.transaction() as conn:
            cursor = conn.cursor()
            for tid, team in ((self.coach_a, self.club_a), (self.coach_b, self.club_b)):
                cursor.execute(
                    "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                    "VALUES (?, ?, ?, 'player', datetime('now', '+3 hours'))",
                    (tid, f"coach_{tid}", team)
                )
            cursor.execute(
                "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                "VALUES (?, 'bettor', 'Клуб Х', 'player', datetime('now', '+3 hours'))",
                (self.bettor,)
            )
        database.add_coins(self.bettor, 50_000)
        database.add_coins(self.coach_a, 50_000)

        self.stage_id = self._seed_stage([
            (self.club_a, self.club_b),
            (self.club_c, self.club_d),
        ])
        betting_engine.generate_stage_markets(STAGE, season_id=self.season)
        rows = database.get_cup_stage_matches(STAGE, season_id=self.season)
        self.game_1 = [r for r in rows if not r["is_series_header"] and r["series_num"] == 1][0]["match_id"]
        self.game_2 = [r for r in rows if not r["is_series_header"] and r["series_num"] == 2][0]["match_id"]
        self.header_1 = [r for r in rows if r["is_series_header"] and r["series_num"] == 1][0]["match_id"]

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def _seed_stage(self, pairs):
        database.create_cup_series(STAGE, pairs, season_id=self.season)
        stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        database.provision_cup_stage_line(STAGE, season_id=self.season)
        ok, message = database.open_cup_stage_bets(stage_id)
        self.assertTrue(ok, message)
        return stage_id

    def _odd(self, match_id, market_key, selection_key):
        markets = {m["market_key"]: m for m in odds_engine.get_match_markets(match_id)}
        for s in markets[market_key]["selections"]:
            if s["selection_key"] == selection_key:
                return float(s["odds_value"])
        raise AssertionError(f"{market_key}/{selection_key} not offered for match #{match_id}")

    def _bet(self, user_id, selections, amount=100):
        return database.place_user_bet(user_id, amount, selections)

    # --- 1. принимаем -------------------------------------------------------

    def test_01_cup_single_is_accepted(self):
        balance_before = database.get_or_create_wallet(self.bettor)["balance"]
        odd = self._odd(self.game_1, "1x2", "p1")
        ok, result = self._bet(self.bettor, [{"match_id": self.game_1, "outcome": "p1"}])
        self.assertTrue(ok, result)
        self.assertEqual(database.get_or_create_wallet(self.bettor)["balance"], balance_before - 100)
        with database.transaction() as conn:
            cursor = conn.cursor()
            bet = cursor.execute("SELECT * FROM user_bets WHERE id = ?", (result,)).fetchone()
            item = cursor.execute("SELECT * FROM bet_items WHERE bet_id = ?", (result,)).fetchone()
        self.assertEqual(bet["status"], "pending")
        self.assertEqual(bet["bet_type"], "single")
        self.assertAlmostEqual(float(bet["total_odd"]), odd, places=2)
        self.assertIsNotNone(item["market_id"], "нога привязана к реляционному рынку")
        self.assertIsNotNone(item["selection_id"])

    def test_02_series_market_is_accepted(self):
        ok, result = self._bet(self.bettor, [{"match_id": self.header_1, "outcome": "cs_2_0"}])
        self.assertTrue(ok, result)

    # --- 2-5. MARKET_NOT_OFFERED ------------------------------------------

    def test_03_draw_outcome_is_refused_with_its_own_code(self):
        ok, result = self._bet(self.bettor, [{"match_id": self.game_1, "outcome": "x"}])
        self.assertFalse(ok)
        self.assertEqual(result["error"], "MARKET_NOT_OFFERED")
        self.assertEqual(result["match_id"], self.game_1)
        self.assertEqual(result["outcome"], "x")
        self.assertIn("кубка", result["message"])

    def test_04_draw_alias_is_refused_too(self):
        for alias in ("X", "draw", "1x", "x2", "12"):
            ok, result = self._bet(self.bettor, [{"match_id": self.game_1, "outcome": alias}])
            self.assertFalse(ok, alias)
            self.assertEqual(result["error"], "MARKET_NOT_OFFERED", alias)

    def test_05_risk_engine_gives_the_same_verdict(self):
        """Слои не разъезжаются: код отказа одинаков и до списания, и в нём самом."""
        decision = RiskEngine.evaluate_bet(
            user_id=self.bettor, amount=100,
            selections=[{"match_id": self.game_1, "outcome": "x"}],
            division_id=database.CUP_DIVISION_SENTINEL,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "MARKET_NOT_OFFERED")

    def test_06_header_offers_no_game_markets(self):
        for outcome in ("it1_over_1.5", "h1_minus_1.5", "btts_yes"):
            ok, result = self._bet(self.bettor, [{"match_id": self.header_1, "outcome": outcome}])
            self.assertFalse(ok, outcome)
            self.assertEqual(result["error"], "MARKET_NOT_OFFERED", outcome)

    def test_07_legacy_tile_line_cannot_price_a_cup_outcome(self):
        """Если в `bet_markets` появится строка кубка, `x` всё равно не купят.

        Сам кубок туда не пишет (`get_cup_stage_line`), но защита должна держать
        и случай случайной строки: фолбэк по legacy-колонке для кубка отключён.
        """
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, "
                "odd_tb25, odd_tm25, odd_btts_yes, odd_btts_no, is_active, created_at) "
                "VALUES (?, 1, 'Клуб А', 'Клуб Б', 1.9, 3.0, 4.0, 1.8, 1.9, 1.7, 2.0, 1, datetime('now', '+3 hours'))",
                (self.game_1,)
            )
        ok, result = self._bet(self.bettor, [{"match_id": self.game_1, "outcome": "x"}])
        self.assertFalse(ok)
        self.assertEqual(result["error"], "MARKET_NOT_OFFERED")

    # --- 6-8. состояние линии ---------------------------------------------

    def test_08_closed_line_refuses_and_names_the_stage(self):
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE cup_stages SET bets_open = 0 WHERE id = ?", (self.stage_id,))
        ok, result = self._bet(self.bettor, [{"match_id": self.game_1, "outcome": "p1"}])
        self.assertFalse(ok)
        self.assertIn(STAGE, str(result))

    def test_09_started_stage_refuses(self):
        ok, message = database.start_cup_stage(self.stage_id)
        self.assertTrue(ok, message)
        ok, result = self._bet(self.bettor, [{"match_id": self.game_1, "outcome": "p1"}])
        self.assertFalse(ok)
        self.assertIn("уже открыт", str(result))

    def test_10_nothing_is_debited_on_a_refusal(self):
        balance_before = database.get_or_create_wallet(self.bettor)["balance"]
        self._bet(self.bettor, [{"match_id": self.game_1, "outcome": "x"}])
        self.assertEqual(database.get_or_create_wallet(self.bettor)["balance"], balance_before)
        with database.transaction() as conn:
            self.assertEqual(
                conn.cursor().execute("SELECT COUNT(*) FROM user_bets").fetchone()[0], 0)

    # --- 9. 322-защита на серии -------------------------------------------

    def test_11_manager_cannot_bet_his_own_series(self):
        ok, result = self._bet(self.coach_a, [{"match_id": self.header_1, "outcome": "p1"}])
        self.assertFalse(ok)
        self.assertEqual(result["error"], "SELF_BET_PROHIBITED")

    def test_12_manager_cannot_bet_his_own_game(self):
        ok, result = self._bet(self.coach_a, [{"match_id": self.game_1, "outcome": "p1"}])
        self.assertFalse(ok)
        self.assertEqual(result["error"], "SELF_BET_PROHIBITED")

    # --- 10-11. корреляция ног одной серии ---------------------------------

    def test_13_two_legs_of_one_series_are_not_an_express(self):
        ok, result = self._bet(self.bettor, [
            {"match_id": self.game_1, "outcome": "p1"},
            {"match_id": self.header_1, "outcome": "p1"},
        ])
        self.assertFalse(ok)
        self.assertEqual(result["error"], "SERIES_CORRELATED")
        self.assertEqual(result["series_id"], 1)

    def test_14_legs_of_different_series_form_an_express(self):
        ok, result = self._bet(self.bettor, [
            {"match_id": self.game_1, "outcome": "p1"},
            {"match_id": self.game_2, "outcome": "p1"},
        ])
        self.assertTrue(ok, result)
        with database.transaction() as conn:
            bet = conn.cursor().execute("SELECT * FROM user_bets WHERE id = ?", (result,)).fetchone()
        self.assertEqual(bet["bet_type"], "express")
        self.assertGreater(float(bet["total_odd"]), 1.0)

    # --- 12. дивизионные лимиты кубку не указ ------------------------------

    def test_15_division_limits_do_not_bind_the_cup(self):
        """Матч без дивизиона не получает лимиты Дивизиона 1.

        Sentinel 0 выбран именно поэтому: `division_id or 1` подмешал бы кубок в
        чужие настройки, а `NULL` читается в проекте как «дивизион 1».
        """
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO risk_limits_config (scope_type, scope_id, limit_key, limit_value, updated_at) "
                "VALUES ('division', 1, 'max_bet', 1000, datetime('now', '+3 hours'))"
            )
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(self.bettor, division_id=1)["max_bet"], 1000)
        self.assertGreater(
            BettingLimitsService.get_user_effective_limits(
                self.bettor, division_id=database.CUP_DIVISION_SENTINEL)["max_bet"], 1000)

        ok, result = self._bet(self.bettor, [{"match_id": self.game_1, "outcome": "p1"}], amount=2000)
        self.assertTrue(ok, result)


if __name__ == "__main__":
    unittest.main()
