"""
tests/test_draft_flow.py

Сквозной прогон _process_draft_group_delayed: скриншот → ИИ → поиск матча → черновик.

Регрессия: `dropped_games` читалась в конце функции, но нигде не присваивалась, и
КАЖДЫЙ черновик падал NameError после OCR — в топике навсегда оставалось
«⏳ Обрабатываю результат через ИИ...». Asyncio глотает исключение фоновой задачи,
поэтому тесты ниже проверяют не «не упало», а что черновик реально опубликован.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.drafts as drafts

WARNING = "Лишние игры не занесены"


def _game(left, right):
    return {
        "team1": "Торино", "team2": "Спортинг",
        "left_score": left, "right_score": right,
        "side1_goals": [], "side2_goals": [], "side1_assists": [], "side2_assists": [],
    }


def _match(match_id, game_num=1):
    return {
        "id": match_id, "round_number": 11, "division_id": 1, "game_num_in_series": game_num,
        "player1_team": "Торино", "player2_team": "Спортинг",
        "player1_username": "@anton", "player2_username": "@sp",
    }


class TestDraftFlow(unittest.IsolatedAsyncioTestCase):
    async def _run(self, games, found_matches):
        """Гоняет обработчик; found_matches — что по очереди вернёт поиск активного матча."""
        key = "user_draft_flow_test"
        drafts.draft_media_groups[key] = {
            "photos": [b"img"], "photo_file_ids": ["f1"], "caption": "11 тур Торино - Спортинг",
            "user_id": 1, "message_ids": [10], "division_id": 1,
        }
        status_msg = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
        update = MagicMock()
        update.effective_message.reply_text = AsyncMock(return_value=status_msg)
        update.effective_chat.id = -100
        context = MagicMock()
        context.bot_data = {}
        context.bot.send_photo = AsyncMock()
        context.bot.send_message = AsyncMock()
        ai_res = {"matches": games} if len(games) > 1 else games[0]
        empty = ({}, {}, {}, {}, True)

        with patch("handlers.drafts.asyncio.sleep", new=AsyncMock()), \
             patch("handlers.drafts.recognize_match_screenshots_bytes", return_value=ai_res), \
             patch("handlers.drafts.database.detect_teams_from_players", return_value=(None, None)), \
             patch("handlers.drafts.database.resolve_team_name", side_effect=lambda n: n), \
             patch("handlers.drafts.database.get_active_match_by_teams", side_effect=found_matches), \
             patch("handlers.drafts.database.save_draft"), \
             patch("handlers.drafts.match_and_enrich_squad", return_value=empty), \
             patch("handlers.drafts.resolve_mvp_player_name", return_value=None), \
             patch("services.external_squad_lookup.find_new_goal_action_players", return_value=[]):
            await drafts._process_draft_group_delayed(key, update, context)

        return status_msg, context

    @staticmethod
    def _published_text(context):
        """Текст черновика: подпись к фото или отдельное сообщение."""
        if context.bot.send_photo.await_count:
            return context.bot.send_photo.call_args.kwargs["caption"]
        return context.bot.send_message.call_args.kwargs["text"]

    async def test_single_game_draft_is_published(self):
        status_msg, context = await self._run([_game(1, 0)], [_match(6001)])

        status_msg.delete.assert_awaited_once()
        self.assertTrue(context.bot.send_photo.await_count or context.bot.send_message.await_count)
        self.assertNotIn(WARNING, self._published_text(context))
        (draft,) = context.bot_data["drafts"].values()
        self.assertFalse(draft["is_multi"])
        self.assertEqual(draft["match_id"], 6001)

    async def test_series_with_enough_matches_has_no_warning(self):
        status_msg, context = await self._run(
            [_game(1, 0), _game(2, 2)], [_match(6001, 1), _match(6002, 2)]
        )

        status_msg.delete.assert_awaited_once()
        self.assertNotIn(WARNING, self._published_text(context))
        (draft,) = context.bot_data["drafts"].values()
        self.assertTrue(draft["is_multi"])
        self.assertEqual([g["match_id"] for g in draft["games"]], [6001, 6002])

    async def test_extra_games_are_dropped_with_a_warning(self):
        # ИИ увидел две игры, а свободный матч в расписании один.
        status_msg, context = await self._run([_game(1, 0), _game(2, 2)], [_match(6001), None])

        status_msg.delete.assert_awaited_once()
        text = self._published_text(context)
        self.assertIn(WARNING, text)
        self.assertIn("ИИ распознал игр: 2", text)
        self.assertIn("только 1", text)
        (draft,) = context.bot_data["drafts"].values()
        self.assertEqual(len(draft["games"]), 1)


if __name__ == "__main__":
    unittest.main()
