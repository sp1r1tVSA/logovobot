"""
tests/test_shootout_manual.py

Результат с серией пенальти — табло «(5) 3 - 3 (4)» — со скриншота не берётся.

EA FC Mobile пишет голы серии в колонку «Г», и отличить их от голов с игры нельзя
(матч #6112 записался как «МЮ 4 : 5 Бавария»). Поэтому ИИ только замечает серию,
а результат вносится вручную: счёт основного времени, авторы голов с игры,
«Пропустить остаток», в кубке — вопрос «кто прошёл дальше».
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.cabinet as cabinet
import handlers.drafts as drafts
from services.ai.ai_recognizer import detect_shootout


class TestDetectShootout(unittest.TestCase):
    def test_brackets_are_read_as_a_shootout(self):
        self.assertEqual(detect_shootout({"left_pens": 5, "right_pens": 4}), {"left": 5, "right": 4})
        self.assertEqual(detect_shootout({"left_pens": "(4)", "right_pens": " 2 "}), {"left": 4, "right": 2})

    def test_no_shootout(self):
        for left, right in ((None, None), (0, 0), ("", ""), ("abc", 3), (True, 2), (99, 1), (5, None)):
            self.assertIsNone(detect_shootout({"left_pens": left, "right_pens": right}), (left, right))
        self.assertIsNone(detect_shootout({}))

    def test_has_shootout_checks_every_game(self):
        self.assertFalse(cabinet.has_shootout(None))
        self.assertFalse(cabinet.has_shootout({"shootout": None}))
        self.assertTrue(cabinet.has_shootout({"shootout": {"left": 5, "right": 4}}))
        self.assertTrue(cabinet.has_shootout({"matches": [{"shootout": None}, {"shootout": {"left": 1, "right": 3}}]}))


class TestShootoutGoesToManualEntry(unittest.IsolatedAsyncioTestCase):
    async def test_ai_card_is_replaced_by_manual_entry(self):
        update = MagicMock()
        query = MagicMock()
        query.data = "ai_recognize_now_6112"
        query.from_user = MagicMock(id=1)
        query.answer = AsyncMock()
        query.message.reply_text = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        update.callback_query = query
        context = MagicMock()
        context.user_data = {"ai_photos_list": ["photo_1"], "report_mvp_player": "Kane"}
        context.bot.get_file = AsyncMock(return_value=MagicMock(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"img"))))
        context.bot.send_photo = AsyncMock()
        ai_res = {"home_score": 3, "away_score": 3, "left_score": 3, "right_score": 3,
                  "shootout": {"left": 5, "right": 4}}
        match = {"id": 6112, "player1_team": "Бавария", "player2_team": "Манчестер Юнайтед"}

        with patch("handlers.cabinet.ai_recognizer.recognize_match_screenshots_bytes", return_value=ai_res), \
             patch("handlers.cabinet.database.get_match", return_value=match):
            await cabinet.ai_recognize_now(update, context)

        context.bot.send_photo.assert_not_awaited()
        self.assertNotIn("report_home_goals", context.user_data)
        self.assertNotIn("report_mvp_player", context.user_data)
        self.assertEqual(context.user_data["report_photo_id"], "photo_1")
        text = query.message.reply_text.call_args_list[-1][0][0]
        self.assertIn("серия пенальти", text)
        markup = query.message.reply_text.call_args_list[-1].kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("cb_report_choice_manual_6112", callbacks)

    async def test_topic_draft_is_refused(self):
        key = "user_shootout_manual_test"
        drafts.draft_media_groups[key] = {
            "photos": [b"img"], "photo_file_ids": ["f1"], "caption": "",
            "user_id": 1, "message_ids": [10], "division_id": None,
        }
        status_msg = MagicMock(edit_text=AsyncMock())
        update = MagicMock()
        update.effective_message.reply_text = AsyncMock(return_value=status_msg)
        ai_res = {"left_score": 3, "right_score": 3, "shootout": {"left": 5, "right": 4}}

        with patch("handlers.drafts.asyncio.sleep", new=AsyncMock()), \
             patch("handlers.drafts.recognize_match_screenshots_bytes", return_value=ai_res), \
             patch("handlers.drafts.database.detect_teams_from_players") as detect:
            await drafts._process_draft_group_delayed(key, update, MagicMock())

        detect.assert_not_called()
        self.assertIn("серия пенальти", status_msg.edit_text.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
