"""
Экраны, куда попадают ники, названия клубов и прочий пользовательский текст,
шлются в HTML с экранированием. На legacy Markdown ник с «_» ронял отправку
с «Can't parse entities» — и экран просто не открывался.
"""
import unittest
from html.parser import HTMLParser
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers.admin import (
    admin_confirm_wipe_player,
    admin_delete_options,
    admin_edit_club_start,
    admin_edit_username_start,
)

NASTY = "bad_nick*<x>"
NASTY_CLUB = "Club_[A] & <B>"
ALLOWED_TAGS = {"b", "i", "code"}


class _TagChecker(HTMLParser):
    """Telegram HTML: только разрешённые теги, и каждый закрыт."""

    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in ALLOWED_TAGS, f"unexpected <{tag}>"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"unbalanced </{tag}>"


def assert_valid_telegram_html(text: str) -> None:
    checker = _TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.stack, f"unclosed tags: {checker.stack}"


class TestUserTextIsEscaped(unittest.IsolatedAsyncioTestCase):
    player_id = 985511

    async def asyncSetUp(self):
        database.init_db()
        with database.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role) VALUES (?, ?, ?, ?)",
                (self.player_id, NASTY, NASTY_CLUB, "player"),
            )

    async def asyncTearDown(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM users WHERE telegram_id = ?", (self.player_id,))

    def _update(self, data: str):
        update = MagicMock()
        update.callback_query.data = data
        update.callback_query.from_user.id = 1
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        return update

    async def _render(self, handler, prefix: str) -> str:
        update = self._update(f"{prefix}{self.player_id}")
        context = MagicMock()
        context.user_data = {}
        with patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.base.is_admin", return_value=True):
            await handler(update, context)
        args, kwargs = update.callback_query.edit_message_text.call_args
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        return args[0] if args else kwargs["text"]

    async def test_player_screens_escape_nick_and_club(self):
        for handler, prefix in (
            (admin_delete_options, "admin_delete_options_"),
            (admin_confirm_wipe_player, "admin_confirm_wipe_player_"),
            (admin_edit_club_start, "admin_edit_club_start_"),
            (admin_edit_username_start, "admin_edit_username_start_"),
        ):
            with self.subTest(handler=handler.__name__):
                text = await self._render(handler, prefix)
                self.assertIn("bad_nick*&lt;x&gt;", text)
                self.assertNotIn("<x>", text)
                assert_valid_telegram_html(text)


def test_removal_messages_are_plain_text():
    """remove_player отдаёт текст без Markdown — он идёт и в HTML, и в alert."""
    pid = 985512
    with database.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role) VALUES (?, ?, ?, ?)",
            (pid, "plain_nick", "Plain Club", "player"),
        )
    ok, msg = database.remove_player(str(pid))
    assert ok
    assert "**" not in msg
    assert "@plain_nick" in msg


if __name__ == "__main__":
    unittest.main()
