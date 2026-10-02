"""Жизненный цикл тура: scheduled → open → closed, долги в match_debts.

Главный сценарий регламента: тур закрыт досрочно — к 48 часам отыгрыша
добавляется остаток до дедлайна, округлённый вверх до часа. Пример из
регламента: дедлайн 23.09 22:00, закрыт 22.09 22:00 → 48 + 24 часа,
эскалация 25.09 22:00.
"""
import datetime
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
from services import debt_policy

FMT = "%d.%m.%Y %H:%M"
OPENED_AT = datetime.datetime(2026, 9, 21, 0, 0)
DEADLINE = datetime.datetime(2026, 9, 23, 22, 0)


class RoundFixture(unittest.TestCase):
    ROUND = 1

    def setUp(self):
        code = f"LC_{uuid.uuid4().hex[:6].upper()}"
        self.div_id = database.create_division(f"Lifecycle {code}", code)
        self.season_id = int(database._resolve_season_id(None))
        self.match_ids = []
        with database.transaction() as conn:
            for home, away, status in (("Альфа", "Бета", "pending"), ("Гамма", "Дельта", "confirmed")):
                cur = conn.execute(
                    "INSERT INTO matches (round_number, division_id, season_id, player1_team, player2_team, "
                    "status, tournament_type) VALUES (?, ?, ?, ?, ?, ?, 'league')",
                    (self.ROUND, self.div_id, self.season_id, home, away, status),
                )
                self.match_ids.append(cur.lastrowid)
        self.pending_id, self.played_id = self.match_ids

    def at(self, moment):
        return patch("database.now_msk", return_value=moment)

    def open_round(self, deadline=DEADLINE, moment=OPENED_AT):
        with self.at(moment):
            database.update_round_status(self.ROUND, True, deadline=deadline.strftime(FMT),
                                         division_id=self.div_id, season_id=self.season_id)

    def close(self, moment, admin_id=77):
        with self.at(moment):
            return database.close_round(self.ROUND, self.div_id, admin_id, season_id=self.season_id)

    def round_info(self):
        return database.get_round_info(self.ROUND, self.div_id, season_id=self.season_id)


class TestRoundStatus(RoundFixture):
    def test_open_sets_status_and_close_records_who_and_when(self):
        self.open_round()
        self.assertEqual(debt_policy.round_status(self.round_info()), debt_policy.ROUND_OPEN)

        closed_at = datetime.datetime(2026, 9, 24, 10, 0)
        self.close(closed_at, admin_id=4242)
        info = self.round_info()
        self.assertEqual(info["status"], "closed")
        self.assertEqual(info["closed_by"], 4242)
        self.assertEqual(debt_policy.parse_msk(info["closed_at"]), closed_at)

    def test_closing_a_never_opened_round_makes_no_debts(self):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline, status) "
                "VALUES (?, ?, ?, 0, NULL, 'scheduled')",
                (self.season_id, self.div_id, self.ROUND),
            )
        self.close(OPENED_AT)
        self.assertIsNone(database.get_match_debt(self.pending_id))
        self.assertEqual(self.round_info()["status"], "scheduled")

    def test_phase_is_overdue_after_deadline_while_still_open(self):
        self.open_round()
        self.assertEqual(debt_policy.round_phase(self.round_info(), DEADLINE + datetime.timedelta(minutes=1)),
                         debt_policy.ROUND_OVERDUE)


class TestCloseRound(RoundFixture):
    def test_early_close_adds_time_left_to_deadline(self):
        """Регламент: закрыт за 24 часа до дедлайна → 48 + 24 часа на отыгрыш."""
        self.open_round()
        closed_at = datetime.datetime(2026, 9, 22, 22, 0)

        with self.at(closed_at):
            preview = database.preview_close_round(self.ROUND, self.div_id, season_id=self.season_id,
                                                   now=closed_at)
        self.assertTrue(preview["early"])
        self.assertEqual(preview["grace_hours"], 24)
        self.assertEqual([m["id"] for m in preview["matches"]], [self.pending_id])

        result = self.close(closed_at)
        self.assertTrue(result["early"])
        self.assertEqual(result["grace_hours"], 24)
        self.assertEqual(result["escalate_at"], datetime.datetime(2026, 9, 25, 22, 0))

        debt = database.get_match_debt(self.pending_id)
        self.assertEqual(debt["state"], "active")
        self.assertEqual(debt["grace_hours"], 24)
        self.assertEqual(debt_policy.parse_msk(debt["became_debt_at"]), closed_at)
        self.assertEqual(debt_policy.parse_msk(debt["escalate_at"]), datetime.datetime(2026, 9, 25, 22, 0))
        self.assertIsNone(database.get_match_debt(self.played_id), "сыгранный матч долгом не становится")

    def test_early_close_rounds_the_remainder_up_to_the_hour(self):
        self.open_round()
        result = self.close(DEADLINE - datetime.timedelta(hours=23, minutes=20))
        self.assertEqual(result["grace_hours"], 24)

    def test_close_after_deadline_counts_from_deadline(self):
        self.open_round()
        result = self.close(DEADLINE + datetime.timedelta(hours=5))
        self.assertFalse(result["early"])
        self.assertEqual(result["grace_hours"], 0)
        debt = database.get_match_debt(self.pending_id)
        self.assertEqual(debt_policy.parse_msk(debt["became_debt_at"]), DEADLINE)
        self.assertEqual(debt_policy.parse_msk(debt["escalate_at"]), DEADLINE + datetime.timedelta(hours=48))

    def test_second_close_keeps_the_original_moment(self):
        self.open_round()
        first = datetime.datetime(2026, 9, 22, 22, 0)
        self.close(first)
        self.close(first + datetime.timedelta(hours=10))
        self.assertEqual(debt_policy.parse_msk(self.round_info()["closed_at"]), first)
        self.assertEqual(database.get_match_debt(self.pending_id)["grace_hours"], 24)


class TestReopenAndDeadlineChange(RoundFixture):
    def test_reopen_cancels_debts_without_verdict(self):
        self.open_round()
        self.close(datetime.datetime(2026, 9, 22, 22, 0))
        self.assertEqual(database.get_match_debt(self.pending_id)["state"], "active")

        self.open_round(deadline=datetime.datetime(2026, 9, 30, 22, 0), moment=datetime.datetime(2026, 9, 23, 0, 0))
        debt = database.get_match_debt(self.pending_id)
        self.assertEqual(debt["state"], "cancelled")
        self.assertEqual(debt["resolution"], "deadline_moved")
        self.assertEqual(debt_policy.round_status(self.round_info()), debt_policy.ROUND_OPEN)
        self.assertIsNone(self.round_info()["closed_at"])

    def test_reopen_keeps_debt_with_applied_verdict(self):
        self.open_round()
        self.close(datetime.datetime(2026, 9, 22, 22, 0))
        with database.transaction() as conn:
            conn.execute("UPDATE match_debts SET verdict_applied_at = ? WHERE match_id = ?",
                         ("22.09.2026 23:00", self.pending_id))
        self.open_round(deadline=datetime.datetime(2026, 9, 30, 22, 0), moment=datetime.datetime(2026, 9, 23, 0, 0))
        self.assertEqual(database.get_match_debt(self.pending_id)["state"], "active")

    def test_close_after_reopen_revives_the_cancelled_debt(self):
        self.open_round()
        self.close(datetime.datetime(2026, 9, 22, 22, 0))
        new_deadline = datetime.datetime(2026, 9, 30, 22, 0)
        self.open_round(deadline=new_deadline, moment=datetime.datetime(2026, 9, 23, 0, 0))
        self.close(new_deadline + datetime.timedelta(hours=1))
        debt = database.get_match_debt(self.pending_id)
        self.assertEqual(debt["state"], "active")
        self.assertEqual(debt_policy.parse_msk(debt["became_debt_at"]), new_deadline)

    def test_reopen_round_requires_a_future_deadline(self):
        with self.assertRaises(database.RoundDeadlineError):
            database.reopen_round(self.ROUND, self.div_id, "01.01.2020 10:00", season_id=self.season_id)


class TestExtendRoundDeadline(RoundFixture):
    """Продление открытого тура — только срок, без повторного открытия."""
    NEW_DEADLINE = datetime.datetime(2026, 9, 30, 22, 0)

    def extend(self, deadline=None, moment=datetime.datetime(2026, 9, 22, 0, 0)):
        deadline = deadline or self.NEW_DEADLINE
        with self.at(moment):
            return database.extend_round_deadline(
                self.ROUND, self.div_id, deadline.strftime(FMT), season_id=self.season_id
            )

    def _set_bets_open(self, round_number, value):
        with database.transaction() as conn:
            conn.execute(
                "UPDATE rounds SET bets_open = ? WHERE division_id = ? AND round_number = ?",
                (value, self.div_id, round_number),
            )

    def test_changes_only_the_deadline_and_returns_the_old_one(self):
        self.open_round()
        before = self.round_info()

        old = self.extend()

        after = self.round_info()
        self.assertEqual(old, DEADLINE.strftime(FMT))
        self.assertEqual(after["deadline"], self.NEW_DEADLINE.strftime(FMT))
        self.assertEqual(after["status"], "open")
        self.assertEqual(after["is_open"], 1)
        self.assertEqual(after["bets_open"], before["bets_open"])
        self.assertEqual(after["closed_at"], before["closed_at"])

    def test_does_not_touch_betting_lines_or_advance_the_pair(self):
        self.open_round()
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline, status, bets_open) "
                "VALUES (?, ?, 2, 0, NULL, 'scheduled', 1)",
                (self.season_id, self.div_id),
            )

        with patch("database.advance_betting_line_pair") as advance, \
             patch("database.close_round_betting_line") as close_line, \
             patch("database.set_round_bets_open") as set_line:
            self.extend()

        advance.assert_not_called()
        close_line.assert_not_called()
        set_line.assert_not_called()
        nxt = database.get_round_info(2, self.div_id, season_id=self.season_id)
        self.assertEqual(nxt["bets_open"], 1, "линия соседнего тура остаётся открытой")

    def test_extension_after_deadline_cancels_the_debt(self):
        self.open_round()
        with self.at(DEADLINE + datetime.timedelta(hours=1)):
            database.sync_match_debts(season_id=self.season_id)
        self.assertEqual(database.get_match_debt(self.pending_id)["state"], "active")

        self.extend(moment=DEADLINE + datetime.timedelta(hours=2))

        debt = database.get_match_debt(self.pending_id)
        self.assertEqual(debt["state"], "cancelled")
        self.assertEqual(debt["resolution"], "deadline_moved")
        self.assertEqual(debt_policy.round_status(self.round_info()), debt_policy.ROUND_OPEN)

    def test_keeps_debt_with_applied_verdict(self):
        self.open_round()
        with self.at(DEADLINE + datetime.timedelta(hours=1)):
            database.sync_match_debts(season_id=self.season_id)
        with database.transaction() as conn:
            conn.execute("UPDATE match_debts SET verdict_applied_at = ? WHERE match_id = ?",
                         ("23.09.2026 23:30", self.pending_id))

        self.extend(moment=DEADLINE + datetime.timedelta(hours=2))

        self.assertEqual(database.get_match_debt(self.pending_id)["state"], "active")

    def test_resets_deadline_reminders(self):
        self.open_round()
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO round_reminders (division_id, round_number, reminder_type, sent_at) "
                "VALUES (?, ?, '24h', '21.09.2026 10:00')",
                (self.div_id, self.ROUND),
            )

        self.extend()

        with database.transaction() as conn:
            left = conn.execute(
                "SELECT COUNT(*) FROM round_reminders WHERE round_number = ? AND division_id = ?",
                (self.ROUND, self.div_id),
            ).fetchone()[0]
        self.assertEqual(left, 0)

    def test_rejects_closed_and_scheduled_rounds(self):
        with self.assertRaises(database.RoundNotOpenError):
            self.extend()  # тура ещё нет

        self.open_round()
        self.close(datetime.datetime(2026, 9, 22, 22, 0))
        with self.assertRaises(database.RoundNotOpenError):
            self.extend(moment=datetime.datetime(2026, 9, 23, 0, 0))
        self.assertEqual(self.round_info()["status"], "closed")

    def test_requires_a_future_deadline(self):
        self.open_round()
        with self.assertRaises(database.RoundDeadlineError):
            self.extend(deadline=datetime.datetime(2026, 9, 20, 10, 0))
        self.assertEqual(self.round_info()["deadline"], DEADLINE.strftime(FMT))


class TestExtendDeadlineHandler(unittest.IsolatedAsyncioTestCase):
    """«Открыть тур» на уже открытом туре: объявление и ЛС, но без смены линий."""

    async def asyncSetUp(self):
        code = f"LC_{uuid.uuid4().hex[:6].upper()}"
        self.div_id = database.create_division(f"Lifecycle {code}", code)
        self.season_id = int(database._resolve_season_id(None))
        self.p1, self.p2 = 870000 + self.div_id, 871000 + self.div_id
        with database.transaction() as conn:
            for tg, name in ((self.p1, "ext_a"), (self.p2, "ext_b")):
                conn.execute(
                    "INSERT INTO users (telegram_id, username, team_name, division_id) VALUES (?, ?, ?, ?)",
                    (tg, name, f"Клуб {name} {code}", self.div_id),
                )
            conn.execute(
                "INSERT INTO matches (round_number, division_id, season_id, player1_id, player2_id, "
                "player1_team, player2_team, status, tournament_type) "
                "VALUES (4, ?, ?, ?, ?, 'Эй', 'Би', 'pending', 'league')",
                (self.div_id, self.season_id, self.p1, self.p2),
            )
            conn.execute(
                "INSERT INTO matches (round_number, division_id, season_id, player1_id, player2_id, "
                "player1_team, player2_team, status, tournament_type) "
                "VALUES (4, ?, ?, NULL, NULL, 'Си', 'Ди', 'confirmed', 'league')",
                (self.div_id, self.season_id),
            )
        old = (database.now_msk() + datetime.timedelta(hours=5)).strftime(FMT)
        database.update_round_status(4, True, deadline=old, division_id=self.div_id, season_id=self.season_id)
        self.old_deadline = old
        self.new_deadline = (database.now_msk() + datetime.timedelta(days=3)).strftime(FMT)

    async def _save(self):
        from handlers import admin

        update = MagicMock()
        update.effective_user.id = 1
        update.message.text = self.new_deadline
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {"admin_round_to_open": 4, "admin_round_open_div": self.div_id}

        with patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.admin._announce_rounds_opened", new=AsyncMock(return_value=True)) as announce, \
             patch("handlers.admin.safe_send_notification", new=AsyncMock(return_value=True)) as dm, \
             patch("handlers.admin.notify_players_rounds_opened", new=AsyncMock()) as opened, \
             patch("database.update_round_status") as update_status, \
             patch("services.betting_notifications.notify_division_betting_line_closed",
                   new=AsyncMock()) as line_closed, \
             patch("services.betting_notifications.notify_division_betting_line_opened",
                   new=AsyncMock()) as line_opened:
            await admin.admin_open_round_save(update, context)
        return update, announce, dm, opened, update_status, line_closed, line_opened

    async def test_extension_notifies_topic_and_pending_players_only(self):
        update, announce, dm, opened, update_status, line_closed, line_opened = await self._save()

        info = database.get_round_info(4, self.div_id, season_id=self.season_id)
        self.assertEqual(info["deadline"], self.new_deadline)
        self.assertEqual(info["status"], "open")

        announce.assert_awaited_once()
        self.assertIn("продлён", announce.await_args.args[2])
        self.assertIn(self.new_deadline, announce.await_args.args[2])
        self.assertFalse(announce.await_args.kwargs.get("include_table"))

        # ЛС — только двум игрокам несыгранного матча, сыгранный пропущен.
        self.assertEqual({c.args[1] for c in dm.await_args_list}, {self.p1, self.p2})
        self.assertIn("продлён", dm.await_args_list[0].args[2])

        self.assertIn("продлён", update.message.reply_text.await_args.args[0])

    async def test_extension_does_not_reopen_the_round_or_touch_lines(self):
        _, _, _, opened, update_status, line_closed, line_opened = await self._save()

        update_status.assert_not_called()
        opened.assert_not_awaited()
        line_closed.assert_not_awaited()
        line_opened.assert_not_awaited()

    async def test_shortening_is_announced_as_moved(self):
        self.new_deadline = (database.now_msk() + datetime.timedelta(hours=1)).strftime(FMT)
        _, announce, dm, *_ = await self._save()

        self.assertIn("перенесён", announce.await_args.args[2])
        self.assertIn("перенесён", dm.await_args_list[0].args[2])


class TestSyncMatchDebts(RoundFixture):
    def test_sync_creates_debt_after_deadline_and_resolves_when_played(self):
        self.open_round()
        before = DEADLINE - datetime.timedelta(hours=1)
        database.sync_match_debts(now=before, season_id=self.season_id)
        self.assertIsNone(database.get_match_debt(self.pending_id))

        after = DEADLINE + datetime.timedelta(hours=1)
        database.sync_match_debts(now=after, season_id=self.season_id)
        first = database.get_match_debt(self.pending_id)
        self.assertEqual(first["state"], "active")
        self.assertEqual(debt_policy.parse_msk(first["became_debt_at"]), DEADLINE)
        database.sync_match_debts(now=after + datetime.timedelta(hours=1), season_id=self.season_id)
        self.assertEqual(database.get_match_debt(self.pending_id), first, "идемпотентно")

        with database.transaction() as conn:
            conn.execute("UPDATE matches SET status = 'confirmed' WHERE id = ?", (self.pending_id,))
        database.sync_match_debts(now=after, season_id=self.season_id)
        debt = database.get_match_debt(self.pending_id)
        self.assertEqual((debt["state"], debt["resolution"]), ("resolved", "played"))


class TestDeadlineValidation(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 21, 12, 0)

    def test_accepts_future_deadline(self):
        self.assertEqual(database.validate_round_deadline("23.09.2026 22:00", self.NOW),
                         datetime.datetime(2026, 9, 23, 22, 0))

    def test_rejects_empty_garbage_and_past(self):
        for text in (None, "", "   ", "завтра вечером", "20.09.2026 22:00", "21.09.2026 12:00"):
            with self.subTest(text=text), self.assertRaises(database.RoundDeadlineError) as ctx:
                database.validate_round_deadline(text, self.NOW)
            self.assertTrue(ctx.exception.reason)


class TestReminderMilestones(RoundFixture):
    def test_passed_milestones_are_marked_on_open(self):
        """Тур открыт за 50 часов — вехи 72…54 уже позади и не приходят."""
        self.open_round(moment=DEADLINE - datetime.timedelta(hours=50))
        tags = database.get_sent_reminder_tags(self.ROUND, self.div_id)
        self.assertEqual(tags, {"72h", "66h", "60h", "54h"})

    def test_plan_catches_up_with_one_message(self):
        sent = {"72h", "66h"}
        milestone, tags = debt_policy.plan_deadline_reminder(40.0, sent)
        self.assertEqual(milestone, 42)
        self.assertEqual(set(tags), {"60h", "54h", "48h", "42h"})

    def test_plan_is_empty_after_deadline_or_when_all_sent(self):
        self.assertIsNone(debt_policy.plan_deadline_reminder(0, set()))
        self.assertIsNone(debt_policy.plan_deadline_reminder(-3, set()))
        self.assertIsNone(debt_policy.plan_deadline_reminder(23.5, {"24h"}, milestones=(24,)))

    def test_labels(self):
        self.assertEqual(debt_policy.hours_label(1), "1 час")
        self.assertEqual(debt_policy.hours_label(24), "24 часа")
        self.assertEqual(debt_policy.hours_label(48), "48 часов")
        self.assertEqual(debt_policy.hours_label(11), "11 часов")
        self.assertEqual(debt_policy.deadline_reminder_label(1, 0.8), "1 час! 🚨")
        self.assertEqual(debt_policy.deadline_reminder_label(24, 23.4), "24 часа")
        self.assertEqual(debt_policy.deadline_reminder_label(42, 40.2), "40 часов")


class TestDeadlinePassedAdminSignal(unittest.IsolatedAsyncioTestCase):
    async def test_admins_get_one_close_prompt_after_deadline(self):
        from handlers import admin

        code = f"LC_{uuid.uuid4().hex[:6].upper()}"
        div_id = database.create_division(f"Lifecycle {code}", code)
        season_id = int(database._resolve_season_id(None))
        past = (database.now_msk() - datetime.timedelta(hours=2)).strftime(FMT)
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO matches (round_number, division_id, season_id, player1_team, player2_team, "
                "status, tournament_type) VALUES (3, ?, ?, 'А', 'Б', 'pending', 'league')",
                (div_id, season_id),
            )
            conn.execute(
                "INSERT INTO rounds (season_id, division_id, round_number, is_open, deadline, status) "
                "VALUES (?, ?, 3, 1, ?, 'open')",
                (season_id, div_id, past),
            )

        context = MagicMock()
        with patch("handlers.admin._resolve_debt_admins", new=AsyncMock(return_value=[501])), \
             patch("handlers.admin.safe_send_notification", new=AsyncMock(return_value=True)) as send, \
             patch("handlers.admin.send_round_reminders", new=AsyncMock()) as remind:
            await admin.job_check_deadlines_and_remind(context)
            await admin.job_check_deadlines_and_remind(context)

        self.assertNotIn(div_id, {c.kwargs.get("division_id") for c in remind.await_args_list})
        own = [c for c in send.await_args_list if f"admin_div_round_close:{div_id}:3" in str(c.kwargs.get("reply_markup"))]
        self.assertEqual(len(own), 1, "сигнал админам — ровно один раз")
        self.assertIn("Не сыграно матчей: <b>1</b>", own[0].args[2])


class TestCloseConfirmHandler(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_closes_and_notifies_debtors(self):
        from handlers import admin

        code = f"LC_{uuid.uuid4().hex[:6].upper()}"
        div_id = database.create_division(f"Lifecycle {code}", code)
        season_id = int(database._resolve_season_id(None))
        future = (database.now_msk() + datetime.timedelta(hours=30)).strftime(FMT)
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO users (telegram_id, username, team_name, division_id) VALUES (?, 'lc_home', ?, ?)",
                (880000 + div_id, f"Хозяева {code}", div_id),
            )
            conn.execute(
                "INSERT INTO matches (round_number, division_id, season_id, player1_id, player2_id, "
                "player1_team, player2_team, status, tournament_type) "
                "VALUES (5, ?, ?, ?, NULL, ?, 'Гости', 'pending', 'league')",
                (div_id, season_id, 880000 + div_id, f"Хозяева {code}"),
            )
        database.update_round_status(5, True, deadline=future, division_id=div_id, season_id=season_id)

        update = MagicMock()
        update.callback_query.data = f"admin_div_round_close_ok:{div_id}:5"
        update.callback_query.from_user.id = 1
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.effective_user.id = 1

        with patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin._ensure_division_access", new=AsyncMock(return_value=True)), \
             patch("handlers.admin._announce_rounds_opened", new=AsyncMock(return_value=True)), \
             patch("handlers.admin.safe_send_notification", new=AsyncMock(return_value=True)) as send:
            await admin.admin_close_round_confirm(update, MagicMock())

        info = database.get_round_info(5, div_id, season_id=season_id)
        self.assertEqual(info["status"], "closed")
        self.assertEqual(info["closed_by"], 1)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(send.await_args.args[1], 880000 + div_id)
        text = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("Долгов: 1", text)


if __name__ == "__main__":
    unittest.main()
