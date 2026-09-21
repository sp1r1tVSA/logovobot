"""Чистая политика долгов: services.debt_policy без базы и без Telegram."""

import datetime as dt

import pytest

import config
from services import debt_policy as dp

NOW = dt.datetime(2026, 9, 22, 22, 0)


def rnd(is_open=0, deadline=None, status=None, closed_at=None):
    return {"is_open": is_open, "deadline": deadline, "status": status, "closed_at": closed_at}


class TestRoundStatus:
    def test_is_open_wins(self):
        assert dp.round_status(rnd(is_open=1, status="closed")) == dp.ROUND_OPEN

    def test_stored_status(self):
        assert dp.round_status(rnd(status="scheduled", deadline="2026-09-20 22:00:00")) == dp.ROUND_SCHEDULED
        assert dp.round_status(rnd(status="closed")) == dp.ROUND_CLOSED

    def test_legacy_rows_inferred_from_deadline(self):
        assert dp.round_status(rnd(deadline="20.09.2026 22:00")) == dp.ROUND_CLOSED
        assert dp.round_status(rnd()) == dp.ROUND_SCHEDULED
        assert dp.round_status(None) == dp.ROUND_SCHEDULED

    def test_phase_overdue(self):
        assert dp.round_phase(rnd(1, "22.09.2026 21:00"), NOW) == dp.ROUND_OVERDUE
        assert dp.round_phase(rnd(1, "22.09.2026 23:00"), NOW) == dp.ROUND_OPEN


class TestDebtTerms:
    def test_scheduled_round_never_debt(self):
        assert dp.debt_terms(rnd(status="scheduled", deadline="01.09.2026 12:00"), NOW) is None

    def test_open_round_without_deadline_not_debt(self):
        assert dp.debt_terms(rnd(is_open=1), NOW) is None

    def test_open_round_before_deadline_not_debt(self):
        assert dp.debt_terms(rnd(1, "23.09.2026 22:00"), NOW) is None

    def test_deadline_passed(self):
        t = dp.debt_terms(rnd(1, "21.09.2026 22:00"), NOW)
        assert t.became_debt_at == dt.datetime(2026, 9, 21, 22, 0)
        assert t.grace_hours == 0
        assert t.escalate_at == t.became_debt_at + dt.timedelta(hours=config.DEBT_ESCALATION_HOURS)

    def test_closed_after_deadline_counts_from_deadline(self):
        t = dp.debt_terms(rnd(0, "21.09.2026 22:00", "closed", "22.09.2026 10:00"), NOW)
        assert t.became_debt_at == dt.datetime(2026, 9, 21, 22, 0)
        assert t.grace_hours == 0

    def test_early_close_example_from_rules(self):
        """Открыт 21.09 00:00, дедлайн 23.09 22:00, закрыт 22.09 22:00 → 48 + 24 ч."""
        t = dp.debt_terms(rnd(0, "23.09.2026 22:00", "closed", "22.09.2026 22:00"), NOW)
        assert t.became_debt_at == dt.datetime(2026, 9, 22, 22, 0)
        assert t.grace_hours == 24
        assert t.escalate_at == dt.datetime(2026, 9, 22, 22, 0) + dt.timedelta(hours=24 + config.DEBT_ESCALATION_HOURS)

    def test_grace_rounded_up_to_hour(self):
        t = dp.early_close_terms(dt.datetime(2026, 9, 23, 22, 0), dt.datetime(2026, 9, 22, 22, 40))
        assert t.grace_hours == 24  # 23 ч 20 мин → 24

    def test_close_at_or_after_deadline_has_no_grace(self):
        d = dt.datetime(2026, 9, 23, 22, 0)
        assert dp.early_close_terms(d, d) is None
        assert dp.early_close_terms(None, d) is None


class TestFreezeAndHours:
    def test_frozen_shifts_escalation(self):
        t = dp.terms_for(dt.datetime(2026, 9, 21, 22, 0))
        m = {"frozen_seconds": 3600, "is_extended": 1, "frozen_at": "2026-09-22 20:00:00"}
        frozen = dp.frozen_seconds(m, NOW)
        assert frozen == pytest.approx(3 * 3600)
        assert dp.effective_escalate_at(t, frozen) == t.escalate_at + dt.timedelta(hours=3)
        assert dp.hours_overdue(t, frozen, NOW) == pytest.approx(24 - 3)

    def test_hours_overdue_never_negative(self):
        t = dp.terms_for(NOW + dt.timedelta(hours=1))
        assert dp.hours_overdue(t, 0, NOW) == 0.0


class TestIsDebt:
    def test_debt_row_wins(self):
        assert dp.is_debt({"status": "pending"}, rnd(status="scheduled"), {"match_id": 1}, NOW)

    def test_pending_after_deadline(self):
        assert dp.is_debt({"status": "pending"}, rnd(1, "21.09.2026 22:00"), None, NOW)

    def test_played_before_deadline_is_not_debt(self):
        m = {"status": "confirmed", "played_at": "2026-09-21 20:00:00"}
        assert not dp.is_debt(m, rnd(1, "21.09.2026 22:00"), None, NOW)

    def test_played_after_deadline_is_debt(self):
        m = {"status": "confirmed", "played_at": "2026-09-22 10:00:00"}
        assert dp.is_debt(m, rnd(1, "21.09.2026 22:00"), None, NOW)

    def test_stored_terms_win_over_round(self):
        row = {"became_debt_at": "2026-09-22 22:00:00", "grace_hours": 24, "escalate_at": "2026-09-25 22:00:00"}
        t = dp.resolve_terms(row, rnd(0, "23.09.2026 22:00", "closed"), NOW)
        assert t.grace_hours == 24
        assert t.escalate_at == dt.datetime(2026, 9, 25, 22, 0)
