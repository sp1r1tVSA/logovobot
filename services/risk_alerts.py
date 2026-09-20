"""
services/risk_alerts.py

Logovo.bet — Risk Alerting & Market Anomaly Notification Service.
Alert types:
- HIGH_EXPOSURE
- ODDS_ANOMALY
- SUSPICIOUS_ACTIVITY
- RAPID_BETTING
- MARKET_IMBALANCE
- PROVIDER_DATA_ISSUE
- STALE_ODDS

Invariants:
- Deduplicated within a 5-minute sliding window per entity.
- Strictly timestamped with severity ('low', 'medium', 'high', 'critical').
- Division-isolated filtering for Division Admins.
"""

import json
import logging
from dataclasses import dataclass, asdict
from typing import Any, Optional
import database

logger = logging.getLogger(__name__)


@dataclass
class RiskAlert:
    alert_type: str
    severity: str
    message: str
    division_id: Optional[int] = None
    match_id: Optional[int] = None
    market_id: Optional[int] = None
    selection_id: Optional[int] = None
    details: Optional[dict[str, Any]] = None
    status: str = "active"
    id: Optional[int] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None


def create_risk_alert(
    alert_type: str,
    severity: str,
    message: str,
    division_id: Optional[int] = None,
    match_id: Optional[int] = None,
    market_id: Optional[int] = None,
    selection_id: Optional[int] = None,
    details: Optional[dict[str, Any]] = None
) -> Optional[int]:
    """
    Create a new risk alert with 5-minute deduplication window for identical alerts.
    """
    severity_lower = severity.lower()
    if severity_lower not in ("low", "medium", "high", "critical"):
        severity_lower = "medium"

    details_json = json.dumps(details or {}, ensure_ascii=False)

    with database.transaction() as conn:
        cursor = conn.cursor()

        # Check for duplicate active alert in the last 5 minutes
        cursor.execute("""
            SELECT id FROM risk_alerts
            WHERE alert_type = ?
              AND status = 'active'
              AND (division_id IS ? OR division_id = ?)
              AND (match_id IS ? OR match_id = ?)
              AND (market_id IS ? OR market_id = ?)
              AND created_at >= datetime('now', '+3 hours', '-5 minutes')
            LIMIT 1
        """, (
            alert_type,
            division_id, division_id,
            match_id, match_id,
            market_id, market_id
        ))
        existing = cursor.fetchone()
        if existing:
            return existing["id"]

        cursor.execute("""
            INSERT INTO risk_alerts (
                alert_type, severity, division_id, match_id, market_id, selection_id,
                message, details_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', datetime('now', '+3 hours'))
        """, (
            alert_type, severity_lower, division_id, match_id, market_id, selection_id,
            message, details_json
        ))
        alert_id = cursor.lastrowid
        logger.warning(f"🚨 [RISK ALERT] [{severity_lower.upper()}] {alert_type}: {message}")
        return alert_id


def get_risk_alerts(
    division_id: Optional[int] = None,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
) -> list[dict[str, Any]]:
    """Retrieve risk alerts filtered by division, status, or severity."""
    with database.transaction() as conn:
        cursor = conn.cursor()

        query = "SELECT * FROM risk_alerts WHERE 1=1"
        params: list[Any] = []

        if division_id is not None:
            query += " AND (division_id = ? OR division_id IS NULL)"
            params.append(division_id)

        if status:
            query += " AND status = ?"
            params.append(status)

        if severity:
            query += " AND severity = ?"
            params.append(severity.lower())

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("details_json"):
                try:
                    d["details"] = json.loads(d["details_json"])
                except Exception:
                    d["details"] = {}
            result.append(d)
        return result


def acknowledge_risk_alert(alert_id: int, admin_id: Optional[int] = None) -> bool:
    """Transition alert from active to acknowledged."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE risk_alerts
            SET status = 'acknowledged'
            WHERE id = ? AND status = 'active'
        """, (alert_id,))
        return cursor.rowcount > 0


def resolve_risk_alert(alert_id: int, admin_id: Optional[int] = None) -> bool:
    """Resolve an alert."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE risk_alerts
            SET status = 'resolved', resolved_at = datetime('now', '+3 hours')
            WHERE id = ? AND status IN ('active', 'acknowledged')
        """, (alert_id,))
        return cursor.rowcount > 0
