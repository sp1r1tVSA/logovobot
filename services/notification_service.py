"""
services/notification_service.py

Phase 6 Smart Notifications Service:
1. Event Types: MATCH_STARTED, GOAL, RED_CARD, HALFTIME, MATCH_FINISHED,
   ODDS_MOVEMENT, MARKET_SUSPENDED, BET_SETTLED, PREDICTION_UPDATED, HOT_MATCH.
2. Deduplication & Idempotency: Enforced at DB level via UNIQUE(user_id, event_type, source_event_id).
3. Anti-Spam: User preferences in user_notification_settings, configurable cooldowns for frequent events.
4. Priority management: critical/high bypass cooldown, low/normal obey cooldown.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

import config
import database

logger = logging.getLogger(__name__)


def is_smart_notifications_enabled() -> bool:
    """Check if smart notifications service is enabled in config."""
    return bool(getattr(config, "SMART_NOTIFICATIONS_ENABLED", False))

# Standard notification event types
EVENT_TYPE_MATCH_STARTED = "MATCH_STARTED"
EVENT_TYPE_GOAL = "GOAL"
EVENT_TYPE_RED_CARD = "RED_CARD"
EVENT_TYPE_HALFTIME = "HALFTIME"
EVENT_TYPE_MATCH_FINISHED = "MATCH_FINISHED"
EVENT_TYPE_ODDS_MOVEMENT = "ODDS_MOVEMENT"
EVENT_TYPE_MARKET_SUSPENDED = "MARKET_SUSPENDED"
EVENT_TYPE_BET_SETTLED = "BET_SETTLED"
EVENT_TYPE_PREDICTION_UPDATED = "PREDICTION_UPDATED"
EVENT_TYPE_HOT_MATCH = "HOT_MATCH"

VALID_EVENT_TYPES = {
    EVENT_TYPE_MATCH_STARTED,
    EVENT_TYPE_GOAL,
    EVENT_TYPE_RED_CARD,
    EVENT_TYPE_HALFTIME,
    EVENT_TYPE_MATCH_FINISHED,
    EVENT_TYPE_ODDS_MOVEMENT,
    EVENT_TYPE_MARKET_SUSPENDED,
    EVENT_TYPE_BET_SETTLED,
    EVENT_TYPE_PREDICTION_UPDATED,
    EVENT_TYPE_HOT_MATCH,
}

# Priorities that bypass non-critical cooldowns
HIGH_PRIORITY_TYPES = {
    EVENT_TYPE_GOAL,
    EVENT_TYPE_RED_CARD,
    EVENT_TYPE_BET_SETTLED,
    EVENT_TYPE_MATCH_FINISHED,
}


def is_notification_enabled(user_id: int, event_type: str) -> bool:
    """
    Check if a notification type is enabled for the user in user_notification_settings.
    Defaults to True (1) if no custom preference exists.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT is_enabled
            FROM user_notification_settings
            WHERE user_id = ? AND notification_type = ?
        """, (user_id, event_type))
        row = cursor.fetchone()
        if row is not None:
            return bool(row["is_enabled"])
        return True


def set_notification_preference(user_id: int, event_type: str, is_enabled: bool) -> None:
    """
    Set a user's notification preference for a specific event type.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_notification_settings (user_id, notification_type, is_enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, notification_type) DO UPDATE SET
                is_enabled = excluded.is_enabled
        """, (user_id, event_type, 1 if is_enabled else 0))


def queue_notification(
    user_id: int,
    event_type: str,
    source_event_id: str,
    title: str,
    body: str = "",
    link: str = "",
    priority: str = "normal",
    cooldown_seconds: int = 0
) -> tuple[bool, str]:
    """
    Queue a notification event for delivery:
    1. Checks if smart notifications are globally enabled (skips if in development/disabled).
    2. Checks user preference (skips if disabled).
    3. Checks cooldown for this (user_id, event_type) if cooldown_seconds > 0 and priority not in HIGH_PRIORITY_TYPES.
    4. Attempts INSERT into notification_events (enforcing UNIQUE(user_id, event_type, source_event_id)).
    5. Also inserts into legacy in-app notifications for instant Mini App visibility.

    Returns:
        (True, "queued") on success
        (False, "disabled") if user turned off this notification type or service disabled
        (False, "cooldown") if throttled by cooldown
        (False, "duplicate") if already queued/sent (DB unique constraint)
    """
    if not is_smart_notifications_enabled():
        logger.debug("Smart notifications are globally disabled (in development).")
        return False, "disabled"

    if not is_notification_enabled(user_id, event_type):
        logger.debug("Notification %s disabled by user %s", event_type, user_id)
        return False, "disabled"

    with database.transaction() as conn:
        cursor = conn.cursor()

        # Cooldown check for low/normal priority events
        if cooldown_seconds > 0 and event_type not in HIGH_PRIORITY_TYPES and priority not in ("high", "critical"):
            cursor.execute("""
                SELECT strftime('%s', 'now', '+3 hours') - strftime('%s', created_at) as diff_sec
                FROM notification_events
                WHERE user_id = ? AND event_type = ?
                ORDER BY id DESC
                LIMIT 1
            """, (user_id, event_type))
            row = cursor.fetchone()
            if row is not None and row["diff_sec"] is not None and int(row["diff_sec"]) < cooldown_seconds:
                logger.debug("Notification %s throttled for user %s by cooldown", event_type, user_id)
                return False, "cooldown"

        # Unique insertion into notification_events
        try:
            cursor.execute("""
                INSERT INTO notification_events (
                    user_id, event_type, source_event_id, title, body, link, priority, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', datetime('now', '+3 hours'))
            """, (user_id, event_type, str(source_event_id), title, body, link, priority))
        except sqlite3.IntegrityError:
            logger.debug("Duplicate notification blocked: user=%s, type=%s, source=%s", user_id, event_type, source_event_id)
            return False, "duplicate"

        # Mirror to in-app notifications
        try:
            cursor.execute("""
                INSERT INTO notifications (user_id, type, title, body, reference_id, is_read, created_at)
                VALUES (?, ?, ?, ?, ?, 0, datetime('now', '+3 hours'))
            """, (user_id, event_type, title, body, None))
        except Exception as e:
            logger.warning("Failed to mirror in-app notification: %s", e)

        return True, "queued"


def broadcast_match_event(
    match_id: int,
    event_type: str,
    source_event_id: str,
    title: str,
    body: str = "",
    priority: str = "normal"
) -> int:
    """
    Broadcast a match event to all users with an active interest:
    - Users with pending or won/lost bets on this match.
    - Users with favorited teams playing in this match.

    Returns count of successfully queued notifications.
    """
    if not is_smart_notifications_enabled():
        logger.debug("Smart notifications are globally disabled (in development); skipping broadcast.")
        return 0

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT player1_team, player2_team FROM matches WHERE id = ?", (match_id,))
        match_row = cursor.fetchone()
        if not match_row:
            return 0

        t1, t2 = match_row["player1_team"], match_row["player2_team"]

        # 1. Users with bets on this match
        cursor.execute("""
            SELECT DISTINCT ub.user_id
            FROM user_bets ub
            JOIN bet_items bi ON bi.bet_id = ub.id
            WHERE bi.match_id = ?
        """, (match_id,))
        bettor_ids = {r["user_id"] for r in cursor.fetchall()}

        # 2. Users favoriting either team
        cursor.execute("""
            SELECT DISTINCT user_id
            FROM favorites
            WHERE target_type = 'team' AND target_id IN (?, ?)
        """, (t1, t2))
        favorite_user_ids = {r["user_id"] for r in cursor.fetchall()}

        target_user_ids = bettor_ids | favorite_user_ids

    queued_count = 0
    for uid in target_user_ids:
        success, _ = queue_notification(
            user_id=uid,
            event_type=event_type,
            source_event_id=f"m_{match_id}_{source_event_id}",
            title=title,
            body=body,
            link=f"/live/{match_id}",
            priority=priority
        )
        if success:
            queued_count += 1

    return queued_count


def get_user_notification_events(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Retrieve recent notification events for a user."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, event_type, source_event_id, title, body, link, priority, status, created_at, sent_at
            FROM notification_events
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in cursor.fetchall()]


def mark_notification_sent(event_id: int) -> None:
    """Mark a queued notification event as sent."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE notification_events
            SET status = 'sent', sent_at = datetime('now', '+3 hours')
            WHERE id = ?
        """, (event_id,))
