"""
tests/test_betting_line_notifications.py

Comprehensive test suite for Logovo.bet betting line notifications:
- Opening line notification in PM with inline button "🎰 Сделать ставку" (Telegram Mini App WebAppInfo)
- Closing line notification in PM
- No posting to the division analytics/reports topic (PM only)
- Rate limit handling, cooldown throttling, and user filtering (positive telegram_id only)
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import InlineKeyboardMarkup, WebAppInfo

import config
import database
from services.betting_notifications import (
    _notif_cooldown,
    build_line_closed_text,
    build_line_opened_text,
    get_betting_webapp_markup,
    notify_division_betting_line_closed,
    notify_division_betting_line_opened,
)


class TestBettingLineNotifications(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        database.init_db()
        _notif_cooldown.clear()

    def test_01_markup_private_has_miniapp_button(self):
        """В ЛС инлайн-кнопка '🎰 Сделать ставку' открывает Telegram Mini App (WebAppInfo)."""
        markup = get_betting_webapp_markup(is_private=True)
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(len(markup.inline_keyboard), 1)
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "🎰 Сделать ставку")
        self.assertIsNotNone(btn.web_app)
        self.assertIsInstance(btn.web_app, WebAppInfo)
        self.assertTrue(btn.web_app.url.startswith("http"))

    def test_02_markup_group_uses_deeplink_not_webapp(self):
        """В группе/топике кнопка использует URL ссылку на бота (Telegram запрещает WebApp кнопки в группах)."""
        markup = get_betting_webapp_markup(is_private=False, bot_username="logovobot")
        btn = markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "🎰 Сделать ставку")
        self.assertIsNone(btn.web_app)
        self.assertIn("https://t.me/logovobot?start=bet", btn.url)

    def test_03_text_builders(self):
        """Проверка текстов открытия и закрытия линии."""
        matches = [
            {"player1_team": "Реал Мадрид", "player2_team": "Барселона"},
            {"player1_team": "Арсенал", "player2_team": "Челси"},
        ]
        opened = build_line_opened_text("Дивизион 1", 5, matches)
        self.assertIn("Тур 5", opened)
        self.assertIn("Дивизион 1", opened)
        self.assertIn("Реал Мадрид", opened)
        self.assertIn("Барселона", opened)
        self.assertIn("Logovo.bet", opened)

        closed = build_line_closed_text("Дивизион 1", 5)
        self.assertIn("Линия ставок закрыта", closed)
        self.assertIn("Тур 5", closed)

    async def test_04_notify_line_opened_delivers_to_positive_ids_with_miniapp_button(self):
        """Открытие линии отправляет в ЛС игрокам дивизиона сообщение с WebApp кнопкой."""
        mock_context = MagicMock()
        mock_bot = AsyncMock()
        mock_context.bot = mock_bot

        # Создаём тестовых пользователей: 2 реальных игрока и 1 плейсхолдер с отрицательным ID
        division_id = 99
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO divisions (id, tournament_id, name, code, season_id) VALUES (?, 1, 'Дивизион Тест', 'DIV_T', 1)",
                (division_id,)
            )
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id) VALUES (?, ?, ?, ?)",
                (10001, "player_one", "Arsenal", division_id)
            )
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id) VALUES (?, ?, ?, ?)",
                (10002, "player_two", "Chelsea", division_id)
            )
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id) VALUES (?, ?, ?, ?)",
                (-555, "placeholder", "Draft", division_id)
            )

        with patch("services.betting_notifications.safe_send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True

            sent_count = await notify_division_betting_line_opened(mock_context, division_id, 1)

            self.assertEqual(sent_count, 2)
            self.assertEqual(mock_send.call_count, 2)

            # Проверяем, что оба вызова были реальным telegram_id
            call_ids = [call.args[1] for call in mock_send.call_args_list]
            self.assertIn(10001, call_ids)
            self.assertIn(10002, call_ids)
            self.assertNotIn(-555, call_ids)

            # Проверяем, что в reply_markup передана инлайн-кнопка с WebAppInfo
            for call in mock_send.call_args_list:
                markup = call.kwargs.get("reply_markup")
                self.assertIsNotNone(markup)
                self.assertEqual(markup.inline_keyboard[0][0].text, "🎰 Сделать ставку")
                self.assertIsNotNone(markup.inline_keyboard[0][0].web_app)

            # В топик группы объявление не уходит — только ЛС.
            mock_bot.send_message.assert_not_called()

    async def test_05_notify_line_closed_delivers_to_players(self):
        """Закрытие линии отправляет уведомления игрокам."""
        mock_context = MagicMock()
        mock_bot = AsyncMock()
        mock_context.bot = mock_bot

        division_id = 98
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO divisions (id, tournament_id, name, code, season_id) VALUES (?, 1, 'Дивизион 98', 'DIV_98', 1)",
                (division_id,)
            )
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id) VALUES (?, ?, ?, ?)",
                (20001, "player_close", "Milan", division_id)
            )

        with patch("services.betting_notifications.safe_send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True

            sent = await notify_division_betting_line_closed(mock_context, division_id, 3, was_open=True)

            self.assertEqual(sent, 1)
            mock_send.assert_called_once()
            self.assertEqual(mock_send.call_args.args[1], 20001)
            self.assertIn("Линия ставок закрыта", mock_send.call_args.args[2])
            mock_bot.send_message.assert_not_called()

    async def test_06_was_open_false_skips_closed_notification(self):
        """Если линия не была открыта, уведомление о закрытии не отправляется."""
        mock_context = MagicMock()
        with patch("services.betting_notifications.safe_send_notification", new_callable=AsyncMock) as mock_send:
            sent = await notify_division_betting_line_closed(mock_context, 1, 10, was_open=False)
            self.assertEqual(sent, 0)
            mock_send.assert_not_called()

    async def test_07_cooldown_throttles_duplicate_notifications(self):
        """Повторный вызов уведомления в течение 30 секунд глушится кулдауном."""
        mock_context = MagicMock()
        with patch("services.betting_notifications.safe_send_notification", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True

            # Первый вызов проходит
            c1 = await notify_division_betting_line_opened(mock_context, 1, 99)

            # Немедленный второй вызов отсекается кулдауном
            c2 = await notify_division_betting_line_opened(mock_context, 1, 99)
            self.assertEqual(c2, 0)


if __name__ == "__main__":
    unittest.main()
