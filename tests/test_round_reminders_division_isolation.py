"""Unit tests for division isolation in round deadline reminders."""
import datetime
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers.admin import job_check_deadlines_and_remind


class TestRoundRemindersDivisionIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.div_a_id = database.create_division(name=f"Rem Div A {uid}", code=f"RMA_{uid}")
        self.div_b_id = database.create_division(name=f"Rem Div B {uid}", code=f"RMB_{uid}")
        self.round_num = 1

    async def asyncTearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM round_reminders WHERE round_number = ?", (self.round_num,))
            c.execute("DELETE FROM matches WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM rounds WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM divisions WHERE id IN (?, ?)", (self.div_a_id, self.div_b_id))

    def test_round_reminders_schema_has_composite_pk(self):
        """round_reminders must include division_id in the primary key."""
        with database.transaction() as conn:
            c = conn.cursor()
            cols = c.execute("PRAGMA table_info(round_reminders)").fetchall()
        pk_cols = [col[1] for col in cols if col[5] > 0]
        self.assertIn("division_id", pk_cols)
        self.assertIn("round_number", pk_cols)
        self.assertIn("reminder_type", pk_cols)

    def test_multi_division_reminders_do_not_overwrite_each_other(self):
        """Recording a reminder for div A must not clear or overwrite div B."""
        # Record for div A
        database.record_reminder_sent(self.round_num, "24h", division_id=self.div_a_id)
        self.assertTrue(database.has_reminder_been_sent(self.round_num, "24h", division_id=self.div_a_id))
        self.assertFalse(database.has_reminder_been_sent(self.round_num, "24h", division_id=self.div_b_id))

        # Record for div B
        database.record_reminder_sent(self.round_num, "24h", division_id=self.div_b_id)
        self.assertTrue(database.has_reminder_been_sent(self.round_num, "24h", division_id=self.div_b_id))

        # Div A must STILL be True! (The bug previously caused it to be False)
        self.assertTrue(
            database.has_reminder_been_sent(self.round_num, "24h", division_id=self.div_a_id),
            "Division A reminder must not be overwritten by Division B reminder",
        )

    async def test_job_check_deadlines_and_remind_does_not_repeat_across_divisions(self):
        """job_check_deadlines_and_remind should send reminders once per division, not loop."""
        # Set deadlines ~24 hours in the future (between 23 and 25 hours)
        target_time = database.now_msk() + datetime.timedelta(hours=24)
        dl_str = target_time.strftime("%d.%m.%Y %H:%M")

        season_id = int(database.get_active_season() or 1)
        with database.transaction() as conn:
            c = conn.cursor()
            # Open round 1 in div A and div B with 24h deadline
            c.execute(
                "INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, ?, ?, 1, ?)",
                (season_id, self.div_a_id, self.round_num, dl_str),
            )
            c.execute(
                "INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, ?, ?, 1, ?)",
                (season_id, self.div_b_id, self.round_num, dl_str),
            )

        context = MagicMock()
        context.bot = MagicMock()

        with patch("handlers.admin.send_round_reminders", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = (2, 1)

            # First tick: should send for BOTH div A and div B
            await job_check_deadlines_and_remind(context)
            self.assertEqual(mock_send.await_count, 2)
            sent_divs = {call.kwargs.get("division_id") for call in mock_send.await_args_list}
            self.assertEqual(sent_divs, {self.div_a_id, self.div_b_id})

            # Second tick (simulating 30 minutes later, still in 23-25h window):
            # Neither should receive a second reminder!
            mock_send.reset_mock()
            await job_check_deadlines_and_remind(context)
            self.assertEqual(mock_send.await_count, 0, "No reminders should be re-sent on subsequent tick")

    async def test_job_check_deadlines_supports_6h_cycle_milestones(self):
        """Reminders should trigger on 6-hour cycle milestones (e.g. 18h, 12h, 6h)."""
        # Set deadline to ~18 hours in the future
        target_time = database.now_msk() + datetime.timedelta(hours=18)
        dl_str = target_time.strftime("%d.%m.%Y %H:%M")

        season_id = int(database.get_active_season() or 1)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, ?, ?, 1, ?)",
                (season_id, self.div_a_id, self.round_num, dl_str),
            )

        context = MagicMock()
        context.bot = MagicMock()

        with patch("handlers.admin.send_round_reminders", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = (2, 1)

            # First tick at 18h: should trigger 18h reminder
            await job_check_deadlines_and_remind(context)
            self.assertEqual(mock_send.await_count, 1)
            call_kwargs = mock_send.await_args[1]
            self.assertEqual(call_kwargs.get("time_left_str"), "18 часов")
            self.assertEqual(call_kwargs.get("division_id"), self.div_a_id)

            # Re-running immediately: should not duplicate
            mock_send.reset_mock()
            await job_check_deadlines_and_remind(context)
            self.assertEqual(mock_send.await_count, 0)
