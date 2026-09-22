"""
Аудит 9a — переименование дивизиона обязано обновлять TopicCache.

Инвариант (`AGENTS.md`, «Division topic routing → services/topic_cache
(+ reload_cache() после мутаций)»): `divisions.name` — кэшируемое поле. TopicCache
кладёт `division_name` копией при загрузке (`services/topic_cache.py:47`), а
`handlers/base.py:791-822` берёт его из кэша и печатает в подписи и на картинке
турнирной таблицы. Поэтому `/table` в топиках дивизиона после переименования
обязан показывать новое имя без перезапуска процесса.

До фикса `admin_div_rename_receive` (`handlers/admin.py`) писал `database.update_division`
и не трогал кэш — все остальные мутации привязок (`handlers/admin.py:2133-2161`,
`:2238-2244`, `handlers/topic_management.py:212,293`) кэш обновляют.
"""
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import ConversationHandler

import config
import database
from handlers.admin import ADMIN_EXPECT_DIV_RENAME, admin_div_rename_receive
from services.topic_cache import topic_cache

GROUP_CHAT_ID = -1001999000001
REPORTS_THREAD_ID = 700


class TestDivisionRenameRefreshesTopicCache(unittest.IsolatedAsyncioTestCase):
    """Свой файл БД на тест — привязки топиков не затекают между тестами."""

    async def asyncSetUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div = database.create_division(f"Дивизион До {self.uid}", f"PRE{self.uid[:4]}")
        self.super_admin = 973001
        database.register_user(self.super_admin, f"rename_super_{self.uid}", team_name=None, role="admin")

        database.set_division_topic(self.div, "reports", REPORTS_THREAD_ID, GROUP_CHAT_ID)
        topic_cache.reload_cache()
        self.assertEqual(
            topic_cache.get_by_topic(GROUP_CHAT_ID, REPORTS_THREAD_ID)["division_name"],
            f"Дивизион До {self.uid}",
            "Кэш обязан стартовать со старого имени — иначе тест пустой",
        )

        self._admin_ids_patch = patch.object(config, "ADMIN_IDS", [self.super_admin])
        self._admin_ids_patch.start()

    async def asyncTearDown(self):
        self._admin_ids_patch.stop()
        topic_cache.reload_cache()

    # --- фабрики ----------------------------------------------------------

    def _text_update(self, user_id: int, text: str) -> MagicMock:
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.effective_user.username = "rename_tester"
        update.effective_chat = MagicMock()
        update.effective_chat.id = user_id
        update.effective_chat.type = "private"
        update.callback_query = None
        msg = MagicMock()
        msg.text = text
        msg.reply_text = AsyncMock()
        update.message = msg
        return update

    def _context(self, div_id: int) -> MagicMock:
        context = MagicMock()
        context.user_data = {"rename_div_id": div_id}
        return context

    async def _rename(self, new_name: str) -> MagicMock:
        update = self._text_update(self.super_admin, new_name)
        code = await admin_div_rename_receive(update, self._context(self.div))
        self.assertEqual(code, ConversationHandler.END)
        return update

    # --- A. кэш обновляется ------------------------------------------------

    async def test_rename_updates_forward_topic_lookup(self):
        """Прямой путь потребителя: handlers/base.py:791 get_by_topic → :821-822."""
        await self._rename("Дивизион После")
        binding = topic_cache.get_by_topic(GROUP_CHAT_ID, REPORTS_THREAD_ID)
        self.assertEqual(binding["division_name"], "Дивизион После")

    async def test_rename_updates_reverse_division_lookup(self):
        await self._rename("Дивизион После")
        entry = topic_cache.get_by_division(self.div, "reports")
        self.assertEqual(entry["division_name"], "Дивизион После")

    async def test_rename_keeps_binding_routing_intact(self):
        """Перегрузка не должна потерять привязку: роутинг топик → дивизион жив."""
        await self._rename("Дивизион После")
        self.assertEqual(topic_cache.get_by_topic(GROUP_CHAT_ID, REPORTS_THREAD_ID)["division_id"], self.div)

    # --- B. имя в БД и в кэше совпадает -----------------------------------

    async def test_database_and_cache_agree_after_rename(self):
        await self._rename("Дивизион После")
        self.assertEqual(database.get_division(self.div)["name"], "Дивизион После")
        self.assertEqual(
            topic_cache.get_by_topic(GROUP_CHAT_ID, REPORTS_THREAD_ID)["division_name"],
            database.get_division(self.div)["name"],
        )

    async def test_short_name_is_rejected_and_cache_untouched(self):
        """Короткое имя не проходит и не имеет права трогать кэш."""
        update = self._text_update(self.super_admin, "А")
        code = await admin_div_rename_receive(update, self._context(self.div))
        self.assertEqual(code, ADMIN_EXPECT_DIV_RENAME)
        self.assertEqual(
            topic_cache.get_by_topic(GROUP_CHAT_ID, REPORTS_THREAD_ID)["division_name"],
            f"Дивизион До {self.uid}",
        )

    # --- C. non-vacuity: без reload кэш остаётся старым -------------------

    async def test_writing_the_name_without_reload_leaves_the_cache_stale(self):
        """Тот самый дефект: та же запись в БД без reload_cache() → старое имя."""
        database.update_division(self.div, name="Дивизион В обход хендлера")
        self.assertEqual(
            topic_cache.get_by_topic(GROUP_CHAT_ID, REPORTS_THREAD_ID)["division_name"],
            f"Дивизион До {self.uid}",
            "Если кэш сам подхватил имя из БД, защита не доказана — тест пустой",
        )
        topic_cache.reload_cache()
        self.assertEqual(
            topic_cache.get_by_topic(GROUP_CHAT_ID, REPORTS_THREAD_ID)["division_name"],
            "Дивизион В обход хендлера",
        )


if __name__ == "__main__":
    unittest.main()
