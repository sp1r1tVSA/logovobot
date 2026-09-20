"""
tests/test_manual_report_stale_session.py

Regression tests for the manual result-entry flow in `handlers/cabinet.py` after
a bot restart.

The whole flow — match id, team names, the squads behind the buttons — lives in
in-memory `user_data`. A restart wipes it while the inline keyboard on the
already-sent message stays clickable, so `cb_report_ag_*` used to walk on with
`report_home_goals` missing (→ 0, so the home phases were skipped) and
`report_away_team` `None`, crashing in `html.escape(None)`:

    AttributeError: 'NoneType' object has no attribute 'replace'

Every callback of the flow must instead report an expired session and stop.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.cabinet import (
    cb_report_home_goals,
    cb_report_away_goals,
    cb_pick_goal,
    cb_skip_goals,
    cb_pick_assist,
    cb_skip_assists,
)


def _make_update(callback_data: str) -> MagicMock:
    update = MagicMock()
    query = update.callback_query
    query.data = callback_data
    query.from_user = MagicMock(id=93000101)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


STALE_CALLBACKS = (
    (cb_report_home_goals, "cb_report_hg_2"),
    (cb_report_away_goals, "cb_report_ag_2"),
    (cb_pick_goal, "cb_pick_goal_idx_0"),
    (cb_skip_goals, "cb_skip_goals"),
    (cb_pick_assist, "cb_pick_assist_idx_0"),
    (cb_skip_assists, "cb_skip_assists"),
)


class TestManualReportStaleSession(unittest.IsolatedAsyncioTestCase):
    """Empty user_data — the state a restart leaves behind."""

    async def test_every_step_reports_expiry_instead_of_crashing(self):
        for handler, data in STALE_CALLBACKS:
            with self.subTest(handler=handler.__name__):
                update = _make_update(data)
                ctx = _make_context()

                with patch("handlers.cabinet.database.get_squad") as get_squad:
                    await handler(update, ctx)

                # The squad lookup belongs to the pickers we must never reach.
                get_squad.assert_not_called()

                update.callback_query.edit_message_text.assert_awaited_once()
                text = update.callback_query.edit_message_text.await_args.args[0]
                self.assertIn("устарела", text)

    async def test_half_a_session_is_still_treated_as_lost(self):
        """A match id without team names is just as unusable."""
        update = _make_update("cb_report_ag_1")
        ctx = _make_context()
        ctx.user_data["reporting_match_id"] = 777

        with patch("handlers.cabinet.database.get_squad") as get_squad:
            await cb_report_away_goals(update, ctx)

        get_squad.assert_not_called()
        update.callback_query.edit_message_text.assert_awaited_once()


class TestManualReportLiveSession(unittest.IsolatedAsyncioTestCase):
    """A live session must be untouched by the guard."""

    def setUp(self):
        self.ctx = _make_context()
        self.ctx.user_data.update({
            "reporting_match_id": 777,
            "report_home_team": "Бавария",
            "report_away_team": "Реал Мадрид",
        })

    async def test_away_goals_still_opens_the_scorer_picker(self):
        update = _make_update("cb_report_ag_2")
        self.ctx.user_data["report_home_goals"] = 0

        with patch("handlers.cabinet.database.get_squad", return_value=["Мбаппе", "Винисиус"]):
            await cb_report_away_goals(update, self.ctx)

        self.assertEqual(self.ctx.user_data["report_away_goals"], 2)
        self.assertEqual(self.ctx.user_data["current_picking_phase"], "away_goals")
        self.assertEqual(self.ctx.user_data["goals_to_pick"], 2)

        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Реал Мадрид", text)
        self.assertNotIn("устарела", text)


if __name__ == "__main__":
    unittest.main()
