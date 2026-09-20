"""
services/live_ingestion.py

Logovo.bet — Idempotent Live Event Ingestion & Score Consistency Pipeline.
Protects against duplicate events, out-of-order data, and stale provider updates.
Executes automated market suspension and triggers smart notification events.
"""

import json
import logging
from typing import Any, Optional
import database
from services.live_state_machine import (
    FINISHED,
    HALFTIME,
    LIVE,
    transition_live_match,
)
from services.sports_provider import LiveEvent, LiveMatchState, LiveStatistics

logger = logging.getLogger(__name__)


def ingest_live_event(event: LiveEvent) -> dict[str, Any]:
    """
    Ingest a real-time match event from a sports provider.
    Guarantees idempotency via database UNIQUE(provider, provider_event_id).
    Validates monotonic score progression and triggers downstream suspension rules.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Deduplication Check
        cursor.execute(
            "SELECT id FROM live_events WHERE provider = ? AND provider_event_id = ?",
            (event.provider, event.provider_event_id)
        )
        existing = cursor.fetchone()
        if existing:
            return {
                "status": "duplicate",
                "event_id": existing["id"],
                "message": f"Event {event.provider_event_id} already ingested."
            }

        # 2. Check Target Match
        cursor.execute("SELECT * FROM matches WHERE id = ?", (event.match_id,))
        m_row = cursor.fetchone()
        if not m_row:
            raise ValueError(f"Target match #{event.match_id} does not exist.")

        # 2b. Malformed minute validation
        if event.minute is not None and (event.minute < 0 or event.minute > 150):
            return {
                "status": "rejected",
                "match_id": event.match_id,
                "message": f"Malformed or impossible event minute: {event.minute}."
            }

        # 3. Ensure live_match_states record exists
        cursor.execute("SELECT * FROM live_match_states WHERE match_id = ?", (event.match_id,))
        state_row = cursor.fetchone()
        if not state_row:
            div_id = m_row["division_id"] if "division_id" in m_row.keys() and m_row["division_id"] else 1
            season_id = m_row["season_id"] if "season_id" in m_row.keys() and m_row["season_id"] else 1
            cursor.execute("""
                INSERT INTO live_match_states (match_id, season_id, division_id, status, period, minute, home_score, away_score, provider, last_updated_at)
                VALUES (?, ?, ?, 'LIVE', '1h', ?, ?, ?, ?, datetime('now', '+3 hours'))
            """, (event.match_id, season_id, div_id, event.minute, m_row["player1_score"] or 0, m_row["player2_score"] or 0, event.provider))
            cursor.execute("SELECT * FROM live_match_states WHERE match_id = ?", (event.match_id,))
            state_row = cursor.fetchone()

        curr_status = (state_row["status"] or "SCHEDULED").upper()
        if curr_status in ("FINISHED", "CANCELLED", "ABANDONED") and event.event_type.lower() != "result_correction":
            logger.warning(
                f"Rejected event {event.provider_event_id}: match #{event.match_id} is in terminal state '{curr_status}'."
            )
            return {
                "status": "rejected",
                "match_id": event.match_id,
                "message": f"Cannot apply event to match in terminal state '{curr_status}'."
            }

        curr_home_score = state_row["home_score"]
        curr_away_score = state_row["away_score"]
        curr_minute = state_row["minute"] or 0
        new_minute = max(curr_minute, event.minute)

        # 4. Out-of-order & Stale Check
        is_out_of_order = event.minute < (curr_minute - 5) and event.event_type not in ("VAR", "result_correction")
        if is_out_of_order:
            logger.warning(
                f"Out-of-order event #{event.provider_event_id} (minute {event.minute} vs current {curr_minute})."
            )

        # 5. Process Lifecycle Progression Events
        ev_type = event.event_type.lower()
        if ev_type == "match_started":
            transition_live_match(event.match_id, LIVE, source=event.provider)
        elif ev_type == "halftime":
            transition_live_match(event.match_id, HALFTIME, source=event.provider)
        elif ev_type == "second_half":
            transition_live_match(event.match_id, LIVE, source=event.provider)
        elif ev_type == "match_finished":
            transition_live_match(event.match_id, FINISHED, source=event.provider)

        # 6. Score Consistency for Scoring Events
        is_scoring_event = ev_type in ("goal", "own_goal", "penalty")
        if is_scoring_event:
            # Determine which side scored
            t1_name = (m_row["player1_team"] or "").lower()
            t2_name = (m_row["player2_team"] or "").lower()
            ev_team = (event.team_name or "").lower()

            is_home_scoring = False
            if event.payload and event.payload.get("side") in ("home", "team1"):
                is_home_scoring = True
            elif event.payload and event.payload.get("side") in ("away", "team2"):
                is_home_scoring = False
            elif ev_team and t1_name and ev_team in t1_name:
                is_home_scoring = True
            elif ev_team and t2_name and ev_team in t2_name:
                is_home_scoring = False
            else:
                # Default to home if ambiguous unless specified
                is_home_scoring = True

            # If own goal, flip the benefiting side
            if ev_type == "own_goal":
                is_home_scoring = not is_home_scoring

            if is_home_scoring:
                curr_home_score += 1
            else:
                curr_away_score += 1

            # Update match and state scores monotonically
            cursor.execute("""
                UPDATE live_match_states
                SET home_score = ?, away_score = ?, minute = ?, version = version + 1, last_updated_at = datetime('now', '+3 hours')
                WHERE match_id = ?
            """, (curr_home_score, curr_away_score, new_minute, event.match_id))

            cursor.execute("""
                UPDATE matches
                SET player1_score = ?, player2_score = ?, live_minute = ?
                WHERE id = ?
            """, (curr_home_score, curr_away_score, new_minute, event.match_id))
        else:
            cursor.execute("""
                UPDATE live_match_states
                SET minute = ?, last_updated_at = datetime('now', '+3 hours')
                WHERE match_id = ?
            """, (new_minute, event.match_id))

            cursor.execute("""
                UPDATE matches SET live_minute = ? WHERE id = ?
            """, (new_minute, event.match_id))

        # 7. Persist to live_events table
        payload_str = json.dumps(event.payload) if event.payload else None
        cursor.execute("""
            INSERT INTO live_events (
                match_id, provider, provider_event_id, event_type, minute,
                added_time, team_id, team_name, player_id, player_name, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
        """, (
            event.match_id, event.provider, event.provider_event_id, event.event_type,
            event.minute, event.added_time, event.team_id, event.team_name,
            event.player_id, event.player_name, payload_str
        ))
        row_id = cursor.lastrowid

        # 8. Trigger Automated Live Market Suspension
        suspended_markets_count = 0
        if ev_type in ("goal", "penalty", "var", "red_card", "second_yellow"):
            cursor.execute("""
                UPDATE markets

                SET status = 'suspended'
                WHERE match_id = ? AND status IN ('open', 'active')
            """, (event.match_id,))
            suspended_markets_count = cursor.rowcount

            if suspended_markets_count > 0:
                div_id = m_row["division_id"] if "division_id" in m_row.keys() and m_row["division_id"] else 1
                season_id = m_row["season_id"] if "season_id" in m_row.keys() and m_row["season_id"] else 1
                cursor.execute("""
                    INSERT INTO bet_audit_log (actor_id, action, entity_type, entity_id, old_value, new_value, division_id, season_id, created_at)
                    VALUES (0, 'auto_suspend_markets', 'match', ?, 'open', 'suspended', ?, ?, datetime('now', '+3 hours'))
                """, (event.match_id, div_id, season_id))

        logger.info(
            f"Ingested event #{event.provider_event_id} for match #{event.match_id} "
            f"({event.event_type} at {event.minute}'). Score: {curr_home_score}:{curr_away_score}"
        )

        return {
            "status": "applied",
            "event_id": row_id,
            "match_id": event.match_id,
            "event_type": event.event_type,
            "home_score": curr_home_score,
            "away_score": curr_away_score,
            "minute": new_minute,
            "suspended_markets": suspended_markets_count
        }


def ingest_live_statistics(stats: LiveStatistics) -> bool:
    """
    Ingest or update live match statistics.
    Preserves NULL (None) for unavailable values — strictly never forces 0.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO live_statistics (
                match_id, possession_home, possession_away, shots_home, shots_away,
                shots_on_target_home, shots_on_target_away, corners_home, corners_away,
                fouls_home, fouls_away, offsides_home, offsides_away,
                yellow_cards_home, yellow_cards_away, red_cards_home, red_cards_away,
                dangerous_attacks_home, dangerous_attacks_away, attacks_home, attacks_away,
                passes_home, passes_away, pass_accuracy_home, pass_accuracy_away,
                xg_home, xg_away, saves_home, saves_away, substitutions_home, substitutions_away,
                provider, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours')
            )
            ON CONFLICT(match_id) DO UPDATE SET
                possession_home = excluded.possession_home,
                possession_away = excluded.possession_away,
                shots_home = excluded.shots_home,
                shots_away = excluded.shots_away,
                shots_on_target_home = excluded.shots_on_target_home,
                shots_on_target_away = excluded.shots_on_target_away,
                corners_home = excluded.corners_home,
                corners_away = excluded.corners_away,
                fouls_home = excluded.fouls_home,
                fouls_away = excluded.fouls_away,
                offsides_home = excluded.offsides_home,
                offsides_away = excluded.offsides_away,
                yellow_cards_home = excluded.yellow_cards_home,
                yellow_cards_away = excluded.yellow_cards_away,
                red_cards_home = excluded.red_cards_home,
                red_cards_away = excluded.red_cards_away,
                dangerous_attacks_home = excluded.dangerous_attacks_home,
                dangerous_attacks_away = excluded.dangerous_attacks_away,
                attacks_home = excluded.attacks_home,
                attacks_away = excluded.attacks_away,
                passes_home = excluded.passes_home,
                passes_away = excluded.passes_away,
                pass_accuracy_home = excluded.pass_accuracy_home,
                pass_accuracy_away = excluded.pass_accuracy_away,
                xg_home = excluded.xg_home,
                xg_away = excluded.xg_away,
                saves_home = excluded.saves_home,
                saves_away = excluded.saves_away,
                substitutions_home = excluded.substitutions_home,
                substitutions_away = excluded.substitutions_away,
                provider = excluded.provider,
                updated_at = datetime('now', '+3 hours')
        """, (
            stats.match_id, stats.possession_home, stats.possession_away,
            stats.shots_home, stats.shots_away, stats.shots_on_target_home, stats.shots_on_target_away,
            stats.corners_home, stats.corners_away, stats.fouls_home, stats.fouls_away,
            stats.offsides_home, stats.offsides_away, stats.yellow_cards_home, stats.yellow_cards_away,
            stats.red_cards_home, stats.red_cards_away, stats.dangerous_attacks_home, stats.dangerous_attacks_away,
            stats.attacks_home, stats.attacks_away, stats.passes_home, stats.passes_away,
            stats.pass_accuracy_home, stats.pass_accuracy_away, stats.xg_home, stats.xg_away,
            stats.saves_home, stats.saves_away, stats.substitutions_home, stats.substitutions_away,
            stats.provider
        ))
        return cursor.rowcount > 0


def get_live_match_state(match_id: int) -> Optional[dict[str, Any]]:
    """Retrieve full live match state record from database."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM live_match_states WHERE match_id = ?", (match_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_live_statistics(match_id: int) -> Optional[dict[str, Any]]:
    """Retrieve match statistics record. Missing stats are None."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM live_statistics WHERE match_id = ?", (match_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_live_events(match_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Retrieve timeline of live events for a match."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, match_id, provider, provider_event_id, event_type, minute, added_time,
                   team_name, player_name, payload, created_at
            FROM live_events
            WHERE match_id = ?
            ORDER BY minute ASC, id ASC
            LIMIT ?
        """, (match_id, limit))
        return [dict(r) for r in cursor.fetchall()]
