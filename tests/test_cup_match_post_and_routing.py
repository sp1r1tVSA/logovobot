"""
tests/test_cup_match_post_and_routing.py

Проверка двух предрелизных фиксов:
1. `build_formatted_match_post` для кубковых матчей печатает этап и игру серии вместо «Тур -1».
2. `resolve_division_target` для CUP_DIVISION_SENTINEL (0) направляет отчёты в кубковый топик `reports`
   и не утекает в легаси results_topic_id регулярной лиги.
"""

import os
import tempfile
import unittest

import database
from constants import CUP_DIVISION_SENTINEL
from handlers.base import resolve_division_target
from handlers.cabinet import build_formatted_match_post


class CupMatchPostAndRoutingTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Post Season', 'active')")
            self.season_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                "VALUES (101, 'p1', 'Реал Мадрид', 'player', datetime('now', '+3 hours'))"
            )
            cursor.execute(
                "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                "VALUES (102, 'p2', 'Барселона', 'player', datetime('now', '+3 hours'))"
            )

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    def test_01_cup_match_post_shows_stage_and_game_instead_of_round_minus_one(self):
        """Кубковый матч из БД с cup_stage='1/64' и game_num_in_series=1 не содержит 'Тур -1'."""
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team,
                                     status, division_id, season_id, tournament_type, cup_stage,
                                     game_num_in_series)
                VALUES (-1, 101, 102, 'Реал Мадрид', 'Барселона',
                        'confirmed', ?, ?, 'cup', '1/64', 1)
                """,
                (CUP_DIVISION_SENTINEL, self.season_id)
            )
            match_id = cursor.lastrowid

        # 1. Групповой пост
        group_post = build_formatted_match_post(
            round_number=-1,
            home_team="Реал Мадрид",
            away_team="Барселона",
            h_score=2,
            a_score=1,
            match_id=match_id,
            is_draft=False,
            is_pm=False,
        )
        self.assertNotIn("Тур -1", group_post)
        self.assertIn("Кубок · 1/64 (Игра 1)", group_post)

        # 2. Личное сообщение (PM)
        pm_post = build_formatted_match_post(
            round_number=-1,
            home_team="Реал Мадрид",
            away_team="Барселона",
            h_score=2,
            a_score=1,
            match_id=match_id,
            is_draft=False,
            is_pm=True,
        )
        self.assertNotIn("Тур -1", pm_post)
        self.assertIn("Кубок · 1/64 (Игра 1)", pm_post)

        # 3. Черновик (Draft)
        draft_post = build_formatted_match_post(
            round_number=-1,
            home_team="Реал Мадрид",
            away_team="Барселона",
            h_score=2,
            a_score=1,
            match_id=match_id,
            is_draft=True,
            is_pm=False,
        )
        self.assertNotIn("Тур -1", draft_post)
        self.assertIn("Кубок · 1/64 (Игра 1)", draft_post)

    def test_02_cup_match_without_game_num_shows_stage(self):
        """Если номер игры в серии не указан, пишется 'Кубок · 1/32' без (Игра None)."""
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team,
                                     status, division_id, season_id, tournament_type, cup_stage,
                                     game_num_in_series)
                VALUES (-1, 101, 102, 'Реал Мадрид', 'Барселона',
                        'confirmed', ?, ?, 'cup', '1/32', NULL)
                """,
                (CUP_DIVISION_SENTINEL, self.season_id)
            )
            match_id = cursor.lastrowid

        post = build_formatted_match_post(
            round_number=-1,
            home_team="Реал Мадрид",
            away_team="Барселона",
            h_score=3,
            a_score=0,
            match_id=match_id,
            is_draft=False,
        )
        self.assertNotIn("Тур -1", post)
        self.assertIn("Кубок · 1/32", post)
        self.assertNotIn("Игра", post)

    def test_03_fallback_round_minus_one_without_match_id(self):
        """Если match_id не передан, но round_number=-1, пишется 'Кубок', а не 'Тур -1'."""
        post = build_formatted_match_post(
            round_number=-1,
            home_team="Реал Мадрид",
            away_team="Барселона",
            h_score=1,
            a_score=1,
            match_id=None,
            is_draft=False,
        )
        self.assertNotIn("Тур -1", post)
        self.assertIn("Кубок", post)

    def test_04_league_match_keeps_regular_tour_label(self):
        """Обычный матч регулярного чемпионата сохраняет 'Тур X'."""
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team,
                                     status, division_id, season_id, tournament_type)
                VALUES (5, 101, 102, 'Реал Мадрид', 'Барселона',
                        'confirmed', 1, ?, 'league')
                """,
                (self.season_id,)
            )
            match_id = cursor.lastrowid

        post = build_formatted_match_post(
            round_number=5,
            home_team="Реал Мадрид",
            away_team="Барселона",
            h_score=2,
            a_score=2,
            match_id=match_id,
            is_draft=False,
        )
        self.assertIn("Тур 5", post)
        self.assertNotIn("Кубок", post)

    async def test_05_cup_routing_resolves_to_cup_reports_topic(self):
        """resolve_division_target с CUP_DIVISION_SENTINEL направляет в cup_topics(reports)."""
        database.bind_cup_topic("reports", -100777888, 999, season_id=self.season_id)

        # Конфигурируем также легаси результаты регулярной лиги
        database.set_config("group_id", "-100111222")
        database.set_config("results_topic_id", "123")

        target = await resolve_division_target(
            CUP_DIVISION_SENTINEL,
            "results", "reports",
            legacy_topic_keys=("results_topic_id", "reports_topic_id")
        )
        self.assertEqual(target, (-100777888, 999), "Кубковый результат обязан уходить в кубковую тему")

    async def test_06_cup_routing_does_not_leak_to_league_legacy_when_unbound(self):
        """Если кубковый топик не настроен, результат НЕ отправляется в легаси тему лиги."""
        database.set_config("group_id", "-100111222")
        database.set_config("results_topic_id", "123")

        target = await resolve_division_target(
            CUP_DIVISION_SENTINEL,
            "results", "reports",
            legacy_topic_keys=("results_topic_id", "reports_topic_id")
        )
        self.assertEqual(target, (None, None), "Без кубковой темы публикация пропускается, а не спамит в лигу")


if __name__ == "__main__":
    unittest.main()
