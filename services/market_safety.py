"""
services/market_safety.py

Logovo.bet — Live Market Suspension Engine & In-Play Safety Guards.
Provides:
1. Configurable LIVE_EVENT_SUSPEND_RULES (goal, penalty, VAR, red cards).
2. Automated market suspension and safe resumption flows.
3. Administrative force close and voiding with full audit trails.
"""

import logging
from typing import Any, Optional
import database

logger = logging.getLogger(__name__)

# Configurable Live Market Suspension Rules
LIVE_EVENT_SUSPEND_RULES: dict[str, dict[str, Any]] = {
    "goal": {
        "action": "suspend_all",
        "reason": "Goal scored in live match"
    },
    "penalty": {
        "action": "suspend_all",
        "reason": "Penalty awarded in live match"
    },
    "var": {
        "action": "suspend_all",
        "reason": "VAR review in progress"
    },
    "red_card": {
        "action": "suspend_types",
        "categories": ["main", "totals", "handicap"],
        "reason": "Red card issued"
    },
    "halftime": {
        "action": "suspend_types",
        "categories": ["1st_half"],
        "reason": "Halftime interval"
    },
    "match_finished": {
        "action": "close_all",
        "reason": "Match completed"
    },
    "odds_anomaly": {
        "action": "suspend_all",
        "reason": "Extreme odds anomaly detected"
    },
    "provider_data_stale": {
        "action": "suspend_all",
        "reason": "Provider live feed delayed or stale (>120s)"
    },
    "provider_unavailable": {
        "action": "suspend_all",
        "reason": "Provider live feed unavailable"
    }
}


def evaluate_and_apply_suspend_rules(
    match_id: int,
    event_type: str,
    actor_id: Optional[int] = None
) -> int:
    """
    Evaluate in-play event against LIVE_EVENT_SUSPEND_RULES and apply suspensions.
    Returns number of markets updated.
    """
    rule = LIVE_EVENT_SUSPEND_RULES.get(event_type.lower())
    if not rule:
        return 0

    action = rule["action"]
    reason = rule.get("reason", f"Live event: {event_type}")

    with database.transaction() as conn:
        cursor = conn.cursor()

        # Fetch match metadata for division/season audit
        cursor.execute("SELECT division_id, season_id FROM matches WHERE id = ?", (match_id,))
        m_row = cursor.fetchone()
        div_id = m_row["division_id"] if m_row and "division_id" in m_row.keys() else 1
        season_id = m_row["season_id"] if m_row and "season_id" in m_row.keys() else 1

        updated_count = 0

        if action == "suspend_all":
            cursor.execute("""
                UPDATE markets
                SET status = 'suspended'
                WHERE match_id = ? AND status = 'open'
            """, (match_id,))
            updated_count = cursor.rowcount

        elif action == "suspend_types":
            cats = rule.get("categories", [])
            placeholders = ",".join("?" for _ in cats)
            cursor.execute(f"""
                UPDATE markets
                SET status = 'suspended'
                WHERE match_id = ? AND status = 'open' AND category IN ({placeholders})
            """, (match_id, *cats))
            updated_count = cursor.rowcount

        elif action == "close_all":
            cursor.execute("""
                UPDATE markets
                SET status = 'closed'
                WHERE match_id = ? AND status IN ('open', 'suspended')
            """, (match_id,))
            updated_count = cursor.rowcount

        if updated_count > 0:
            cursor.execute("""
                INSERT INTO bet_audit_log (actor_id, action, entity_type, entity_id, old_value, new_value, division_id, season_id, created_at)
                VALUES (?, 'rule_market_suspension', 'match', ?, 'open', ?, ?, ?, datetime('now', '+3 hours'))
            """, (actor_id or 0, match_id, action, div_id, season_id))
            logger.info(f"Applied {action} on {updated_count} markets for match #{match_id} (Reason: {reason})")

        return updated_count


def resume_match_markets(
    match_id: int,
    actor_id: Optional[int] = None,
    reason: Optional[str] = None
) -> int:
    """
    Resume suspended markets for a match after VAR/goal verification.
    Only transitions markets from 'suspended' back to 'open'.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT division_id, season_id FROM matches WHERE id = ?", (match_id,))
        m_row = cursor.fetchone()
        div_id = m_row["division_id"] if m_row and "division_id" in m_row.keys() else 1
        season_id = m_row["season_id"] if m_row and "season_id" in m_row.keys() else 1

        cursor.execute("""
            UPDATE markets
            SET status = 'open'
            WHERE match_id = ? AND status = 'suspended'
        """, (match_id,))
        count = cursor.rowcount

        if count > 0:
            cursor.execute("""
                INSERT INTO bet_audit_log (actor_id, action, entity_type, entity_id, old_value, new_value, division_id, season_id, created_at)
                VALUES (?, 'resume_match_markets', 'match', ?, 'suspended', 'open', ?, ?, datetime('now', '+3 hours'))
            """, (actor_id or 0, match_id, div_id, season_id))
            logger.info(f"Resumed {count} suspended markets for match #{match_id}")

        return count


def force_close_match_markets(
    match_id: int,
    actor_id: Optional[int] = None,
    reason: Optional[str] = None
) -> int:
    """Force close all active/suspended markets for a match."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT division_id, season_id FROM matches WHERE id = ?", (match_id,))
        m_row = cursor.fetchone()
        div_id = m_row["division_id"] if m_row and "division_id" in m_row.keys() else 1
        season_id = m_row["season_id"] if m_row and "season_id" in m_row.keys() else 1

        cursor.execute("""
            UPDATE markets
            SET status = 'closed'
            WHERE match_id = ? AND status IN ('open', 'suspended')
        """, (match_id,))
        count = cursor.rowcount

        if count > 0:
            cursor.execute("""
                INSERT INTO bet_audit_log (actor_id, action, entity_type, entity_id, old_value, new_value, division_id, season_id, created_at)
                VALUES (?, 'force_close_markets', 'match', ?, 'active', 'closed', ?, ?, datetime('now', '+3 hours'))
            """, (actor_id or 0, match_id, div_id, season_id))
            logger.info(f"Force closed {count} markets for match #{match_id}")

        return count
