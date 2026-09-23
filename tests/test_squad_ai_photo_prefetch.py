"""
Проверяет, что после распознавания состава (`squad_ai_apply`) бот запускает
фоновую предзагрузку фото игроков через `services.graphics.player_photos`,
и что при отмене распознавания предзагрузка не запускается.
"""

import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
import handlers.squad_ai as squad_ai


class TestSquadAiPhotoPrefetch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.club = f"Test FC {uid}"
        self.players = [
            {"player_name": "Test Player One", "position": "ST"},
            {"player_name": "Test Player Two", "position": "GK"},
        ]

    async def asyncTearDown(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM squad_players WHERE team_name = ?", (self.club,))

    def _build_update_and_context(self, callback_data: str):
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query

        context = MagicMock()
        context.user_data = {
            squad_ai.PENDING_KEY: {
                "club": self.club,
                "players": self.players,
                "back_cb": "back",
            }
        }
        return update, context

    async def test_add_schedules_photo_prefetch(self):
        update, context = self._build_update_and_context("squadai_add")

        with patch(
            "services.graphics.player_photos.fetch_all_players", new=MagicMock(return_value={})
        ) as mock_fetch:
            await squad_ai.squad_ai_apply(update, context)
            # allow the fire-and-forget asyncio.create_task to run
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        mock_fetch.assert_called_once()
        (pairs,), _ = mock_fetch.call_args
        self.assertEqual(
            sorted(pairs),
            sorted([("Test Player One", self.club, "ST"), ("Test Player Two", self.club, "GK")]),
        )

    async def test_replace_schedules_photo_prefetch(self):
        update, context = self._build_update_and_context("squadai_replace")

        with patch(
            "services.graphics.player_photos.fetch_all_players", new=MagicMock(return_value={})
        ) as mock_fetch:
            await squad_ai.squad_ai_apply(update, context)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        mock_fetch.assert_called_once()

    async def test_cancel_does_not_schedule_photo_prefetch(self):
        update, context = self._build_update_and_context("squadai_cancel")

        with patch(
            "services.graphics.player_photos.fetch_all_players", new=MagicMock(return_value={})
        ) as mock_fetch:
            await squad_ai.squad_ai_apply(update, context)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        mock_fetch.assert_not_called()


    async def test_offer_recognized_squad_auto_saves_when_squad_empty(self):
        """When a club has 0 players and AI recognizes >= 11 players, auto-save triggers immediately."""
        eleven_players = [
            {"player_name": f"P_{i}", "position": "CM"} for i in range(11)
        ]

        update = MagicMock()
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        update.effective_message.reply_text = AsyncMock(return_value=status_msg)

        context = MagicMock()
        context.user_data = {}

        with patch("handlers.squad_ai.recognize_squad_photo", new=AsyncMock(return_value=eleven_players)), \
             patch("services.graphics.player_photos.fetch_all_players", new=MagicMock(return_value={})) as mock_fetch:
            await squad_ai.offer_recognized_squad(
                update, context,
                club=self.club,
                file_id="photo_file_123",
                back_cb="cabinet_my_squad",
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        # Database must now contain the 11 players
        saved_squad = database.get_squad(self.club)
        self.assertEqual(len(saved_squad), 11)

        # Photo prefetch scheduled
        mock_fetch.assert_called_once()

        # Status message edited with success confirmation and [✏️ Изменить] button
        status_msg.edit_text.assert_called_once()
        text_arg = status_msg.edit_text.call_args[0][0]
        self.assertIn("ИИ распознал и добавил 11 игроков", text_arg)
        self.assertIn("Если нужно отредактировать", text_arg)

        reply_markup = status_msg.edit_text.call_args[1]["reply_markup"]
        btn = reply_markup.inline_keyboard[0][0]
        self.assertEqual(btn.text, "✏️ Изменить")
        self.assertEqual(btn.callback_data, "cabinet_my_squad")

        # No pending review state left
        self.assertNotIn(squad_ai.PENDING_KEY, context.user_data)

    async def test_offer_recognized_squad_does_not_auto_save_when_squad_already_populated(self):
        """When a club already has players in DB, do not auto-save; show review buttons."""
        database.add_squad(self.club, [{"player_name": "Existing Star", "position": "ST"}])

        eleven_players = [
            {"player_name": f"New_P_{i}", "position": "CM"} for i in range(11)
        ]

        update = MagicMock()
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        update.effective_message.reply_text = AsyncMock(return_value=status_msg)

        context = MagicMock()
        context.user_data = {}

        with patch("handlers.squad_ai.recognize_squad_photo", new=AsyncMock(return_value=eleven_players)):
            await squad_ai.offer_recognized_squad(
                update, context,
                club=self.club,
                file_id="photo_file_123",
                back_cb="cabinet_my_squad",
            )

        # Should NOT overwrite database yet
        saved_squad = database.get_squad(self.club)
        self.assertEqual(len(saved_squad), 1)
        self.assertEqual(saved_squad[0], "Existing Star")

        # Pending key must be set for manual confirmation
        self.assertIn(squad_ai.PENDING_KEY, context.user_data)

        # Status text must ask to choose action
        text_arg = status_msg.edit_text.call_args[0][0]
        self.assertIn("Проверьте список и выберите действие", text_arg)

    async def test_offer_recognized_squad_does_not_auto_save_when_fewer_than_11_players(self):
        """When AI recognizes fewer than 11 players for an empty club, do not auto-save."""
        five_players = [
            {"player_name": f"P_{i}", "position": "CM"} for i in range(5)
        ]

        update = MagicMock()
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        update.effective_message.reply_text = AsyncMock(return_value=status_msg)

        context = MagicMock()
        context.user_data = {}

        with patch("handlers.squad_ai.recognize_squad_photo", new=AsyncMock(return_value=five_players)):
            await squad_ai.offer_recognized_squad(
                update, context,
                club=self.club,
                file_id="photo_file_123",
                back_cb="cabinet_my_squad",
            )

        # Database must still be empty
        saved_squad = database.get_squad(self.club)
        self.assertEqual(len(saved_squad), 0)

        # Pending key must be set for review
        self.assertIn(squad_ai.PENDING_KEY, context.user_data)


if __name__ == "__main__":
    unittest.main()

