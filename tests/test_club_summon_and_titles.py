import os
import sys
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import database
from handlers.text_commands import handle_temshik_command, cmd_summon_club, cmd_sync_club_titles
from services.chat_titles import assign_club_title, sync_division_club_titles, MAX_CUSTOM_TITLE_LEN


def _make_msg_update(user_id: int, text: str, is_group: bool = True, username: str = "caller_user"):
    update = MagicMock()
    msg = MagicMock()
    msg.text = text
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    msg.chat = MagicMock()
    msg.chat.id = -100999888777
    msg.chat.type = "supergroup" if is_group else "private"
    msg.from_user = MagicMock(id=user_id, username=username, first_name="Caller")

    update.message = msg
    update.effective_message = msg
    update.effective_user = msg.from_user
    update.effective_chat = msg.chat
    return update, msg


class TestClubSummonAndTitles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.admin_id = 77112233
        self.player_id = 77112244
        self.player_username = f"koln_coach_{self.uid}"

        self._orig_admins = list(config.ADMIN_IDS)
        config.ADMIN_IDS = [self.admin_id]
        self.addCleanup(setattr, config, "ADMIN_IDS", self._orig_admins)

        self.div_id = database.create_division(name=f"DivSummon {self.uid}", code=f"DS_{self.uid}")
        database.register_user(self.player_id, self.player_username, team_name="Кёльн")
        with database.transaction() as conn:
            conn.execute("UPDATE users SET division_id = ? WHERE telegram_id = ?", (self.div_id, self.player_id))

    def test_find_coach_by_club(self):
        # 1. Exact match
        coach = database.find_coach_by_club("Кёльн")
        self.assertIsNotNone(coach)
        self.assertEqual(coach["telegram_id"], self.player_id)
        self.assertEqual(coach["username"], self.player_username)

        # 2. Alias match (e.g. Кельн without ё)
        coach_alias = database.find_coach_by_club("Кельн")
        self.assertIsNotNone(coach_alias)
        self.assertEqual(coach_alias["telegram_id"], self.player_id)

        # 3. Scoped by division
        coach_div = database.find_coach_by_club("Кёльн", division_id=self.div_id)
        self.assertIsNotNone(coach_div)
        self.assertEqual(coach_div["division_id"], self.div_id)

        # 4. Non-existent club
        coach_none = database.find_coach_by_club("НесуществующийКлуб123")
        self.assertIsNone(coach_none)

    def test_get_coaches_for_division(self):
        coaches = database.get_coaches_for_division(self.div_id)
        self.assertTrue(any(c["telegram_id"] == self.player_id for c in coaches))

    async def test_summon_command_tags_coach(self):
        update, msg = _make_msg_update(self.admin_id, "Темшик позвать Кельн")
        context = MagicMock()

        handled = await handle_temshik_command(update, context)
        self.assertTrue(handled)
        msg.reply_text.assert_awaited()

        reply_content = msg.reply_text.await_args[0][0]
        self.assertIn(f"@{self.player_username}", reply_content)
        self.assertIn("Кёльн", reply_content)
        self.assertIn("вызывает", reply_content)

    async def test_summon_alias_pozoivi(self):
        update, msg = _make_msg_update(self.admin_id, "Темшик позови Кёльн")
        context = MagicMock()

        handled = await handle_temshik_command(update, context)
        self.assertTrue(handled)
        reply_content = msg.reply_text.await_args[0][0]
        self.assertIn(f"@{self.player_username}", reply_content)

    async def test_slash_command_summon(self):
        update, msg = _make_msg_update(self.admin_id, "/summon Кельн")
        context = MagicMock()
        context.args = ["Кельн"]

        await cmd_summon_club(update, context)
        msg.reply_text.assert_awaited()
        reply_content = msg.reply_text.await_args[0][0]
        self.assertIn(f"@{self.player_username}", reply_content)

    async def test_assign_club_title_promotion_and_set_title(self):
        bot = MagicMock()
        # User is regular member
        member = MagicMock(status="member")
        bot.get_chat_member = AsyncMock(return_value=member)
        bot.promote_chat_member = AsyncMock()
        bot.set_chat_administrator_custom_title = AsyncMock()

        chat_id = -100123456789
        long_club_name = "Боруссия Мёнхенгладбах"
        ok, msg = await assign_club_title(bot, chat_id, self.player_id, long_club_name)

        self.assertTrue(ok)
        self.assertIn("Установлена плашка", msg)

        # Verified promotion was called with safe minimal permissions
        bot.promote_chat_member.assert_awaited_once()
        promote_kwargs = bot.promote_chat_member.await_args.kwargs
        self.assertTrue(promote_kwargs["can_invite_users"])
        self.assertFalse(promote_kwargs["can_delete_messages"])
        self.assertFalse(promote_kwargs["can_restrict_members"])

        # Verified title was truncated to 16 characters
        bot.set_chat_administrator_custom_title.assert_awaited_once()
        title_arg = bot.set_chat_administrator_custom_title.await_args.kwargs["custom_title"]
        self.assertLessEqual(len(title_arg), MAX_CUSTOM_TITLE_LEN)
        self.assertEqual(title_arg, long_club_name[:16])

    async def test_assign_club_title_creator_skipped(self):
        bot = MagicMock()
        member = MagicMock(status="creator")
        bot.get_chat_member = AsyncMock(return_value=member)

        ok, msg = await assign_club_title(bot, -1001234, self.admin_id, "Кёльн")
        self.assertFalse(ok)
        self.assertIn("Владелец", msg)

    async def test_sync_division_club_titles(self):
        bot = MagicMock()
        member = MagicMock(status="member")
        bot.get_chat_member = AsyncMock(return_value=member)
        bot.promote_chat_member = AsyncMock()
        bot.set_chat_administrator_custom_title = AsyncMock()

        stats = await sync_division_club_titles(bot, -1001234, self.div_id)
        self.assertGreaterEqual(stats["total"], 1)
        self.assertGreaterEqual(stats["success"], 1)


if __name__ == "__main__":
    unittest.main()
