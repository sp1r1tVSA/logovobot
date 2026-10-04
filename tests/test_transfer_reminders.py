"""Напоминания трансферного окна: перед автозакрытием и о зависших заявках."""

import asyncio
import datetime as dt
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import database
from time_utils import DT_FORMAT, now_msk
from transfers import jobs, reminders, repo

MANAGER = 777


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in (
            "transfer_reminders", "transfer_squad_ops", "transfer_slot_purchases",
            "transfer_club_budgets", "transfer_core_snapshot", "transfer_players", "transfer_topics",
            "transfer_topics_ext", "transfer_sanctions", "squad_players", "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    yield


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _at(delta: dt.timedelta) -> str:
    return (now_msk() + delta).strftime(DT_FORMAT)


def _window(closes_in=dt.timedelta(minutes=50)):
    wid = repo.create_window(1, 10, title="ТО Зима")
    repo.open_window(wid, 1)
    repo.update_window_settings(wid, {"auto_close_at": _at(closes_in)})
    _user(101, "city", "Манчестер Сити")
    _user(102, "chelsea", "Челси")
    repo.set_club_budget(wid, "Манчестер Сити", 100000, 1)
    repo.set_club_budget(wid, "Челси", 100000, 1)
    return wid


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    return bot


def _sent(bot):
    return {c.kwargs["chat_id"]: c.kwargs["text"] for c in bot.send_message.await_args_list}


def _run(bot, now=None):
    return asyncio.run(reminders.run(bot, now))


def _age(transfer_id, hours):
    stamp = (now_msk() - dt.timedelta(hours=hours)).strftime(DT_FORMAT)
    with database.transaction() as conn:
        conn.execute("UPDATE transfers SET created_at = ?, updated_at = ? WHERE id = ?",
                     (stamp, stamp, transfer_id))


class TestDueStep:
    def test_far_from_close_nothing(self):
        wid = _window(dt.timedelta(days=3))
        assert reminders.due_close_step(repo.get_window(wid)) is None

    def test_day_step(self):
        wid = _window(dt.timedelta(hours=20))
        tag, label = reminders.due_close_step(repo.get_window(wid))
        assert tag.startswith("close24h@") and label == "сутки"

    def test_hour_step_wins_over_day(self):
        wid = _window(dt.timedelta(minutes=40))
        assert reminders.due_close_step(repo.get_window(wid))[0].startswith("close1h@")

    def test_no_deadline_or_past_or_not_open(self):
        wid = _window(dt.timedelta(hours=1))
        window = dict(repo.get_window(wid))
        assert reminders.due_close_step({**window, "auto_close_at": None}) is None
        assert reminders.due_close_step({**window, "auto_close_at": _at(-dt.timedelta(minutes=5))}) is None
        assert reminders.due_close_step({**window, "status": "closed"}) is None

    def test_moving_deadline_changes_tag(self):
        wid = _window(dt.timedelta(hours=20))
        first = reminders.due_close_step(repo.get_window(wid))[0]
        repo.update_window_settings(wid, {"auto_close_at": _at(dt.timedelta(hours=21))})
        assert reminders.due_close_step(repo.get_window(wid))[0] != first


class TestCloseReminders:
    def test_coaches_get_dm_with_slots_and_budget(self):
        _window()
        bot = _bot()
        assert _run(bot) == 2
        sent = _sent(bot)
        assert set(sent) == {101, 102}
        assert "Окно закроется" in sent[101] and "Манчестер Сити" in sent[101]
        assert "0/3" in sent[101] and "100" in sent[101]

    def test_sent_once_per_deadline(self):
        _window()
        bot = _bot()
        _run(bot)
        before = bot.send_message.await_count
        assert _run(bot) == 0
        assert bot.send_message.await_count == before

    def test_new_deadline_rearms(self):
        wid = _window()
        bot = _bot()
        _run(bot)
        repo.update_window_settings(wid, {"auto_close_at": _at(dt.timedelta(minutes=30))})
        assert _run(bot) == 2

    def test_day_and_hour_are_separate(self):
        wid = _window(dt.timedelta(hours=20))
        bot = _bot()
        assert _run(bot) == 2
        repo.update_window_settings(wid, {"auto_close_at": _at(dt.timedelta(minutes=50))})
        assert _run(bot) == 2

    def test_nothing_before_the_step(self):
        _window(dt.timedelta(days=3))
        bot = _bot()
        assert _run(bot) == 0
        bot.send_message.assert_not_awaited()

    def test_incoming_offer_listed_for_the_other_side_only(self):
        wid = _window()
        repo.insert_transfer(wid, "deal", "Rodri", "pending_counterparty", from_club="Челси",
                             to_club="Манчестер Сити", from_user=102, to_user=101, price_k=20000,
                             ovr=105, initiator_id=101)
        bot = _bot()
        _run(bot)
        sent = _sent(bot)
        assert "Ждут вашего ответа" in sent[102] and "Rodri" in sent[102]
        assert "Ждут вашего ответа" not in sent[101]

    def test_coach_with_nothing_to_do_is_skipped(self):
        wid = _window()
        for i in range(3):
            repo.insert_transfer(wid, "urn_buy", f"Buy{i}", "approved", to_club="Манчестер Сити",
                                 to_user=101, price_k=1000, ovr=90, initiator_id=101)
            repo.insert_transfer(wid, "urn_sale", f"Sell{i}", "approved", from_club="Манчестер Сити",
                                 from_user=101, price_k=1000, ovr=90, initiator_id=101)
        bot = _bot()
        _run(bot)
        assert 101 not in _sent(bot) and 102 in _sent(bot)

    def test_sanctioned_coach_skipped(self, monkeypatch):
        _window()
        monkeypatch.setattr(reminders.sanctions, "for_user",
                            lambda user_id, club: {"id": 1} if user_id == 101 else None)
        bot = _bot()
        _run(bot)
        assert 101 not in _sent(bot) and 102 in _sent(bot)

    def test_manager_gets_pending_summary(self):
        wid = _window()
        repo.insert_transfer(wid, "deal", "Rodri", "pending_manager", from_club="Челси",
                             to_club="Манчестер Сити", from_user=102, to_user=101, price_k=20000,
                             ovr=105, initiator_id=101)
        repo.insert_transfer(wid, "deal", "Foden", "pending_counterparty", from_club="Манчестер Сити",
                             to_club="Челси", from_user=101, to_user=102, price_k=20000,
                             ovr=105, initiator_id=101)
        bot = _bot()
        _run(bot)
        text = _sent(bot)[MANAGER]
        assert "Ждут вашего решения" in text and "Rodri" in text
        assert "Не подтверждены" in text

    def test_manager_not_pinged_when_queue_empty(self):
        _window()
        bot = _bot()
        _run(bot)
        assert MANAGER not in _sent(bot)

    def test_no_window_is_a_noop(self):
        bot = _bot()
        assert _run(bot) == 0


class TestStaleNudge:
    def _pending(self, wid, name="Rodri"):
        return repo.insert_transfer(wid, "deal", name, "pending_manager", from_club="Челси",
                                    to_club="Манчестер Сити", from_user=102, to_user=101,
                                    price_k=20000, ovr=105, initiator_id=101)

    def test_old_request_nudges_manager_once(self):
        wid = _window(dt.timedelta(days=5))
        tid = self._pending(wid)
        _age(tid, 13)
        bot = _bot()
        assert _run(bot) == 1
        assert "Rodri" in _sent(bot)[MANAGER]
        assert _run(bot) == 0

    def test_fresh_request_is_left_alone(self):
        wid = _window(dt.timedelta(days=5))
        _age(self._pending(wid), 2)
        bot = _bot()
        assert _run(bot) == 0
        bot.send_message.assert_not_awaited()

    def test_only_manager_queue_counts(self):
        wid = _window(dt.timedelta(days=5))
        tid = repo.insert_transfer(wid, "deal", "Rodri", "pending_counterparty", from_club="Челси",
                                   to_club="Манчестер Сити", from_user=102, to_user=101,
                                   price_k=1, ovr=90, initiator_id=101)
        _age(tid, 30)
        assert _run(_bot()) == 0

    def test_new_stale_request_nudged_separately(self):
        wid = _window(dt.timedelta(days=5))
        _age(self._pending(wid, "Rodri"), 13)
        bot = _bot()
        _run(bot)
        _age(self._pending(wid, "Foden"), 14)
        assert _run(bot) == 1
        assert "Foden" in list(_sent(bot).values())[-1]

    def test_works_after_window_closed(self):
        wid = _window(dt.timedelta(days=5))
        _age(self._pending(wid), 20)
        repo.close_window(wid, 1)
        assert _run(_bot()) == 1


class TestClaim:
    def test_claim_is_unique_per_window_and_tag(self):
        wid = _window()
        assert repo.claim_reminder(wid, "x") is True
        assert repo.claim_reminder(wid, "x") is False
        assert repo.claim_reminder(wid, "y") is True


class TestJobWiring:
    def test_job_calls_run(self, monkeypatch):
        run = AsyncMock(return_value=0)
        monkeypatch.setattr(reminders, "run", run)
        bot = _bot()
        asyncio.run(jobs.job_reminders(MagicMock(bot=bot)))
        run.assert_awaited_once_with(bot)

    def test_job_registered_in_main(self):
        import main

        source = inspect.getsource(main)
        assert "transfer_reminders" in source and "job_reminders" in source
