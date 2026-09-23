"""
tests/test_cup_results_gate.py

Кубковая игра закрыта для ввода результата, пока этап не стартовал.

Пока этап на линии (`cup_stages.is_open = 0`), на его игры принимаются
прогнозы; результат, занесённый в это время, — ставка на известный исход.
Ввод открывает только «▶ начать этап». Запрет стоит в
`confirm_and_finalize_match`, куда сходятся кабинет, черновики и админка, а
кабинет дополнительно прячет кнопку ввода и помечает игру 🔒.
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from handlers import cabinet

STAGE = "1/64"


class CupResultsGateTest(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Gate Season', 'active')")
            self.season = cursor.lastrowid
            for tid, team in ((1, "Клуб А"), (2, "Клуб Б")):
                cursor.execute(
                    "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                    "VALUES (?, ?, ?, 'player', datetime('now', '+3 hours'))",
                    (tid, f"coach_{tid}", team)
                )
        database.create_cup_series(STAGE, [("Клуб А", "Клуб Б")], season_id=self.season)
        self.stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        database.provision_cup_stage_line(STAGE, season_id=self.season)
        ok, message = database.open_cup_stage_bets(self.stage_id)
        self.assertTrue(ok, message)
        rows = database.get_cup_stage_matches(STAGE, season_id=self.season)
        self.game = next(r["match_id"] for r in rows
                         if not r["is_series_header"] and r["game_num_in_series"] == 1)
        self.header = next(r["match_id"] for r in rows if r["is_series_header"])

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    def _status(self, match_id):
        with database.transaction() as conn:
            return conn.execute("SELECT status FROM matches WHERE id = ?", (match_id,)).fetchone()["status"]

    def test_reason_until_the_stage_starts(self):
        reason = database.cup_results_closed_reason(self.game)
        self.assertIsNotNone(reason)
        self.assertIn("1/64", reason)
        ok, message = database.start_cup_stage(self.stage_id, actor_id=1)
        self.assertTrue(ok, message)
        self.assertIsNone(database.cup_results_closed_reason(self.game))

    def test_header_and_unknown_rows_are_not_gated(self):
        self.assertIsNone(database.cup_results_closed_reason(self.header))
        self.assertIsNone(database.cup_results_closed_reason(999_999))

    def test_confirm_is_refused_on_the_line_and_rolled_back(self):
        with self.assertRaises(ValueError) as caught:
            database.confirm_and_finalize_match(self.game, 2, 0, [], reporter_id=1)
        self.assertIn("не начат", str(caught.exception))
        self.assertEqual(self._status(self.game), "pending")
        with database.transaction() as conn:
            wins = conn.execute("SELECT team1_wins FROM cup_series WHERE stage = ?", (STAGE,)).fetchone()[0]
        self.assertEqual(wins, 0, "серия не сдвинулась")

    def test_admin_manual_score_is_refused_on_the_line(self):
        with self.assertRaises(ValueError):
            database.admin_set_match_score(self.game, 2, 0, admin_id=1)
        self.assertEqual(self._status(self.game), "pending")

    def test_confirm_goes_through_after_the_start(self):
        database.start_cup_stage(self.stage_id, actor_id=1)
        database.confirm_and_finalize_match(self.game, 2, 0, [], reporter_id=1)
        self.assertEqual(self._status(self.game), "confirmed")

    def _card(self):
        query = SimpleNamespace(data=f"cabinet_view_match_{self.game}",
                                from_user=SimpleNamespace(id=1), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
        edit = AsyncMock()
        with patch.object(cabinet, "check_group_card_access", return_value=True), \
                patch.object(cabinet, "safe_edit_or_reply", edit):
            asyncio.run(cabinet.cabinet_view_match(update, SimpleNamespace(user_data={})))
        args, kwargs = edit.await_args
        text = kwargs.get("text") or next(a for a in args if isinstance(a, str))
        buttons = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        return text, buttons

    def test_card_hides_the_entry_button_until_the_start(self):
        text, buttons = self._card()
        self.assertIn("Ввод результата закрыт", text)
        self.assertFalse(any(str(b).startswith("cabinet_report_score_") for b in buttons), buttons)

        database.start_cup_stage(self.stage_id, actor_id=1)
        text, buttons = self._card()
        self.assertNotIn("Ввод результата закрыт", text)


if __name__ == "__main__":
    unittest.main()
