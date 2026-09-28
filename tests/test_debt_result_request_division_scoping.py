"""
tests/test_debt_result_request_division_scoping.py

Tests that requests for permission to enter debt match results are routed to the division admin
(not global admins), and that division admins can approve them while admins of other divisions cannot.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database
import handlers.cabinet as cabinet
from time_utils import SQL_NOW


class TestDebtResultRequestDivisionScoping(unittest.IsolatedAsyncioTestCase):
    P1_ID = 8801001
    P2_ID = 8801002
    GLOBAL_ADMIN_ID = 8809999
    DIV1_ADMIN_ID = 8802001
    DIV2_ADMIN_ID = 8802002

    def setUp(self):
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.tf.name
        self.tf.close()

        self.orig_config_path = config.DB_PATH
        self.orig_database_path = database.DB_PATH
        self.orig_admin_ids = config.ADMIN_IDS

        config.DB_PATH = self.temp_db_path
        database.DB_PATH = self.temp_db_path
        config.ADMIN_IDS = [self.GLOBAL_ADMIN_ID]

        database.init_db()

        self.div1_id = database.create_division(name="Первый дивизион", code="DIV1")
        self.div2_id = database.create_division(name="Второй дивизион", code="DIV2")

        # Create users first to satisfy foreign key constraints
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"INSERT INTO users (telegram_id, username, team_name, division_id, registered_at) VALUES (?, ?, ?, ?, {SQL_NOW})",
                (self.GLOBAL_ADMIN_ID, "global_admin", None, None),
            )
            cursor.execute(
                f"INSERT INTO users (telegram_id, username, team_name, division_id, registered_at) VALUES (?, ?, ?, ?, {SQL_NOW})",
                (self.DIV1_ADMIN_ID, "div1_admin", None, self.div1_id),
            )
            cursor.execute(
                f"INSERT INTO users (telegram_id, username, team_name, division_id, registered_at) VALUES (?, ?, ?, ?, {SQL_NOW})",
                (self.DIV2_ADMIN_ID, "div2_admin", None, self.div2_id),
            )
            cursor.execute(
                f"INSERT INTO users (telegram_id, username, team_name, division_id, registered_at) VALUES (?, ?, ?, ?, {SQL_NOW})",
                (self.P1_ID, "player1", "Chelsea", self.div1_id),
            )
            cursor.execute(
                f"INSERT INTO users (telegram_id, username, team_name, division_id, registered_at) VALUES (?, ?, ?, ?, {SQL_NOW})",
                (self.P2_ID, "player2", "Arsenal", self.div1_id),
            )
            cursor.execute(
                """
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status, is_extended)
                VALUES (7701, 1, ?, ?, ?, 'Chelsea', 'Arsenal', 'pending', 0)
                """,
                (self.div1_id, self.P1_ID, self.P2_ID),
            )

        database.add_division_admin(self.div1_id, self.DIV1_ADMIN_ID)
        database.add_division_admin(self.div2_id, self.DIV2_ADMIN_ID)

    def tearDown(self):
        config.DB_PATH = self.orig_config_path
        database.DB_PATH = self.orig_database_path
        config.ADMIN_IDS = self.orig_admin_ids
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except OSError:
                pass

    async def test_request_routes_to_division_admin_not_global_admin(self):
        """When player requests debt result permission, it goes to the division admin, not global admin."""
        update = MagicMock()
        query = MagicMock()
        query.data = "cb_request_admin_result_7701"
        query.from_user.id = self.P1_ID
        query.from_user.username = "player1"
        query.from_user.full_name = "Player One"
        query.message.reply_markup.inline_keyboard = []
        query.answer = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await cabinet.cb_request_admin_result(update, context)

        # Division admin should receive the message
        sent_chats = [call.kwargs.get("chat_id") for call in context.bot.send_message.call_args_list]
        self.assertIn(self.DIV1_ADMIN_ID, sent_chats)
        # Global admin must NOT receive it when division admin exists
        self.assertNotIn(self.GLOBAL_ADMIN_ID, sent_chats)
        # Check text includes division info
        sent_text = context.bot.send_message.call_args_list[0].kwargs.get("text", "")
        self.assertIn("Первый дивизион", sent_text)

    async def test_request_falls_back_to_global_admin_if_no_division_admin(self):
        """If division has no division admin, request falls back to global admins."""
        div_empty_id = database.create_division(name="Дивизион без админа", code="DIV_EMPTY")
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status, is_extended)
                VALUES (7702, 1, ?, ?, ?, 'Chelsea', 'Arsenal', 'pending', 0)
                """,
                (div_empty_id, self.P1_ID, self.P2_ID),
            )

        update = MagicMock()
        query = MagicMock()
        query.data = "cb_request_admin_result_7702"
        query.from_user.id = self.P1_ID
        query.from_user.username = "player1"
        query.from_user.full_name = "Player One"
        query.message.reply_markup.inline_keyboard = []
        query.answer = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await cabinet.cb_request_admin_result(update, context)

        sent_chats = [call.kwargs.get("chat_id") for call in context.bot.send_message.call_args_list]
        self.assertIn(self.GLOBAL_ADMIN_ID, sent_chats)

    async def test_approve_allows_division_admin(self):
        """Division admin of match's division can approve debt result entry."""
        update = MagicMock()
        query = MagicMock()
        query.data = f"cb_admin_approve_7701_{self.P1_ID}"
        query.from_user.id = self.DIV1_ADMIN_ID
        query.from_user.username = "div1_admin"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await cabinet.cb_admin_approve_result(update, context)

        # Match must now be unlocked (is_extended == 1)
        m = database.get_match(7701)
        self.assertEqual(m["is_extended"], 1)

        # Player must be notified
        player_notification = context.bot.send_message.call_args.kwargs
        self.assertEqual(player_notification.get("chat_id"), self.P1_ID)
        self.assertIn("Разрешение получено", player_notification.get("text", ""))

    async def test_approve_denies_wrong_division_admin(self):
        """Admin of another division is not allowed to approve this division's match."""
        update = MagicMock()
        query = MagicMock()
        query.data = f"cb_admin_approve_7701_{self.P1_ID}"
        # DIV2_ADMIN_ID is admin of div2, not div1
        query.from_user.id = self.DIV2_ADMIN_ID
        query.from_user.username = "div2_admin"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await cabinet.cb_admin_approve_result(update, context)

        # Must not be unlocked
        m = database.get_match(7701)
        self.assertEqual(m["is_extended"], 0)

        # Denied alert must be shown
        alert_calls = [c.kwargs.get("text") for c in query.answer.call_args_list if c.kwargs.get("text")]
        if not alert_calls:
            alert_calls = [c.args[0] for c in query.answer.call_args_list if c.args]
        self.assertTrue(any("Только администратор этого дивизиона" in text for text in alert_calls))
