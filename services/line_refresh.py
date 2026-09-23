"""
services/line_refresh.py

Throttled, off-loop repricing of a round's betting line.

`/api/markets/tours` used to call `generate_round_markets` and then
`generate_match_markets` for every line match on *each* request, synchronously
on the shared bot/API event loop. Every lobby open repriced the whole line and
stalled the bot for the duration.

`ensure_round_line` runs that same work in a worker thread, at most once per
`LINE_REFRESH_TTL_SECONDS` per (round, division, season). Concurrent viewers of
the same round share one in-flight refresh instead of each starting their own,
and a failed refresh is not remembered, so the next request retries it.
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Tuple

import database
import services.odds_engine as odds_engine
from services.betting_engine import generate_round_markets

logger = logging.getLogger(__name__)

# Repricing is smoothed to ±15% per step anyway, so a minute-old line is not stale.
LINE_REFRESH_TTL_SECONDS = 60.0

# Played matches keep their market rows but are no longer priced.
_FINISHED_MATCH_STATUSES = ("confirmed", "completed", "finished", "cancelled")

_Key = Tuple[int, Optional[int], Optional[int]]

_last_refresh: Dict[_Key, float] = {}
_in_flight: Dict[_Key, asyncio.Future] = {}


def _refresh_round_line(round_number: int, division_id: Optional[int], season_id: Optional[int]) -> None:
    """Blocking: prices the round's line and every open match already in it."""
    try:
        generate_round_markets(round_number, division_id=division_id, season_id=season_id)
    except Exception as e:
        logger.debug(f"Could not generate round markets for tour #{round_number}: {e}")

    markets = database.get_active_bet_markets(round_number, division_id=division_id, season_id=season_id)
    for m in markets:
        if (m.get("match_status") or "pending") in _FINISHED_MATCH_STATUSES:
            continue
        try:
            odds_engine.generate_match_markets(m["match_id"], m["team1_name"], m["team2_name"])
        except Exception as e:
            logger.debug(f"Could not generate relational markets for match #{m['match_id']}: {e}")


def _on_refresh_done(key: _Key, task: asyncio.Future) -> None:
    if _in_flight.get(key) is task:
        _in_flight.pop(key, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        _last_refresh[key] = time.monotonic()
    else:
        logger.warning(f"Line refresh failed for tour #{key[0]} (division {key[1]}): {exc}")


async def ensure_round_line(round_number: int, division_id: Optional[int] = None,
                            season_id: Optional[int] = None) -> None:
    """Refreshes the round's line in a thread unless it was refreshed recently.

    Never raises: a failed refresh leaves the previous line in place. The refresh
    is shielded, so a viewer who disconnects does not cancel it for the others.
    """
    key: _Key = (round_number, division_id, season_id)

    task = _in_flight.get(key)
    if task is None:
        last = _last_refresh.get(key)
        if last is not None and time.monotonic() - last < LINE_REFRESH_TTL_SECONDS:
            return
        task = asyncio.ensure_future(
            asyncio.to_thread(_refresh_round_line, round_number, division_id, season_id)
        )
        _in_flight[key] = task
        task.add_done_callback(lambda t: _on_refresh_done(key, t))

    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass  # already logged by _on_refresh_done


def reset() -> None:
    """Forgets every throttle window — tests and admin-side line rebuilds."""
    _last_refresh.clear()
    _in_flight.clear()
