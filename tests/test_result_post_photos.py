"""
tests/test_result_post_photos.py

Пост результата несёт все скрины матча, а не только первый.

Игрок присылает до трёх скринов (счёт, лента голов, статистика); раньше в
РЕЗУЛЬТАТЫ уходил один `report_photo_id`. Теперь `send_result_post` шлёт один
скрин фото с подписью, несколько — альбомом, а текст длиннее подписи —
отдельным сообщением.
"""

import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
import handlers.cabinet as cabinet
from handlers.base import send_result_post, unique_photo_ids

TARGET = {"chat_id": -1001234567, "message_thread_id": 42}


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_media_group = AsyncMock()
    return bot


class SendResultPostTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_photos_is_a_text_post(self):
        bot = _bot()
        await send_result_post(bot, TARGET, "итог", [])
        bot.send_message.assert_awaited_once_with(**TARGET, text="итог", parse_mode="HTML")
        bot.send_photo.assert_not_awaited()
        bot.send_media_group.assert_not_awaited()

    async def test_one_photo_carries_the_caption(self):
        bot = _bot()
        await send_result_post(bot, TARGET, "итог", ["p1"])
        bot.send_photo.assert_awaited_once_with(**TARGET, photo="p1", caption="итог", parse_mode="HTML")
        bot.send_message.assert_not_awaited()

    async def test_several_photos_go_as_one_album(self):
        bot = _bot()
        await send_result_post(bot, TARGET, "итог", ["p1", "p2", "p3"])
        bot.send_media_group.assert_awaited_once()
        kwargs = bot.send_media_group.await_args.kwargs
        self.assertEqual(kwargs["message_thread_id"], 42)
        media = kwargs["media"]
        self.assertEqual([m.media for m in media], ["p1", "p2", "p3"])
        self.assertEqual(media[0].caption, "итог")
        self.assertIsNone(media[1].caption)
        bot.send_message.assert_not_awaited()

    async def test_long_text_follows_the_album(self):
        bot = _bot()
        text = "x" * 1500
        await send_result_post(bot, TARGET, text, ["p1", "p2"])
        media = bot.send_media_group.await_args.kwargs["media"]
        self.assertTrue(all(m.caption is None for m in media))
        bot.send_message.assert_awaited_once_with(**TARGET, text=text, parse_mode="HTML")

    async def test_cup_reply_target_reaches_the_album(self):
        bot = _bot()
        cup = {"chat_id": -100777, "reply_to_message_id": 3853, "allow_sending_without_reply": True}
        await send_result_post(bot, cup, "итог", ["p1", "p2"])
        kwargs = bot.send_media_group.await_args.kwargs
        self.assertEqual(kwargs["reply_to_message_id"], 3853)
        self.assertTrue(kwargs["allow_sending_without_reply"])

    async def test_failed_album_still_publishes_the_text(self):
        bot = _bot()
        bot.send_media_group.side_effect = RuntimeError("bad file id")
        await send_result_post(bot, TARGET, "итог", ["p1", "p2"])
        bot.send_message.assert_awaited_once_with(**TARGET, text="итог", parse_mode="HTML")

    def test_unique_photo_ids_keeps_order_and_drops_blanks(self):
        self.assertEqual(unique_photo_ids(["a", "b", None, "a"], "c", None, "b"), ["a", "b", "c"])


class ConfirmPostsAllScreenshotsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        div_id = database.create_division(name=f"Photo Div {uid}", code=f"PHD_{uid}")
        self.home_id, self.away_id = 93100101, 93100102
        database.register_user(self.home_id, f"home_{uid}", team_name=f"HomeFC_{uid}")
        database.register_user(self.away_id, f"away_{uid}", team_name=f"AwayFC_{uid}")
        round_number = 81000 + (int(uid, 16) % 1000)
        database.create_round(round_number, division_id=div_id)
        self.match_id = database.create_match(round_number, self.home_id, self.away_id, division_id=div_id)

    async def test_ai_confirm_sends_every_screenshot(self):
        query = MagicMock()
        query.data = f"cb_confirm_ai_final_{self.match_id}"
        query.from_user = MagicMock(id=self.home_id)
        query.answer = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update = MagicMock(callback_query=query)
        context = MagicMock()
        context.bot = _bot()
        context.user_data = {
            "reporting_match_id": self.match_id,
            "report_home_goals": 1,
            "report_away_goals": 0,
            "home_goals_count": {"Player One": 1},
            "report_photo_id": "shot_1",
            "ai_photos_list": ["shot_1", "shot_2", "shot_3"],
        }

        with patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
                patch("handlers.cabinet.refresh_league_table", new=AsyncMock()), \
                patch("handlers.cabinet.handle_debt_played_rewards", new=AsyncMock()), \
                patch("handlers.cabinet.safe_send_notification", new=AsyncMock()), \
                patch("handlers.cabinet.resolve_post_target", new=AsyncMock(return_value=TARGET)):
            await cabinet.cb_confirm_ai_final(update, context)

        self.assertEqual(database.get_match(self.match_id)["status"], "confirmed")
        context.bot.send_media_group.assert_awaited_once()
        media = context.bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual([m.media for m in media], ["shot_1", "shot_2", "shot_3"])

    def test_manual_payload_keeps_every_screenshot(self):
        context = MagicMock()
        context.user_data = {"ai_photos_list": ["a", "b"], "report_photo_id": "a"}
        match = {"player1_team": "H", "player2_team": "A", "player1_nickname": None, "player2_nickname": None}
        payload = cabinet.collect_report_payload(context, match)
        self.assertEqual(payload["photo_ids"], ["a", "b"])
        self.assertEqual(payload["photo_id"], "a")


if __name__ == "__main__":
    unittest.main()
