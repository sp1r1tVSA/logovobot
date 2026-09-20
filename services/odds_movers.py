"""
services/odds_movers.py

Logovo.bet — Odds Movement Tracking & Market Movers Intelligence Service.
Tracks absolute change, percentage change, velocity (delta / time), and direction.
Provides analytics for:
- Biggest Drops
- Biggest Rises
- Fastest Velocity
- Suspended Markets
"""

import logging
from typing import Any, Optional
import database
from time_utils import now_msk, parse_msk

logger = logging.getLogger(__name__)


def record_odds_movement(
    selection_id: int,
    market_id: int,
    match_id: int,
    old_odds: float,
    new_odds: float,
    reason: Optional[str] = None,
    source: str = "system"
) -> Optional[int]:
    """
    Record an odds shift in the odds_movement table.
    Calculates percentage change, direction, and velocity relative to the prior record.
    """
    import math
    try:
        old_odds = float(old_odds)
        new_odds = float(new_odds)
    except (ValueError, TypeError):
        return None

    if not math.isfinite(old_odds) or not math.isfinite(new_odds) or old_odds <= 0 or new_odds <= 0:
        return None

    old_odds = round(old_odds, 2)
    new_odds = round(new_odds, 2)

    if abs(new_odds - old_odds) < 0.001:
        return None  # No meaningful movement

    pct_change = round(((new_odds - old_odds) / max(0.01, old_odds)) * 100, 2)
    direction = "up" if new_odds > old_odds else "down"

    with database.transaction() as conn:
        cursor = conn.cursor()

        # Find timestamp of last movement to compute velocity
        cursor.execute(
            "SELECT created_at FROM odds_movement WHERE selection_id = ? ORDER BY id DESC LIMIT 1",
            (selection_id,)
        )
        prev_row = cursor.fetchone()
        velocity = 0.0
        if prev_row and prev_row["created_at"]:
            try:
                prev_time = parse_msk(prev_row["created_at"])
                delta_sec = max(1.0, (now_msk() - prev_time).total_seconds())
                velocity = round(abs(pct_change) / delta_sec, 4)
            except Exception:
                velocity = round(abs(pct_change) / 60.0, 4)
        else:
            velocity = round(abs(pct_change) / 60.0, 4)

        cursor.execute("""
            INSERT INTO odds_movement (
                selection_id, market_id, match_id, old_odds, new_odds,
                pct_change, direction, velocity, reason, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
        """, (selection_id, market_id, match_id, old_odds, new_odds, pct_change, direction, velocity, reason, source))
        movement_id = cursor.lastrowid

        # Phase 9: Classification & Anomaly Alerting
        cls = classify_movement(pct_change, velocity)
        if cls == "ANOMALY":
            try:
                from services.risk_alerts import create_risk_alert
                cursor.execute("SELECT division_id FROM matches WHERE id = ?", (match_id,))
                m_row = cursor.fetchone()
                div_id = m_row["division_id"] if m_row and "division_id" in m_row.keys() else None
                create_risk_alert(
                    alert_type="ODDS_ANOMALY",
                    severity="high",
                    message=f"Резкое изменение коэффициента {old_odds} → {new_odds} ({pct_change}%, v={velocity})",
                    division_id=div_id,
                    match_id=match_id,
                    market_id=market_id,
                    selection_id=selection_id,
                    details={"old_odds": old_odds, "new_odds": new_odds, "pct_change": pct_change, "velocity": velocity}
                )
            except Exception as e:
                logger.debug(f"Failed to emit risk alert for odds anomaly: {e}")

        return movement_id


def classify_movement(pct_change: float, velocity: float = 0.0) -> str:
    """Classify odds movement into STABLE, MOVING, FAST_MOVE, or ANOMALY."""
    abs_pct = abs(float(pct_change))
    vel = abs(float(velocity))
    if abs_pct >= 15.0 or vel >= 0.5:
        return "ANOMALY"
    elif abs_pct >= 8.0 or vel >= 0.2:
        return "FAST_MOVE"
    elif abs_pct >= 2.0:
        return "MOVING"
    return "STABLE"


def get_odds_movers(
    division_id: Optional[int] = None,
    season_id: Optional[int] = None,
    limit: int = 10
) -> dict[str, Any]:
    """
    Retrieve categorized odds movers:
    1. biggest_drops: largest negative percentage shifts.
    2. biggest_rises: largest positive percentage shifts.
    3. fastest_movement: highest velocity shifts within the last 24 hours.
    4. suspended_markets: all currently suspended markets.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()

        base_filter = "WHERE 1=1"
        params: list[Any] = []
        if division_id is not None:
            base_filter += " AND m.division_id = ?"
            params.append(division_id)
        if season_id is not None:
            base_filter += " AND m.season_id = ?"
            params.append(season_id)

        # 1. Biggest Drops (steepest negative % change in last 24h)
        query_drops = f"""
            SELECT om.*, ms.selection_key, ms.selection_name, mk.market_key, mk.market_name,
                   m.player1_team, m.player2_team, m.division_id, m.season_id
            FROM odds_movement om
            JOIN market_selections ms ON om.selection_id = ms.id
            JOIN markets mk ON om.market_id = mk.id
            JOIN matches m ON om.match_id = m.id
            {base_filter} AND om.pct_change < 0
            ORDER BY om.pct_change ASC, om.id DESC
            LIMIT ?
        """
        cursor.execute(query_drops, (*params, limit))
        biggest_drops = [dict(r) for r in cursor.fetchall()]

        # 2. Biggest Rises (highest positive % change in last 24h)
        query_rises = f"""
            SELECT om.*, ms.selection_key, ms.selection_name, mk.market_key, mk.market_name,
                   m.player1_team, m.player2_team, m.division_id, m.season_id
            FROM odds_movement om
            JOIN market_selections ms ON om.selection_id = ms.id
            JOIN markets mk ON om.market_id = mk.id
            JOIN matches m ON om.match_id = m.id
            {base_filter} AND om.pct_change > 0
            ORDER BY om.pct_change DESC, om.id DESC
            LIMIT ?
        """
        cursor.execute(query_rises, (*params, limit))
        biggest_rises = [dict(r) for r in cursor.fetchall()]

        # 3. Fastest Movement (highest velocity)
        query_velocity = f"""
            SELECT om.*, ms.selection_key, ms.selection_name, mk.market_key, mk.market_name,
                   m.player1_team, m.player2_team, m.division_id, m.season_id
            FROM odds_movement om
            JOIN market_selections ms ON om.selection_id = ms.id
            JOIN markets mk ON om.market_id = mk.id
            JOIN matches m ON om.match_id = m.id
            {base_filter}
            ORDER BY om.velocity DESC, om.id DESC
            LIMIT ?
        """
        cursor.execute(query_velocity, (*params, limit))
        fastest_movement = [dict(r) for r in cursor.fetchall()]

        # 4. Suspended Markets
        query_suspended = f"""
            SELECT mk.*, m.player1_team, m.player2_team, m.division_id, m.season_id,
                   lms.status as live_status, lms.minute as live_minute
            FROM markets mk
            JOIN matches m ON mk.match_id = m.id
            LEFT JOIN live_match_states lms ON mk.match_id = lms.match_id
            {base_filter} AND mk.status = 'suspended'
            ORDER BY mk.id DESC
            LIMIT ?
        """
        cursor.execute(query_suspended, (*params, limit))
        suspended_markets = [dict(r) for r in cursor.fetchall()]

    return {
        "status": "ok",
        "biggest_drops": biggest_drops,
        "biggest_rises": biggest_rises,
        "fastest_movement": fastest_movement,
        "suspended_markets": suspended_markets,
    }


def detect_odds_anomalies(
    division_id: Optional[int] = None,
    season_id: Optional[int] = None,
    limit: int = 20
) -> list[dict[str, Any]]:
    """
    Detect statistically abnormal movements:
    1. Sharp shifts (>= 15% change).
    2. High velocity movements (>= 0.5% per second).
    Strict Invariant: Analytical flags only — NEVER asserts insider trading or match manipulation.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        base_filter = "WHERE 1=1"
        params: list[Any] = []
        if division_id is not None:
            base_filter += " AND m.division_id = ?"
            params.append(division_id)
        if season_id is not None:
            base_filter += " AND m.season_id = ?"
            params.append(season_id)

        cursor.execute(f"""
            SELECT om.*, ms.selection_key, ms.selection_name, mk.market_key, mk.market_name,
                   m.player1_team, m.player2_team, m.division_id, m.season_id
            FROM odds_movement om
            JOIN market_selections ms ON om.selection_id = ms.id
            JOIN markets mk ON om.market_id = mk.id
            JOIN matches m ON om.match_id = m.id
            {base_filter} AND (ABS(om.pct_change) >= 15.0 OR om.velocity >= 0.5)
            ORDER BY om.id DESC
            LIMIT ?
        """, (*params, limit))
        raw_anomalies = [dict(r) for r in cursor.fetchall()]

    flagged = []
    for a in raw_anomalies:
        pct = abs(float(a.get("pct_change", 0.0)))
        vel = float(a.get("velocity", 0.0))

        if pct >= 25.0 or vel >= 1.0:
            severity = "HIGH"
        elif pct >= 15.0 or vel >= 0.5:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        flagged.append({
            "id": a["id"],
            "match_id": a["match_id"],
            "market_id": a["market_id"],
            "selection_id": a["selection_id"],
            "team1": a["player1_team"],
            "team2": a["player2_team"],
            "market_name": a["market_name"],
            "selection_name": a["selection_name"],
            "old_odds": a["old_odds"],
            "new_odds": a["new_odds"],
            "pct_change": a["pct_change"],
            "direction": a["direction"],
            "velocity": vel,
            "severity": severity,
            "anomaly_type": "sharp_drop" if a["direction"] == "down" else "sharp_rise",
            "explanation": f"Резкое движение коэффициента ({a['old_odds']} -> {a['new_odds']}) со сдвигом {a['pct_change']}%."
        })

    return flagged

