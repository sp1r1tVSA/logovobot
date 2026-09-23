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


if __name__ == "__main__":
    unittest.main()
