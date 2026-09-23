"""
Throttled off-loop line repricing used by GET /api/markets/tours.

`services/line_refresh.ensure_round_line` must price a round at most once per
TTL, share one in-flight refresh between concurrent viewers, and forget a
failed refresh so the next request retries it.
"""

import asyncio
import threading
from unittest.mock import patch

from services import line_refresh


def _run(coro):
    return asyncio.run(coro)


class _Recorder:
    def __init__(self, fail=False, delay=0.0):
        self.calls = []
        self.threads = set()
        self.fail = fail
        self.delay = delay

    def __call__(self, round_number, division_id, season_id):
        import time
        self.calls.append((round_number, division_id, season_id))
        self.threads.add(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")


def test_second_request_within_ttl_is_skipped():
    rec = _Recorder()

    async def scenario():
        await line_refresh.ensure_round_line(3, division_id=1, season_id=1)
        await line_refresh.ensure_round_line(3, division_id=1, season_id=1)

    with patch.object(line_refresh, "_refresh_round_line", rec):
        _run(scenario())
    assert rec.calls == [(3, 1, 1)]


def test_refresh_runs_off_the_event_loop_thread():
    rec = _Recorder()
    with patch.object(line_refresh, "_refresh_round_line", rec):
        _run(line_refresh.ensure_round_line(1, division_id=1, season_id=1))
    assert threading.get_ident() not in rec.threads


def test_concurrent_viewers_share_one_refresh():
    rec = _Recorder(delay=0.05)

    async def scenario():
        await asyncio.gather(*[
            line_refresh.ensure_round_line(5, division_id=2, season_id=1) for _ in range(5)
        ])

    with patch.object(line_refresh, "_refresh_round_line", rec):
        _run(scenario())
    assert rec.calls == [(5, 2, 1)]


def test_each_round_and_division_has_its_own_window():
    rec = _Recorder()

    async def scenario():
        await line_refresh.ensure_round_line(1, division_id=1, season_id=1)
        await line_refresh.ensure_round_line(2, division_id=1, season_id=1)
        await line_refresh.ensure_round_line(1, division_id=2, season_id=1)

    with patch.object(line_refresh, "_refresh_round_line", rec):
        _run(scenario())
    assert len(rec.calls) == 3


def test_failed_refresh_is_retried_and_never_raises():
    rec = _Recorder(fail=True)

    async def scenario():
        await line_refresh.ensure_round_line(4, division_id=1, season_id=1)
        await line_refresh.ensure_round_line(4, division_id=1, season_id=1)

    with patch.object(line_refresh, "_refresh_round_line", rec):
        _run(scenario())
    assert len(rec.calls) == 2


def test_expired_window_refreshes_again():
    rec = _Recorder()

    async def scenario():
        await line_refresh.ensure_round_line(6, division_id=1, season_id=1)
        with patch.object(line_refresh, "LINE_REFRESH_TTL_SECONDS", 0.0):
            await line_refresh.ensure_round_line(6, division_id=1, season_id=1)

    with patch.object(line_refresh, "_refresh_round_line", rec):
        _run(scenario())
    assert len(rec.calls) == 2
