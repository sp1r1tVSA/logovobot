"""safe_send_notification must skip pre-registered coaches (negative temp ids)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from handlers.cabinet import safe_send_notification


@pytest.mark.parametrize("chat_id", [-57, 0, None])
def test_skips_pre_registered_ids(chat_id):
    bot = AsyncMock()
    assert asyncio.run(safe_send_notification(bot, chat_id, "hi")) is False
    bot.send_message.assert_not_called()


def test_sends_to_real_user():
    bot = AsyncMock()
    assert asyncio.run(safe_send_notification(bot, 12345, "hi")) is True
    bot.send_message.assert_awaited_once()


def test_bad_markup_is_resent_as_plain_text():
    """Битая разметка (например, «_» в нике) не должна терять уведомление."""
    from telegram.error import BadRequest

    bot = AsyncMock()
    bot.send_message.side_effect = [
        BadRequest("Can't parse entities: can't find end of the entity starting at byte offset 157"),
        None,
    ]
    assert asyncio.run(safe_send_notification(bot, 12345, "<b>bad", parse_mode="HTML")) is True
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[1].kwargs["parse_mode"] is None


@pytest.mark.parametrize("error", [
    "BadRequest('Chat not found')",
    "Forbidden('bot was blocked by the user')",
    "Forbidden('Forbidden: user is deactivated')",
    "TelegramError('boom')",
])
def test_telegram_errors_are_swallowed(error):
    """Раньше любой не-Forbidden сбой падал с AttributeError на telegram.error.UserDeactivated."""
    import telegram.error as tg_err

    bot = AsyncMock()
    bot.send_message.side_effect = eval(error, vars(tg_err))
    assert asyncio.run(safe_send_notification(bot, 12345, "hi")) is False
    bot.send_message.assert_awaited_once()
