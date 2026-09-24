"""
tests/test_division_cups.py

Кубки дивизионов и темы «Кубок».

У каждого дивизиона свой кубок на 16 клубов (1/8 → финал), рядом с общим. Этап
кубка дивизиона лежит в `cup_stages` под ключом «1/8@D<id>» с `division_id`,
а серии и матчи — с обычным именем стадии; чей это кубок, говорит этап.

Здесь закреплено:
- кубок дивизиона принимает только клубы своего дивизиона, и стадии разных
  кубков одного сезона не мешают друг другу;
- результат кубкового матча уходит в тему его кубка, а не общего;
- проход серии объявляется ровно один раз;
- закреп с сеткой редактируется, а если не вышло — публикуется заново;
- `/api/cup?division_id=` отдаёт выбранный кубок и список всех кубков;
- админ дивизиона видит в /cup только свой кубок;
- сид `--division` проверяет ростер и число серий, умеет файл и победителей.
"""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import config
import database
import scripts.seed_cup_bracket as seeder


class _FreshDbCase(unittest.TestCase):
    """Своя база на каждый тест: сетки кубков уникальны в сезоне."""

    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        self.season = database._resolve_season_id(None)
        self.div = {d["code"]: int(d["id"]) for d in database.get_divisions()}

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    def clubs(self, code: str) -> list[str]:
        return list(config.DIVISION_CLUBS[code])

    def seed_d3(self, stage="1/8", pairs=None):
        clubs = self.clubs("DIV_3")
        pairs = pairs or [(clubs[i], clubs[i + 1]) for i in range(0, 16, 2)]
        return database.create_cup_series(stage, pairs, season_id=self.season, division_id=self.div["DIV_3"])

    def decide(self, series_id: int, winner: str):
        with database.transaction() as conn:
            conn.execute(
                "UPDATE cup_series SET winner_name = ?, status = 'finished' WHERE id = ?",
                (winner, series_id),
            )


class DivisionCupSchemaTest(_FreshDbCase):
    def test_schema_carries_scope_topics_and_announcement(self):
        with database.transaction() as conn:
            stage_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cup_stages)")}
            series_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cup_series)")}
            topic_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cup_topics)")}
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (database.MIGRATION_027_DIVISION_CUPS,),
            ).fetchone()
        self.assertIn("division_id", stage_cols)
        self.assertIn("announced_winner", series_cols)
        self.assertTrue({"topic_type", "message_thread_id", "anchor_message_id"} <= topic_cols)
        self.assertIsNotNone(applied)

    def test_scope_helpers(self):
        d3 = self.div["DIV_3"]
        self.assertIsNone(database.cup_scope(0))
        self.assertIsNone(database.cup_scope(""))
        self.assertEqual(database.cup_scope(str(d3)), d3)
        with self.assertRaises(ValueError):
            database.cup_scope("abc")
        key = database.cup_stage_key("1/8", d3)
        self.assertEqual(database.split_cup_stage_key(key), ("1/8", d3))
        self.assertEqual(database.split_cup_stage_key("1/8"), ("1/8", None))
        self.assertEqual(database.cup_scope_label(None), "Общий кубок")
        self.assertEqual(database.cup_scope_label(d3), "Кубок Д3")


class DivisionCupRepositoryTest(_FreshDbCase):
    def test_same_stage_in_two_cups_does_not_collide(self):
        d3 = self.div["DIV_3"]
        self.seed_d3()
        d4 = self.clubs("DIV_4")
        database.create_cup_series("1/8", [(d4[0], d4[1])], season_id=self.season)

        self.assertEqual(len(database.get_cup_bracket("1/8", self.season, division_id=d3)), 8)
        self.assertEqual(len(database.get_cup_bracket("1/8", self.season)), 1)
        self.assertEqual(database.list_cup_scopes(self.season), [None, d3])

        stages = database.list_cup_stages(self.season, division_id=d3)
        self.assertEqual([s["stage"] for s in stages], ["1/8"])
        self.assertEqual(stages[0]["division_id"], d3)
        # Серии хранят обычное имя стадии — линия и тексты не видят суффикса.
        self.assertEqual(database.get_cup_bracket("1/8", self.season, division_id=d3)[0]["stage"], "1/8")

    def test_foreign_club_is_refused(self):
        d1 = self.clubs("DIV_1")
        d3 = self.clubs("DIV_3")
        with self.assertRaises(ValueError) as ctx:
            database.create_cup_series(
                "1/8", [(d3[0], d1[0])], season_id=self.season, division_id=self.div["DIV_3"],
            )
        self.assertIn("не из дивизиона", str(ctx.exception))
        self.assertIsNone(database.get_cup_stage("1/8", self.season, division_id=self.div["DIV_3"]))

    def test_match_knows_its_cup(self):
        d3 = self.div["DIV_3"]
        self.seed_d3()
        report = database.provision_cup_stage_line("1/8", season_id=self.season, division_id=d3)
        self.assertEqual(report["series"], 8)
        matches = database.get_cup_stage_matches("1/8", season_id=self.season, division_id=d3)
        game = next(m for m in matches if not m.get("is_series_header"))
        scope = database.get_match_cup_scope(game["match_id"] if "match_id" in game else game["id"])
        self.assertEqual(scope["division_id"], d3)
        self.assertEqual(scope["stage"], "1/8")
        self.assertEqual(scope["season_id"], self.season)

    def test_series_win_is_announced_once(self):
        ids = self.seed_d3()
        self.assertIsNone(database.claim_cup_series_announcement(ids[0]), "победителя ещё нет")
        winner = database.get_cup_bracket("1/8", self.season, division_id=self.div["DIV_3"])[0]["team1_name"]
        self.decide(ids[0], winner)
        claimed = database.claim_cup_series_announcement(ids[0])
        self.assertEqual(claimed["winner_name"], winner)
        self.assertEqual(claimed["division_id"], self.div["DIV_3"])
        self.assertIsNone(database.claim_cup_series_announcement(ids[0]), "второй раз не объявляется")

    def test_topics_are_per_cup(self):
        d3 = self.div["DIV_3"]
        database.set_cup_topic(None, -100500, 11, season_id=self.season)
        database.set_cup_topic(d3, -100500, 33, season_id=self.season)
        database.set_cup_bracket_anchor(d3, 777, season_id=self.season)

        self.assertEqual(database.get_cup_topic(None, self.season)["message_thread_id"], 11)
        topic = database.get_cup_topic(d3, self.season)
        self.assertEqual((topic["message_thread_id"], topic["anchor_message_id"]), (33, 777))

        # Перепривязка темы забывает закреп: сетку в новой теме публикуют заново.
        database.set_cup_topic(d3, -100500, 34, season_id=self.season)
        self.assertIsNone(database.get_cup_topic(d3, self.season)["anchor_message_id"])

        # Одна тема — одно назначение: чужой кубок её теряет.
        database.set_cup_topic(d3, -100500, 11, season_id=self.season)
        self.assertIsNone(database.get_cup_topic(None, self.season))

        self.assertTrue(database.clear_cup_topic(d3, self.season))
        self.assertIsNone(database.get_cup_topic(d3, self.season))
        self.assertFalse(database.clear_cup_topic(d3, self.season))


class CupBroadcastTest(_FreshDbCase):
    def _bot(self, edit_error=None):
        bot = MagicMock()
        bot.edit_message_media = AsyncMock(side_effect=edit_error)
        bot.send_photo = AsyncMock(return_value=SimpleNamespace(chat_id=-100500, message_id=4242))
        bot.pin_chat_message = AsyncMock()
        bot.send_message = AsyncMock()
        return bot

    def test_renderer_draws_empty_and_seeded_cups(self):
        from services.graphics.cup_bracket_generator import generate_cup_bracket_image

        empty = generate_cup_bracket_image([], "Кубок Д3", division_id=self.div["DIV_3"])
        self.assertTrue(empty.getvalue().startswith(b"\x89PNG"))
        self.seed_d3()
        bracket = database.get_cup_full_bracket(self.div["DIV_3"], self.season)
        png = generate_cup_bracket_image(bracket, "Кубок Д3", division_id=self.div["DIV_3"], subtitle="Идёт 1/8")
        self.assertIsInstance(png, io.BytesIO)
        self.assertTrue(png.getvalue().startswith(b"\x89PNG"))

    def test_no_topic_means_silence(self):
        from services import cup_broadcast

        bot = self._bot()
        self.assertFalse(asyncio.run(cup_broadcast.refresh_cup_bracket(bot, self.div["DIV_3"], self.season)))
        bot.send_photo.assert_not_called()

    def test_first_refresh_publishes_and_pins_then_edits(self):
        from services import cup_broadcast

        d3 = self.div["DIV_3"]
        self.seed_d3()
        database.set_cup_topic(d3, -100500, 33, season_id=self.season)
        bot = self._bot()

        self.assertTrue(asyncio.run(cup_broadcast.refresh_cup_bracket(bot, d3, self.season)))
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.call_args.kwargs["message_thread_id"], 33)
        bot.pin_chat_message.assert_awaited_once()
        self.assertEqual(database.get_cup_topic(d3, self.season)["anchor_message_id"], 4242)

        self.assertTrue(asyncio.run(cup_broadcast.refresh_cup_bracket(bot, d3, self.season)))
        bot.edit_message_media.assert_awaited_once()
        self.assertEqual(bot.edit_message_media.call_args.kwargs["message_id"], 4242)
        self.assertEqual(bot.send_photo.await_count, 1, "закреп отредактирован, а не опубликован заново")

    def test_uneditable_anchor_is_republished(self):
        from services import cup_broadcast

        d3 = self.div["DIV_3"]
        database.set_cup_topic(d3, -100500, 33, season_id=self.season)
        database.set_cup_bracket_anchor(d3, 1, season_id=self.season)
        bot = self._bot(edit_error=Exception("Message to edit not found"))

        self.assertTrue(asyncio.run(cup_broadcast.refresh_cup_bracket(bot, d3, self.season)))
        bot.send_photo.assert_awaited_once()
        self.assertEqual(database.get_cup_topic(d3, self.season)["anchor_message_id"], 4242)

    def test_not_modified_is_success(self):
        from services import cup_broadcast

        d3 = self.div["DIV_3"]
        database.set_cup_topic(d3, -100500, 33, season_id=self.season)
        database.set_cup_bracket_anchor(d3, 1, season_id=self.season)
        bot = self._bot(edit_error=Exception("Message is not modified"))

        self.assertTrue(asyncio.run(cup_broadcast.refresh_cup_bracket(bot, d3, self.season)))
        bot.send_photo.assert_not_called()

    def test_result_goes_to_the_topic_of_its_own_cup(self):
        from constants import CUP_DIVISION_SENTINEL
        from handlers.base import resolve_post_target

        d3 = self.div["DIV_3"]
        self.seed_d3()
        database.provision_cup_stage_line("1/8", season_id=self.season, division_id=d3)
        database.set_cup_topic(None, -100500, 11, season_id=self.season)
        database.set_cup_topic(d3, -100500, 33, season_id=self.season)
        game = next(m for m in database.get_cup_stage_matches("1/8", self.season, division_id=d3)
                    if not m.get("is_series_header"))
        match_id = game["match_id"] if "match_id" in game else game["id"]

        target = asyncio.run(resolve_post_target(CUP_DIVISION_SENTINEL, "results", match_id=match_id))
        self.assertEqual(target, {"chat_id": -100500, "message_thread_id": 33})

        database.clear_cup_topic(d3, self.season)
        self.assertIsNone(
            asyncio.run(resolve_post_target(CUP_DIVISION_SENTINEL, "results", match_id=match_id)),
            "без темы своего кубка результат не уходит в тему общего",
        )


class CupOverviewApiTest(_FreshDbCase):
    def test_overview_lists_cups_and_follows_the_parameter(self):
        from api import routes_cup

        d3 = self.div["DIV_3"]
        self.seed_d3()
        d4 = self.clubs("DIV_4")
        database.create_cup_series("1/8", [(d4[0], d4[1])], season_id=self.season)

        default = routes_cup._load_overview(False, None)
        self.assertIsNone(default["division_id"])
        self.assertEqual(default["cup_label"], "Общий кубок")
        self.assertEqual([c["division_id"] for c in default["cups"]], [None, d3])
        self.assertEqual([c["label"] for c in default["cups"]], ["Общий", "Д3"])

        chosen = routes_cup._load_overview(True, d3)
        self.assertEqual(chosen["division_id"], d3)
        self.assertEqual(chosen["cup_label"], "Кубок Д3")
        self.assertEqual(chosen["stages"][0]["division_id"], d3)
        self.assertEqual(chosen["stages"][0]["series_total"], 8)

    def test_default_falls_back_to_the_first_seeded_cup(self):
        from api import routes_cup

        self.seed_d3()
        overview = routes_cup._load_overview(False, None)
        self.assertEqual(overview["division_id"], self.div["DIV_3"])

    def test_bad_parameter_is_rejected(self):
        from api import routes_cup

        request = SimpleNamespace(query={"division_id": "abc"})
        with self.assertRaises(ValueError):
            routes_cup._requested_scope(request)
        self.assertEqual(routes_cup._requested_scope(SimpleNamespace(query={"division_id": "0"})), (True, None))
        self.assertEqual(routes_cup._requested_scope(SimpleNamespace(query={})), (False, None))


class CupPanelScopeTest(unittest.TestCase):
    """Админ дивизиона правит только свой кубок."""

    D3 = 3

    def _run(self, data, series=None, stage=None):
        from handlers import cup_management

        query = SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(from_user=SimpleNamespace(id=999, is_bot=True)),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        context = SimpleNamespace(user_data={}, bot=MagicMock())
        render = AsyncMock()
        card = AsyncMock()
        with patch.object(cup_management, "is_global_admin", return_value=False), \
                patch.object(cup_management.database, "get_admin_divisions", return_value=[{"id": self.D3}]), \
                patch.object(cup_management.database, "get_cup_series", return_value=series), \
                patch.object(cup_management.database, "get_cup_stage_by_id", return_value=stage), \
                patch.object(cup_management, "_render_panel", render), \
                patch.object(cup_management, "_render_series_card", card):
            asyncio.run(cup_management.cb_cup(update, context))
        return query, render, card, context

    def test_scopes_of_a_division_admin(self):
        from handlers import cup_management

        with patch.object(cup_management, "is_global_admin", return_value=False), \
                patch.object(cup_management.database, "get_admin_divisions", return_value=[{"id": self.D3}]):
            self.assertEqual(cup_management._manageable_scopes(42), [self.D3])

    def test_general_cup_is_denied(self):
        query, render, _, _ = self._run("cup_scope_0")
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
        render.assert_not_called()

    def test_own_cup_opens(self):
        query, render, _, context = self._run(f"cup_scope_{self.D3}")
        self.assertFalse(query.answer.call_args.kwargs.get("show_alert"))
        render.assert_awaited_once()
        self.assertEqual(context.user_data["cup_scope"], self.D3)

    def test_foreign_series_and_stage_are_denied(self):
        query, _, card, _ = self._run("cup_ser_7", series={"id": 7, "division_id": 1})
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
        card.assert_not_called()

        query, render, _, _ = self._run("cup_open_5", stage={"id": 5, "stage": "1/8", "division_id": None})
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))
        render.assert_not_called()

    def test_own_series_opens(self):
        query, _, card, _ = self._run("cup_ser_7", series={"id": 7, "division_id": self.D3})
        query.answer.assert_awaited_once_with()
        card.assert_awaited_once()

    def test_bracket_and_scope_buttons_match_the_pattern(self):
        from handlers import cup_management

        app = MagicMock()
        cup_management.register_cup_handlers(app)
        callback = next(c.args[0] for c in app.add_handler.call_args_list
                        if hasattr(c.args[0], "pattern"))
        for data in ("cup_scope_0", "cup_scope_3", "cup_bracket_0", "cup_bracket_5"):
            self.assertTrue(callback.pattern.match(data), data)


class DivisionCupSeedTest(_FreshDbCase):
    def _main(self, argv):
        old = sys.argv
        sys.argv = ["seed_cup_bracket.py"] + argv
        try:
            return seeder.main()
        finally:
            sys.argv = old

    def _pairs_file(self, lines):
        fh = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        fh.write("\n".join(lines))
        fh.close()
        self.addCleanup(os.remove, fh.name)
        return fh.name

    def _d3_lines(self):
        clubs = self.clubs("DIV_3")
        return ["# жеребьёвка Д3"] + [f"{i // 2 + 1}. {clubs[i]} — {clubs[i + 1]}" for i in range(0, 16, 2)]

    def test_pairs_file_parsing(self):
        path = self._pairs_file(["Аль-Наср — Челси", "", "# комментарий", "2) Байер - Милан", "Рома; Бетис",
                                 "МЮ vs Бавария"])
        self.assertEqual(seeder.parse_pairs_file(path), [
            ("Аль-Наср", "Челси"), ("Байер", "Милан"), ("Рома", "Бетис"), ("МЮ", "Бавария"),
        ])
        bad = self._pairs_file(["Только один клуб"])
        with self.assertRaises(ValueError):
            seeder.parse_pairs_file(bad)

    def test_dry_run_then_apply_by_code(self):
        path = self._pairs_file(self._d3_lines())
        d3 = self.div["DIV_3"]
        self.assertEqual(self._main(["--division", "DIV_3", "--pairs-file", path]), 0)
        self.assertEqual(database.get_cup_bracket("1/8", self.season, division_id=d3), [])

        self.assertEqual(self._main(["--division", str(d3), "--pairs-file", path, "--apply"]), 0)
        bracket = database.get_cup_bracket("1/8", self.season, division_id=d3)
        self.assertEqual(len(bracket), 8)
        self.assertEqual(bracket[0]["team1_name"], self.clubs("DIV_3")[0])
        # Общий кубок не тронут.
        self.assertIsNone(database.get_cup_stage("1/8", self.season))

        # Повтор не удваивает сетку.
        self.assertEqual(self._main(["--division", str(d3), "--pairs-file", path, "--apply"]), 0)
        self.assertEqual(len(database.get_cup_bracket("1/8", self.season, division_id=d3)), 8)

    def test_foreign_club_and_wrong_count_are_reported(self):
        lines = self._d3_lines()
        lines[1] = f"{self.clubs('DIV_1')[0]} — {self.clubs('DIV_3')[1]}"
        with self.assertRaises(ValueError) as ctx:
            seeder.validate_pairs("1/8", seeder.parse_pairs_file(self._pairs_file(lines)), "DIV_3")
        self.assertIn("допущены DIV_3", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            seeder.validate_pairs("1/8", seeder.parse_pairs_file(self._pairs_file(self._d3_lines()[:4])), "DIV_3")
        self.assertIn("8 сер.", str(ctx.exception))

    def test_general_cup_stage_is_refused_for_a_division(self):
        path = self._pairs_file(self._d3_lines())
        self.assertEqual(self._main(["--division", "DIV_3", "--stage", "1/64", "--pairs-file", path, "--apply"]), 1)
        self.assertEqual(database.list_cup_stages(self.season, division_id=self.div["DIV_3"]), [])

    def test_unknown_division_is_refused(self):
        self.assertEqual(self._main(["--division", "DIV_99"]), 1)

    def test_from_winners_pairs_the_previous_stage(self):
        d3 = self.div["DIV_3"]
        ids = self.seed_d3()
        # Пока не все серии решены — пары не составляются.
        self.assertEqual(self._main(["--division", str(d3), "--stage", "1/4", "--from-winners", "--apply"]), 1)

        bracket = database.get_cup_bracket("1/8", self.season, division_id=d3)
        for series in bracket:
            self.decide(series["id"], series["team2_name"])
        self.assertEqual(self._main(["--division", str(d3), "--stage", "1/4", "--from-winners", "--apply"]), 0)

        quarter = database.get_cup_bracket("1/4", self.season, division_id=d3)
        self.assertEqual(len(quarter), 4)
        self.assertEqual((quarter[0]["team1_name"], quarter[0]["team2_name"]),
                         (bracket[0]["team2_name"], bracket[1]["team2_name"]))
        self.assertEqual(len(ids), 8)


if __name__ == "__main__":
    unittest.main()
