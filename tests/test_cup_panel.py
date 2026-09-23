"""
tests/test_cup_panel.py

Панель /cup: регистрация, права и разбор кнопок.

Каждая из ошибок ниже была тихой: кириллическая команда роняла весь
`register_all_handlers`, права проверялись по автору сообщения (боту), второй
`answer` на тот же callback отвергался Telegram, `asyncio.to_thread` от корутины
возвращал невыполненную корутину вместо числа рынков.

Команды /cup_topic и кнопки «результаты под пост» больше нет: результаты кубка
участники выкладывают под постом сами.
"""

import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import CommandHandler

from handlers import cup_management


def _update(data: str, user_id: int = 1):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        # Автор панели — бот; права по нему не проверяются.
        message=SimpleNamespace(from_user=SimpleNamespace(id=999, is_bot=True)),
        answer=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query, effective_user=query.from_user), query


def _context():
    return SimpleNamespace(user_data={}, bot=MagicMock())


STAGE = {"id": 5, "stage": "1/64", "season_id": 3, "is_open": 0, "bets_open": 0}


class CupPanelRegistrationTest(unittest.TestCase):
    def test_commands_are_latin(self):
        app = MagicMock()
        cup_management.register_cup_handlers(app)
        commands = set()
        for call in app.add_handler.call_args_list:
            handler = call.args[0]
            if isinstance(handler, CommandHandler):
                commands |= set(handler.commands)
        self.assertEqual(commands, {"cup"})
        self.assertTrue(all(c.isascii() for c in commands))

    def test_every_panel_button_matches_the_registered_pattern(self):
        app = MagicMock()
        cup_management.register_cup_handlers(app)
        callback = next(c.args[0] for c in app.add_handler.call_args_list
                        if not isinstance(c.args[0], CommandHandler))
        rows = cup_management._stage_keyboard(5, decided=False)
        for button in (b for row in rows for b in row):
            self.assertTrue(callback.pattern.match(button.callback_data), button.callback_data)


class CupPanelCallbackTest(unittest.TestCase):
    def _run(self, data, admin=True, **patches):
        update, query = _update(data)
        context = _context()
        render = AsyncMock()
        with patch.object(cup_management, "is_global_admin", return_value=admin) as is_admin, \
                patch.object(cup_management, "_render_panel", render), \
                patch.object(cup_management.database, "get_cup_stage_by_id", return_value=dict(STAGE)):
            with patch.multiple(cup_management, **patches) if patches else nullcontext():
                asyncio.run(cup_management.cb_cup(update, context))
        return query, render, is_admin

    def test_rights_follow_the_presser_not_the_message_author(self):
        query, render, is_admin = self._run("cup_refresh", admin=True)
        is_admin.assert_called_once_with(1)
        render.assert_awaited_once()

    def test_denied_answers_once_with_an_alert(self):
        query, render, _ = self._run("cup_refresh", admin=False)
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))
        render.assert_not_awaited()

    def test_allowed_answers_exactly_once(self):
        query, _, _ = self._run("cup_stage_5")
        query.answer.assert_awaited_once_with()

    def test_open_actually_prices_the_stage(self):
        with patch.object(cup_management.database, "open_cup_stage_bets", return_value=(True, "Ставки открыты.")), \
                patch("services.betting_engine.generate_stage_markets", return_value=[1, 2, 3]) as generate:
            _, render, _ = self._run("cup_open_5")
        generate.assert_called_once_with("1/64", season_id=3)
        self.assertIn("Выставлено объектов линии: 3", render.await_args.kwargs["note"])

    def test_nothing_is_published_under_a_post(self):
        """Ни линии, ни результатов бот под пост не шлёт: кнопок и колбэков для них нет."""
        app = MagicMock()
        cup_management.register_cup_handlers(app)
        callback = next(c.args[0] for c in app.add_handler.call_args_list
                        if not isinstance(c.args[0], CommandHandler))
        for data in ("cup_post_line_5", "cup_post_results_5"):
            self.assertIsNone(callback.pattern.match(data), data)
        for decided in (False, True):
            datas = [b.callback_data for row in cup_management._stage_keyboard(5, decided=decided) for b in row]
            self.assertFalse(any("post_" in d for d in datas), datas)

    def test_stale_publish_button_does_nothing(self):
        """Старая панель в чате ещё может прислать `cup_post_results_5` — это не действие."""
        query, render, _ = self._run("cup_post_results_5")
        query.answer.assert_awaited_once_with()
        render.assert_not_awaited()



SERIES = {"id": 11, "stage": "1/64", "series_num": 3, "team1_name": "Бавария",
          "team2_name": "Манчестер Юнайтед", "team1_wins": 1, "team2_wins": 0,
          "winner_name": None, "status": "active", "stage_id": 5}


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


class CupSeriesScreensTest(unittest.TestCase):
    """Серии этапа и их игры — как туры и матчи тура в «Управлении матчами»."""

    def _callback_pattern(self):
        app = MagicMock()
        cup_management.register_cup_handlers(app)
        return next(c.args[0] for c in app.add_handler.call_args_list
                    if not isinstance(c.args[0], CommandHandler)).pattern

    def test_series_button_on_every_stage(self):
        for decided in (False, True):
            datas = [b.callback_data for row in cup_management._stage_keyboard(5, decided=decided) for b in row]
            self.assertIn("cup_series_5", datas)

    def test_series_list_links_every_series(self):
        update, query = _update("cup_series_5")
        query.edit_message_text = AsyncMock()
        bracket = [dict(SERIES), dict(SERIES, id=12, series_num=4, winner_name="Реал Мадрид", team1_wins=2)]
        games = [{"series_id": 11, "status": "disputed"}]
        with patch.object(cup_management, "is_global_admin", return_value=True), \
                patch.object(cup_management.database, "get_cup_stage_by_id", return_value=dict(STAGE)), \
                patch.object(cup_management.database, "get_cup_bracket", return_value=bracket), \
                patch.object(cup_management.database, "get_cup_stage_games", return_value=games):
            asyncio.run(cup_management.cb_cup(update, _context()))
        buttons = _buttons(query.edit_message_text.await_args.kwargs["reply_markup"])
        self.assertEqual([b.callback_data for b in buttons], ["cup_ser_11", "cup_ser_12", "cup_stage_5"])
        self.assertTrue(buttons[0].text.startswith("⚠️ 3. Бавария 1:0"))
        self.assertTrue(buttons[1].text.startswith("✅ 4."))
        pattern = self._callback_pattern()
        for b in buttons:
            self.assertTrue(pattern.match(b.callback_data), b.callback_data)

    def test_series_card_opens_the_admin_match_card(self):
        update, query = _update("cup_ser_11")
        query.edit_message_text = AsyncMock()
        games = [
            {"match_id": 101, "game_num_in_series": 1, "status": "confirmed",
             "player1_score": 3, "player2_score": 3, "cup_winner_team": "Бавария"},
            {"match_id": 102, "game_num_in_series": 2, "status": "pending",
             "player1_score": None, "player2_score": None, "cup_winner_team": None},
        ]
        with patch.object(cup_management, "is_global_admin", return_value=True), \
                patch.object(cup_management.database, "get_cup_series", return_value=dict(SERIES)), \
                patch.object(cup_management.database, "get_cup_series_games", return_value=games):
            asyncio.run(cup_management.cb_cup(update, _context()))
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Счёт серии: <code>1 : 0</code>", text)
        buttons = _buttons(query.edit_message_text.await_args.kwargs["reply_markup"])
        self.assertEqual([b.callback_data for b in buttons],
                         ["admin_view_match_101", "admin_view_match_102", "cup_series_5"])
        self.assertEqual(buttons[0].text, "Игра 1: 3:3, пен. → Бавария")
        self.assertEqual(buttons[1].text, "Игра 2: ⚔️")


class CupMatchCardTest(unittest.IsolatedAsyncioTestCase):
    """Карточка кубковой игры возвращает к серии и не предлагает ТП/ТН/продление."""

    async def test_back_goes_to_the_series(self):
        from handlers import admin

        match = {"id": 101, "round_number": -1, "division_id": 0, "tournament_type": "cup",
                 "cup_stage": "1/64", "cup_series_id": 11, "game_num_in_series": 2,
                 "status": "pending", "player1_team": "Бавария", "player2_team": "Манчестер Юнайтед",
                 "player1_nickname": "a", "player2_nickname": "b", "player1_score": None,
                 "player2_score": None, "is_extended": 0, "photo_id": None}
        query = MagicMock(data="admin_view_match_101", answer=AsyncMock(), edit_message_text=AsyncMock())
        query.from_user.id = 1
        query.message.photo = None
        update = MagicMock(callback_query=query)
        update.effective_user.id = 1
        with patch("handlers.base.is_admin", return_value=True), \
                patch.object(admin, "is_admin", return_value=True), \
                patch.object(admin, "is_global_admin", return_value=True), \
                patch.object(admin.database, "get_match", return_value=match), \
                patch.object(admin.database, "is_match_overdue", return_value=False):
            await admin.admin_view_match(update, MagicMock())
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Кубок 1/64, игра 2", text)
        datas = [b.callback_data for b in _buttons(query.edit_message_text.await_args.kwargs["reply_markup"])]
        self.assertIn("cup_ser_11", datas)
        self.assertIn("admin_reset_match_execute_101", datas)
        self.assertFalse(any(d and d.startswith(("admin_tp_", "admin_extend_")) for d in datas), datas)


if __name__ == "__main__":
    unittest.main()
