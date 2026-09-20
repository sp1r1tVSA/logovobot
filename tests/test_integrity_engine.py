"""
tests/test_integrity_engine.py

Детектор договорных матчей (Integrity Engine):
1. Скоринг — «чистая» ставка дела не создаёт, «грязная» уходит в high/critical.
2. Правило двух семейств — одинокий выброс не считается сигналом.
3. Холодный старт — поведенческие признаки снимаются, сумма весов сохраняется.
4. Фильтр входа — мелкие ставки и внешние фикстуры в выборку не попадают.
5. Проигравшая нога — дело обнуляется.
6. Идемпотентность — повторный прогон не плодит строки, вердикт не затирается.
7. Живые проводки модели — Elo двигается и не применяется дважды, прогнозы разрешаются.
"""

import itertools
import json
import os
import tempfile
import unittest

import config
import database
from services import integrity_engine as ie

_ID_SEQ = itertools.count(880001)


def _next_id() -> int:
    return next(_ID_SEQ)


# Модельный расклад: разгромный фаворит слева, аутсайдер справа.
PRED_FAVOURITE = {
    "home_probability": 0.72,
    "draw_probability": 0.20,
    "away_probability": 0.08,
    "goals_markets": {"btts_yes": 0.45, "btts_no": 0.55, "over_2_5": 0.55},
    "correct_scores": {"2:0": 0.12, "1:0": 0.11, "3:0": 0.08, "1:1": 0.07, "0:5": 0.001},
}


def _item(**over) -> dict:
    """Нога ставки в том виде, в каком её отдаёт get_unscored_bet_items."""
    base = {
        "bet_id": 1,
        "bet_item_id": 1,
        "user_id": 1000,
        "match_id": 10,
        "division_id": 1,
        "season_id": 1,
        "amount": 900.0,
        "odds_at_placement": 2.10,
        "odd": 2.10,
        "selection_key": "p1",
        "market_key": "1x2",
        "item_status": "pending",
        "balance_before": 12000.0,
        "bets_opened_at": "2026-09-10 10:00:00",
        "placed_at": "2026-09-10 14:00:00",
        "player1_score": None,
        "player2_score": None,
        "actual_payout": 0.0,
    }
    base.update(over)
    return base


def _profile(**over) -> dict:
    """Профиль давно играющего беттора с ровными ставками."""
    base = {
        "amounts": [800.0, 950.0, 1000.0, 870.0, 1100.0, 920.0, 990.0],
        "bets_count": 7,
        "market_counts": {"1x2": 6, "totals": 4},
        "last_bet_at": "2026-09-09 18:00:00",
    }
    base.update(over)
    return base


class TestIntegrityScoring(unittest.TestCase):
    """Чистая логика движка: DB и сеть не нужны."""

    def test_clean_bet_produces_no_case(self):
        """Обычная сумма на фаворита в обычное время — не повод для дела."""
        case = ie.score_case(_item(), _profile(), PRED_FAVOURITE, market_odds=[1.45, 4.2, 7.5])

        self.assertFalse(case["reportable"])
        self.assertLess(case["total_score"], ie.CASE_MIN_SCORE)

    def test_dirty_bet_scores_high(self):
        """Ва-банк на аутсайдера, новый рынок, сразу после открытия линии, зашла."""
        item = _item(
            amount=5000.0,
            balance_before=6000.0,
            odds_at_placement=9.5,
            odd=9.5,
            selection_key="p2",
            market_key="handicap",
            item_status="won",
            player1_score=0,
            player2_score=5,
            actual_payout=47500.0,
            placed_at="2026-09-10 10:05:00",
        )
        profile = _profile(last_bet_at="2026-08-01 12:00:00")

        case = ie.score_case(
            item, profile, PRED_FAVOURITE,
            market_odds=[1.45, 4.2, 7.5],
            volume={"selection_amount": 5200.0, "match_amount": 6000.0},
            resolved=True,
        )

        self.assertIsNone(case["gate"])
        self.assertTrue(case["reportable"])
        self.assertGreaterEqual(case["total_score"], 70.0)
        self.assertIn(case["severity"], ("high", "critical"))
        self.assertEqual(case["stage"], "resolved")

    def test_two_families_rule_blocks_lone_outlier(self):
        """Крупная сумма на очевидного фаворита — просто крупная ставка."""
        item = _item(amount=9000.0, balance_before=10000.0, selection_key="p1", odds_at_placement=1.31)
        # Линия сходится с моделью, так что odds_edge не набирает: остаётся
        # одна только сумма, а одного семейства для дела не хватает.
        case = ie.score_case(item, _profile(), PRED_FAVOURITE, market_odds=[1.31, 4.72, 11.80])

        self.assertEqual(case["gate"], "families")
        self.assertEqual(case["total_score"], 0.0)
        self.assertGreater(case["online_score"], 0.0)  # разбор сохранён

    def test_cold_start_drops_behaviour_and_keeps_weight_sum(self):
        """У новичка нет распределения: поведенческие признаки снимаются."""
        case = ie.score_case(
            _item(), _profile(amounts=[500.0, 700.0], bets_count=2),
            PRED_FAVOURITE, market_odds=[1.45, 4.2, 7.5]
        )

        self.assertTrue(case["low_confidence"])
        rows = {r["name"]: r for r in case["features"]["online"]}
        for name in ie.COLD_START_FEATURES:
            self.assertEqual(rows[name]["weight"], 0.0, name)

        kept = sum(r["weight"] for r in case["features"]["online"])
        self.assertAlmostEqual(kept, sum(ie.WEIGHTS_ONLINE.values()), places=0)

    def test_lost_leg_is_gated(self):
        """Нога не зашла — знать результат заранее было нечего."""
        item = _item(
            amount=5000.0, balance_before=6000.0, odds_at_placement=9.5,
            selection_key="p2", market_key="handicap", item_status="lost",
            player1_score=4, player2_score=0, placed_at="2026-09-10 10:05:00",
        )
        case = ie.score_case(item, _profile(), PRED_FAVOURITE, resolved=True)

        self.assertEqual(case["gate"], "lost")
        self.assertEqual(case["total_score"], 0.0)
        self.assertEqual(case["post_score"], 0.0)
        self.assertFalse(case["reportable"])

    def test_unknown_selection_key_drops_the_feature(self):
        """Неизвестный ключ исхода не подставляет догадку."""
        self.assertIsNone(ie.model_probability("who_knows", PRED_FAVOURITE))
        self.assertIsNone(ie.model_probability(None, PRED_FAVOURITE))

    def test_model_probability_reads_derived_markets(self):
        """Двойной шанс и точный счёт считаются из ансамбля и сетки."""
        self.assertAlmostEqual(ie.model_probability("1x", PRED_FAVOURITE), 0.92, places=4)
        self.assertAlmostEqual(ie.model_probability("x2", PRED_FAVOURITE), 0.28, places=4)
        # Сетка усечена по числу голов, поэтому точный счёт нормируется на её массу.
        grid_mass = sum(PRED_FAVOURITE["correct_scores"].values())
        self.assertAlmostEqual(
            ie.model_probability("cs_2_0", PRED_FAVOURITE), 0.12 / grid_mass, places=4
        )

    def test_severity_bands(self):
        self.assertEqual(ie.severity_for(92.0), "critical")
        self.assertEqual(ie.severity_for(71.0), "high")
        self.assertEqual(ie.severity_for(55.0), "medium")
        self.assertEqual(ie.severity_for(12.0), "low")


class TestIntegrityRepository(unittest.TestCase):
    """Выборки и запись дел в базе."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp_dir.name, "test_integrity.db")
        database.DB_PATH = self.db_path
        database.close_thread_connection()
        database.init_db()

        self.div_id = database.create_division(name="Дивизион Тест", code="DIV_T")
        self.bettor_id = _next_id()
        database.register_user(self.bettor_id, "integrity_bettor", role="player", team_name="Аякс")
        database.assign_user_division(self.bettor_id, self.div_id)

    def tearDown(self):
        database.close_thread_connection()
        self.tmp_dir.cleanup()

    def _seed_leg(self, amount: float, division_id, match_id=None, status="pending"):
        """Матч + купон + нога напрямую, без прохода через place_user_bet."""
        with database.transaction() as conn:
            cur = conn.cursor()
            if match_id is None:
                cur.execute(
                    """INSERT INTO matches (tournament_id, round_number, division_id, season_id,
                                            player1_team, player2_team, status)
                       VALUES (1, 1, ?, 1, 'Ювентус', 'Порту', 'scheduled')""",
                    (division_id,)
                )
                match_id = cur.lastrowid

            cur.execute(
                """INSERT INTO user_bets (user_id, bet_type, amount, total_odd,
                                          potential_win, status)
                   VALUES (?, 'single', ?, 2.0, ?, ?)""",
                (self.bettor_id, amount, amount * 2, status)
            )
            bet_id = cur.lastrowid
            cur.execute(
                """INSERT INTO bet_items (bet_id, match_id, outcome_type, odd,
                                          status, odds_at_placement)
                   VALUES (?, ?, 'p1', 2.0, 'pending', 2.0)""",
                (bet_id, match_id)
            )
            return bet_id, cur.lastrowid, match_id

    def test_entry_filter_skips_small_stakes_and_external_fixtures(self):
        """Мелкая ставка и матч без дивизиона не доходят до скоринга."""
        big_bet, big_item, _ = self._seed_leg(config.INTEGRITY_MIN_STAKE + 100, self.div_id)
        small_bet, small_item, _ = self._seed_leg(10, self.div_id)
        ext_bet, ext_item, _ = self._seed_leg(config.INTEGRITY_MIN_STAKE + 100, None)

        got = {r["bet_item_id"] for r in database.get_unscored_bet_items(limit=50)}

        self.assertIn(big_item, got)
        self.assertNotIn(small_item, got)
        self.assertNotIn(ext_item, got)

    def test_scored_leg_leaves_the_queue(self):
        """Строка в integrity_cases снимает ногу с повторного пересчёта."""
        bet_id, item_id, match_id = self._seed_leg(config.INTEGRITY_MIN_STAKE + 500, self.div_id)
        self.assertIn(item_id, {r["bet_item_id"] for r in database.get_unscored_bet_items()})

        database.upsert_integrity_case(
            bet_id, item_id, self.bettor_id, match_id, self.div_id, 1,
            0.0, 0.0, 0.0, "low", "online", 0, json.dumps({})
        )

        self.assertNotIn(item_id, {r["bet_item_id"] for r in database.get_unscored_bet_items()})

    def test_upsert_is_idempotent_and_keeps_the_verdict(self):
        """Постматчевый проход апдейтит ту же строку и не сбрасывает вердикт админа."""
        bet_id, item_id, match_id = self._seed_leg(config.INTEGRITY_MIN_STAKE + 500, self.div_id)

        case_id = database.upsert_integrity_case(
            bet_id, item_id, self.bettor_id, match_id, self.div_id, 1,
            45.0, 0.0, 45.0, "low", "online", 0, json.dumps({"online": []})
        )
        self.assertTrue(database.set_integrity_case_status(case_id, "acknowledged", 777, "смотрел"))

        again = database.upsert_integrity_case(
            bet_id, item_id, self.bettor_id, match_id, self.div_id, 1,
            45.0, 30.0, 75.0, "high", "resolved", 0, json.dumps({"online": [], "post": []})
        )
        self.assertEqual(again, case_id)

        case = database.get_integrity_case(case_id)
        self.assertEqual(case["total_score"], 75.0)
        self.assertEqual(case["severity"], "high")
        self.assertEqual(case["stage"], "resolved")
        # Вердикт супер-админа перескоринг не отменяет.
        self.assertEqual(case["status"], "acknowledged")
        self.assertEqual(case["reviewed_by"], 777)
        self.assertEqual(database.count_integrity_cases(status="open"), 0)

    def test_list_filters_by_score_and_status(self):
        """Служебные строки-заглушки не засоряют экран супер-админа."""
        noise_bet, noise_item, noise_match = self._seed_leg(config.INTEGRITY_MIN_STAKE + 1, self.div_id)
        real_bet, real_item, real_match = self._seed_leg(config.INTEGRITY_MIN_STAKE + 2, self.div_id)

        database.upsert_integrity_case(
            noise_bet, noise_item, self.bettor_id, noise_match, self.div_id, 1,
            4.0, 0.0, 4.0, "low", "online", 0, json.dumps({})
        )
        database.upsert_integrity_case(
            real_bet, real_item, self.bettor_id, real_match, self.div_id, 1,
            60.0, 25.0, 82.0, "high", "resolved", 0, json.dumps({})
        )

        shown = database.get_integrity_cases(status="open", min_score=ie.CASE_MIN_SCORE, limit=10)
        self.assertEqual([c["bet_item_id"] for c in shown], [real_item])
        self.assertEqual(
            database.count_integrity_cases(status="open", min_score=ie.CASE_MIN_SCORE), 1
        )
        self.assertEqual(database.count_integrity_cases(), 2)

    def test_user_profile_is_built_from_the_past_only(self):
        """Профиль на момент ставки не должен видеть её саму и более поздние."""
        for amount in (700, 800, 900):
            self._seed_leg(amount, self.div_id, status="won")

        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE user_bets SET created_at = '2026-09-01 12:00:00' WHERE user_id = ?",
                (self.bettor_id,)
            )

        self._seed_leg(50000, self.div_id, status="pending")
        with database.transaction() as conn:
            conn.cursor().execute(
                """UPDATE user_bets SET created_at = '2026-09-15 12:00:00'
                    WHERE user_id = ? AND amount = 50000""",
                (self.bettor_id,)
            )

        profile = database.get_user_bet_profile(self.bettor_id, before="2026-09-10 00:00:00")
        self.assertEqual(profile["bets_count"], 3)
        self.assertNotIn(50000.0, profile["amounts"])


class TestModelWiring(unittest.TestCase):
    """Проводки, без которых сигнал «отклонение от модели» не имеет смысла."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self.tmp_dir.name, "test_wiring.db")
        database.DB_PATH = self.db_path
        database.close_thread_connection()
        database.init_db()

        self.div_id = database.create_division(name="Дивизион Elo", code="DIV_E")
        self.p1 = _next_id()
        self.p2 = _next_id()
        database.register_user(self.p1, "elo_home", role="player", team_name="Аякс")
        database.register_user(self.p2, "elo_away", role="player", team_name="Порту")
        database.assign_user_division(self.p1, self.div_id)
        database.assign_user_division(self.p2, self.div_id)

        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO matches (tournament_id, round_number, division_id, season_id,
                                        player1_id, player2_id, player1_team, player2_team, status)
                   VALUES (1, 1, ?, 1, ?, ?, 'Аякс', 'Порту', 'scheduled')""",
                (self.div_id, self.p1, self.p2)
            )
            self.match_id = cur.lastrowid

    def tearDown(self):
        database.close_thread_connection()
        self.tmp_dir.cleanup()

    def test_elo_moves_and_is_idempotent(self):
        """Рейтинги расходятся от 1500, повторный прогон их не двигает."""
        self.assertTrue(database._apply_elo_after_match(self.match_id, 3, 0))

        winner = database.get_team_elo("Аякс", self.div_id, 1)
        loser = database.get_team_elo("Порту", self.div_id, 1)
        self.assertGreater(winner, 1500.0)
        self.assertLess(loser, 1500.0)

        database._apply_elo_after_match(self.match_id, 3, 0)
        self.assertAlmostEqual(database.get_team_elo("Аякс", self.div_id, 1), winner, places=2)
        self.assertAlmostEqual(database.get_team_elo("Порту", self.div_id, 1), loser, places=2)

    def test_score_correction_rewinds_the_previous_delta(self):
        """Исправление счёта переписывает вклад матча, а не наслаивает второй."""
        database._apply_elo_after_match(self.match_id, 3, 0)
        database._apply_elo_after_match(self.match_id, 0, 3)

        self.assertLess(database.get_team_elo("Аякс", self.div_id, 1), 1500.0)
        self.assertGreater(database.get_team_elo("Порту", self.div_id, 1), 1500.0)

    def test_technical_result_does_not_move_ratings(self):
        """ТП/ТН — административный вердикт, а не сыгранный матч."""
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET is_technical = 1 WHERE id = ?", (self.match_id,)
            )

        self.assertFalse(database._apply_elo_after_match(self.match_id, 3, 0))
        self.assertEqual(database.get_team_elo("Аякс", self.div_id, 1), 1500.0)

    def test_predictions_are_resolved_with_a_brier_score(self):
        """После счёта прогноз закрывается и получает Brier в [0, 2]."""
        with database.transaction() as conn:
            conn.cursor().execute(
                """INSERT INTO predictions (match_id, division_id, season_id, model_version,
                                            home_probability, draw_probability, away_probability)
                   VALUES (?, ?, 1, 'test', 0.6, 0.25, 0.15)""",
                (self.match_id, self.div_id)
            )

        self.assertEqual(database.resolve_ai_predictions(self.match_id, 2, 0), 1)

        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM predictions WHERE match_id = ?", (self.match_id,))
            row = cur.fetchone()

        self.assertIsNotNone(row["resolved_at"])
        self.assertEqual(row["actual_result"], "home")
        self.assertEqual(row["is_correct"], 1)
        self.assertGreaterEqual(row["brier_score"], 0.0)
        self.assertLessEqual(row["brier_score"], 2.0)

        # Повторный вызов уже закрытый прогноз не трогает.
        self.assertEqual(database.resolve_ai_predictions(self.match_id, 2, 0), 0)


if __name__ == "__main__":
    unittest.main()
