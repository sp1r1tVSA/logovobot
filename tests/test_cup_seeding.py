"""
tests/test_cup_seeding.py

Сетка кубка дивизиона из бота (`services/cup_seeding` и кнопки «🎲 Завести сетку»
в /cup): разбор пар, проверка ростера и числа серий, следующая стадия из
победителей, права админа дивизиона и запись только по «✅ Записать».
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import config
import database
from handlers import cup_management
from services import cup_seeding


class _FreshDbCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        self.div = {d["code"]: int(d["id"]) for d in database.get_divisions()}
        self.d3 = self.div["DIV_3"]
        clubs = list(config.DIVISION_CLUBS["DIV_3"])
        self.pairs = [(clubs[i], clubs[i + 1]) for i in range(0, 16, 2)]
        cup_management._seed_state.clear()

    def tearDown(self):
        cup_management._seed_state.clear()
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    def text(self, pairs=None):
        return "\n".join(f"{n}. {a} — {b}" for n, (a, b) in enumerate(pairs or self.pairs, start=1))

    def decide_all(self, stage):
        with database.transaction() as conn:
            for s in database.get_cup_bracket(stage, division_id=self.d3):
                conn.execute("UPDATE cup_series SET winner_name = ?, status = 'finished' WHERE id = ?",
                             (s["team1_name"], s["id"]))


class ParsePairsTest(unittest.TestCase):
    def test_numbering_comments_and_separators(self):
        text = "# сетка\n1. Арсенал — Челси\n2) Аль-Наср - Милан\n\nЛидс; Брайтон\nА vs Б"
        self.assertEqual(cup_seeding.parse_pairs_text(text), [
            ("Арсенал", "Челси"), ("Аль-Наср", "Милан"), ("Лидс", "Брайтон"), ("А", "Б")])

    def test_bad_line_lists_every_problem(self):
        with self.assertRaises(ValueError) as cm:
            cup_seeding.parse_pairs_text("Арсенал\nЧелси — Лидс\nХолм")
        self.assertIn("строка 1", str(cm.exception))
        self.assertIn("строка 3", str(cm.exception))

    def test_empty_is_refused(self):
        with self.assertRaises(ValueError):
            cup_seeding.parse_pairs_text("\n# ничего\n")


class ValidateAndSeedTest(_FreshDbCase):
    def test_valid_pairs_are_canonical(self):
        parsed = cup_seeding.parse_pairs_text(self.text())
        self.assertEqual(cup_seeding.validate_pairs("1/8", parsed, self.d3), self.pairs)

    def test_foreign_club_duplicate_and_count(self):
        foreign = config.DIVISION_CLUBS["DIV_1"][0]
        pairs = list(self.pairs[:7]) + [(foreign, self.pairs[0][0])]
        with self.assertRaises(ValueError) as cm:
            cup_seeding.validate_pairs("1/8", pairs, self.d3)
        msg = str(cm.exception)
        self.assertIn("нет такого клуба", msg)
        self.assertIn("встречается дважды", msg)
        with self.assertRaises(ValueError) as cm:
            cup_seeding.validate_pairs("1/8", self.pairs[:3], self.d3)
        self.assertIn("нужно 8 пар", str(cm.exception))

    def test_self_pair(self):
        club = self.pairs[0][0]
        with self.assertRaises(ValueError) as cm:
            cup_seeding.validate_pairs("1/8", [(club, club)] + self.pairs[1:], self.d3)
        self.assertIn("сам с собой", str(cm.exception))

    def test_unknown_stage(self):
        with self.assertRaises(ValueError):
            cup_seeding.validate_pairs("1/64", self.pairs, self.d3)

    def test_next_seed_walks_the_bracket(self):
        self.assertEqual(cup_seeding.next_seed(self.d3), ("1/8", "pairs"))
        cup_seeding.seed("1/8", self.pairs, self.d3)
        self.assertIsNone(cup_seeding.next_seed(self.d3))
        self.decide_all("1/8")
        self.assertEqual(cup_seeding.next_seed(self.d3), ("1/4", "winners"))
        pairs = cup_seeding.winners_pairs("1/4", self.d3)
        self.assertEqual(len(pairs), 4)
        self.assertEqual(pairs[0], (self.pairs[0][0], self.pairs[1][0]))
        cup_seeding.seed("1/4", pairs, self.d3)
        self.assertIsNone(cup_seeding.next_seed(self.d3))

    def test_winners_need_decided_previous_stage(self):
        with self.assertRaises(ValueError):
            cup_seeding.winners_pairs("1/4", self.d3)
        cup_seeding.seed("1/8", self.pairs, self.d3)
        with self.assertRaises(ValueError) as cm:
            cup_seeding.winners_pairs("1/4", self.d3)
        self.assertIn("не решены", str(cm.exception))

    def test_seeding_twice_is_refused(self):
        cup_seeding.seed("1/8", self.pairs, self.d3)
        with self.assertRaises(ValueError):
            cup_seeding.seed("1/8", self.pairs, self.d3)


def _callback(data, user_id=42):
    query = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(from_user=SimpleNamespace(id=999, is_bot=True)), answer=AsyncMock())
    return SimpleNamespace(callback_query=query, effective_user=query.from_user), query


class PanelSeedFlowTest(_FreshDbCase):
    def _press(self, data, user_id=42):
        update, query = _callback(data, user_id)
        context = SimpleNamespace(user_data={}, bot=MagicMock())
        render, edit = AsyncMock(), AsyncMock()
        with patch.object(cup_management, "is_global_admin", return_value=False), \
                patch.object(cup_management.database, "get_admin_divisions",
                             return_value=[{"id": self.d3}]), \
                patch.object(cup_management, "_render_panel", render), \
                patch.object(cup_management, "_edit_or_reply", edit), \
                patch("services.cup_broadcast.refresh_cup_bracket", AsyncMock(return_value=True)), \
                patch.object(cup_management.admin_journal, "record", AsyncMock()) as journal:
            asyncio.run(cup_management.cb_cup(update, context))
        return query, render, edit, journal

    def _send(self, text, user_id=42):
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user_id))
        with patch.object(cup_management, "is_global_admin", return_value=False), \
                patch.object(cup_management.database, "get_admin_divisions", return_value=[{"id": self.d3}]):
            asyncio.run(cup_management.on_seed_input(update, SimpleNamespace(user_data={}, bot=MagicMock())))
        return message

    def test_buttons_match_the_registered_pattern(self):
        app = MagicMock()
        cup_management.register_cup_handlers(app)
        callback = next(c.args[0] for c in app.add_handler.call_args_list if hasattr(c.args[0], "pattern"))
        for data in ("cup_seed_3", "cup_seedok_3", "cup_seedno_3"):
            self.assertTrue(callback.pattern.match(data), data)

    def test_general_and_foreign_cup_are_denied(self):
        for data in ("cup_seed_0", "cup_seedok_0", f"cup_seed_{self.div['DIV_1']}"):
            query, _, edit, _ = self._press(data)
            self.assertTrue(query.answer.call_args.kwargs.get("show_alert"), data)
            edit.assert_not_called()
        self.assertEqual(cup_management._seed_state, {})

    def test_full_flow_writes_only_on_confirm(self):
        _, _, edit, _ = self._press(f"cup_seed_{self.d3}")
        edit.assert_awaited_once()
        self.assertTrue(cup_management._get_seed(42)["awaiting"])

        message = self._send(self.text())
        message.reply_text.assert_awaited_once()
        self.assertEqual(database.get_cup_bracket("1/8", division_id=self.d3), [])
        self.assertFalse(cup_management._get_seed(42)["awaiting"])

        _, render, _, journal = self._press(f"cup_seedok_{self.d3}")
        self.assertEqual(len(database.get_cup_bracket("1/8", division_id=self.d3)), 8)
        journal.assert_awaited_once()
        self.assertEqual(journal.await_args.args[1], "cup_bracket_seeded")
        self.assertNotIn("не записана", render.await_args.kwargs.get("note", ""))
        self.assertIsNone(cup_management._get_seed(42))

    def test_bad_input_keeps_waiting(self):
        self._press(f"cup_seed_{self.d3}")
        message = self._send(self.text(self.pairs[:3]))
        self.assertIn("нужно 8 пар", message.reply_text.await_args.args[0])
        self.assertTrue(cup_management._get_seed(42)["awaiting"])
        self.assertEqual(database.get_cup_bracket("1/8", division_id=self.d3), [])

    def test_cancel_drops_the_draft(self):
        self._press(f"cup_seed_{self.d3}")
        self._press(f"cup_seedno_{self.d3}")
        self.assertIsNone(cup_management._get_seed(42))
        self.assertEqual(database.get_cup_bracket("1/8", division_id=self.d3), [])

    def test_confirm_without_draft_is_stale(self):
        _, render, _, journal = self._press(f"cup_seedok_{self.d3}")
        self.assertIn("устарел", render.await_args.kwargs["note"])
        journal.assert_not_called()

    def test_winners_stage_goes_through_preview(self):
        cup_seeding.seed("1/8", self.pairs, self.d3)
        self.decide_all("1/8")
        _, _, edit, _ = self._press(f"cup_seed_{self.d3}")
        self.assertIn("1/4", edit.await_args.args[1])
        self._press(f"cup_seedok_{self.d3}")
        self.assertEqual(len(database.get_cup_bracket("1/4", division_id=self.d3)), 4)

    def test_input_of_a_stranger_is_ignored(self):
        self._press(f"cup_seed_{self.d3}")
        message = self._send(self.text(), user_id=77)
        message.reply_text.assert_not_called()


class EntryPointsTest(unittest.TestCase):
    def test_cup_is_in_the_admin_menu(self):
        from handlers import bot_menu
        self.assertIn("cup", [c.command for c in bot_menu.ADMIN_COMMANDS])
        self.assertNotIn("cup", [c.command for c in bot_menu.DEFAULT_COMMANDS])

    def test_seed_action_is_journaled_in_catalogue(self):
        from services import admin_journal
        self.assertEqual(admin_journal.ACTIONS["cup_bracket_seeded"][0], "cups")


if __name__ == "__main__":
    unittest.main()
