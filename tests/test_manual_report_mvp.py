"""
tests/test_manual_report_mvp.py

The MVP step of the manual result entry in `handlers/cabinet.py`.

After the away assists the flow used to go straight to the screenshot prompt, so
a manually entered result could never carry a player of the match. It now stops
at a picker — the match's scorers and assisters as quick picks, either squad on
demand, or «Без MVP» — and the choice lands in `report_mvp_player`, which
`collect_report_payload` already hands to `confirm_and_finalize_match`.
Admins enter results through the same `cb_report_choice_manual` flow.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.cabinet import (
    _show_manual_confirmation,
    cb_mvp_back,
    cb_mvp_pick,
    cb_mvp_skip,
    cb_mvp_team,
    cb_pick_assist,
    cb_report_away_goals,
    cb_skip_assists,
    collect_report_payload,
)

HOME, AWAY = "Бавария", "Реал Мадрид"
HOME_SQUAD = ["Гарри Кейн", "Джамал Мусиала", "Мануэль Нойер"]
AWAY_SQUAD = ["Килиан Мбаппе", "Винисиус", "Тибо Куртуа"]


def _make_update(callback_data: str) -> MagicMock:
    update = MagicMock()
    query = update.callback_query
    query.data = callback_data
    query.from_user = MagicMock(id=93000202)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.effective_user = MagicMock(id=93000202)
    return update


def _squad(team):
    return {HOME: HOME_SQUAD, AWAY: AWAY_SQUAD}.get(team, [])


def _buttons(update) -> list:
    markup = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    return [b for row in markup.inline_keyboard for b in row]


def _callbacks(update) -> list[str]:
    return [b.callback_data for b in _buttons(update)]


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.user_data = {
            "reporting_match_id": 777,
            "reporting_mode": "manual",
            "report_home_team": HOME,
            "report_away_team": AWAY,
            "report_home_goals": 2,
            "report_away_goals": 1,
            "home_goals_count": {"Гарри Кейн": 2},
            "home_assists_count": {"Джамал Мусиала": 1},
            "away_goals_count": {"Килиан Мбаппе": 1},
            "away_assists_count": {},
        }
        patcher = patch("handlers.cabinet.database.get_squad", side_effect=_squad)
        self.get_squad = patcher.start()
        self.addCleanup(patcher.stop)

    async def _open_picker(self) -> MagicMock:
        """Finish the away assists — the step right before the MVP picker."""
        self.ctx.user_data["current_picking_phase"] = "away_assists"
        update = _make_update("cb_skip_assists")
        await cb_skip_assists(update, self.ctx)
        return update


class TestMvpPickerAppears(_Base):
    async def test_picker_follows_the_away_assists(self):
        update = await self._open_picker()

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("MVP", text)
        self.assertNotIn("скриншот", text.lower())
        self.assertFalse(self.ctx.user_data.get("awaiting_report_photo"))

    async def test_quick_picks_are_the_matchs_scorers_and_assisters(self):
        update = await self._open_picker()

        labels = [b.text for b in _buttons(update) if b.callback_data.startswith("cb_mvp_pick_idx_")]
        self.assertEqual(labels, ["👑 Гарри Кейн", "👑 Килиан Мбаппе", "👑 Джамал Мусиала"])

        callbacks = _callbacks(update)
        for expected in ("cb_mvp_team_home", "cb_mvp_team_away", "cb_mvp_skip", "cabinet_view_match_777"):
            self.assertIn(expected, callbacks)

    async def test_last_assist_opens_the_picker(self):
        self.ctx.user_data.update({"current_picking_phase": "away_assists", "assists_to_pick": 1})
        self.ctx.user_data["temp_active_squad_assists"] = AWAY_SQUAD
        update = _make_update("cb_pick_assist_idx_1")

        await cb_pick_assist(update, self.ctx)

        self.assertIn("cb_mvp_skip", _callbacks(update))

    async def test_goalless_away_side_opens_the_picker(self):
        self.ctx.user_data.update({"report_home_goals": 0, "home_goals_count": {}, "home_assists_count": {}})
        update = _make_update("cb_report_ag_0")

        await cb_report_away_goals(update, self.ctx)

        callbacks = _callbacks(update)
        self.assertIn("cb_mvp_skip", callbacks)
        # 0:0 — nobody to offer as a quick pick, the squads are still there.
        self.assertFalse(any(c.startswith("cb_mvp_pick_idx_") for c in callbacks))
        self.assertIn("cb_mvp_team_home", callbacks)

    async def test_new_score_drops_an_earlier_pick(self):
        self.ctx.user_data["report_mvp_player"] = "Гарри Кейн"
        await cb_report_away_goals(_make_update("cb_report_ag_1"), self.ctx)
        self.assertNotIn("report_mvp_player", self.ctx.user_data)


class TestMvpChoice(_Base):
    async def test_quick_pick_sets_the_mvp_and_asks_for_the_screenshot(self):
        await self._open_picker()
        update = _make_update("cb_mvp_pick_idx_1")

        await cb_mvp_pick(update, self.ctx)

        self.assertEqual(self.ctx.user_data["report_mvp_player"], "Килиан Мбаппе")
        self.assertTrue(self.ctx.user_data.get("awaiting_report_photo"))
        self.assertIn("cb_skip_report_photo", _callbacks(update))

    async def test_any_squad_player_can_be_the_mvp(self):
        await self._open_picker()
        team_update = _make_update("cb_mvp_team_away")
        await cb_mvp_team(team_update, self.ctx)

        self.get_squad.assert_called_with(AWAY)
        text = team_update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn(AWAY, text)
        self.assertIn("cb_mvp_back", _callbacks(team_update))

        await cb_mvp_pick(_make_update("cb_mvp_squad_idx_2"), self.ctx)
        self.assertEqual(self.ctx.user_data["report_mvp_player"], "Тибо Куртуа")

    async def test_back_returns_to_the_quick_picks(self):
        await self._open_picker()
        await cb_mvp_team(_make_update("cb_mvp_team_home"), self.ctx)

        update = _make_update("cb_mvp_back")
        await cb_mvp_back(update, self.ctx)

        self.assertIn("cb_mvp_team_away", _callbacks(update))
        self.assertNotIn("report_mvp_player", self.ctx.user_data)

    async def test_out_of_range_index_does_not_crown_anyone(self):
        await self._open_picker()
        update = _make_update("cb_mvp_pick_idx_9")

        await cb_mvp_pick(update, self.ctx)

        self.assertNotIn("report_mvp_player", self.ctx.user_data)
        self.assertIn("cb_mvp_skip", _callbacks(update))

    async def test_skip_leaves_the_match_without_mvp(self):
        self.ctx.user_data["report_mvp_player"] = "Гарри Кейн"
        update = _make_update("cb_mvp_skip")

        await cb_mvp_skip(update, self.ctx)

        self.assertNotIn("report_mvp_player", self.ctx.user_data)
        self.assertTrue(self.ctx.user_data.get("awaiting_report_photo"))

    async def test_admin_cancel_goes_back_to_the_admin_match_card(self):
        self.ctx.user_data["is_admin_reporting"] = True
        update = await self._open_picker()
        self.assertIn("admin_view_match_777", _callbacks(update))


class TestMvpReachesTheResult(_Base):
    MATCH = {"player1_team": HOME, "player2_team": AWAY}

    async def _confirmation_text(self) -> str:
        update = _make_update("cb_skip_report_photo")
        self.ctx.bot.send_message = AsyncMock()
        with patch("handlers.cabinet.database.get_match", return_value=self.MATCH):
            await _show_manual_confirmation(update, self.ctx, photo_id=None)
        return self.ctx.bot.send_message.await_args.kwargs["text"]

    async def test_confirmation_card_shows_the_mvp(self):
        self.ctx.user_data["report_mvp_player"] = "Гарри Кейн"
        self.assertIn("👑 <b>MVP:</b> Гарри Кейн", await self._confirmation_text())

    async def test_confirmation_card_says_when_there_is_none(self):
        self.assertIn("MVP:</b> не выбран", await self._confirmation_text())

    async def test_payload_carries_the_pick(self):
        self.ctx.user_data["report_mvp_player"] = "Винисиус"
        self.assertEqual(collect_report_payload(self.ctx, self.MATCH)["mvp_player"], "Винисиус")


if __name__ == "__main__":
    unittest.main()
