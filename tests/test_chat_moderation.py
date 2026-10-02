"""«Темшик мут 30м спам» — мут в чате дивизиона.

Парсер срока, права (глобальный админ / админ своего дивизиона), защита
админов и ботов, форумный топик и запись в журнал.
"""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

from handlers import chat_moderation as cm
from handlers.text_commands import handle_temshik_command
from services import admin_journal

GROUP_ID = -100500
ADMIN_ID = 111
VICTIM_ID = 222


def run(coro):
    return asyncio.run(coro)


class TestParseDuration(unittest.TestCase):

    def test_numbered_units(self):
        for text, seconds in [
            ("30м", 1800), ("30 мин", 1800), ("15 минут", 900), ("2ч", 7200),
            ("2 часа", 7200), ("1д", 86400), ("3 дня", 3 * 86400), ("на 1 день", 86400),
            ("10m", 600), ("5h", 18000), ("2d", 2 * 86400),
        ]:
            delta, reason = cm.parse_duration(text)
            self.assertEqual(delta, timedelta(seconds=seconds), text)
            self.assertEqual(reason, "", text)

    def test_reason_is_kept(self):
        delta, reason = cm.parse_duration("30м спам в чате")
        self.assertEqual(delta, timedelta(minutes=30))
        self.assertEqual(reason, "спам в чате")

    def test_bare_words(self):
        self.assertEqual(cm.parse_duration("на час")[0], timedelta(hours=1))
        self.assertEqual(cm.parse_duration("сутки флуд")[0], timedelta(days=1))

    def test_no_duration_means_all_text_is_reason(self):
        delta, reason = cm.parse_duration("мин не прошло, флуд")
        self.assertIsNone(delta)
        self.assertEqual(reason, "мин не прошло, флуд")
        self.assertEqual(cm.parse_duration(""), (None, ""))

    def test_unit_is_not_a_prefix_of_a_word(self):
        # «30 минус» и «5 дом» — не срок
        self.assertIsNone(cm.parse_duration("30 минус")[0])
        self.assertIsNone(cm.parse_duration("5 дом")[0])

    def test_format_duration(self):
        self.assertEqual(cm.format_duration(timedelta(minutes=30)), "30 мин")
        self.assertEqual(cm.format_duration(timedelta(hours=2)), "2 ч")
        self.assertEqual(cm.format_duration(timedelta(hours=1, minutes=15)), "1 ч 15 мин")
        self.assertEqual(cm.format_duration(timedelta(days=1, hours=6)), "1 дн 6 ч")


def build_update(user_id=ADMIN_ID, text="Темшик мут 30м", *, reply_to=VICTIM_ID,
                 chat_type="supergroup", thread_id=None, reply_is_topic=False):
    update = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    update.effective_user.id = user_id
    update.effective_user.username = "moder"
    update.effective_chat.id = GROUP_ID
    update.effective_chat.type = chat_type
    if reply_to is None:
        update.message.reply_to_message = None
    else:
        reply = MagicMock()
        reply.from_user.id = reply_to
        reply.from_user.username = "victim"
        reply.from_user.full_name = "Victim"
        reply.from_user.is_bot = False
        reply.forum_topic_created = MagicMock() if reply_is_topic else None
        update.message.reply_to_message = reply
    return update


def build_context(member_status="member"):
    context = MagicMock()
    context.bot.id = 999
    context.bot.restrict_chat_member = AsyncMock()
    context.bot.get_chat_member = AsyncMock(return_value=MagicMock(status=member_status))
    context.bot.get_chat = AsyncMock(return_value=MagicMock(permissions="DEFAULT_PERMS"))
    return context


def patched(*, global_admin=False, division_admin=False, league_admin=False, division=7):
    """Контекст-менеджер: роли и дивизион чата подменены."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch.object(cm, "is_global_admin", return_value=global_admin))
    stack.enter_context(patch.object(cm.database, "is_division_admin", return_value=division_admin))
    stack.enter_context(patch.object(cm, "is_admin", return_value=league_admin))
    stack.enter_context(patch.object(
        cm.database, "get_division_by_group", return_value={"id": division} if division else None))
    stack.enter_context(patch("services.topic_cache.topic_cache.get_by_topic", return_value=None))
    record = stack.enter_context(patch.object(cm.admin_journal, "record", new=AsyncMock()))
    return stack, record


class TestMuteCommand(unittest.TestCase):

    def test_global_admin_mutes_by_reply(self):
        update, context = build_update(), build_context()
        stack, record = patched(global_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, "30м спам"))
        kwargs = context.bot.restrict_chat_member.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], GROUP_ID)
        self.assertEqual(kwargs["user_id"], VICTIM_ID)
        self.assertFalse(kwargs["permissions"].can_send_messages)
        self.assertIsNotNone(kwargs["until_date"].tzinfo)
        record.assert_awaited_once()
        args, rec_kwargs = record.await_args
        self.assertEqual(args[:4], (ADMIN_ID, "chat_mute", "user", VICTIM_ID))
        self.assertEqual(rec_kwargs["division_id"], 7)
        self.assertEqual(rec_kwargs["reason"], "спам")
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("30 мин", text)
        self.assertIn("спам", text)

    def test_default_duration_is_one_hour(self):
        update, context = build_update(), build_context()
        stack, record = patched(global_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, ""))
        self.assertEqual(record.await_args.kwargs["new"], "1 ч")

    def test_division_admin_of_this_chat_can_mute(self):
        update, context = build_update(), build_context()
        stack, _ = patched(division_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, "15м"))
        context.bot.restrict_chat_member.assert_awaited_once()

    def test_division_admin_of_other_division_is_refused(self):
        update, context = build_update(), build_context()
        stack, record = patched(division_admin=False)
        with stack:
            run(cm.handle_mute_command(update, context, "15м"))
        context.bot.restrict_chat_member.assert_not_awaited()
        record.assert_not_awaited()
        self.assertIn("администраторы этого дивизиона", update.message.reply_text.await_args.args[0])

    def test_chat_without_division_only_for_global_admin(self):
        update, context = build_update(), build_context()
        stack, _ = patched(division_admin=True, division=None)
        with stack:
            run(cm.handle_mute_command(update, context, "15м"))
        context.bot.restrict_chat_member.assert_not_awaited()

    def test_private_chat_is_refused(self):
        update, context = build_update(chat_type="private"), build_context()
        stack, _ = patched(global_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, "15м"))
        context.bot.restrict_chat_member.assert_not_awaited()
        self.assertIn("только в чатах", update.message.reply_text.await_args.args[0])

    def test_topic_binding_decides_division(self):
        update, context = build_update(thread_id=55), build_context()
        stack, record = patched(global_admin=True, division=None)
        with stack, patch("services.topic_cache.topic_cache.get_by_topic",
                          return_value={"division_id": 3}) as lookup:
            run(cm.handle_mute_command(update, context, "15м"))
        lookup.assert_called_once_with(GROUP_ID, 55)
        self.assertEqual(record.await_args.kwargs["division_id"], 3)

    def test_topic_root_is_not_a_reply(self):
        update = build_update(reply_is_topic=True, thread_id=55)
        context = build_context()
        stack, _ = patched(global_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, "15м"))
        context.bot.restrict_chat_member.assert_not_awaited()
        self.assertIn("Ответьте командой", update.message.reply_text.await_args.args[0])

    def test_target_by_username_without_reply(self):
        update, context = build_update(reply_to=None), build_context()
        stack, _ = patched(global_admin=True)
        with stack, patch.object(cm.database, "find_user_by_ref", return_value={
                "telegram_id": VICTIM_ID, "username": "victim", "team_name": "Ренн"}) as find:
            run(cm.handle_mute_command(update, context, "@victim 2ч флуд"))
        find.assert_called_once_with("@victim")
        kwargs = context.bot.restrict_chat_member.await_args.kwargs
        self.assertEqual(kwargs["user_id"], VICTIM_ID)

    def test_no_target_shows_usage(self):
        update, context = build_update(reply_to=None), build_context()
        stack, _ = patched(global_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, "30м"))
        context.bot.restrict_chat_member.assert_not_awaited()
        self.assertIn("Ответьте командой", update.message.reply_text.await_args.args[0])

    def test_duration_limits(self):
        for text in ("0м", "31д", "999999д"):
            update, context = build_update(), build_context()
            stack, _ = patched(global_admin=True)
            with stack:
                run(cm.handle_mute_command(update, context, text))
            context.bot.restrict_chat_member.assert_not_awaited()
            self.assertIn("от 1 минуты до 30 дней", update.message.reply_text.await_args.args[0], text)

    def test_cannot_mute_self_bot_or_admins(self):
        cases = [
            ("self", dict(reply_to=ADMIN_ID), {}, "member"),
            ("league admin", {}, dict(league_admin=True), "member"),
            ("chat admin", {}, {}, "administrator"),
            ("chat owner", {}, {}, "creator"),
        ]
        for name, upd_kw, patch_kw, status in cases:
            update, context = build_update(**upd_kw), build_context(member_status=status)
            stack, record = patched(global_admin=True, **patch_kw)
            with stack:
                run(cm.handle_mute_command(update, context, "15м"))
            context.bot.restrict_chat_member.assert_not_awaited()
            record.assert_not_awaited()

        update, context = build_update(), build_context()
        update.message.reply_to_message.from_user.is_bot = True
        stack, _ = patched(global_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, "15м"))
        context.bot.restrict_chat_member.assert_not_awaited()

    def test_telegram_error_is_reported_and_not_journaled(self):
        update, context = build_update(), build_context()
        context.bot.restrict_chat_member.side_effect = telegram.error.BadRequest("Not enough rights")
        stack, record = patched(global_admin=True)
        with stack:
            run(cm.handle_mute_command(update, context, "15м"))
        record.assert_not_awaited()
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("Not enough rights", text)
        self.assertIn("Блокировка пользователей", text)


class TestUnmuteCommand(unittest.TestCase):

    def test_unmute_restores_group_permissions(self):
        update, context = build_update(text="Темшик размут"), build_context()
        stack, record = patched(global_admin=True)
        with stack:
            run(cm.handle_unmute_command(update, context, ""))
        kwargs = context.bot.restrict_chat_member.await_args.kwargs
        self.assertEqual(kwargs["user_id"], VICTIM_ID)
        self.assertEqual(kwargs["permissions"], "DEFAULT_PERMS")
        self.assertNotIn("until_date", kwargs)
        self.assertEqual(record.await_args.args[:4], (ADMIN_ID, "chat_unmute", "user", VICTIM_ID))

    def test_unmute_requires_rights(self):
        update, context = build_update(), build_context()
        stack, _ = patched()
        with stack:
            run(cm.handle_unmute_command(update, context, ""))
        context.bot.restrict_chat_member.assert_not_awaited()


class TestDispatch(unittest.TestCase):
    """«Темшик …» доходит до модуля с правильными аргументами."""

    def dispatch(self, text):
        update = build_update(text=text)
        context = build_context()
        with patch("handlers.chat_moderation.handle_mute_command", new=AsyncMock()) as mute, \
             patch("handlers.chat_moderation.handle_unmute_command", new=AsyncMock()) as unmute, \
             patch("handlers.text_commands.is_admin", return_value=True):
            handled = run(handle_temshik_command(update, context))
        return handled, mute, unmute

    def test_mute_variants(self):
        for text in ("Темшик мут 30м спам", "темшик замуть 30м спам", "Темшик mute 30м спам"):
            handled, mute, unmute = self.dispatch(text)
            self.assertTrue(handled, text)
            mute.assert_awaited_once()
            self.assertEqual(mute.await_args.args[2], "30м спам", text)
            unmute.assert_not_awaited()

    def test_unmute_variants(self):
        for text, rest in [
            ("Темшик размут", ""),
            ("Темшик размут @victim", "@victim"),
            ("Темшик снять мут", ""),
            ("Темшик снять мут @victim", "@victim"),
        ]:
            handled, mute, unmute = self.dispatch(text)
            self.assertTrue(handled, text)
            unmute.assert_awaited_once()
            self.assertEqual(unmute.await_args.args[2], rest, text)
            mute.assert_not_awaited()


class TestJournalCatalog(unittest.TestCase):

    def test_actions_are_labelled_under_discipline(self):
        for action in ("chat_mute", "chat_unmute"):
            category, label = admin_journal.ACTIONS[action]
            self.assertEqual(category, "discipline")
            self.assertTrue(label)


if __name__ == "__main__":
    unittest.main()
