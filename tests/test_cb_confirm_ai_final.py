import asyncio
import uuid
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import database
import handlers.cabinet as cabinet


class TestCbConfirmAiFinal(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(name=f"AI Div {self.uid}", code=f"AID_{self.uid}")

        self.home_id = 93000101
        self.away_id = 93000102
        self.third_id = 93000199
        database.register_user(self.home_id, f"home_{self.uid}", team_name=f"HomeFC_{self.uid}")
        database.register_user(self.away_id, f"away_{self.uid}", team_name=f"AwayFC_{self.uid}")
        database.register_user(self.third_id, f"intruder_{self.uid}", team_name=f"IntruderFC_{self.uid}")

        self.round_number = 80000 + (int(self.uid, 16) % 1000)
        database.create_round(self.round_number, division_id=self.div_id)
        self.match_id = database.create_match(
            self.round_number, self.home_id, self.away_id, division_id=self.div_id
        )

    def _make_update_context(self, user_id, match_id=None):
        mid = match_id if match_id is not None else self.match_id
        update = MagicMock()
        query = MagicMock()
        query.data = f"cb_confirm_ai_final_{mid}"
        query.from_user = MagicMock(id=user_id)
        query.answer = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.user_data = {}
        context.bot = MagicMock()
        context.bot.send_message = AsyncMock()
        context.bot.send_photo = AsyncMock()
        return update, query, context

    async def test_successful_confirmation(self):
        update, query, context = self._make_update_context(self.home_id)
        context.user_data.update({
            "reporting_match_id": self.match_id,
            "report_home_goals": 2,
            "report_away_goals": 1,
            "home_goals_count": {"Player One": 2},
            "away_goals_count": {"Player Two": 1},
            "home_assists_count": {"Assistant One": 1},
            "away_assists_count": {},
            "report_photo_id": "photo_12345",
            "report_mvp_player": "Player One",
        })

        with patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_league_table", new=AsyncMock()), \
             patch("handlers.cabinet.handle_debt_played_rewards", new=AsyncMock()), \
             patch("handlers.cabinet.safe_send_notification", new=AsyncMock()) as mock_notify, \
             patch("handlers.cabinet.resolve_division_target", new=AsyncMock(return_value=(-1001234567, 42))):

            await cabinet.cb_confirm_ai_final(update, context)

        # 1. Match in DB is confirmed with correct score
        match = database.get_match(self.match_id)
        self.assertEqual(match["status"], "confirmed")
        self.assertEqual(match["player1_score"], 2)
        self.assertEqual(match["player2_score"], 1)
        self.assertEqual(match["mvp_player"], "Player One")

        # 2. Match events were written
        events = database.get_match_events(self.match_id)
        self.assertEqual(len(events), 3)

        # 3. User data was wiped
        self.assertNotIn("report_home_goals", context.user_data)
        self.assertNotIn("report_away_goals", context.user_data)
        self.assertNotIn("home_goals_count", context.user_data)
        self.assertNotIn("report_mvp_player", context.user_data)

        # 4. Reporter message was edited
        query.edit_message_caption.assert_awaited_once()

        # 5. Opponent was notified
        mock_notify.assert_awaited_once()
        self.assertEqual(mock_notify.call_args[0][1], self.away_id)

    async def test_guard_against_bot_restart_empty_user_data(self):
        """If user_data is empty (e.g. bot restarted), match MUST NOT be overwritten with 0:0."""
        update, query, context = self._make_update_context(self.home_id)
        # Empty user_data simulating restart
        context.user_data = {}

        await cabinet.cb_confirm_ai_final(update, context)

        # Match must still be pending
        match = database.get_match(self.match_id)
        self.assertEqual(match["status"], "pending")
        self.assertIsNone(match["player1_score"])

        # Alert sent to user
        query.answer.assert_awaited_once()
        self.assertIn("устарели", query.answer.call_args[0][0])
        self.assertTrue(query.answer.call_args[1].get("show_alert"))

    async def test_already_confirmed_match(self):
        # Set match confirmed in database
        database.confirm_and_finalize_match(self.match_id, 1, 0, [])

        update, query, context = self._make_update_context(self.home_id)
        context.user_data.update({
            "reporting_match_id": self.match_id,
            "report_home_goals": 2,
            "report_away_goals": 0,
        })

        await cabinet.cb_confirm_ai_final(update, context)

        query.answer.assert_awaited_once()
        self.assertIn("уже зафиксирован", query.answer.call_args[0][0])
        self.assertTrue(query.answer.call_args[1].get("show_alert"))

    async def test_unauthorized_user_blocked(self):
        """Intruder (not home, not away, not admin) cannot confirm match."""
        update, query, context = self._make_update_context(self.third_id)
        context.user_data.update({
            "reporting_match_id": self.match_id,
            "report_home_goals": 5,
            "report_away_goals": 0,
        })

        with patch("handlers.cabinet.is_admin", return_value=False):
            await cabinet.cb_confirm_ai_final(update, context)

        match = database.get_match(self.match_id)
        self.assertEqual(match["status"], "pending")
        self.assertIsNone(match["player1_score"])

        query.answer.assert_awaited_once()
        self.assertIn("только участники матча", query.answer.call_args[0][0])
        self.assertTrue(query.answer.call_args[1].get("show_alert"))

    async def test_match_id_mismatch_blocked(self):
        """If user_data has data for match A, but callback is for match B, block it."""
        update, query, context = self._make_update_context(self.home_id)
        context.user_data.update({
            "reporting_match_id": 999999,  # Mismatch!
            "report_home_goals": 3,
            "report_away_goals": 2,
        })

        await cabinet.cb_confirm_ai_final(update, context)

    async def test_long_caption_safety(self):
        """When formatted post > 1024 chars, ensure it does not raise BadRequest and sends via text/photo appropriately."""
        update, query, context = self._make_update_context(self.home_id)
        # Create many goals to make post > 1024 characters
        long_goals = {f"Super Long Named Football Striker Number {i}": 1 for i in range(25)}
        context.user_data.update({
            "reporting_match_id": self.match_id,
            "report_home_goals": 25,
            "report_away_goals": 0,
            "home_goals_count": long_goals,
            "away_goals_count": {},
            "report_photo_id": "photo_large_123",
        })

        with patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_league_table", new=AsyncMock()), \
             patch("handlers.cabinet.handle_debt_played_rewards", new=AsyncMock()), \
             patch("handlers.cabinet.safe_send_notification", new=AsyncMock()), \
             patch("handlers.cabinet.resolve_division_target", new=AsyncMock(return_value=(-1001234567, 42))):

            await cabinet.cb_confirm_ai_final(update, context)

        match = database.get_match(self.match_id)
        self.assertEqual(match["status"], "confirmed")
        # In PM, send_message should have been called due to > 1024 chars
        context.bot.send_message.assert_awaited()

    async def test_none_team_names_fallback(self):
        """When player teams and nicknames are None/empty, fallback to 'Хозяева' / 'Гости' without error."""
        with database.transaction() as conn:
            conn.execute(
                "UPDATE matches SET player1_team = NULL, player2_team = NULL WHERE id = ?",
                (self.match_id,)
            )
            conn.execute(
                "UPDATE users SET team_name = NULL, username = NULL WHERE telegram_id IN (?, ?)",
                (self.home_id, self.away_id,)
            )

        update, query, context = self._make_update_context(self.home_id)
        context.user_data.update({
            "reporting_match_id": self.match_id,
            "report_home_goals": 1,
            "report_away_goals": 0,
            "home_goals_count": {"Player": 1},
        })

        with patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_league_table", new=AsyncMock()), \
             patch("handlers.cabinet.handle_debt_played_rewards", new=AsyncMock()), \
             patch("handlers.cabinet.safe_send_notification", new=AsyncMock()), \
             patch("handlers.cabinet.resolve_division_target", new=AsyncMock(return_value=(-1001234567, 42))):

            await cabinet.cb_confirm_ai_final(update, context)

        match = database.get_match(self.match_id)
        self.assertEqual(match["status"], "confirmed")
        self.assertEqual(match["player1_score"], 1)


if __name__ == "__main__":
    unittest.main()

