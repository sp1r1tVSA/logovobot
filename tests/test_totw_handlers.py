"""
Символическая сборная в боте: автопубликация (идемпотентность, топик), джоб,
кнопка админа, меню дивизиона, текстовые команды и регистрация хендлеров.

Сеть и Gemini не трогаются: рендер и отправка замоканы.
"""

import io
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers import admin as admin_handlers
from handlers import text_commands
from handlers.base import show_division_totw, show_division_totw_menu

PAYLOAD = {"xi": [{"player_name": "Игрок"}], "bench": [], "captain": None}


def _context():
    context = MagicMock()
    sent = MagicMock()
    sent.message_id = 4242
    context.bot.send_photo = AsyncMock(return_value=sent)
    context.bot.send_message = AsyncMock()
    return context


class TotwHandlersBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(name=f"Хендлеры {uid}", code=f"TOTWH_{uid}")

    def _render(self, payload=PAYLOAD):
        return AsyncMock(return_value=(payload, io.BytesIO(b"png"), "🌟 подпись"))


class TestPostTotw(TotwHandlersBase):
    async def test_posts_once_and_records_the_block(self):
        context = _context()
        with patch.object(database, "is_round_range_completed", return_value=True), \
                patch.object(admin_handlers, "_resolve_totw_topic", AsyncMock(return_value=(-1001, 7))), \
                patch.object(admin_handlers, "render_totw", self._render()) as render:
            first = await admin_handlers.post_totw(context, self.div_id, 1, 5)
            second = await admin_handlers.post_totw(context, self.div_id, 1, 5)

        self.assertTrue(first)
        self.assertFalse(second)
        context.bot.send_photo.assert_awaited_once()
        kwargs = context.bot.send_photo.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -1001)
        self.assertEqual(kwargs["message_thread_id"], 7)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        render.assert_awaited_once()
        # Автопост — с ИИ-подписью и подкачкой фото.
        self.assertTrue(render.call_args.kwargs["use_ai"])
        self.assertTrue(render.call_args.kwargs["prefetch"])
        self.assertTrue(database.has_round_content_post(self.div_id, 5, "totw"))

    async def test_force_publishes_again(self):
        database.record_round_content_post(self.div_id, 5, "totw", message_id=1)
        context = _context()
        with patch.object(database, "is_round_range_completed", return_value=False), \
                patch.object(admin_handlers, "_resolve_totw_topic", AsyncMock(return_value=(-1001, 7))), \
                patch.object(admin_handlers, "render_totw", self._render()):
            ok = await admin_handlers.post_totw(context, self.div_id, 1, 5, force=True)
        self.assertTrue(ok)
        context.bot.send_photo.assert_awaited_once()

    async def test_unfinished_block_is_skipped(self):
        context = _context()
        with patch.object(database, "is_round_range_completed", return_value=False), \
                patch.object(admin_handlers, "render_totw", self._render()) as render:
            ok = await admin_handlers.post_totw(context, self.div_id, 1, 5)
        self.assertFalse(ok)
        render.assert_not_awaited()
        context.bot.send_photo.assert_not_awaited()

    async def test_no_topic_is_skipped(self):
        context = _context()
        with patch.object(database, "is_round_range_completed", return_value=True), \
                patch.object(admin_handlers, "_resolve_totw_topic", AsyncMock(return_value=None)), \
                patch.object(admin_handlers, "render_totw", self._render()) as render:
            ok = await admin_handlers.post_totw(context, self.div_id, 1, 5)
        self.assertFalse(ok)
        render.assert_not_awaited()
        self.assertFalse(database.has_round_content_post(self.div_id, 5, "totw"))

    async def test_empty_team_is_not_posted_nor_recorded(self):
        context = _context()
        with patch.object(database, "is_round_range_completed", return_value=True), \
                patch.object(admin_handlers, "_resolve_totw_topic", AsyncMock(return_value=(-1001, 7))), \
                patch.object(admin_handlers, "render_totw", self._render({"xi": [], "bench": []})):
            ok = await admin_handlers.post_totw(context, self.div_id, 1, 5)
        self.assertFalse(ok)
        context.bot.send_photo.assert_not_awaited()
        self.assertFalse(database.has_round_content_post(self.div_id, 5, "totw"))


class TestTopicResolution(TotwHandlersBase):
    async def test_tables_first_then_analytics(self):
        from services.topic_cache import topic_cache

        def by_division(_div, topic_type):
            return {"group_chat_id": -100, "message_thread_id": 11} if topic_type == "analytics" else None

        with patch.object(topic_cache, "get_by_division", side_effect=by_division), \
                patch.object(database, "get_division_topics_map", return_value={}):
            self.assertEqual(await admin_handlers._resolve_totw_topic(self.div_id), (-100, 11))

        def with_tables(_div, topic_type):
            return {"group_chat_id": -100, "message_thread_id": 22 if topic_type == "tables" else 11}

        with patch.object(topic_cache, "get_by_division", side_effect=with_tables):
            self.assertEqual(await admin_handlers._resolve_totw_topic(self.div_id), (-100, 22))

    async def test_no_topic_at_all(self):
        from services.topic_cache import topic_cache

        with patch.object(topic_cache, "get_by_division", return_value=None), \
                patch.object(database, "get_division_topics_map", return_value={}):
            self.assertIsNone(await admin_handlers._resolve_totw_topic(self.div_id))


class TestTotwJob(unittest.IsolatedAsyncioTestCase):
    async def test_one_failing_block_does_not_stop_the_others(self):
        blocks = [
            {"division_id": 1, "season_id": 1, "start_round": 1, "end_round": 5},
            {"division_id": 2, "season_id": 1, "start_round": 6, "end_round": 10},
        ]
        post = AsyncMock(side_effect=[RuntimeError("boom"), True])
        with patch.object(database, "get_completed_totw_blocks_pending_publication", return_value=blocks), \
                patch.object(admin_handlers, "post_totw", post):
            await admin_handlers.job_post_totw(_context())
        self.assertEqual(post.await_count, 2)
        self.assertEqual(post.call_args.kwargs["division_id"], 2)
        self.assertEqual(post.call_args.kwargs["start_round"], 6)
        self.assertEqual(post.call_args.kwargs["end_round"], 10)


class TestPublishButton(TotwHandlersBase):
    def _update(self, data):
        update = MagicMock()
        update.effective_user.id = 555
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.message.chat_id = 12345
        query.message.is_topic_message = False
        update.callback_query = query
        return update, query

    async def test_non_admin_is_refused(self):
        update, query = self._update(f"totw_publish:{self.div_id}:1:5")
        post = AsyncMock()
        with patch.object(admin_handlers, "is_global_admin", return_value=False), \
                patch.object(database, "is_division_admin", return_value=False), \
                patch.object(admin_handlers, "post_totw", post):
            await admin_handlers.cb_totw_publish(update, _context())
        post.assert_not_awaited()
        self.assertTrue(query.answer.call_args.kwargs.get("show_alert"))

    async def test_admin_force_publishes(self):
        update, _query = self._update(f"totw_publish:{self.div_id}:6:10")
        post = AsyncMock(return_value=True)
        context = _context()
        with patch.object(admin_handlers, "is_global_admin", return_value=True), \
                patch.object(admin_handlers, "post_totw", post):
            await admin_handlers.cb_totw_publish(update, context)
        post.assert_awaited_once()
        self.assertEqual(post.call_args.args[1:4], (self.div_id, 6, 10))
        self.assertTrue(post.call_args.kwargs["force"])
        self.assertIn("опубликована", context.bot.send_message.call_args.kwargs["text"])


class TestDivisionMenu(TotwHandlersBase):
    def _query(self, data):
        update = MagicMock()
        update.effective_user.id = 555
        query = MagicMock()
        query.data = data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.photo = None
        query.message.chat_id = 12345
        query.message.is_topic_message = False
        query.message.delete = AsyncMock()
        update.callback_query = query
        return update, query

    async def test_block_picker(self):
        update, query = self._query(f"division_totw:1:{self.div_id}")
        with patch.object(database, "get_completed_totw_blocks", return_value=[(1, 5), (6, 10)]), \
                patch.object(database, "get_last_completed_round", return_value=12):
            await show_division_totw_menu(update, MagicMock())

        markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks, [
            f"division_totw_view:1:{self.div_id}:1:5",
            f"division_totw_view:1:{self.div_id}:6:10",
            f"division_totw_view:1:{self.div_id}:12:12",
            f"division_totw_view:1:{self.div_id}:1:12",
            f"division_view:1:{self.div_id}",
        ])

    async def test_nothing_played_yet(self):
        update, query = self._query(f"division_totw:1:{self.div_id}")
        with patch.object(database, "get_completed_totw_blocks", return_value=[]), \
                patch.object(database, "get_last_completed_round", return_value=None):
            await show_division_totw_menu(update, MagicMock())
        text = query.edit_message_text.call_args.args[0]
        self.assertIn("ещё не сыгран", text)
        markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        self.assertEqual([b.callback_data for row in markup.inline_keyboard for b in row],
                         [f"division_view:1:{self.div_id}"])

    async def test_view_shows_publish_button_to_admins_only(self):
        for can_publish in (True, False):
            update, query = self._query(f"division_totw_view:1:{self.div_id}:1:5")
            context = _context()
            with patch("handlers.base.render_totw", self._render()) as render, \
                    patch("handlers.base._can_publish_totw", AsyncMock(return_value=can_publish)):
                await show_division_totw(update, context)
            # Просмотр в меню — быстрый: без ИИ и без сети.
            self.assertFalse(render.call_args.kwargs["use_ai"])
            self.assertFalse(render.call_args.kwargs["prefetch"])
            markup = context.bot.send_photo.call_args.kwargs["reply_markup"]
            callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertEqual(f"totw_publish:{self.div_id}:1:5" in callbacks, can_publish)
            self.assertIn(f"division_totw:1:{self.div_id}", callbacks)


class TestTextCommands(TotwHandlersBase):
    def _update(self, text):
        update = MagicMock()
        update.effective_user.id = 555
        msg = MagicMock()
        msg.text = text
        msg.chat_id = 777
        msg.message_id = 9
        msg.is_topic_message = False
        msg.reply_text = AsyncMock()
        update.effective_message = msg
        return update, msg

    async def _run(self, text, blocks=((1, 5), (6, 10)), last_round=12, division_id=None):
        update, msg = self._update(text)
        view = AsyncMock()
        div = self.div_id if division_id is None else division_id
        with patch.object(text_commands, "resolve_command_division",
                          AsyncMock(side_effect=lambda _u, args: (div or None, args, []))), \
                patch.object(database, "get_completed_totw_blocks", return_value=list(blocks)), \
                patch.object(database, "get_last_completed_round", return_value=last_round), \
                patch.object(text_commands, "send_totw_view", view):
            handled = await text_commands.handle_temshik_command(update, MagicMock())
        return handled, view, msg

    def _bounds(self, view):
        return view.call_args.args[5], view.call_args.args[6]

    async def test_default_is_the_latest_block(self):
        handled, view, _ = await self._run("Темшик сборная")
        self.assertTrue(handled)
        self.assertEqual(self._bounds(view), (6, 10))

    async def test_explicit_range_and_alias(self):
        _, view, _ = await self._run("Темшик тотв 1-5")
        self.assertEqual(self._bounds(view), (1, 5))

    async def test_whole_season(self):
        _, view, _ = await self._run("Темшик сборная сезон")
        self.assertEqual(self._bounds(view), (1, 12))

    async def test_last_round_when_no_block_is_played(self):
        _, view, _ = await self._run("Темшик сборная", blocks=(), last_round=3)
        self.assertEqual(self._bounds(view), (3, 3))

    async def test_nothing_played(self):
        _, view, msg = await self._run("Темшик сборная", blocks=(), last_round=None)
        view.assert_not_awaited()
        self.assertIn("ни один тур", msg.reply_text.call_args.args[0])

    async def test_unknown_division_gets_a_hint(self):
        _, view, msg = await self._run("Темшик сборная", division_id=0)
        view.assert_not_awaited()
        self.assertIn("дивизион", msg.reply_text.call_args.args[0].lower())

    async def test_slash_command(self):
        update, _msg = self._update("/totw 6-10")
        context = MagicMock()
        context.args = ["6-10"]
        view = AsyncMock()
        with patch.object(text_commands, "resolve_command_division",
                          AsyncMock(return_value=(self.div_id, "6-10", []))), \
                patch.object(database, "get_last_completed_round", return_value=10), \
                patch.object(text_commands, "send_totw_view", view):
            await text_commands.cmd_totw(update, context)
        self.assertEqual(self._bounds(view), (6, 10))


class TestRegistration(unittest.TestCase):
    def test_handlers_are_registered_before_the_ai_catch_all(self):
        from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler
        from handlers import register_all_handlers

        app = MagicMock()
        registered = []
        app.add_handler.side_effect = lambda h, *a, **kw: registered.append(h)
        register_all_handlers(app)

        patterns = {
            h.pattern.pattern: i for i, h in enumerate(registered)
            if isinstance(h, CallbackQueryHandler) and getattr(h, "pattern", None)
        }
        commands = {
            cmd: i for i, h in enumerate(registered) if isinstance(h, CommandHandler) for cmd in h.commands
        }
        catch_all = max(
            i for i, h in enumerate(registered)
            if isinstance(h, MessageHandler) and getattr(h.callback, "__name__", "") == "handle_ai_chat"
        )

        for pattern in (
            r"^division_totw:(\d+):(\d+)$",
            r"^division_totw_view:(\d+):(\d+):(\d+):(\d+)$",
            r"^totw_publish:\d+:\d+:\d+$",
        ):
            self.assertIn(pattern, patterns)
            self.assertLess(patterns[pattern], catch_all)
        for cmd in ("totw", "totw_post"):
            self.assertIn(cmd, commands)
            self.assertLess(commands[cmd], catch_all)

    def test_register_jobs_schedules_the_totw_job(self):
        import main

        app = MagicMock()
        main.register_jobs(app)
        callbacks = [c.args[0] for c in app.job_queue.run_repeating.call_args_list]
        self.assertIn(admin_handlers.job_post_totw, callbacks)


if __name__ == "__main__":
    unittest.main()
