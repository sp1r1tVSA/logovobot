"""
tests/test_p0_nameerror_regressions.py

Regression tests for the four production `NameError`s found in the 2026-09
architecture audit (see Project_Audit_Report.md §2.1, P0-1 .. P0-4).

Each of these bugs shipped past a fully green 548-test suite because the broken
statement lives on a branch no existing test executes. The tests below execute
exactly those branches — that is their entire purpose. Keep them narrow.

| Test | Guards | Original failure |
|---|---|---|
| `test_ai_chat_builds_full_context_and_replies` | `handlers/chat.py` | `cup_info_text` referenced but never assigned → AI chat 100% dead |
| `test_player_report_is_finalized_immediately` / `..._does_not_ask_the_opponent` | `handlers/cabinet.py` | `photo_id` (and the score/scorer names) never bound from `payload` |
| `test_evaluate_bet_handles_round_with_deadline` | `services/risk_engine.py` | missing `import datetime` → crash on any round carrying a deadline |
| `test_club_schedule_sorts_pending_fixtures` | `database.py` | `lambda x: l[...]` — comprehension variable does not leak in Python 3 |
"""

import os
import sys
import uuid
import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from handlers.chat import handle_ai_chat
from handlers.cabinet import submit_report_to_guest, cb_guest_confirm, cb_guest_reject
from services.risk_engine import RiskEngine


def _make_context() -> MagicMock:
    """A ContextTypes stand-in with async bot methods and a real user_data dict."""
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot = MagicMock()
    ctx.bot.id = 700000001
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.send_message = AsyncMock()
    ctx.bot.send_photo = AsyncMock()
    return ctx


class TestAiChatContextAssembly(unittest.IsolatedAsyncioTestCase):
    """P0-1 — `handlers/chat.py`: `cup_info_text` was used but never defined."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.user_id = 92000101
        database.register_user(self.user_id, f"chat_user_{self.uid}", team_name=f"ChatClub_{self.uid}")

    async def test_ai_chat_builds_full_context_and_replies(self):
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "Темшик, как дела?"
        update.message.voice = None
        update.message.reply_to_message = None
        update.message.reply_text = AsyncMock()
        update.effective_user = MagicMock(id=self.user_id, username=f"chat_user_{self.uid}")
        update.effective_chat = MagicMock(id=-100123456)

        ctx = _make_context()

        with patch("handlers.chat.handle_temshik_command", new=AsyncMock(return_value=False)), \
             patch("handlers.chat.ai_chat.generate_chat_reply", return_value="Норм, погнали.") as gen:
            await handle_ai_chat(update, ctx)

        # The whole point: assembling the prompt must not raise NameError.
        gen.assert_called_once()
        context_data = gen.call_args.args[3]

        # The cup block is the section that was missing entirely.
        # Заголовок переехал на дивизионную формулировку: кубок общий на турнир,
        # но в контекст попадают только серии клубов текущего дивизиона.
        self.assertIn("КУБОК", context_data)
        # And its siblings must still be there — a stubbed-out fix would drop these.
        for section in ("ТУРНИРНАЯ ТАБЛИЦА", "ТОП БОМБАРДИРОВ", "ФОРМА КОМАНД",
                        "РАСПИСАНИЕ ПРЕДСТОЯЩИХ МАТЧЕЙ", "ОФИЦИАЛЬНЫЙ РЕГЛАМЕНТ",
                        "СТРУКТУРА ТУРНИРА"):
            self.assertIn(section, context_data, f"missing prompt section: {section}")

        update.message.reply_text.assert_awaited_once_with("Норм, погнали.")

    async def test_get_all_cup_series_returns_expected_shape(self):
        """The reader added for P0-1 must expose every field chat.py formats."""
        rows = database.get_all_cup_series()
        self.assertIsInstance(rows, list)
        for row in rows:
            for key in ("stage", "team1_name", "team2_name",
                        "team1_wins", "team2_wins", "winner_name", "status"):
                self.assertIn(key, row)


class TestGuestReportPayloadBinding(unittest.IsolatedAsyncioTestCase):
    """P0-2 — `handlers/cabinet.py`: `photo_id` & friends were never bound from `payload`.

    Opponent confirmation has since been removed: a player's report is written
    straight to the table on submit, exactly like an admin's. The payload
    binding this test was written for is therefore checked at the
    `confirm_and_finalize_match` call instead of at the message sent to the
    opponent — and the absence of that message is now part of the contract.
    """

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(name=f"P0 Div {self.uid}", code=f"P0D_{self.uid}")

        self.home_id = 92000201
        self.away_id = 92000202
        database.register_user(self.home_id, f"home_{self.uid}", team_name=f"HomeFC_{self.uid}")
        database.register_user(self.away_id, f"away_{self.uid}", team_name=f"AwayFC_{self.uid}")

        self.round_number = 90000 + (int(self.uid, 16) % 1000)
        database.create_round(self.round_number, division_id=self.div_id)
        self.match_id = database.create_match(
            self.round_number, self.home_id, self.away_id, division_id=self.div_id
        )

    def _make_update(self):
        update = MagicMock()
        query = MagicMock()
        query.data = f"cb_submit_report_to_guest_{self.match_id}"
        query.from_user = MagicMock(id=self.home_id)
        query.answer = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        return update, query

    def _fill_report(self, ctx, photo_id=None):
        ctx.user_data.update({
            "reporting_match_id": self.match_id,
            "report_home_goals": 3,
            "report_away_goals": 1,
            "home_goals_count": {"Igor Paixao": 2, "Gittens": 1},
            "away_goals_count": {"Bardghji": 1},
            "home_assists_count": {"Ndoye": 1},
            "away_assists_count": {},
        })
        if photo_id:
            ctx.user_data["report_photo_id"] = photo_id

    async def _run(self, photo_id=None):
        update, query = self._make_update()
        ctx = _make_context()
        self._fill_report(ctx, photo_id=photo_id)

        notify = AsyncMock()
        finalize = MagicMock(wraps=database.confirm_and_finalize_match)
        with patch("handlers.cabinet.is_admin", return_value=False), \
             patch.object(database, "confirm_and_finalize_match", new=finalize), \
             patch("handlers.cabinet.notify_match_confirmed", new=notify), \
             patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_league_table", new=AsyncMock()):
            await submit_report_to_guest(update, ctx)

        return ctx, query, finalize, notify

    async def test_player_report_is_finalized_immediately(self):
        """A non-admin submit must write the result itself, not park it."""
        ctx, query, finalize, notify = await self._run(photo_id="AgACAgIAAxkBAAI_TEST_PHOTO")

        finalize.assert_called_once()
        args, kwargs = finalize.call_args
        self.assertEqual(args[0], self.match_id)
        self.assertEqual((args[1], args[2]), (3, 1))
        self.assertEqual(kwargs["photo_id"], "AgACAgIAAxkBAAI_TEST_PHOTO")
        self.assertEqual(kwargs["reporter_id"], self.home_id)

        # Scorers and assists come from the same payload unpack that was missing.
        events = {(e[1], e[2], e[3]) for e in args[3]}
        self.assertIn(("Igor Paixao", "goal", 2), events)
        self.assertIn(("Gittens", "goal", 1), events)
        self.assertIn(("Bardghji", "goal", 1), events)
        self.assertIn(("Ndoye", "assist", 1), events)

        # The match is confirmed in the DB, not waiting on anyone.
        match = database.get_match(self.match_id)
        self.assertEqual(match["status"], "confirmed")
        self.assertEqual((match["player1_score"], match["player2_score"]), (3, 1))

        notify.assert_awaited_once()
        query.edit_message_caption.assert_awaited_once()
        self.assertIn(
            f"#{self.match_id}",
            query.edit_message_caption.await_args.kwargs["caption"],
        )

    async def test_player_report_does_not_ask_the_opponent(self):
        """No confirmation card to the opponent, no pending row left behind."""
        ctx, _query, _finalize, _notify = await self._run(photo_id=None)

        ctx.bot.send_photo.assert_not_awaited()
        ctx.bot.send_message.assert_not_awaited()
        self.assertIsNone(database.get_pending_report(self.match_id))

    async def test_confirmed_match_is_not_finalized_twice(self):
        """The duplicate guard is the only thing standing in for confirmation."""
        await self._run(photo_id=None)

        update, query = self._make_update()
        ctx = _make_context()
        self._fill_report(ctx)
        finalize = MagicMock(wraps=database.confirm_and_finalize_match)
        with patch("handlers.cabinet.is_admin", return_value=False), \
             patch.object(database, "confirm_and_finalize_match", new=finalize), \
             patch("handlers.cabinet.notify_match_confirmed", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_league_table", new=AsyncMock()):
            await submit_report_to_guest(update, ctx)

        finalize.assert_not_called()


class TestObsoleteGuestButtons(unittest.IsolatedAsyncioTestCase):
    """Stale «Подтвердить»/«Отклонить» buttons must answer, not raise."""

    async def _press(self, handler, data):
        update = MagicMock()
        query = MagicMock()
        query.data = data
        query.from_user = MagicMock(id=92000201)
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update.callback_query = query
        await handler(update, _make_context())
        return query

    async def test_stale_confirm_button_is_answered(self):
        query = await self._press(cb_guest_confirm, "cb_guest_confirm_1")
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs.get("show_alert"))
        query.edit_message_reply_markup.assert_awaited_once()

    async def test_stale_reject_button_is_answered(self):
        query = await self._press(cb_guest_reject, "cb_guest_reject_1")
        query.answer.assert_awaited_once()
        query.edit_message_reply_markup.assert_awaited_once()


class TestRiskEngineDeadlineBranch(unittest.TestCase):
    """P0-3 — `services/risk_engine.py`: `datetime` was used without being imported."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(name=f"Risk Div {self.uid}", code=f"RKD_{self.uid}")

        self.p1 = 92000301
        self.p2 = 92000302
        self.bettor = 92000303
        database.register_user(self.p1, f"rp1_{self.uid}", team_name=f"RiskA_{self.uid}")
        database.register_user(self.p2, f"rp2_{self.uid}", team_name=f"RiskB_{self.uid}")
        database.register_user(self.bettor, f"rbet_{self.uid}", team_name=f"RiskC_{self.uid}")
        database.get_or_create_wallet(self.bettor)

        self.round_number = 91000 + (int(self.uid, 16) % 1000)

    def _make_round_with_deadline(self, deadline: str) -> int:
        database.create_round(self.round_number, deadline=deadline, division_id=self.div_id)
        with database.transaction() as conn:
            conn.execute(
                # Линия открыта, тур ещё не открыт для игры — состояние, в котором принимаются ставки.
                "UPDATE rounds SET is_open = 0, bets_open = 1, deadline = ? WHERE division_id = ? AND round_number = ?",
                (deadline, self.div_id, self.round_number),
            )
        return database.create_match(
            self.round_number, self.p1, self.p2, division_id=self.div_id
        )

    def test_evaluate_bet_rejects_after_expired_deadline(self):
        past = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        match_id = self._make_round_with_deadline(past)

        decision = RiskEngine.evaluate_bet(
            user_id=self.bettor,
            amount=100,
            selections=[{"match_id": match_id, "outcome": "p1", "odd": 2.0}],
            division_id=self.div_id,
        )

        # Before the fix this raised NameError instead of returning a decision.
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "MARKET_SUSPENDED")

    def test_evaluate_bet_passes_deadline_check_when_still_open(self):
        future = (datetime.datetime.now() + datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        match_id = self._make_round_with_deadline(future)

        decision = RiskEngine.evaluate_bet(
            user_id=self.bettor,
            amount=100,
            selections=[{"match_id": match_id, "outcome": "p1", "odd": 2.0}],
            division_id=self.div_id,
        )

        # The deadline branch must be *entered* and *survived*: whatever the engine
        # decides afterwards, it must not be a deadline rejection.
        self.assertNotEqual(decision.reason, "MARKET_SUSPENDED")

    def test_evaluate_bet_tolerates_malformed_deadline(self):
        match_id = self._make_round_with_deadline("не указан")

        decision = RiskEngine.evaluate_bet(
            user_id=self.bettor,
            amount=100,
            selections=[{"match_id": match_id, "outcome": "p1", "odd": 2.0}],
            division_id=self.div_id,
        )
        self.assertNotEqual(decision.reason, "MARKET_SUSPENDED")


class TestClubSchedulePendingSort(unittest.TestCase):
    """P0-4 — `database.py`: the pending-fixture sort referenced a comprehension variable."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(name=f"Sched Div {self.uid}", code=f"SCD_{self.uid}")

        self.p1 = 92000401
        self.p2 = 92000402
        self.team1 = f"SchedA_{self.uid}"
        self.team2 = f"SchedB_{self.uid}"
        database.register_user(self.p1, f"sp1_{self.uid}", team_name=self.team1)
        database.register_user(self.p2, f"sp2_{self.uid}", team_name=self.team2)

        # Three pending rounds, deliberately created out of order so the sort has work to do.
        base = 92000 + (int(self.uid, 16) % 1000)
        self.rounds = [base + 2, base, base + 1]
        for rn in self.rounds:
            database.create_round(rn, division_id=self.div_id)
            match_id = database.create_match(rn, self.p1, self.p2, division_id=self.div_id)
            # get_club_schedule_and_results matches on the denormalized team columns,
            # which create_match does not populate.
            with database.transaction() as conn:
                conn.execute(
                    "UPDATE matches SET player1_team = ?, player2_team = ? WHERE id = ?",
                    (self.team1, self.team2, match_id),
                )
                conn.execute(
                    "UPDATE rounds SET is_open = 1 WHERE round_number = ? AND division_id = ?",
                    (rn, self.div_id),
                )

    def test_club_schedule_sorts_pending_fixtures(self):
        # Before the fix this raised NameError as soon as pending fixtures existed.
        result = database.get_club_schedule_and_results(self.team1)

        self.assertGreaterEqual(result["pending_count"], 3)

        mine = [m for m in result["matches"] if m["round_number"] in self.rounds]
        self.assertEqual(len(mine), 3)

        # Nearest upcoming round first — ascending, per the inline comment.
        numbers = [m["round_number"] for m in mine]
        self.assertEqual(numbers, sorted(self.rounds))
        self.assertTrue(all(not m["is_completed"] for m in mine))


if __name__ == "__main__":
    unittest.main()
