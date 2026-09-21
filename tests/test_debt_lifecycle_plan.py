"""Планировщик трекера долгов (`services.debt_lifecycle`) и то, как трекер его исполняет."""

import asyncio
import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
from services import debt_lifecycle as dl
from services.debt_lifecycle import plan_debt_actions

T0 = datetime.datetime(2026, 9, 23, 22, 0)  # стал долгом
ESC = T0 + datetime.timedelta(hours=config.DEBT_ESCALATION_HOURS)


def _h(hours: float) -> datetime.datetime:
    return T0 + datetime.timedelta(hours=hours)


def _fmt(moment: datetime.datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else None


def _match(debt=None, escalate_at=ESC, **extra) -> dict:
    m = {"id": 1, "division_id": 1, "escalate_at": escalate_at, "debt": dict(debt or {})}
    m.update(extra)
    return m


def simulate(until_hours: int, step_hours: float = 0.5, escalate_at=ESC) -> list[tuple[float, str]]:
    """Прогнать трекер каждые полчаса с идеальной доставкой; вернуть (час, действие)."""
    debt: dict = {}
    log: list[tuple[float, str]] = []
    t = 0.0
    while t <= until_hours:
        now = _h(t)
        for action in plan_debt_actions(_match(debt, escalate_at), now):
            log.append((t, action))
            ts = _fmt(now)
            if action in (dl.NOTIFY, dl.REMIND):
                debt["last_reminder_at"] = ts
            elif action == dl.SOFT_WARN:
                debt["soft_warned_at"] = debt["last_reminder_at"] = ts
            elif action == dl.ESCALATE:
                debt["escalated_at"] = debt["last_escalation_at"] = ts
            elif action == dl.REESCALATE:
                debt["last_escalation_at"] = ts
            elif action == dl.ESCALATE_GLOBAL:
                debt["global_escalated_at"] = ts
        t += step_hours
    return log


class TestPlanTimeline(unittest.TestCase):
    def test_full_timeline_of_an_ignored_debt(self):
        log = simulate(120)
        by_action: dict[str, list[float]] = {}
        for t, a in log:
            by_action.setdefault(a, []).append(t)
        self.assertEqual(by_action[dl.NOTIFY], [0.0])
        self.assertEqual(by_action[dl.SOFT_WARN], [24.0])
        self.assertEqual(by_action[dl.ESCALATE], [48.0])
        self.assertEqual(by_action[dl.REESCALATE], [72.0, 96.0, 120.0])
        self.assertEqual(by_action[dl.ESCALATE_GLOBAL], [96.0])
        self.assertEqual(by_action[dl.REMIND], [12.0, 36.0, 48.0, 60.0, 72.0, 84.0, 96.0, 108.0, 120.0])

    def test_early_close_grace_shifts_every_stage(self):
        """Тур закрыт за 24 ч до дедлайна: срок 48+24, мягкое предупреждение на 48-м часе."""
        log = simulate(100, escalate_at=_h(72))
        firsts = {}
        for t, a in log:
            firsts.setdefault(a, t)
        self.assertEqual(firsts[dl.SOFT_WARN], 48.0)
        self.assertEqual(firsts[dl.ESCALATE], 72.0)
        self.assertEqual(firsts[dl.REESCALATE], 96.0)
        self.assertNotIn(dl.ESCALATE_GLOBAL, firsts)

    def test_first_seen_after_deadline_notifies_and_escalates_at_once(self):
        self.assertEqual(plan_debt_actions(_match(), _h(50)), [dl.NOTIFY, dl.ESCALATE])

    def test_nothing_sent_twice_in_one_window(self):
        debt = {"last_reminder_at": _fmt(_h(0))}
        self.assertEqual(plan_debt_actions(_match(debt), _h(11.5)), [])

    def test_soft_warning_is_not_sent_after_the_deadline(self):
        debt = {"last_reminder_at": _fmt(_h(0))}
        self.assertEqual(plan_debt_actions(_match(debt), _h(49)), [dl.REMIND, dl.ESCALATE])


class TestPlanExtension(unittest.TestCase):
    def test_active_extension_freezes_everything(self):
        m = _match(is_extended=1, extended_until=_fmt(_h(80)))
        self.assertEqual(plan_debt_actions(m, _h(60)), [])

    def test_escalated_and_extended_gets_no_reescalation(self):
        debt = {"escalated_at": _fmt(_h(48)), "last_escalation_at": _fmt(_h(48)),
                "last_reminder_at": _fmt(_h(48))}
        m = _match(debt, is_extended=1, extended_until=_fmt(_h(200)))
        self.assertEqual(plan_debt_actions(m, _h(100)), [])

    def test_expired_extension_resumes_on_the_shifted_clock(self):
        # Заморожен на 24 ч: escalate_at уже сдвинут на 72-й час.
        debt = {"last_reminder_at": _fmt(_h(30))}
        m = _match(debt, escalate_at=_h(72), is_extended=1, extended_until=_fmt(_h(54)))
        self.assertEqual(plan_debt_actions(m, _h(55)), [dl.EXPIRE_EXTENSION, dl.SOFT_WARN])

    def test_missing_escalation_moment_only_handles_the_extension(self):
        m = _match(escalate_at=None, is_extended=1, extended_until=_fmt(_h(1)))
        self.assertEqual(plan_debt_actions(m, _h(2)), [dl.EXPIRE_EXTENSION])


def _ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _view(debt: dict, hours: float) -> dict:
    return {
        "id": 77, "round_number": 3, "division_id": 5,
        "player1_team": "Барселона", "player2_team": "Бавария",
        "player1_id": 101, "player2_id": 102,
        "p1_username": "one", "p2_username": "two",
        "escalate_at": ESC, "grace_hours": 0,
        "hours_overdue": hours, "hours_to_escalation": 48 - hours,
        "debt": debt,
    }


class TestTrackerExecutesThePlan(unittest.TestCase):
    def _run(self, view, now, participants=None, admins=(900,), fail_send=False):
        from handlers import admin as admin_handlers

        ctx = _ctx()
        if fail_send:
            ctx.bot.send_message.side_effect = RuntimeError("blocked")
        players = [{"id": 101, "warns": 1}, {"id": 102, "warns": 0}] if participants is None else participants
        with patch("database.sync_match_debts"), \
             patch("database.get_detailed_overdue_matches", return_value=[view]), \
             patch("database.mark_debt_stage", return_value=True) as mark, \
             patch("handlers.admin.now_msk", return_value=now), \
             patch("handlers.admin._debt_participants", AsyncMock(return_value=players)), \
             patch("handlers.admin._resolve_debt_admins", AsyncMock(return_value=list(admins))), \
             patch("handlers.admin.database.get_division", return_value={"name": "Div"}), \
             patch.object(config, "ADMIN_IDS", [900, 901]):
            asyncio.run(admin_handlers._run_debt_lifecycle_tracker(ctx))
        return ctx, [c.args[1] for c in mark.call_args_list]

    def test_first_run_dms_both_players_once(self):
        ctx, marks = self._run(_view({"state": "active"}, 1), _h(1))
        self.assertEqual(marks, ["reminded"])
        chats = sorted(c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list)
        self.assertEqual(chats, [101, 102])
        text = ctx.bot.send_message.await_args_list[0].kwargs["text"]
        self.assertIn("25.09.2026 22:00", text)
        self.assertIn("1/4", text)

    def test_escalation_goes_to_division_admins_and_is_marked(self):
        debt = {"state": "active", "last_reminder_at": _fmt(_h(40))}
        ctx, marks = self._run(_view(debt, 48.5), _h(48.5))
        self.assertEqual(marks, ["escalated"])
        self.assertEqual([c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list], [900])

    def test_global_escalation_skips_admins_who_already_have_the_card(self):
        debt = {"state": "escalated", "last_reminder_at": _fmt(_h(95)),
                "escalated_at": _fmt(_h(48)), "last_escalation_at": _fmt(_h(90))}
        ctx, marks = self._run(_view(debt, 96.5), _h(96.5))
        self.assertEqual(marks, ["global_escalated"])
        self.assertEqual([c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list], [901])

    def test_failed_delivery_is_not_marked(self):
        _, marks = self._run(_view({"state": "active"}, 1), _h(1), fail_send=True)
        self.assertEqual(marks, [])

    def test_club_without_owner_still_reaches_the_other_player(self):
        ctx, marks = self._run(_view({"state": "active"}, 1), _h(1), participants=[{"id": 101, "warns": 0}])
        self.assertEqual(marks, ["reminded"])
        self.assertEqual([c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list], [101])

    def test_debt_without_row_is_skipped(self):
        view = _view({}, 1)
        view["debt"] = None
        ctx, marks = self._run(view, _h(1))
        self.assertEqual(marks, [])
        ctx.bot.send_message.assert_not_awaited()


class TestMarkDebtStage(unittest.TestCase):
    def test_unknown_stage_is_rejected(self):
        import database
        with self.assertRaises(ValueError):
            database.mark_debt_stage(1, "admin_escalated_48h")


if __name__ == "__main__":
    unittest.main()
