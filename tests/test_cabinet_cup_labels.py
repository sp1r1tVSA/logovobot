"""
tests/test_cabinet_cup_labels.py

«Мои матчи» и карточка матча: у кубковой игры нет тура. Номер тура там
служебный −1, и кнопки читались как «⚽ Тур -1: 🆚 Байя» — три одинаковые на
серию. Подпись должна называть этап и номер игры.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from constants import CUP_DIVISION_SENTINEL
from handlers import cabinet

CUP_GAME = {
    "id": 101, "round_number": -1, "tournament_type": "cup", "cup_stage": "1/64",
    "game_num_in_series": 2, "division_id": CUP_DIVISION_SENTINEL,
    "opponent_team": "Байя", "opponent_username": "bahia_coach",
}
LEAGUE_GAME = {
    "id": 7, "round_number": 3, "tournament_type": "league", "division_id": 4,
    "opponent_team": "Порту", "opponent_username": "porto_coach",
}


class MatchLabelTest(unittest.TestCase):
    def test_cup_game_names_stage_and_game(self):
        self.assertEqual(cabinet._match_round_label(CUP_GAME), "Кубок · 1/64 · игра 2")

    def test_league_game_keeps_its_round(self):
        self.assertEqual(cabinet._match_round_label(LEAGUE_GAME), "Тур 3")

    def test_cup_row_without_stage_details_is_still_a_cup(self):
        self.assertEqual(cabinet._match_round_label({"round_number": -1}), "Кубок")


class MyMatchesButtonsTest(unittest.TestCase):
    def test_buttons_never_say_round_minus_one(self):
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query)
        edit = AsyncMock()
        with patch.object(cabinet, "check_group_card_access", return_value=True), \
                patch.object(cabinet.database, "get_pending_matches", return_value=[CUP_GAME, LEAGUE_GAME]), \
                patch.object(cabinet, "safe_edit_or_reply", edit):
            asyncio.run(cabinet.show_my_matches(update, SimpleNamespace()))
        labels = [row[0].text for row in edit.await_args.kwargs["reply_markup"].inline_keyboard]
        self.assertIn("🏆 Кубок · 1/64 · игра 2: 🆚 Байя", labels)
        self.assertIn("⚽ Тур 3: 🆚 Порту", labels)
        self.assertFalse(any("Тур -1" in label for label in labels))


if __name__ == "__main__":
    unittest.main()
