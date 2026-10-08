"""
tests/test_admin_broadcast.py

Тесты централизованной рассылки для супер-администраторов (Logovo Broadcast):
1. Шаблоны и кнопки format_broadcast_content (ставки, новости, тур, свободное).
2. Безопасная отправка safe_send_broadcast (текст, фото, флуд-контроль RetryAfter, Forbidden, fallback HTML).
3. Разграничение прав: только глобальные админы (is_global_admin). Обычные игроки и админы дивизионов не имеют доступа.
4. Пошаговый мастер в ЛС: выбор категории -> ввод текста/фото -> выбор цели -> предпросмотр -> отправка/отмена.
5. Быстрая команда «Темшик рассылка [ставки|новости|тур] <текст>».
6. Запросы получателей в database.py (get_broadcast_user_ids, get_broadcast_chat_targets).
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import telegram.error

import config
import database
from handlers.admin_broadcast import (
    BROADCAST_CATEGORIES,
    TARGET_TITLES,
    admin_broadcast_cancel,
    admin_broadcast_cat_selected,
    admin_broadcast_confirm,
    admin_broadcast_hub,
    admin_broadcast_retarget,
    admin_broadcast_target_selected,
    format_broadcast_content,
    handle_broadcast_text_input,
    run_quick_broadcast,
    safe_send_broadcast,
)
from handlers.text_commands import handle_temshik_command

SUPER_ADMIN_ID = 999111
DIV_ADMIN_ID = 999222
REGULAR_USER_ID = 999333


class TestBroadcastFormatting(unittest.TestCase):
    """Тестирование шаблонов и формирования инлайн-кнопок."""

    def test_betting_template(self):
        text, markup = format_broadcast_content("betting", "Кэфы 2.5 на победу Барселоны!")
        self.assertIn("ЛОГОВО ФИФАРЕЙ", text)
        self.assertIn("Кэфы 2.5 на победу Барселоны!", text)
        self.assertIsNotNone(markup)
        btn = markup.inline_keyboard[0][0]
        self.assertIn("Сделать ставку", btn.text)

    def test_news_template(self):
        text, markup = format_broadcast_content("news", "Стартует новый сезон лиги!")
        self.assertIn("НОВОСТИ ЛИГИ", text)
        self.assertIn("Стартует новый сезон лиги!", text)
        self.assertIsNotNone(markup)
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.callback_data, "menu_cabinet")

    def test_tour_template(self):
        text, markup = format_broadcast_content("tour", "1-й тур открыт, дедлайн в воскресенье!")
        self.assertIn("СТАРТ ТУРА", text)
        self.assertIn("1-й тур открыт", text)
        self.assertIsNotNone(markup)
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.callback_data, "cabinet_my_matches")

    def test_free_template(self):
        text, markup = format_broadcast_content("free", "Произвольное сообщение")
        self.assertIn("ОБЪЯВЛЕНИЕ", text)
        self.assertIn("Произвольное сообщение", text)
        self.assertIsNone(markup)


class TestSafeSendBroadcast(unittest.IsolatedAsyncioTestCase):
    """Тестирование безопасной отправки сообщений получателям."""

    async def test_send_text_success(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=True)

        ok = await safe_send_broadcast(bot, chat_id=12345, thread_id=None, text="Hello world")
        self.assertTrue(ok)
        bot.send_message.assert_awaited_once()

    async def test_send_photo_success(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=True)

        ok = await safe_send_broadcast(bot, chat_id=12345, thread_id=99, text="Caption", photo_id="photo123")
        self.assertTrue(ok)
        bot.send_photo.assert_awaited_once()

    async def test_forbidden_user_blocked_bot(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=telegram.error.Forbidden("Forbidden: bot was blocked by the user"))

        ok = await safe_send_broadcast(bot, chat_id=12345, thread_id=None, text="Hello")
        self.assertFalse(ok)

    async def test_html_parse_error_fallback(self):
        bot = MagicMock()
        # Первая попытка с HTML падает из-за битого тега, вторая без HTML успешна
        bot.send_message = AsyncMock(
            side_effect=[
                telegram.error.BadRequest("Can't parse entities in message"),
                True,
            ]
        )

        ok = await safe_send_broadcast(bot, chat_id=12345, thread_id=None, text="<b>broken tag")
        self.assertTrue(ok)
        self.assertEqual(bot.send_message.await_count, 2)


class TestBroadcastAccessControl(unittest.IsolatedAsyncioTestCase):
    """Проверка прав: только global admin может вызывать рассылку."""

    def setUp(self):
        self.orig_admin_ids = getattr(config, "ADMIN_IDS", [])
        config.ADMIN_IDS = [SUPER_ADMIN_ID]

    def tearDown(self):
        config.ADMIN_IDS = self.orig_admin_ids

    async def test_non_admin_denied_in_hub(self):
        update = MagicMock()
        update.effective_user.id = REGULAR_USER_ID
        update.callback_query.answer = AsyncMock()
        context = MagicMock()

        await admin_broadcast_hub(update, context)
        update.callback_query.answer.assert_awaited_once_with(
            "⛔ Доступно только главным администраторам лиги.", show_alert=True
        )

    async def test_quick_command_denied_for_regular_user(self):
        update = MagicMock()
        update.effective_user.id = REGULAR_USER_ID
        update.effective_message.reply_text = AsyncMock()
        context = MagicMock()

        await run_quick_broadcast(update, context, "новости Привет всем")
        update.effective_message.reply_text.assert_awaited_once()
        args, kwargs = update.effective_message.reply_text.call_args
        self.assertIn("только главным администраторам", args[0])


class TestBroadcastWizardFlow(unittest.IsolatedAsyncioTestCase):
    """Пошаговый мастер рассылки в ЛС."""

    def setUp(self):
        self.orig_admin_ids = getattr(config, "ADMIN_IDS", [])
        config.ADMIN_IDS = [SUPER_ADMIN_ID]

    def tearDown(self):
        config.ADMIN_IDS = self.orig_admin_ids

    async def test_full_wizard_lifecycle(self):
        # 1. Стартовый хаб
        update = MagicMock()
        update.effective_user.id = SUPER_ADMIN_ID
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        await admin_broadcast_hub(update, context)
        update.callback_query.edit_message_text.assert_awaited_once()

        # 2. Выбор категории (betting)
        update.callback_query.data = "admin_bcast_cat:betting"
        update.callback_query.edit_message_text = AsyncMock()
        await admin_broadcast_cat_selected(update, context)
        self.assertEqual(context.user_data["broadcast"]["category"], "betting")
        self.assertEqual(context.user_data["broadcast"]["state"], "WAITING_TEXT")

        # 3. Ввод текста
        msg_update = MagicMock()
        msg_update.effective_user.id = SUPER_ADMIN_ID
        msg_update.effective_message.text = "Новая турнирная линия уже в Logovo.bet!"
        msg_update.effective_message.photo = None
        msg_update.effective_message.reply_text = AsyncMock()

        handled = await handle_broadcast_text_input(msg_update, context)
        self.assertTrue(handled)
        self.assertEqual(context.user_data["broadcast"]["state"], "WAITING_TARGET")
        self.assertEqual(
            context.user_data["broadcast"]["raw_text"],
            "Новая турнирная линия уже в Logovo.bet!",
        )

        # 4. Выбор аудитории (all) -> Предпросмотр
        target_update = MagicMock()
        target_update.effective_user.id = SUPER_ADMIN_ID
        target_update.callback_query.data = "admin_bcast_target:all"
        target_update.callback_query.answer = AsyncMock()
        target_update.callback_query.message.reply_text = AsyncMock()

        await admin_broadcast_target_selected(target_update, context)
        self.assertEqual(context.user_data["broadcast"]["target"], "all")
        self.assertEqual(context.user_data["broadcast"]["state"], "CONFIRMING")
        self.assertGreaterEqual(target_update.callback_query.message.reply_text.await_count, 2)

        # 5. Подтверждение и отправка
        confirm_update = MagicMock()
        confirm_update.effective_user.id = SUPER_ADMIN_ID
        confirm_update.callback_query.answer = AsyncMock()
        status_msg_mock = AsyncMock()
        confirm_update.callback_query.edit_message_text = AsyncMock(return_value=status_msg_mock)

        context.bot = MagicMock()
        context.bot.send_message = AsyncMock(return_value=True)

        with patch("database.get_broadcast_user_ids", return_value=[1001, 1002]), \
             patch("database.get_broadcast_chat_targets", return_value=[{"chat_id": -10099, "thread_id": 55}]):
            await admin_broadcast_confirm(confirm_update, context)

        # Сессия рассылки очищена
        self.assertNotIn("broadcast", context.user_data)
        # Статусное сообщение обновлено с отчетом
        status_msg_mock.edit_text.assert_awaited_once()
        report_text = status_msg_mock.edit_text.call_args[0][0]
        self.assertIn("Рассылка успешно выполнена!", report_text)
        self.assertIn("Доставлено в ЛС игрокам: <b>2</b>", report_text)
        self.assertIn("Отправлено в топики/чаты: <b>1</b>", report_text)

    async def test_wizard_cancel(self):
        update = MagicMock()
        update.effective_user.id = SUPER_ADMIN_ID
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        context = MagicMock()
        context.user_data = {"broadcast": {"category": "news", "state": "WAITING_TEXT"}}

        await admin_broadcast_cancel(update, context)
        self.assertNotIn("broadcast", context.user_data)
        update.callback_query.edit_message_text.assert_awaited_once()


class TestQuickBroadcastCommand(unittest.IsolatedAsyncioTestCase):
    """Быстрая текстовая команда рассылки «Темшик рассылка ...»."""

    def setUp(self):
        self.orig_admin_ids = getattr(config, "ADMIN_IDS", [])
        config.ADMIN_IDS = [SUPER_ADMIN_ID]

    def tearDown(self):
        config.ADMIN_IDS = self.orig_admin_ids

    async def test_quick_command_betting_category(self):
        update = MagicMock()
        update.effective_user.id = SUPER_ADMIN_ID
        update.effective_message.text = "Темшик рассылка ставки Сегодня открыта линия 3-го тура!"
        update.effective_message.photo = None
        update.effective_message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        handled = await handle_temshik_command(update, context)
        self.assertTrue(handled)
        self.assertIn("broadcast", context.user_data)
        bcast = context.user_data["broadcast"]
        self.assertEqual(bcast["category"], "betting")
        self.assertEqual(bcast["raw_text"], "Сегодня открыта линия 3-го тура!")
        self.assertEqual(bcast["state"], "CONFIRMING")

    async def test_quick_command_free_category(self):
        update = MagicMock()
        update.effective_user.id = SUPER_ADMIN_ID
        update.effective_message.text = "Темшик рассылка Завтра технические работы на сервере"
        update.effective_message.photo = None
        update.effective_message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {}

        handled = await handle_temshik_command(update, context)
        self.assertTrue(handled)
        self.assertIn("broadcast", context.user_data)
        bcast = context.user_data["broadcast"]
        self.assertEqual(bcast["category"], "free")
        self.assertEqual(bcast["raw_text"], "Завтра технические работы на сервере")


class TestDatabaseBroadcastQueries(unittest.TestCase):
    """Проверка функций выборки пользователей и чатов для рассылки."""

    def test_get_broadcast_user_ids(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')", (888001, "u1"))
            c.execute("INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')", (888002, "u2"))

        uids = database.get_broadcast_user_ids()
        self.assertIn(888001, uids)
        self.assertIn(888002, uids)

    def test_get_broadcast_chat_targets(self):
        database.set_config("group_id", "-100777888")
        targets = database.get_broadcast_chat_targets()
        chat_ids = [t["chat_id"] for t in targets]
        self.assertIn(-100777888, chat_ids)
