"""
api/routes_admin_risk.py

Phase 9 — Logovo.bet Admin Risk Center & Exposure Management.
Endpoints:
  - GET  /api/admin/risk/exposure : Global, division, and market exposure telemetry.
  - GET  /api/admin/risk/alerts   : Filtered risk alerts and anomalies.
  - POST /api/admin/risk/alerts/{id}/ack : Acknowledge alert.
  - POST /api/admin/risk/alerts/{id}/resolve : Resolve alert.
  - GET  /api/admin/risk/limits   : View centralized betting limits.
  - POST /api/admin/risk/limits   : Update centralized limits (Global Admin only).
  - POST /api/admin/risk/suspend  : Emergency market suspension.

Strict RBAC:
  - Global Admin (in ADMIN_IDS): Unrestricted access.
  - Division Admin (in division_admins): Scoped strictly to assigned division(s).
  - Player: 403 Forbidden.
  - Unauthenticated: 401 Unauthorized.
"""

import asyncio
import logging
from aiohttp import web
import database
from api.auth import get_authenticated_user
from api.params import body_int, path_int, query_int
import config
from services.betting_limits import BettingLimitsService
from services.exposure_service import get_market_exposure, get_division_exposure, get_global_exposure
import services.risk_alerts as risk_alerts

logger = logging.getLogger(__name__)


def _get_actor_id(request: web.Request) -> int | None:
    """Extract and verify authenticated user ID from request."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return None
    return user_info["id"]


def _is_global_admin(actor_id: int) -> bool:
    return actor_id in config.ADMIN_IDS


def _get_division_admin_divisions(actor_id: int) -> list[int]:
    """Return division IDs that the actor administers."""
    try:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT division_id FROM division_admins WHERE user_id = ?",
                (actor_id,)
            )
            return [r["division_id"] for r in cursor.fetchall()]
    except Exception:
        return []


def _alert_access_denied(actor_id: int, alert_id: int) -> web.Response | None:
    """404/403 for an alert the actor may not touch, None when allowed.

    An alert without a division belongs to division 1, as on the dashboard,
    so a division admin only reaches alerts of the divisions they run.
    """
    alert_divs = database.get_betting_entity_divisions("alert", alert_id)
    if alert_divs is None:
        return web.json_response({"status": "error", "error": "not_found"}, status=404)
    if not _is_global_admin(actor_id) and not alert_divs <= set(_get_division_admin_divisions(actor_id)):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)
    return None


async def handle_admin_get_exposure(request: web.Request) -> web.Response:
    """
    GET /api/admin/risk/exposure?market_id=X&division_id=Y&season_id=Z
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    is_global = _is_global_admin(actor_id)
    allowed_divs = _get_division_admin_divisions(actor_id)

    if not is_global and not allowed_divs:
        return web.json_response({"status": "error", "error": "forbidden", "message": "Admin access required"}, status=403)

    market_id_str = request.query.get("market_id")
    division_id_str = request.query.get("division_id")
    season_id_str = request.query.get("season_id")

    season_id = int(season_id_str) if season_id_str and season_id_str.isdigit() else None

    # Single Market Exposure
    if market_id_str and market_id_str.isdigit():
        market_id = int(market_id_str)
        expo = get_market_exposure(market_id)
        if "error" in expo:
            return web.json_response({"status": "error", "error": expo["error"]}, status=404)

        market_div = expo.get("division_id")
        if not is_global and market_div not in allowed_divs:
            return web.json_response({"status": "error", "error": "forbidden", "message": "Cross-division access denied"}, status=403)

        return web.json_response({"status": "ok", "exposure": expo})

    # Division Exposure
    if division_id_str and division_id_str.isdigit():
        division_id = int(division_id_str)
        if not is_global and division_id not in allowed_divs:
            return web.json_response({"status": "error", "error": "forbidden", "message": "Cross-division access denied"}, status=403)

        expo = get_division_exposure(division_id, season_id=season_id)
        return web.json_response({"status": "ok", "exposure": expo})

    # Global Exposure (Global Admin only)
    if not is_global:
        # Division admin querying root exposure defaults to their first assigned division
        div_id = allowed_divs[0]
        expo = get_division_exposure(div_id, season_id=season_id)
        return web.json_response({"status": "ok", "exposure": expo})

    expo = get_global_exposure()
    return web.json_response({"status": "ok", "exposure": expo})


async def handle_admin_get_risk_alerts(request: web.Request) -> web.Response:
    """
    GET /api/admin/risk/alerts?division_id=X&status=active&severity=high&limit=50&offset=0
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    is_global = _is_global_admin(actor_id)
    allowed_divs = _get_division_admin_divisions(actor_id)

    if not is_global and not allowed_divs:
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    division_id_str = request.query.get("division_id")
    status = request.query.get("status")
    severity = request.query.get("severity")
    limit = min(100, max(1, query_int(request, "limit", 50)))
    offset = max(0, query_int(request, "offset", 0))

    division_id = None
    if division_id_str and division_id_str.isdigit():
        division_id = int(division_id_str)
        if not is_global and division_id not in allowed_divs:
            return web.json_response({"status": "error", "error": "forbidden"}, status=403)
    elif not is_global:
        division_id = allowed_divs[0]

    alerts = risk_alerts.get_risk_alerts(
        division_id=division_id,
        status=status,
        severity=severity,
        limit=limit,
        offset=offset
    )

    return web.json_response({"status": "ok", "count": len(alerts), "alerts": alerts})


async def handle_admin_ack_alert(request: web.Request) -> web.Response:
    """
    POST /api/admin/risk/alerts/{id}/ack
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    is_global = _is_global_admin(actor_id)
    allowed_divs = _get_division_admin_divisions(actor_id)
    if not is_global and not allowed_divs:
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    alert_id = path_int(request)
    denied = _alert_access_denied(actor_id, alert_id)
    if denied is not None:
        return denied

    success = risk_alerts.acknowledge_risk_alert(alert_id, admin_id=actor_id)
    return web.json_response({"status": "ok" if success else "error", "alert_id": alert_id})


async def handle_admin_resolve_alert(request: web.Request) -> web.Response:
    """
    POST /api/admin/risk/alerts/{id}/resolve
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    is_global = _is_global_admin(actor_id)
    allowed_divs = _get_division_admin_divisions(actor_id)
    if not is_global and not allowed_divs:
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    alert_id = path_int(request)
    denied = _alert_access_denied(actor_id, alert_id)
    if denied is not None:
        return denied

    success = risk_alerts.resolve_risk_alert(alert_id, admin_id=actor_id)
    return web.json_response({"status": "ok" if success else "error", "alert_id": alert_id})


async def handle_admin_get_limits(request: web.Request) -> web.Response:
    """
    GET /api/admin/risk/limits?division_id=X
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    is_global = _is_global_admin(actor_id)
    allowed_divs = _get_division_admin_divisions(actor_id)
    if not is_global and not allowed_divs:
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    division_id_str = request.query.get("division_id")
    if division_id_str and division_id_str.isdigit():
        div_id = int(division_id_str)
        if not is_global and div_id not in allowed_divs:
            return web.json_response({"status": "error", "error": "forbidden"}, status=403)
        limits = BettingLimitsService.get_division_limits(div_id)
    else:
        limits = BettingLimitsService.get_system_limits()

    return web.json_response({"status": "ok", "limits": limits})


async def handle_admin_set_limits(request: web.Request) -> web.Response:
    """
    POST /api/admin/risk/limits
    Body: {"scope_type": "division", "scope_id": 1, "limit_key": "max_bet", "limit_value": 25000}
    Global Admin only.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    # Strictly Global Admin only for altering limits
    if not _is_global_admin(actor_id):
        return web.json_response({
            "status": "error",
            "error": "forbidden",
            "message": "Only Global Admins can configure centralized risk limits."
        }, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON body"}, status=400)

    scope_type = str(data.get("scope_type", "global")).lower()
    scope_id = body_int(data, "scope_id", 0)
    limit_key = str(data.get("limit_key", "")).strip()
    limit_value = body_int(data, "limit_value", 0)

    if not limit_key or limit_value <= 0:
        return web.json_response({"status": "error", "message": "Invalid limit_key or limit_value"}, status=400)

    BettingLimitsService.set_limit(scope_type, scope_id, limit_key, limit_value)

    return web.json_response({
        "status": "ok",
        "message": f"Limit '{limit_key}' set to {limit_value} for {scope_type}:{scope_id}",
        "scope_type": scope_type,
        "scope_id": scope_id,
        "limit_key": limit_key,
        "limit_value": limit_value
    })


async def handle_admin_emergency_suspend(request: web.Request) -> web.Response:
    """
    POST /api/admin/risk/suspend
    Body: {"market_id": 123, "reason": "Emergency risk suspension"}
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    is_global = _is_global_admin(actor_id)
    allowed_divs = _get_division_admin_divisions(actor_id)
    if not is_global and not allowed_divs:
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON body"}, status=400)

    market_id = body_int(data, "market_id", 0)
    reason = str(data.get("reason", "Emergency risk suspension"))

    # Verify market access for division admins
    if not is_global:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mat.division_id FROM markets m
                JOIN matches mat ON m.match_id = mat.id
                WHERE m.id = ?
            """, (market_id,))
            m_row = cursor.fetchone()
            if not m_row or m_row["division_id"] not in allowed_divs:
                return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        res = await asyncio.to_thread(database.transition_market_status, market_id, "suspended", actor_id)
        return web.json_response({"status": "ok", "result": res})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)
