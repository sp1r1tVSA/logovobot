"""Unit and integration tests for user chat activity and Neat Tree profile card.

Tests migration 034, message/media/toxic tracking, profile data assembly,
toxic detection regex, and profile card formatting.
"""

import pytest
import unittest
from unittest.mock import AsyncMock, MagicMock

import database
from services.chat_activity import is_toxic_message, build_profile_card
from handlers.text_commands import cmd_user_profile


class TestUserChatProfile(unittest.TestCase):
    def setUp(self):
        database.init_db()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_chat_activity")
            cursor.execute("DELETE FROM users WHERE telegram_id IN (998801, 998802, 998803)")
            cursor.execute("DELETE FROM season_snapshots WHERE user_id IN (998801, 998802)")
            cursor.execute("DELETE FROM team_ratings WHERE team_name = 'TestPorto'")

    def test_migration_034_applied(self):
        """Verify migration 034 is recorded in schema_migrations."""
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (database.MIGRATION_034_USER_CHAT_ACTIVITY,))
            self.assertIsNotNone(cursor.fetchone())

    def test_record_user_chat_activity_counters(self):
        """Verify chat activity increments each message/media type and toxic flag."""
        user_id = 998801

        # Text message (clean)
        database.record_user_chat_activity(user_id, "text", is_toxic=False)
        # Sticker
        database.record_user_chat_activity(user_id, "sticker", is_toxic=False)
        # Voice note
        database.record_user_chat_activity(user_id, "voice", is_toxic=False)
        # Photo
        database.record_user_chat_activity(user_id, "photo", is_toxic=False)
        # Another text (toxic)
        database.record_user_chat_activity(user_id, "text", is_toxic=True)

        activity = database.get_user_chat_activity(user_id)
        self.assertEqual(activity["messages_count"], 2)
        self.assertEqual(activity["stickers_count"], 1)
        self.assertEqual(activity["voice_count"], 1)
        self.assertEqual(activity["photos_count"], 1)
        self.assertEqual(activity["toxic_count"], 1)
        self.assertIsNotNone(activity["last_message_at"])

    def test_increment_user_toxic_count(self):
        """Verify admin mute / manual toxic increment works."""
        user_id = 998802
        database.increment_user_toxic_count(user_id, 2)
        activity = database.get_user_chat_activity(user_id)
        self.assertEqual(activity["toxic_count"], 2)

        database.increment_user_toxic_count(user_id, 1)
        activity = database.get_user_chat_activity(user_id)
        self.assertEqual(activity["toxic_count"], 3)

    def test_toxic_message_detection(self):
        """Verify toxic regex correctly flags obscenities and ignores normal chat."""
        # Clean phrases
        self.assertFalse(is_toxic_message("Привет всем, во сколько тур?"))
        self.assertFalse(is_toxic_message("Отличный матч, спасибо за игру!"))
        self.assertFalse(is_toxic_message("Кто готов сыграть товарку?"))
        self.assertFalse(is_toxic_message(None))
        self.assertFalse(is_toxic_message(""))

        # Obscene / toxic phrases
        self.assertTrue(is_toxic_message("Ты че нахуй делаешь"))
        self.assertTrue(is_toxic_message("ну ты и пидор"))
        self.assertTrue(is_toxic_message("это просто пиздец"))
        self.assertTrue(is_toxic_message("сука"))
        self.assertTrue(is_toxic_message("похуй вообще"))

    def test_build_profile_card_registered_player(self):
        """Verify profile card renders neatly for a registered player with team and stats."""
        user_id = 998801
        database.register_user(user_id, "porto_fan", role="player", team_name="TestPorto")
        database.update_team_elo("TestPorto", division_id=1, season_id=1, new_elo=1580.0)

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO season_snapshots (
                    season_id, division_id, user_id, final_rank, final_rating, season_points,
                    wins, losses, settled_bets, win_rate, roi, promotion_status
                ) VALUES (1, 1, ?, 1, 1580.0, 30.0, 10, 0, 10, 100.0, 25.0, 'STAY')
            """, (user_id,))

        database.record_user_chat_activity(user_id, "text", is_toxic=False)
        database.record_user_chat_activity(user_id, "sticker", is_toxic=False)

        card_text, markup = build_profile_card(user_id, target_name="Ислам", target_username="porto_fan")

        # Check Tree elements
        self.assertIn("👤 <b>Ислам</b>", card_text)
        self.assertIn("├ 💬 @porto_fan", card_text)
        self.assertIn("├ 👑 Должность: Участник", card_text)
        self.assertIn("╰ ⚠️ Варны: 0/3 😇", card_text)
        self.assertIn("🌐 <b>КЛУБ И ДИВИЗИОН:</b>", card_text)
        self.assertIn("TestPorto", card_text)
        self.assertIn("⚔️ <b>ТУРНИРНЫЙ РЕЙТИНГ:</b>", card_text)
        self.assertIn("🎖 ELO: 1580 [ELITE]", card_text)
        self.assertIn("🥇 1", card_text)
        self.assertIn("💬 <b>АКТИВНОСТЬ:</b>", card_text)
        self.assertIn("✉️ СМС: 1", card_text)
        self.assertIn("🎭 Стик: 1", card_text)
        self.assertIsNotNone(markup)

    def test_build_profile_card_guest(self):
        """Verify profile card renders gracefully for unregistered guest."""
        user_id = 998803
        card_text, markup = build_profile_card(user_id, target_name="Гость", target_username=None)

        self.assertIn("👤 <b>Гость</b>", card_text)
        self.assertIn("👑 Должность: Гость лиги", card_text)
        self.assertIn("Свободный игрок", card_text)
        self.assertIn("💬 <b>АКТИВНОСТЬ:</b>", card_text)
        self.assertIn("✉️ СМС: 0 | 🤬 Токс: 0", card_text)

    def test_cmd_user_profile_handler(self):
        """Verify cmd_user_profile handles execution and replies."""
        import asyncio
        update = MagicMock()
        context = MagicMock()

        msg = AsyncMock()
        msg.reply_to_message = None
        msg.text = "профиль"
        user = MagicMock()
        user.id = 998801
        user.first_name = "Ислам"
        user.username = "islam_dev"

        update.effective_message = msg
        update.effective_user = user

        asyncio.run(cmd_user_profile(update, context))

        msg.reply_text.assert_awaited_once()
        args, kwargs = msg.reply_text.call_args
        self_text = args[0]
        self.assertIn("👤 <b>Ислам</b>", self_text)
        self.assertIn("💬 <b>АКТИВНОСТЬ:</b>", self_text)
        self.assertEqual(kwargs.get("parse_mode"), "HTML")

