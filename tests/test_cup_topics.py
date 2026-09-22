"""
tests/test_cup_topics.py

Темы вещания общего кубка и тексты, которые в них публикуются.

Кубковая тема живёт в отдельной таблице, а не строкой в `division_topics`:
`division_topics.division_id` — FK на `divisions`, а кубок дивизионом не является.
Синтетическая строка «дивизион КУБОК» просочилась бы в 11 дивизионных читалок и
во вкладки Mini App, поэтому здесь проверяется в том числе и обратное: связка
кубковой темы не видна ни одному дивизионному пикеру.

Тема берётся из сообщения, под которым админ вызвал команду (тот же приём, что у
`cmd_assign_topic`), и одна тема может нести только один формат.
"""

import os
import tempfile
import unittest

import database
from constants import CUP_TOPIC_TYPES
from services.topic_cache import topic_cache

STAGE = "1/64"


class CupTopicsTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Topics Season', 'active')")
            self.season = cursor.lastrowid

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        topic_cache.reload_cache()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    def test_01_bind_and_read(self):
        res = database.bind_cup_topic("line", -100111, 555, season_id=self.season)
        self.assertEqual(res["status"], "bound")
        topic = database.get_cup_topic("line", season_id=self.season)
        self.assertEqual((topic["group_chat_id"], topic["message_thread_id"]), (-100111, 555))
        self.assertEqual(len(database.list_cup_topics(season_id=self.season)), 1)

    def test_02_rebinding_the_same_topic_is_idempotent(self):
        database.bind_cup_topic("line", -100111, 555, season_id=self.season)
        again = database.bind_cup_topic("line", -100111, 555, season_id=self.season)
        self.assertEqual(again["status"], "already_bound")
        self.assertEqual(len(database.list_cup_topics(season_id=self.season)), 1)

    def test_03_one_topic_carries_one_format(self):
        database.bind_cup_topic("line", -100111, 555, season_id=self.season)
        conflict = database.bind_cup_topic("reports", -100111, 555, season_id=self.season)
        self.assertEqual(conflict["status"], "conflict_topic")
        self.assertEqual(conflict["occupied_by"], "line")
        self.assertEqual(database.get_cup_topic("reports", season_id=self.season), None)

    def test_04_reassignment_moves_the_binding(self):
        database.bind_cup_topic("line", -100111, 555, season_id=self.season)
        res = database.bind_cup_topic("line", -100111, 777, season_id=self.season)
        self.assertEqual(res["status"], "bound")
        self.assertEqual(database.get_cup_topic("line", season_id=self.season)["message_thread_id"], 777)
        self.assertEqual(len(database.list_cup_topics(season_id=self.season)), 1,
                         "старая тема не должна оставаться назначенной")

    def test_05_unknown_format_and_missing_coordinates(self):
        self.assertEqual(database.bind_cup_topic("scores", -1, 2, season_id=self.season)["status"], "error")
        self.assertEqual(database.bind_cup_topic("line", -1, None, season_id=self.season)["status"], "error")
        self.assertIsNone(database.get_cup_topic("nope", season_id=self.season))
        self.assertEqual(set(CUP_TOPIC_TYPES), {"line", "reports"})

    def test_06_seasons_do_not_share_topics(self):
        database.bind_cup_topic("line", -100111, 555, season_id=self.season)
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Other Season', 'active')")
            other = cursor.lastrowid
        self.assertIsNone(database.get_cup_topic("line", season_id=other))
        self.assertEqual(len(database.list_cup_topics(season_id=other)), 0)

    def test_07_division_topic_readers_never_see_a_cup_topic(self):
        """Кубковая тема не может просочиться в дивизионные пикеры и в TopicCache."""
        database.bind_cup_topic("line", -100111, 555, season_id=self.season)
        with database.transaction() as conn:
            rows = conn.execute("SELECT division_id, topic_type FROM division_topics").fetchall()
        self.assertEqual([dict(r) for r in rows], [])
        topic_cache.reload_cache()
        self.assertIsNone(topic_cache.get_by_topic(-100111, 555))
        for division in database.get_divisions():
            self.assertIsNone(topic_cache.get_by_division(division["id"], "line"))

    def test_08_line_message_shows_qualification_and_game_prices(self):
        from handlers.cup_management import _format_stage_messages
        from services import betting_engine

        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        stage = database.get_cup_stage(STAGE, season_id=self.season)
        database.provision_cup_stage_line(STAGE, season_id=self.season)
        self.assertTrue(database.open_cup_stage_bets(stage["id"])[0])
        betting_engine.generate_stage_markets(STAGE, season_id=self.season)

        chunks = _format_stage_messages(stage, line=True)
        text = "\n".join(chunks)
        self.assertIn("Клуб А — Клуб Б", text)
        self.assertIn("проход", text)
        self.assertIn("третья игра", text)
        self.assertIn("ОЗ да", text)
        self.assertNotIn("Ничья", text, "ничьей в кубке нет, и в тексте её быть не должно")

    def test_09_results_message_marks_who_advanced(self):
        from handlers.cup_management import _format_stage_messages

        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        stage = database.get_cup_stage(STAGE, season_id=self.season)
        database.provision_cup_stage_line(STAGE, season_id=self.season)
        rows = database.get_cup_stage_matches(STAGE, season_id=self.season)
        games = [r for r in rows if not r["is_series_header"]]
        for game, (h, a) in zip(games[:2], [(2, 0), (1, 0)]):
            database.confirm_and_finalize_match(game["match_id"], h, a, [], reporter_id=None)

        text = "\n".join(_format_stage_messages(stage, line=False))
        self.assertIn("2:0", text)
        self.assertIn("✅ <b>Клуб А</b>", text)


if __name__ == "__main__":
    unittest.main()
