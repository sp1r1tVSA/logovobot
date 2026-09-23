"""
tests/test_cabinet_navigation.py

Back / cancel targets across the cabinet, the club screens and result entry.

- Result entry opened from the admin match card returns there; opened from the
  cabinet it returns to the cabinet — also for an admin reporting their own match.
- Leaving the flow through a menu drops every reporting key, so a stray photo is
  no longer taken for a match result.
- A club card remembers the screen it was opened from, so «Назад» still leads
  there after a detour through its squad or schedule.
- Player cards return to the list they were opened from.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers.cabinet as cabinet
from handlers.cabinet import (
    REPORT_STATE_KEYS,
    cabinet_view_match,
    cancel_score_report_and_navigate,
    clear_report_state,
    get_match_cancel_cb,
    match_done_back_buttons,
    recall_club_back,
    remember_club_back,
    send_or_edit_club_schedule,
    show_cabinet,
    show_clubs_catalog_divisions,
    show_clubs_catalog_for_division,
    show_player_card,
    show_specific_club_card,
)

USER_ID = 93000301
CLUB, OTHER_CLUB = "Бавария", "Реал Мадрид"


def _ctx(**user_data) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = dict(user_data)
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_photo = AsyncMock()
    return ctx


def _update(callback_data: str) -> MagicMock:
    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_user = MagicMock(id=USER_ID)
    query = update.callback_query
    query.data = callback_data
    query.from_user = MagicMock(id=USER_ID)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.delete = AsyncMock()
    return update


def _sent_callbacks(update, ctx) -> list[str]:
    """Callbacks of the keyboard the handler rendered, however it rendered it."""
    for mock in (ctx.bot.send_photo, ctx.bot.send_message, update.callback_query.edit_message_text):
        if mock.await_args:
            markup = mock.await_args.kwargs["reply_markup"]
            return [b.callback_data for row in markup.inline_keyboard for b in row]
    raise AssertionError("nothing was rendered")


class TestResultEntryTargets(unittest.TestCase):
    MATCH = {"player1_id": USER_ID, "player2_id": 2, "division_id": 3, "round_number": 4}

    def test_cabinet_entry_cancels_to_the_cabinet_match_card(self):
        self.assertEqual(get_match_cancel_cb(_ctx(), USER_ID, 777), "cabinet_view_match_777")

    def test_admin_entry_cancels_to_the_admin_match_card(self):
        ctx = _ctx(is_admin_reporting=True)
        self.assertEqual(get_match_cancel_cb(ctx, USER_ID, 777), "admin_view_match_777")

    def test_player_goes_back_to_their_matches_after_submitting(self):
        rows = match_done_back_buttons(_ctx(), self.MATCH, 777)
        self.assertEqual([b.callback_data for row in rows for b in row], ["cabinet_my_matches"])

    def test_admin_goes_back_to_the_match_and_its_division_round(self):
        rows = match_done_back_buttons(_ctx(is_admin_reporting=True), self.MATCH, 777)
        self.assertEqual(
            [b.callback_data for row in rows for b in row],
            ["admin_view_match_777", "admin_div_round_matches:3:4"],
        )

    def test_admin_non_participant_detection(self):
        with patch.object(cabinet, "is_admin", return_value=True):
            self.assertFalse(cabinet._admin_non_participant(USER_ID, self.MATCH))
            self.assertTrue(cabinet._admin_non_participant(555, self.MATCH))
        with patch.object(cabinet, "is_admin", return_value=False):
            self.assertFalse(cabinet._admin_non_participant(555, self.MATCH))


class TestLeavingTheReportFlow(unittest.IsolatedAsyncioTestCase):
    def _reporting_ctx(self) -> MagicMock:
        ctx = _ctx(**{key: 1 for key in REPORT_STATE_KEYS})
        ctx.user_data["club_stats_team"] = CLUB
        return ctx

    def test_clear_report_state_keeps_unrelated_keys(self):
        ctx = self._reporting_ctx()
        clear_report_state(ctx)
        self.assertEqual(ctx.user_data, {"club_stats_team": CLUB})

    def test_clear_report_state_tolerates_missing_user_data(self):
        ctx = MagicMock()
        ctx.user_data = None
        clear_report_state(ctx)

    async def test_cancel_to_the_cabinet_opens_the_cabinet(self):
        ctx = self._reporting_ctx()
        with patch.object(cabinet, "show_cabinet", new=AsyncMock()) as cab, \
                patch.object(cabinet, "show_my_matches", new=AsyncMock()) as mine:
            await cancel_score_report_and_navigate(_update("menu_cabinet"), ctx)
        cab.assert_awaited_once()
        mine.assert_not_awaited()
        self.assertNotIn("awaiting_report_photo", ctx.user_data)

    async def test_cancel_to_the_match_list_opens_the_match_list(self):
        ctx = self._reporting_ctx()
        with patch.object(cabinet, "show_cabinet", new=AsyncMock()) as cab, \
                patch.object(cabinet, "show_my_matches", new=AsyncMock()) as mine:
            await cancel_score_report_and_navigate(_update("cabinet_my_matches"), ctx)
        mine.assert_awaited_once()
        cab.assert_not_awaited()

    async def test_opening_the_cabinet_ends_the_report(self):
        ctx = self._reporting_ctx()
        with patch.object(cabinet.database, "get_user_team", return_value=None), \
                patch.object(cabinet, "is_admin", return_value=False):
            await show_cabinet(_update("menu_cabinet"), ctx)
        self.assertEqual(ctx.user_data, {"club_stats_team": CLUB})

    async def test_match_card_accepts_an_explicit_match_id(self):
        # cb_accept_time / cb_quick_time redraw the card with their own callback_data.
        ctx = _ctx(reporting_match_id=5, awaiting_report_photo=True)
        update = _update("cb_quick_time_5_Сегодня в 19:00")
        with patch.object(cabinet.database, "get_match", return_value=None) as get_match:
            await cabinet_view_match(update, ctx, 5)
        get_match.assert_called_once_with(5)
        self.assertNotIn("awaiting_report_photo", ctx.user_data)


class TestClubCardOrigin(unittest.IsolatedAsyncioTestCase):
    async def _open_card(self, ctx, club=CLUB, owner=None) -> str:
        with patch.object(cabinet, "send_or_edit_club_card", new=AsyncMock()) as card, \
                patch.object(cabinet.database, "find_user_by_team", return_value=owner):
            await show_specific_club_card(_update(f"view_club_{club}"), ctx)
        return card.await_args.kwargs["back_cb"]

    def test_recall_matches_only_the_recorded_club(self):
        ctx = _ctx()
        remember_club_back(ctx, CLUB, "menu_cabinet")
        self.assertEqual(recall_club_back(ctx, CLUB), "menu_cabinet")
        self.assertIsNone(recall_club_back(ctx, OTHER_CLUB))

    async def test_card_reopened_from_its_squad_still_leads_to_the_cabinet(self):
        ctx = _ctx()
        remember_club_back(ctx, CLUB, "menu_cabinet")
        self.assertEqual(await self._open_card(ctx), "menu_cabinet")

    async def test_card_from_the_admin_squad_leads_back_to_it(self):
        ctx = _ctx()
        remember_club_back(ctx, CLUB, f"admin_squad_view_{CLUB}")
        self.assertEqual(await self._open_card(ctx), f"admin_squad_view_{CLUB}")

    async def test_card_picked_from_a_division_list_leads_back_to_it(self):
        ctx = _ctx()
        with patch.object(cabinet.database, "get_division", return_value={"name": "Дивизион 2"}), \
                patch.object(cabinet.database, "get_clubs_summary_for_division", return_value=[]):
            await show_clubs_catalog_for_division(_update("clubs_catalog_div:2"), ctx)
        self.assertEqual(await self._open_card(ctx, OTHER_CLUB), "clubs_catalog_div:2")

    async def test_record_for_another_club_falls_back_to_its_division(self):
        ctx = _ctx()
        remember_club_back(ctx, OTHER_CLUB, "menu_cabinet")
        self.assertEqual(await self._open_card(ctx, owner={"division_id": 3}), "clubs_catalog_div:3")

    async def test_unknown_club_falls_back_to_the_catalog(self):
        self.assertEqual(await self._open_card(_ctx()), "cb_clubs_catalog")


class TestClubSubScreens(unittest.IsolatedAsyncioTestCase):
    async def _schedule(self, back_cb):
        ctx = _ctx()
        update = _update(f"clhist_{CLUB}")
        with patch.object(cabinet.database, "get_club_schedule_and_results", return_value={}), \
                patch.object(cabinet.club_schedule_generator, "generate_club_schedule", return_value=b""):
            await send_or_edit_club_schedule(update, ctx, CLUB, back_cb=back_cb)
        return _sent_callbacks(update, ctx)

    async def test_schedule_from_the_card_goes_back_to_the_card(self):
        callbacks = await self._schedule(f"view_club_{CLUB}")
        self.assertIn(f"view_club_{CLUB}", callbacks)
        self.assertNotIn("menu_cabinet", callbacks)

    async def test_schedule_honours_another_back_target(self):
        callbacks = await self._schedule("menu_cabinet")
        self.assertIn("menu_cabinet", callbacks)
        self.assertIn(f"view_club_{CLUB}", callbacks)

    async def _player_card_back(self, ctx) -> str:
        update = _update("pcard_0")
        stats = {"total_goals": 1, "total_assists": 0}
        with patch.object(cabinet.database, "get_player_card_stats", return_value=stats), \
                patch.object(cabinet.player_card_generator, "generate_player_card", return_value=b""):
            await show_player_card(update, ctx)
        return _sent_callbacks(update, ctx)[-1]

    async def test_player_card_from_club_top_goes_back_to_club_top(self):
        ctx = _ctx(club_stats_team=CLUB, club_stats_players=["Гарри Кейн"], club_stats_back_cb="cabinet_club_stats")
        self.assertEqual(await self._player_card_back(ctx), "cabinet_club_stats")

    async def test_player_card_from_the_squad_goes_back_to_the_squad(self):
        ctx = _ctx(club_stats_team=CLUB, club_stats_players=["Гарри Кейн"], club_stats_back_cb=f"clsquad_{CLUB}")
        self.assertEqual(await self._player_card_back(ctx), f"clsquad_{CLUB}")


class TestCatalogBack(unittest.IsolatedAsyncioTestCase):
    async def _back(self, team, admin=False) -> str:
        ctx = _ctx()
        update = _update("cb_clubs_catalog")
        update.callback_query.message.photo = None
        with patch.object(cabinet.database, "get_active_divisions", return_value=[]), \
                patch.object(cabinet.database, "get_user_team", return_value=team), \
                patch.object(cabinet, "is_admin", return_value=admin):
            await show_clubs_catalog_divisions(update, ctx)
        return _sent_callbacks(update, ctx)[-1]

    async def test_coach_goes_back_to_the_cabinet(self):
        self.assertEqual(await self._back(CLUB), "menu_cabinet")

    async def test_admin_without_a_club_goes_back_to_the_cabinet(self):
        self.assertEqual(await self._back(None, admin=True), "menu_cabinet")

    async def test_unregistered_user_goes_back_to_the_main_menu(self):
        self.assertEqual(await self._back(None), "main_menu")


if __name__ == "__main__":
    unittest.main()
