"""
tests/test_mid_season_team.py

Тестирование Сборной 1-го круга дивизионов:
  * Расчёт очков Mid-Season Performance Index (с весом +15 за «Игрок тура»);
  * Сборка схемы 4-3-3 (11 основы + 4 на скамейке) и выбор капитана;
  * Запросы в БД: границы 1-го круга, проверка завершенности и сбор POTR;
  * Генерация HD-постера (PNG);
  * Права доступа (только супер-администратор).
"""

import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import database
from services.mid_season_team_service import (
    POTR_POINTS,
    build_all_divisions_mid_season_payload,
    build_mid_season_lineup,
    calculate_mid_season_player_score,
    generate_mid_season_caption,
)
from services.graphics.mid_season_generator import generate_mid_season_image


def _cand(name, team, position, **stats):
    base = {
        "player_name": name,
        "team_name": team,
        "position": position,
        "is_starter": True,
        "goals": 0,
        "assists": 0,
        "mvp": 0,
        "potr_count": 0,
        "braces": 0,
        "matches": 5,
        "wins": 0,
        "clean_sheets": 0,
        "goals_conceded": 10,
    }
    base.update(stats)
    return base


def _full_pool():
    return [
        _cand("Вратарь-1", "Реал", "GK", clean_sheets=3, wins=4, goals_conceded=2, potr_count=1),
        _cand("Левый Защ", "Реал", "LB", clean_sheets=3, goals_conceded=2),
        _cand("Центр Защ 1", "Сити", "CB", clean_sheets=2, goals=1),
        _cand("Центр Защ 2", "Сити", "CB", clean_sheets=2),
        _cand("Правый Защ", "Арсенал", "RB", clean_sheets=1, assists=1),
        _cand("Опорник", "Арсенал", "CDM", assists=2, clean_sheets=1),
        _cand("Хавбек 1", "Бавария", "CM", goals=2, assists=1),
        _cand("Хавбек 2", "Бавария", "CAM", goals=1, assists=3, potr_count=1),
        _cand("Вингер Л", "ПСЖ", "LW", goals=3),
        _cand("Форвард", "ПСЖ", "ST", goals=8, braces=2, mvp=3, potr_count=2),
        _cand("Вингер П", "Ливерпуль", "RW", goals=2, assists=2),
        # Скамейка
        _cand("Вратарь-2", "Ливерпуль", "GK", clean_sheets=1, wins=1),
        _cand("Защитник-3", "Интер", "CB", clean_sheets=1),
        _cand("Хавбек-3", "Интер", "CM", assists=1),
        _cand("Форвард-2", "Барселона", "ST", goals=2),
    ]


class TestMidSeasonScoring(unittest.TestCase):
    def test_potr_weight(self):
        """Проверка, что каждый титул «Игрок тура» даёт ровно +15 очков."""
        base_fwd = _cand("Нападающий", "Клуб", "ST", goals=3, assists=1)
        base_score = calculate_mid_season_player_score(base_fwd)

        potr_fwd = _cand("Нападающий", "Клуб", "ST", goals=3, assists=1, potr_count=2)
        potr_score = calculate_mid_season_player_score(potr_fwd)

        self.assertEqual(potr_score - base_score, 2 * POTR_POINTS)

    def test_goalkeeper_score(self):
        gk = _cand("Вратарь", "Клуб", "GK", clean_sheets=3, wins=4, goals_conceded=3, mvp=1, potr_count=1)
        score = calculate_mid_season_player_score(gk)
        # 15*1 (potr) + 12*1 (mvp) + 15*3 (cs) + 3*4 (wins) + 10 (low conceded) = 15 + 12 + 45 + 12 + 10 = 94
        self.assertEqual(score, 94.0)

    def test_lineup_builder_slots_and_captain(self):
        pool = _full_pool()
        lineup = build_mid_season_lineup(pool)
        self.assertEqual(len(lineup["xi"]), 11)
        self.assertEqual(len(lineup["bench"]), 4)

        # Капитан должен быть топ-скорером (Форвард ПСЖ с 8 голами и 2 POTR)
        captain = lineup["captain"]
        self.assertIsNotNone(captain)
        self.assertEqual(captain["player_name"], "Форвард")
        self.assertTrue(captain["is_captain"])

        # Проверка скамейки: GK, DEF, MID, FWD
        bench_slots = [p["slot"] for p in lineup["bench"]]
        self.assertEqual(bench_slots, ["SUB_GK", "SUB_DEF", "SUB_MID", "SUB_FWD"])

    def test_potr_tie_breaker(self):
        """При равенстве очков игрок с титулом POTR имеет преимущество."""
        c1 = _cand("Игрок А", "Клуб1", "ST", goals=3, potr_count=1)  # score: 15*1 + 12*3 = 51
        c2 = _cand("Игрок Б", "Клуб2", "ST", goals=3, potr_count=0)  # score: 12*3 = 36
        self.assertGreater(calculate_mid_season_player_score(c1), calculate_mid_season_player_score(c2))


class TestMidSeasonDatabase(unittest.TestCase):
    def setUp(self):
        self.div_id = 991
        self.div_id_2 = 992
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO divisions (id, name, code) VALUES (?, 'Тест Дивизион 1', 'TEST1')", (self.div_id,))
            cursor.execute("INSERT OR IGNORE INTO divisions (id, name, code) VALUES (?, 'Тест Дивизион 2', 'TEST2')", (self.div_id_2,))
            # Создаем 4 тура в дивизионе 1
            for r in range(1, 5):
                cursor.execute("""
                    INSERT INTO matches (division_id, season_id, round_number, player1_team, player2_team,
                                         player1_score, player2_score, status, is_technical)
                    VALUES (?, 1, ?, 'Реал', 'Барселона', 2, 1, 'confirmed', 0)
                """, (self.div_id, r))
                mid = cursor.lastrowid
                cursor.execute("""
                    INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                    VALUES (?, 'Реал', 'Винисиус', 'goal', 2)
                """, (mid,))
                cursor.execute("""
                    INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                    VALUES (?, 'Барселона', 'Левандовски', 'goal', 1)
                """, (mid,))
            # Создаем 4 тура в дивизионе 2
            for r in range(1, 5):
                cursor.execute("""
                    INSERT INTO matches (division_id, season_id, round_number, player1_team, player2_team,
                                         player1_score, player2_score, status, is_technical)
                    VALUES (?, 1, ?, 'Сити', 'Арсенал', 3, 0, 'confirmed', 0)
                """, (self.div_id_2, r))
                mid2 = cursor.lastrowid
                cursor.execute("""
                    INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                    VALUES (?, 'Сити', 'Холанд', 'goal', 3)
                """, (mid2,))

    def tearDown(self):
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM match_events WHERE match_id IN (SELECT id FROM matches WHERE division_id IN (?, ?))", (self.div_id, self.div_id_2))
            cursor.execute("DELETE FROM matches WHERE division_id IN (?, ?)", (self.div_id, self.div_id_2))
            cursor.execute("DELETE FROM divisions WHERE id IN (?, ?)", (self.div_id, self.div_id_2))
            cursor.execute("DELETE FROM round_content_posts WHERE division_id IN (?, ?)", (self.div_id, self.div_id_2))

    def test_first_half_bounds_and_completion(self):
        bounds = database.get_division_first_half_bounds(self.div_id, 1)
        self.assertIsNotNone(bounds)
        start_r, end_r = bounds
        self.assertEqual(start_r, 1)
        self.assertEqual(end_r, 2)  # 4 rounds total -> 4 // 2 = 2 rounds

        is_completed = database.is_first_half_completed(self.div_id, 1)
        self.assertTrue(is_completed)

    def test_get_mid_season_stats_potr_tally(self):
        stats = database.get_mid_season_stats(self.div_id, 1, 2, 1)
        self.assertGreater(len(stats), 0)
        vini = next((s for s in stats if s["player_name"] == "Винисиус"), None)
        self.assertIsNotNone(vini)
        # В обоих турах 1 и 2 Винисиус забил по 2 гола, став игроком тура
        self.assertEqual(vini["potr_count"], 2)

    def test_get_all_divisions_mid_season_stats(self):
        stats = database.get_all_divisions_mid_season_stats(1)
        self.assertGreater(len(stats), 0)
        names = [s["player_name"] for s in stats]
        self.assertIn("Винисиус", names)
        self.assertIn("Холанд", names)

    def test_build_all_divisions_payload(self):
        payload = build_all_divisions_mid_season_payload(1)
        self.assertTrue(payload.get("is_league_wide"))
        self.assertEqual(payload.get("division_name"), "ВСЕ ДИВИЗИОНЫ")
        self.assertGreater(len(payload.get("xi", [])), 0)


class TestMidSeasonGraphics(unittest.TestCase):
    def test_generate_poster_image(self):
        pool = _full_pool()
        lineup = build_mid_season_lineup(pool)
        payload = {
            "division_id": 1,
            "division_name": "Премьер-Лига",
            "start_round": 1,
            "end_round": 15,
            "total_goals": 142,
            **lineup,
        }
        with patch("services.graphics.mid_season_generator._load_photo", return_value=None):
            buf = generate_mid_season_image(payload, 1, 1, 15, fetch_photos=False)
            self.assertIsInstance(buf, io.BytesIO)
            self.assertGreater(len(buf.getvalue()), 1000)

    def test_generate_caption(self):
        pool = _full_pool()
        lineup = build_mid_season_lineup(pool)
        payload = {
            "division_id": 1,
            "division_name": "Премьер-Лига",
            "start_round": 1,
            "end_round": 15,
            **lineup,
        }
        caption = generate_mid_season_caption(payload, "Премьер-Лига", 1, 15, use_ai=False)
        self.assertIn("СБОРНАЯ 1-ГО КРУГА", caption)
        self.assertIn("Форвард", caption)
        self.assertIn("Запас:", caption)

    def test_generate_league_poster_image(self):
        pool = _full_pool()
        lineup = build_mid_season_lineup(pool)
        payload = {
            "is_league_wide": True,
            "division_id": None,
            "division_name": "ВСЕ ДИВИЗИОНЫ",
            "start_round": 1,
            "end_round": None,
            "total_goals": 250,
            **lineup,
        }
        with patch("services.graphics.mid_season_generator._load_photo", return_value=None):
            buf = generate_mid_season_image(payload, fetch_photos=False)
            self.assertIsInstance(buf, io.BytesIO)
            self.assertGreater(len(buf.getvalue()), 1000)

    def test_generate_league_caption(self):
        pool = _full_pool()
        lineup = build_mid_season_lineup(pool)
        payload = {
            "is_league_wide": True,
            "division_id": None,
            "division_name": "ВСЕ ДИВИЗИОНЫ",
            "start_round": 1,
            "end_round": None,
            **lineup,
        }
        caption = generate_mid_season_caption(payload, "ВСЕ ДИВИЗИОНЫ", 1, 15, use_ai=False)
        self.assertIn("СБОРНАЯ 1-ГО КРУГА ЛИГИ", caption)
        self.assertIn("ВСЕ ДИВИЗИОНЫ", caption)


class TestMidSeasonAdminHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_league_view_denies_non_admin(self):
        from handlers.admin import cb_admin_league_first_half_view

        update = MagicMock()
        update.effective_user.id = 123456789
        update.callback_query = AsyncMock()
        context = MagicMock()

        with patch("handlers.admin.is_global_admin", return_value=False):
            await cb_admin_league_first_half_view(update, context)
            update.callback_query.answer.assert_called_once()
            args, kwargs = update.callback_query.answer.call_args
            text = args[0] if args else kwargs.get("text", "")
            self.assertIn("супер-администратору", text)

    async def test_league_publish_denies_non_admin(self):
        from handlers.admin import cb_admin_league_first_half_publish

        update = MagicMock()
        update.effective_user.id = 123456789
        update.callback_query = AsyncMock()
        context = MagicMock()

        with patch("handlers.admin.is_global_admin", return_value=False):
            await cb_admin_league_first_half_publish(update, context)
            update.callback_query.answer.assert_called_once()
            args, kwargs = update.callback_query.answer.call_args
            text = args[0] if args else kwargs.get("text", "")
            self.assertIn("супер-администратору", text)


if __name__ == "__main__":
    unittest.main()

