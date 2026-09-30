"""Реестр здоровья джоб: services/job_health.py."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from services import job_health
from time_utils import now_msk


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    job_health.reset()
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])
    monkeypatch.setattr(config, "JOB_ALERT_AFTER_FAILURES", 3)
    monkeypatch.setattr(config, "JOB_ALERT_COOLDOWN_HOURS", 6)
    yield
    job_health.reset()


def _context():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))


async def _ok(context):
    return "done"


async def _boom(context):
    raise RuntimeError("provider down")


def _run(wrapper, context):
    return asyncio.run(wrapper(context))


def _state(name):
    return job_health._jobs[name]


def test_success_is_counted():
    wrapper = job_health.tracked("sync", _ok, 60)
    assert _run(wrapper, _context()) == "done"
    state = _state("sync")
    assert state.runs == 1 and state.failures == 0
    assert state.last_ok is not None and state.running is False
    assert job_health.job_status(state) == "ok"


def test_failure_is_reraised_and_recorded():
    wrapper = job_health.tracked("sync", _boom, 60)
    with pytest.raises(RuntimeError):
        _run(wrapper, _context())
    state = _state("sync")
    assert state.failures == 1 and state.consecutive_failures == 1
    assert "RuntimeError: provider down" in state.last_error
    assert job_health.job_status(state) == "failing"


def test_alert_after_threshold_then_cooldown_then_recovery():
    ctx = _context()
    fail = job_health.tracked("sync", _boom, 60)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            _run(fail, ctx)
    ctx.bot.send_message.assert_not_awaited()

    with pytest.raises(RuntimeError):
        _run(fail, ctx)
    # One DM per global admin.
    assert ctx.bot.send_message.await_count == 2
    chats = {c.kwargs["chat_id"] for c in ctx.bot.send_message.await_args_list}
    assert chats == {111, 222}
    assert "sync" in ctx.bot.send_message.await_args.kwargs["text"]

    # Within the cooldown: no repeat.
    with pytest.raises(RuntimeError):
        _run(fail, ctx)
    assert ctx.bot.send_message.await_count == 2

    # After the cooldown: repeat.
    _state("sync").last_alert_at = now_msk() - timedelta(hours=7)
    with pytest.raises(RuntimeError):
        _run(fail, ctx)
    assert ctx.bot.send_message.await_count == 4

    # Recovery: one message per admin, counters cleared.
    ok = job_health.tracked("sync", _ok, 60)
    _run(ok, ctx)
    assert ctx.bot.send_message.await_count == 6
    assert "восстановилась" in ctx.bot.send_message.await_args.kwargs["text"]
    state = _state("sync")
    assert state.consecutive_failures == 0 and state.alerted is False

    # A plain success afterwards says nothing.
    _run(ok, ctx)
    assert ctx.bot.send_message.await_count == 6


def test_blocked_admin_does_not_break_the_wrapper():
    ctx = _context()
    ctx.bot.send_message.side_effect = RuntimeError("bot was blocked")
    fail = job_health.tracked("sync", _boom, 60)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="provider down"):
            _run(fail, ctx)


def test_stale_and_waiting():
    state = job_health.register("slow", 60)
    now = now_msk()
    assert job_health.job_status(state, now) in ("waiting", "stale")
    state.last_start = now - timedelta(seconds=60)
    assert job_health.job_status(state, now) == "waiting"
    state.last_ok = state.last_start
    assert job_health.job_status(state, now) == "ok"
    state.last_start = now - timedelta(seconds=60 * job_health.STALE_FACTOR
                                       + job_health.STALE_GRACE_SECONDS + 1)
    assert job_health.is_stale(state, now)
    assert job_health.job_status(state, now) == "stale"


def test_snapshot_lists_jobs_and_components():
    job_health.tracked("b_job", _ok, 30)
    job_health.tracked("a_job", _ok, 60)
    job_health.record_component("api_server", False, "port busy")
    snap = job_health.snapshot()
    assert [j["name"] for j in snap["jobs"]] == ["a_job", "b_job"]
    assert snap["components"] == [
        {"name": "api_server", "ok": False, "detail": "port busy",
         "updated_at": snap["components"][0]["updated_at"]}
    ]
