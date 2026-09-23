"""
tests/test_divisions_menu.py

Tests for the Divisions navigation menu:
- constants.CB_MENU_DIVISIONS
- database.get_active_season() and database.get_active_divisions()
- handlers.base.get_main_inline_keyboard()
- handlers.base.show_divisions_list()
- handlers.base.show_division_menu()
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import sqlite3
import tempfile
import shutil
import os

import database
from constants import CB_MENU_DIVISIONS, CB_MAIN_MENU
from handlers.base import (
    get_main_inline_keyboard,
    show_divisions_list,
    show_division_menu
)


class TestDivisionsMenu(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_divisions.db")
        
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = self.db_path
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_constants_and_main_keyboard(self):
        """Verify CB_MENU_DIVISIONS exists and get_main_inline_keyboard uses it."""
        self.assertEqual(CB_MENU_DIVISIONS, "menu_divisions")

        keyboard = get_main_inline_keyboard(telegram_id=12345)
        button_found = False
        for row in keyboard.inline_keyboard:
            for btn in row:
                if btn.callback_data == CB_MENU_DIVISIONS:
                    self.assertEqual(btn.text, "🏆 Дивизионы")
                    button_found = True
        self.assertTrue(button_found, "Button '🏆 Дивизионы' with CB_MENU_DIVISIONS must be present in main menu.")

    def test_02_database_get_active_season_and_divisions(self):
        """Verify get_active_season() and get_active_divisions()."""
        # In newly initialized database, season 1 exists
        season = database.get_active_season()
        self.assertIsNotNone(season)
        self.assertEqual(int(season), 1)
        self.assertEqual(season["id"], 1)
        self.assertEqual(season.get("status"), "active")

        # Get active divisions for season 1
        divisions = database.get_active_divisions(season_id=int(season))
        self.assertEqual(len(divisions), 5)
        codes = [d["code"] for d in divisions]
        self.assertIn("DIV_1", codes)
        self.assertIn("DIV_5", codes)
        self.assertEqual(divisions[0]["name"], "Дивизион 1")

    async def test_03_show_divisions_list_with_active_season(self):
        """Verify show_divisions_list renders division buttons when active season exists."""
        update = MagicMock()
        query = MagicMock()
        query.data = "menu_divisions"
        query.message = MagicMock()
        query.message.photo = None
        query.message.chat_id = 12345
        query.message.is_topic_message = False
        query.edit_message_text = AsyncMock()
        query.answer = AsyncMock()
        update.callback_query = query
        update.message = None

        context = MagicMock()

        await show_divisions_list(update, context)

        query.edit_message_text.assert_called_once()
        args, kwargs = query.edit_message_text.call_args
        text = args[0]
        self.assertIn("Дивизионы", text)
        reply_markup = kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)

        # Check buttons: 5 divisions + 1 back button
        buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
        div_callbacks = [b.callback_data for b in buttons if b.callback_data.startswith("division_view:")]
        self.assertEqual(len(div_callbacks), 5)
        self.assertEqual(div_callbacks[0], "division_view:1:1")
        self.assertEqual(div_callbacks[4], "division_view:1:5")

        # Back button
        self.assertEqual(buttons[-1].callback_data, CB_MAIN_MENU)

    async def test_04_show_divisions_list_no_active_season(self):
        """Verify show_divisions_list displays empty message when no active season."""
        # Purge seasons
        with database.transaction() as conn:
            conn.execute("DELETE FROM seasons")

        update = MagicMock()
        query = MagicMock()
        query.data = "menu_divisions"
        query.message = MagicMock()
        query.message.photo = None
        query.message.chat_id = 12345
        query.message.is_topic_message = False
        query.edit_message_text = AsyncMock()
        query.answer = AsyncMock()
        update.callback_query = query
        update.message = None

        context = MagicMock()

        await show_divisions_list(update, context)

        query.edit_message_text.assert_called_once()
        args, kwargs = query.edit_message_text.call_args
        text = args[0]
        self.assertIn("Сейчас нет активного сезона", text)
        reply_markup = kwargs.get("reply_markup")
        buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].callback_data, CB_MAIN_MENU)

    async def test_05_show_division_menu(self):
        """Verify show_division_menu displays Table, Scorers, Assists, TOTW, and Back buttons."""
        update = MagicMock()
        query = MagicMock()
        query.data = "division_view:1:2"
        query.message = MagicMock()
        query.message.photo = None
        query.message.chat_id = 12345
        query.message.is_topic_message = False
        query.edit_message_text = AsyncMock()
        query.answer = AsyncMock()
        update.callback_query = query
        update.message = None

        # Simulate regex match in python-telegram-bot
        match_mock = MagicMock()
        match_mock.group.side_effect = lambda idx: "1" if idx == 1 else "2"
        context = MagicMock()
        context.matches = [match_mock]

        await show_division_menu(update, context)

        query.edit_message_text.assert_called_once()
        args, kwargs = query.edit_message_text.call_args
        text = args[0]
        self.assertIn("Дивизион 2", text)
        reply_markup = kwargs.get("reply_markup")

        expected_callbacks = [
            "division_table:1:2",
            "division_scorers:1:2",
            "division_assists:1:2",
            "division_totw:1:2",
            CB_MENU_DIVISIONS
        ]
        actual_callbacks = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
        self.assertEqual(actual_callbacks, expected_callbacks)

    async def test_06_photo_message_fallback(self):
        """Verify that when the previous message was a photo, it is deleted and a new message is sent."""
        update = MagicMock()
        query = MagicMock()
        query.data = "division_view:1:1"
        query.message = MagicMock()
        query.message.photo = ["mock_photo"]
        query.message.chat_id = 99999
        query.message.is_topic_message = False
        query.message.delete = AsyncMock()
        query.answer = AsyncMock()
        update.callback_query = query
        update.message = None

        context = MagicMock()
        context.matches = []
        context.bot.send_message = AsyncMock()

        await show_division_menu(update, context)

        query.message.delete.assert_called_once()
        context.bot.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
