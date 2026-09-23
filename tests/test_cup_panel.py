"""
tests/test_cup_panel.py

Панель /cup: регистрация, права и разбор кнопок.

Каждая из ошибок ниже была тихой: кириллическая команда роняла весь
`register_all_handlers`, права проверялись по автору сообщения (боту), второй
`answer` на тот же callback отвергался Telegram, `asyncio.to_thread` от корутины
возвращал невыполненную корутину вместо числа рынков, а кнопка публикации
теряла действие при разборе `cup_post_results_5` по первому «_».
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
        self.assertEqual(commands, {"cup", "cup_topic"})
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

    def test_publish_button_reaches_its_action(self):
        publish = AsyncMock(return_value="опубликовано")
        _, render, _ = self._run("cup_post_results_5", _publish=publish)
        publish.assert_awaited_once()
        self.assertEqual(publish.await_args.args[0]["id"], 5)
        self.assertEqual(render.await_args.kwargs["note"], "опубликовано")

    def test_line_is_no_longer_published_under_the_post(self):
        """Линия живёт в Mini App: ни кнопки, ни шаблона колбэка для неё нет."""
        app = MagicMock()
        cup_management.register_cup_handlers(app)
        callback = next(c.args[0] for c in app.add_handler.call_args_list
                        if not isinstance(c.args[0], CommandHandler))
        self.assertIsNone(callback.pattern.match("cup_post_line_5"))
        datas = [b.callback_data for row in cup_management._stage_keyboard(5, decided=False) for b in row]
        self.assertFalse(any("post_line" in d for d in datas))


if __name__ == "__main__":
    unittest.main()


def _command_update(reply=None, thread_id=None, is_forum=False, username=None, chat_id=-1001234):
    message = SimpleNamespace(
        chat_id=chat_id,
        chat=SimpleNamespace(is_forum=is_forum, username=username),
        message_thread_id=thread_id,
        reply_to_message=reply,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1)), message


class CupTopicCommandTest(unittest.TestCase):
    """/cup_topic запоминает пост, на который им ответили, а не тему форума."""

    def _run(self, update, args=(), result=None):
        bind = MagicMock(return_value=result or {"status": "bound"})
        with patch.object(cup_management, "is_global_admin", return_value=True), \
                patch.object(cup_management.database, "bind_cup_topic", bind):
            asyncio.run(cup_management.cmd_cup_topic(update, SimpleNamespace(args=list(args))))
        return bind

    def test_reply_to_forwarded_channel_post_binds_that_post(self):
        post = SimpleNamespace(message_id=3853, is_automatic_forward=True)
        update, message = _command_update(reply=post, thread_id=3853, username="fifulatyrniru")
        bind = self._run(update)
        bind.assert_called_once_with("reports", -1001234, 3853)
        text = message.reply_text.await_args.args[0]
        self.assertIn("https://t.me/fifulatyrniru/3853", text)

    def test_comment_under_post_binds_the_thread_root(self):
        other_comment = SimpleNamespace(message_id=4000, is_automatic_forward=False)
        update, _ = _command_update(reply=other_comment, thread_id=3853)
        bind = self._run(update)
        bind.assert_called_once_with("reports", -1001234, 3853)

    def test_a_leftover_argument_still_binds_results(self):
        """Старая форма `/cup_topic line` не ломается: формат теперь один."""
        update, _ = _command_update(reply=SimpleNamespace(message_id=77, is_automatic_forward=False))
        bind = self._run(update, args=("line",))
        bind.assert_called_once_with("reports", -1001234, 77)

    def test_reply_in_a_plain_group_binds_the_replied_message(self):
        update, _ = _command_update(reply=SimpleNamespace(message_id=77, is_automatic_forward=False))
        bind = self._run(update)
        bind.assert_called_once_with("reports", -1001234, 77)

    def test_without_a_reply_nothing_is_bound(self):
        update, message = _command_update()
        bind = self._run(update)
        bind.assert_not_called()
        self.assertIn("ОТВЕТОМ на пост", message.reply_text.await_args.args[0])

    def test_publish_replies_under_the_bound_post(self):
        target = {"chat_id": -1001234, "reply_to_message_id": 3853, "allow_sending_without_reply": True}
        bot = MagicMock(send_message=AsyncMock())
        with patch.object(cup_management, "resolve_cup_target", AsyncMock(return_value=target)), \
                patch.object(cup_management, "_format_stage_messages", return_value=["a", "b"]):
            note = asyncio.run(cup_management._publish(dict(STAGE), bot))
        self.assertEqual(bot.send_message.await_count, 2)
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["reply_to_message_id"], 3853)
        self.assertNotIn("message_thread_id", kwargs)
        self.assertIn("2", note)

    def test_publish_without_a_bound_post_sends_nothing(self):
        bot = MagicMock(send_message=AsyncMock())
        with patch.object(cup_management, "resolve_cup_target", AsyncMock(return_value=None)):
            note = asyncio.run(cup_management._publish(dict(STAGE), bot))
        bot.send_message.assert_not_awaited()
        self.assertIn("/cup_topic", note)
