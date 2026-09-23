"""Меню команд: общее для всех, личное (с /overview) для админов."""

import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import BadRequest

import config
import database
from handlers import bot_menu


def _bot():
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.delete_my_commands = AsyncMock()
    return bot


class TestCommandLists(unittest.TestCase):
    def test_overview_only_in_admin_menu(self):
        names = [c.command for c in bot_menu.ADMIN_COMMANDS]
        self.assertIn("overview", names)
        self.assertNotIn("overview", [c.command for c in bot_menu.DEFAULT_COMMANDS])
        self.assertEqual(names[:len(bot_menu.DEFAULT_COMMANDS)], [c.command for c in bot_menu.DEFAULT_COMMANDS])

    def test_command_names_are_valid_for_telegram(self):
        for c in bot_menu.ADMIN_COMMANDS:
            self.assertRegex(c.command, re.compile(r"^[a-z0-9_]{1,32}$"))
            self.assertTrue(1 <= len(c.description) <= 256)


class TestRefreshAdminMenu(unittest.IsolatedAsyncioTestCase):
    async def test_admin_gets_personal_menu(self):
        bot = _bot()
        with patch.object(bot_menu, "can_view_overview", return_value=True):
            self.assertTrue(await bot_menu.refresh_admin_menu(bot, 42))
        commands = bot.set_my_commands.call_args.args[0]
        scope = bot.set_my_commands.call_args.kwargs["scope"]
        self.assertEqual(commands, bot_menu.ADMIN_COMMANDS)
        self.assertIsInstance(scope, BotCommandScopeChat)
        self.assertEqual(scope.chat_id, 42)
        bot.delete_my_commands.assert_not_awaited()

    async def test_revoked_admin_falls_back_to_default_menu(self):
        bot = _bot()
        with patch.object(bot_menu, "can_view_overview", return_value=False):
            self.assertFalse(await bot_menu.refresh_admin_menu(bot, 42))
        bot.set_my_commands.assert_not_awaited()
        self.assertEqual(bot.delete_my_commands.call_args.kwargs["scope"].chat_id, 42)

    async def test_chat_not_found_is_swallowed(self):
        bot = _bot()
        bot.set_my_commands.side_effect = BadRequest("Chat not found")
        with patch.object(bot_menu, "can_view_overview", return_value=True):
            self.assertFalse(await bot_menu.refresh_admin_menu(bot, 42))

    async def test_non_positive_ids_are_skipped(self):
        bot = _bot()
        self.assertFalse(await bot_menu.refresh_admin_menu(bot, 0))
        self.assertFalse(await bot_menu.refresh_admin_menu(bot, -100))
        bot.set_my_commands.assert_not_awaited()
        bot.delete_my_commands.assert_not_awaited()

    async def test_default_menu_uses_default_scope(self):
        bot = _bot()
        await bot_menu.set_default_menu(bot)
        self.assertEqual(bot.set_my_commands.call_args.args[0], bot_menu.DEFAULT_COMMANDS)
        self.assertIsInstance(bot.set_my_commands.call_args.kwargs["scope"], BotCommandScopeDefault)


class TestSyncAdminMenus(unittest.IsolatedAsyncioTestCase):
    async def test_covers_env_admins_and_db_admins(self):
        bot = _bot()
        with patch.object(bot_menu.database, "get_admin_candidate_ids", return_value=[7002, 7003]), \
             patch.object(bot_menu.config, "ADMIN_IDS", [7001]), \
             patch.object(bot_menu, "can_view_overview", side_effect=lambda uid: uid != 7003):
            applied = await bot_menu.sync_admin_menus(bot)
        self.assertEqual(applied, 2)
        granted = sorted(c.kwargs["scope"].chat_id for c in bot.set_my_commands.call_args_list)
        self.assertEqual(granted, [7001, 7002])
        # Бывший админ, у которого прав больше нет, получает общее меню обратно.
        self.assertEqual([c.kwargs["scope"].chat_id for c in bot.delete_my_commands.call_args_list], [7003])


class TestAdminCandidateIds(unittest.TestCase):
    def test_roles_and_division_admins(self):
        with database.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO divisions (id, name, code, tournament_id) VALUES (1, 'Дивизион 1', 'DIV_1', 1)")
            conn.execute("INSERT INTO users (telegram_id, username, role) VALUES (8101, 'm_admin', 'admin')")
            conn.execute("INSERT INTO users (telegram_id, username, role) VALUES (8102, 'm_player', 'player')")
            conn.execute("INSERT INTO users (telegram_id, username, role) VALUES (8103, 'm_div', 'player')")
        database.add_division_admin(1, 8103)
        ids = set(database.get_admin_candidate_ids())
        self.assertIn(8101, ids)
        self.assertIn(8103, ids)
        self.assertNotIn(8102, ids)


if __name__ == "__main__":
    unittest.main()
