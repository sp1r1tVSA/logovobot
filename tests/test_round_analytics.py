"""
Автопостинг аналитики тура: сборка данных для превью и итогов.

Gemini здесь не вызывается — генерация текста мокается, проверяется только
шаблонная фолбэк-ветка (реальные ключи живут в серверном .env).
"""

import itertools
import unittest
import uuid

import database
from services import round_preview

# Уникальные telegram_id на весь модуль: файл получает свою БД, но тесты внутри
# него делят её, поэтому пересечение id ломало бы UNIQUE.
_ID_SEQ = itertools.count(870100)


class RoundAnalyticsTestBase(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(
            name=f"Аналитика {self.uid}", code=f"ANALYTICS_{self.uid}"
        )

        # Названия намеренно непохожи друг на друга: teams_match() сводит
        # близкие строки (порог 0.85), и «Клуб A…»/«Клуб C…» склеились бы в одну.
        self.teams = {}
        self.user_ids = {}
        for key, word in (("A", "Альфа"), ("B", "Браво"), ("C", "Чарли"), ("D", "Дельта")):
            uid = next(_ID_SEQ)
            team = f"{word} {self.uid}"
            database.register_user(uid, f"user_{key.lower()}_{self.uid}", team_name=team)
            database.assign_user_division(uid, self.div_id)
            self.teams[key] = team
            self.user_ids[key] = uid

    def _add_match(self, round_number, home, away, score1=None, score2=None, status="scheduled"):
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO matches
                    (round_number, player1_id, player2_id, player1_team, player2_team,
                     player1_score, player2_score, status, division_id, tournament_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'league')
            """, (
                round_number, self.user_ids[home], self.user_ids[away],
                self.teams[home], self.teams[away], score1, score2, status, self.div_id
            ))
            return cursor.lastrowid

    def _add_event(self, match_id, player_name, team_key, event_type, count):
        with database.transaction() as conn:
            conn.cursor().execute("""
                INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                VALUES (?, ?, ?, ?, ?)
            """, (match_id, self.teams[team_key], player_name, event_type, count))

    def _add_round(self, round_number, is_open=1, deadline="2026-09-20 21:00"):
        act = database.get_active_season()
        season_id = act["id"] if act else 1
        with database.transaction() as conn:
            conn.cursor().execute("""
                INSERT OR REPLACE INTO rounds (season_id, division_id, round_number, is_open, deadline)
                VALUES (?, ?, ?, ?, ?)
            """, (season_id, self.div_id, round_number, is_open, deadline))

    def _seed_two_rounds(self):
        """Тур 1 сыгран, тур 2 сыгран — с движением по таблице между ними."""
        self._add_match(1, "A", "B", 3, 0, "confirmed")
        self._add_match(1, "C", "D", 1, 1, "confirmed")
        m3 = self._add_match(2, "B", "A", 2, 0, "confirmed")
        self._add_match(2, "D", "C", 0, 1, "confirmed")
        return m3


class TestStandingsUpToRound(RoundAnalyticsTestBase):
    def test_up_to_round_caps_the_table(self):
        self._seed_two_rounds()

        after_r1 = database.get_standings(division_id=self.div_id, up_to_round=1)
        by_team_r1 = {r["team_name"]: r for r in after_r1}
        self.assertEqual(by_team_r1[self.teams["A"]]["points"], 3)
        self.assertEqual(by_team_r1[self.teams["A"]]["played"], 1)
        self.assertEqual(by_team_r1[self.teams["B"]]["points"], 0)
        self.assertEqual(by_team_r1[self.teams["C"]]["points"], 1)

        after_r2 = database.get_standings(division_id=self.div_id, up_to_round=2)
        by_team_r2 = {r["team_name"]: r for r in after_r2}
        self.assertEqual(by_team_r2[self.teams["A"]]["points"], 3)
        self.assertEqual(by_team_r2[self.teams["A"]]["played"], 2)
        self.assertEqual(by_team_r2[self.teams["B"]]["points"], 3)
        self.assertEqual(by_team_r2[self.teams["C"]]["points"], 4)

    def test_default_behaviour_is_unchanged(self):
        self._seed_two_rounds()
        full = database.get_standings(division_id=self.div_id)
        capped = database.get_standings(division_id=self.div_id, up_to_round=99)
        self.assertEqual(full, capped)

    def test_up_to_round_zero_gives_an_empty_table(self):
        self._seed_two_rounds()
        preseason = database.get_standings(division_id=self.div_id, up_to_round=0)
        self.assertTrue(all(r["played"] == 0 and r["points"] == 0 for r in preseason))


class TestRoundContentPosts(RoundAnalyticsTestBase):
    def test_record_is_idempotent_and_clearable(self):
        self.assertFalse(database.has_round_content_post(self.div_id, 3, "preview"))

        database.record_round_content_post(self.div_id, 3, "preview", message_id=555)
        self.assertTrue(database.has_round_content_post(self.div_id, 3, "preview"))
        # Другой тип контента того же тура не задет
        self.assertFalse(database.has_round_content_post(self.div_id, 3, "digest"))

        # Повторная запись не должна падать на PRIMARY KEY
        database.record_round_content_post(self.div_id, 3, "preview", message_id=556)
        self.assertTrue(database.has_round_content_post(self.div_id, 3, "preview"))

        database.clear_round_content_post(self.div_id, 3, "preview")
        self.assertFalse(database.has_round_content_post(self.div_id, 3, "preview"))

    def test_pending_preview_respects_the_ledger(self):
        self._add_round(5, is_open=1, deadline="2026-09-25 21:00")
        pending = [r for r in database.get_rounds_pending_preview() if r["division_id"] == self.div_id]
        self.assertEqual([r["round_number"] for r in pending], [5])

        database.record_round_content_post(self.div_id, 5, "preview")
        pending = [r for r in database.get_rounds_pending_preview() if r["division_id"] == self.div_id]
        self.assertEqual(pending, [])

    def test_pending_digest_needs_a_finished_round(self):
        self._add_round(1, is_open=1)
        self._add_match(1, "A", "B", 3, 0, "confirmed")
        unconfirmed = self._add_match(1, "C", "D", None, None, "scheduled")

        # Тур открыт и сыгран не полностью — итоги ещё рано
        pending = [r for r in database.get_rounds_pending_digest() if r["division_id"] == self.div_id]
        self.assertEqual(pending, [])

        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET player1_score = 1, player2_score = 1, status = 'confirmed' WHERE id = ?",
                (unconfirmed,)
            )

        pending = [r for r in database.get_rounds_pending_digest() if r["division_id"] == self.div_id]
        self.assertEqual([r["round_number"] for r in pending], [1])
        self.assertEqual(pending[0]["matches_total"], 2)
        self.assertEqual(pending[0]["matches_confirmed"], 2)

        database.record_round_content_post(self.div_id, 1, "digest")
        pending = [r for r in database.get_rounds_pending_digest() if r["division_id"] == self.div_id]
        self.assertEqual(pending, [])

    def test_closed_round_qualifies_without_every_match(self):
        self._add_round(2, is_open=0)
        self._add_match(2, "A", "B", 2, 1, "confirmed")
        self._add_match(2, "C", "D", None, None, "scheduled")

        pending = [r for r in database.get_rounds_pending_digest() if r["division_id"] == self.div_id]
        self.assertEqual([r["round_number"] for r in pending], [2])


class TestRoundPlayerStats(RoundAnalyticsTestBase):
    def test_goals_and_assists_are_scoped_to_the_round(self):
        m1 = self._add_match(1, "A", "B", 3, 0, "confirmed")
        m2 = self._add_match(2, "B", "A", 2, 0, "confirmed")

        self._add_event(m1, "Форвард-1", "A", "goal", 3)
        self._add_event(m2, "Форвард-2", "B", "goal", 2)
        self._add_event(m2, "Плеймейкер", "B", "assist", 2)

        r1 = database.get_round_player_stats(1, division_id=self.div_id)
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1[0]["player_name"], "Форвард-1")
        self.assertEqual(r1[0]["goals"], 3)
        self.assertEqual(r1[0]["assists"], 0)

        r2 = database.get_round_player_stats(2, division_id=self.div_id)
        names = [r["player_name"] for r in r2]
        self.assertEqual(len(r2), 2)
        self.assertIn("Форвард-2", names)
        self.assertIn("Плеймейкер", names)
        # Тай-брейк по голам: у обоих Г+П = 2, но выше тот, кто забивал
        self.assertEqual(r2[0]["player_name"], "Форвард-2")


class TestPreviewPayload(RoundAnalyticsTestBase):
    def test_payload_lists_every_fixture_with_context(self):
        self._add_match(1, "A", "B", 3, 0, "confirmed")
        self._add_match(1, "C", "D", 1, 1, "confirmed")
        self._add_match(2, "B", "A")
        self._add_match(2, "D", "C")
        self._add_round(2)

        payload = round_preview.build_preview_payload(self.div_id, 2)

        self.assertEqual(payload["kind"], "preview")
        self.assertEqual(payload["round_number"], 2)
        self.assertEqual(payload["division_id"], self.div_id)
        self.assertIn(self.uid, payload["division_name"])
        self.assertEqual(len(payload["fixtures"]), 2)

        fixture = payload["fixtures"][0]
        self.assertEqual(fixture["team1"]["name"], self.teams["B"])
        self.assertEqual(fixture["team2"]["name"], self.teams["A"])
        # Ключ прогноза есть всегда, даже когда данных на него не хватило
        self.assertIn("prediction", fixture)
        # Форма и позиция подтягиваются из уже сыгранного тура
        self.assertEqual(fixture["team2"]["form"], ["W"])
        self.assertEqual(fixture["team2"]["position"], 1)
        self.assertEqual(fixture["team2"]["streak"], {"type": "wins", "length": 1})

        self.assertEqual(payload["leaders"][0]["team"], self.teams["A"])
        self.assertEqual(payload["leaders"][0]["points"], 3)
        self.assertEqual(payload["deadline"], "2026-09-20 21:00")

    def test_payload_skips_fixtures_without_both_teams(self):
        self._add_match(3, "A", "B")
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET player1_team = '', player2_team = '' WHERE round_number = 3 AND division_id = ?",
                (self.div_id,)
            )
        payload = round_preview.build_preview_payload(self.div_id, 3)
        self.assertEqual(payload["fixtures"], [])
        self.assertIsNone(payload["match_of_the_round"])

    def test_pending_fixture_includes_completed_matches_with_higher_ids(self):
        """Регрессионный тест: предстоящий матч с меньшим id должен учитывать сыгранные матчи с большим id."""
        # 1. Создаем предстоящий матч тура (получает меньший id)
        m_pending = self._add_match(1, "A", "B", status="pending")

        # 2. Создаем сыгранный матч команды A (получает больший id)
        m_completed = self._add_match(1, "A", "C", score1=3, score2=1, status="confirmed")
        self.assertGreater(m_completed, m_pending)

        from services.feature_engine import FeatureEngine
        features = FeatureEngine.extract_match_features(m_pending)
        # Сыгранный матч команды А должен быть учтен
        self.assertEqual(features["team1_features"]["overall"]["matches_played"], 1)
        self.assertEqual(features["team1_features"]["overall"]["goals_for"], 3)
        self.assertGreater(features["sample_size"], 0)


class TestDigestPayload(RoundAnalyticsTestBase):
    def test_digest_collects_results_rout_player_and_movement(self):
        m3 = self._seed_two_rounds()
        self._add_event(m3, "Форвард-B", "B", "goal", 2)
        self._add_event(m3, "Плеймейкер-B", "B", "assist", 1)

        payload = round_preview.build_digest_payload(self.div_id, 2)

        self.assertEqual(payload["kind"], "digest")
        self.assertEqual(payload["round_number"], 2)
        self.assertEqual(payload["matches_played"], 2)
        self.assertEqual(payload["matches_total"], 2)
        self.assertEqual(payload["goals_total"], 3)

        self.assertEqual(payload["rout"]["team1"], self.teams["B"])
        self.assertEqual(payload["rout"]["margin"], 2)

        self.assertEqual(payload["player_of_the_round"]["player_name"], "Форвард-B")
        self.assertEqual(payload["player_of_the_round"]["goals"], 2)

        # После тура 2 лидирует C (4 очка), а A с первого места опускается
        table = {t["team"]: t for t in payload["table"]}
        self.assertEqual(payload["leader"]["team"], self.teams["C"])
        self.assertEqual(table[self.teams["A"]]["previous_position"], 1)
        self.assertLess(table[self.teams["A"]]["movement"], 0)
        self.assertGreater(table[self.teams["C"]]["movement"], 0)
        self.assertTrue(payload["movers"])

    def test_narrow_win_is_not_a_rout(self):
        self._add_match(1, "A", "B", 1, 0, "confirmed")
        self._add_match(1, "C", "D", 2, 2, "confirmed")

        payload = round_preview.build_digest_payload(self.div_id, 1)
        self.assertIsNone(payload["rout"])
        self.assertIsNone(payload["player_of_the_round"])
        self.assertEqual(payload["goals_total"], 5)

    def test_unconfirmed_matches_stay_out_of_the_results(self):
        self._add_match(1, "A", "B", 3, 0, "confirmed")
        self._add_match(1, "C", "D", None, None, "scheduled")

        payload = round_preview.build_digest_payload(self.div_id, 1)
        self.assertEqual(payload["matches_total"], 2)
        self.assertEqual(payload["matches_played"], 1)
        self.assertEqual(len(payload["results"]), 1)

    def _set_mvp(self, match_id, player_name):
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET mvp_player = ? WHERE id = ?", (player_name, match_id)
            )

    def test_results_carry_the_crown_of_each_match(self):
        crowned = self._add_match(1, "A", "B", 3, 0, "confirmed")
        self._add_match(1, "C", "D", 1, 1, "confirmed")
        self._set_mvp(crowned, "Форвард-A")

        results = {r["match_id"]: r for r in round_preview.build_digest_payload(self.div_id, 1)["results"]}
        self.assertEqual(results[crowned]["mvp_player"], "Форвард-A")
        self.assertIsNone([r for r in results.values() if r["match_id"] != crowned][0]["mvp_player"])

    def test_two_crowns_make_a_player_of_the_round_mvp(self):
        m1 = self._add_match(1, "A", "B", 3, 0, "confirmed")
        m2 = self._add_match(1, "C", "D", 1, 1, "confirmed")
        self._set_mvp(m1, "Форвард-A")
        self._set_mvp(m2, "Форвард-A")

        payload = round_preview.build_digest_payload(self.div_id, 1)
        self.assertEqual(payload["mvp_of_the_round"], {"player_name": "Форвард-A", "mvp_count": 2})

    def test_shared_lead_leaves_the_round_mvp_empty(self):
        """У каждого по короне — выделять некого, иначе Темшик назовёт случайного."""
        m1 = self._add_match(1, "A", "B", 3, 0, "confirmed")
        m2 = self._add_match(1, "C", "D", 1, 1, "confirmed")
        self._set_mvp(m1, "Форвард-A")
        self._set_mvp(m2, "Форвард-C")

        self.assertIsNone(round_preview.build_digest_payload(self.div_id, 1)["mvp_of_the_round"])

    def test_round_without_crowns_has_no_round_mvp(self):
        self._add_match(1, "A", "B", 3, 0, "confirmed")
        self.assertIsNone(round_preview.build_digest_payload(self.div_id, 1)["mvp_of_the_round"])


class TestTextFallbacks(RoundAnalyticsTestBase):
    """Без Gemini публикация всё равно должна состояться — шаблоном."""

    def setUp(self):
        super().setUp()
        self._original_call = round_preview._call_gemini
        round_preview._call_gemini = lambda *args, **kwargs: None
        self.addCleanup(setattr, round_preview, "_call_gemini", self._original_call)

    def test_preview_fallback_is_valid_html_within_limits(self):
        self._add_match(1, "A", "B")
        self._add_match(1, "C", "D")
        self._add_round(1)

        payload = round_preview.build_preview_payload(self.div_id, 1)
        text = round_preview.generate_preview_text(payload)

        self.assertIn("ПРЕВЬЮ ТУРА 1", text)
        self.assertIn(self.teams["A"], text)
        self.assertIn(self.teams["D"], text)
        self.assertNotIn("**", text)
        self.assertLessEqual(len(text), round_preview.PREVIEW_MAX_CHARS)

    def test_digest_fallback_fits_the_telegram_caption_limit(self):
        m3 = self._seed_two_rounds()
        self._add_event(m3, "Форвард-B", "B", "goal", 2)

        payload = round_preview.build_digest_payload(self.div_id, 2)
        caption = round_preview.generate_digest_caption(payload)

        self.assertIn("ИТОГИ ТУРА 2", caption)
        self.assertIn("Форвард-B", caption)
        self.assertNotIn("**", caption)
        self.assertLessEqual(len(caption), round_preview.CAPTION_MAX_CHARS)
        # Лимит подписи к фото в Telegram — 1024 символа
        self.assertLessEqual(len(caption), 1024)


if __name__ == "__main__":
    unittest.main()
