"""
services/live_state_machine.py

Logovo.bet — Strict Match State Machine for Live Betting & Match Lifecycle.
Enforces valid state transitions, preventing illegal progressions (e.g. FINISHED -> LIVE).
All transitions are validated, audited, and idempotent.
"""

import logging
from typing import Optional
import database

logger = logging.getLogger(__name__)

# Valid Lifecycle States
SCHEDULED = "SCHEDULED"
PRE_MATCH = "PRE_MATCH"
LIVE = "LIVE"
HALFTIME = "HALFTIME"
FINISHED = "FINISHED"
POSTPONED = "POSTPONED"
CANCELLED = "CANCELLED"
ABANDONED = "ABANDONED"
SUSPENDED = "SUSPENDED"

ALL_STATES = {
    SCHEDULED, PRE_MATCH, LIVE, HALFTIME, FINISHED,
    POSTPONED, CANCELLED, ABANDONED, SUSPENDED
}

TERMINAL_STATES = {FINISHED, CANCELLED, ABANDONED}

# Whitelist of allowed state transitions
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    SCHEDULED: {PRE_MATCH, POSTPONED, CANCELLED},
    PRE_MATCH: {LIVE, POSTPONED, CANCELLED},
    LIVE: {HALFTIME, SUSPENDED, FINISHED, ABANDONED},
    HALFTIME: {LIVE, ABANDONED},
    SUSPENDED: {LIVE, ABANDONED, CANCELLED},
    POSTPONED: {SCHEDULED, CANCELLED},
    FINISHED: set(),   # Terminal
    CANCELLED: set(),  # Terminal
    ABANDONED: set(),  # Terminal
}


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal match lifecycle transition is attempted."""
    pass


def can_transition(current_state: str, new_state: str, force: bool = False) -> bool:
    """Check whether transitioning from current_state to new_state is permissible."""
    curr = current_state.upper()
    nxt = new_state.upper()

    if nxt not in ALL_STATES:
        return False

    if curr == nxt:
        return True  # Idempotent no-op

    if force:
        return True  # Explicit admin override / correction flow

    return nxt in ALLOWED_TRANSITIONS.get(curr, set())


def transition_live_match(
    match_id: int,
    new_status: str,
    source: str = "provider",
    actor_id: Optional[int] = None,
    reason: Optional[str] = None,
    force: bool = False
) -> tuple[bool, str]:
    """
    Safely and idempotently transition a match to a new state.
    Updates both live_match_states and matches records atomically.
    Raises InvalidStateTransitionError if the transition is prohibited.
    """
    new_status = new_status.upper()
    if new_status not in ALL_STATES:
        raise InvalidStateTransitionError(f"Unknown match state: '{new_status}'")

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Fetch current state from live_match_states (or initialize from matches)
        cursor.execute("SELECT * FROM live_match_states WHERE match_id = ?", (match_id,))
        live_row = cursor.fetchone()

        if not live_row:
            # Check matches table to initialize
            cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
            m_row = cursor.fetchone()
            if not m_row:
                raise ValueError(f"Match #{match_id} does not exist.")

            div_id = m_row["division_id"] if "division_id" in m_row.keys() and m_row["division_id"] else 1
            season_id = m_row["season_id"] if "season_id" in m_row.keys() and m_row["season_id"] else 1
            curr_status = "SCHEDULED"

            cursor.execute("""
                INSERT INTO live_match_states (match_id, season_id, division_id, status, period, home_score, away_score, provider, last_updated_at)
                VALUES (?, ?, ?, ?, 'pre_match', ?, ?, ?, datetime('now', '+3 hours'))
            """, (match_id, season_id, div_id, curr_status, m_row["player1_score"] or 0, m_row["player2_score"] or 0, source))
            curr_state = curr_status
            version = 1
        else:
            curr_state = (live_row["status"] or "SCHEDULED").upper()
            version = live_row["version"]

        # 2. Validate transition
        if curr_state == new_status:
            return True, f"Match #{match_id} is already in state {curr_state}."

        if not can_transition(curr_state, new_status, force=force):
            msg = f"Illegal transition from '{curr_state}' to '{new_status}' for match #{match_id}."
            logger.warning(msg)
            raise InvalidStateTransitionError(msg)

        # 3. Derive period label
        period_map = {
            SCHEDULED: "pre_match",
            PRE_MATCH: "pre_match",
            LIVE: "1h" if curr_state in (SCHEDULED, PRE_MATCH) else "2h",
            HALFTIME: "ht",
            FINISHED: "ft",
            POSTPONED: "postponed",
            CANCELLED: "cancelled",
            ABANDONED: "abandoned",
            SUSPENDED: "suspended",
        }
        new_period = period_map.get(new_status, "live")

        # 4. Update live_match_states
        cursor.execute("""
            UPDATE live_match_states
            SET status = ?, period = ?, version = version + 1, last_updated_at = datetime('now', '+3 hours')
            WHERE match_id = ?
        """, (new_status, new_period, match_id))

        # 5. Sync matches table status
        # Map to matches.status semantics
        legacy_status_map = {
            SCHEDULED: "scheduled",
            PRE_MATCH: "pending",
            LIVE: "live",
            HALFTIME: "live",
            FINISHED: "completed",
            POSTPONED: "postponed",
            CANCELLED: "cancelled",
            ABANDONED: "cancelled",
            SUSPENDED: "pending",
        }
        legacy_status = legacy_status_map.get(new_status, "live")
        is_live_flag = 1 if new_status in (LIVE, HALFTIME) else 0

        cursor.execute("""
            UPDATE matches
            SET status = ?
            WHERE id = ?
        """, (legacy_status, match_id))

        # 6. Audit log
        div_id = live_row["division_id"] if live_row and "division_id" in live_row.keys() else 1
        season_id = live_row["season_id"] if live_row and "season_id" in live_row.keys() else 1
        cursor.execute("""
            INSERT INTO bet_audit_log (actor_id, action, entity_type, entity_id, old_value, new_value, division_id, season_id, created_at)
            VALUES (?, 'match_status_transition', 'match', ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
        """, (actor_id or 0, match_id, curr_state, new_status, div_id, season_id))

        logger.info(f"Match #{match_id} state changed: {curr_state} -> {new_status} (source: {source})")
        return True, f"Successfully transitioned match #{match_id} to {new_status}."
