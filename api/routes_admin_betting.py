"""
api/routes_admin_betting.py

Phase 5 — Logovo.bet Admin Betting Center.
Provides admin-only endpoints for market lifecycle management, odds updates,
bet voiding, and audit log access.

RBAC:
  - Global Admin (in ADMIN_IDS): can manage all divisions.
  - Division Admin (in division_admins): can only manage own division's markets.
  - Player: no access (403).
"""

import asyncio
import logging
from aiohttp import web
import database
from api.auth import get_authenticated_user
from api.params import query_int
import config

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
    """Return division IDs that the actor administers. Empty list means not a division admin."""
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


def _check_market_access(actor_id: int, market_id: int) -> bool:
    """Return True if actor can manage this market (global admin or division admin for its division)."""
    if _is_global_admin(actor_id):
        return True
    allowed_divisions = _get_division_admin_divisions(actor_id)
    if not allowed_divisions:
        return False
    try:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.division_id FROM markets mkt
                JOIN matches m ON mkt.match_id = m.id
                WHERE mkt.id = ?
            """, (market_id,))
            row = cursor.fetchone()
            if row and row["division_id"] in allowed_divisions:
                return True
    except Exception:
        pass
    return False


def _check_bet_access(actor_id: int, bet_id: int) -> bool:
    """Return True if actor can void this bet (global admin, or division admin for bet's division)."""
    if _is_global_admin(actor_id):
        return True
    allowed_divisions = _get_division_admin_divisions(actor_id)
    if not allowed_divisions:
        return False
    try:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT m.division_id
                FROM bet_items bi
                JOIN matches m ON bi.match_id = m.id
                WHERE bi.bet_id = ?
            """, (bet_id,))
            rows = cursor.fetchall()
            bet_divisions = {r["division_id"] for r in rows if r["division_id"]}
            if bet_divisions and bet_divisions.issubset(set(allowed_divisions)):
                return True
    except Exception:
        pass
    return False


async def handle_admin_list_markets(request: web.Request) -> web.Response:
    """
    GET /api/admin/markets?division_id=X&status=active&limit=50&offset=0
    List markets with optional filters.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not (_is_global_admin(actor_id) or _get_division_admin_divisions(actor_id)):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    division_id = query_int(request, "division_id", None)
    status_filter = request.query.get("status")
    limit = min(100, query_int(request, "limit", 50, minimum=1))
    offset = query_int(request, "offset", 0, minimum=0)

    # Division admins are scoped to their divisions
    if not _is_global_admin(actor_id):
        allowed = _get_division_admin_divisions(actor_id)
        if division_id is not None and division_id not in allowed:
            return web.json_response({"status": "error", "error": "forbidden", "message": "Access restricted to your division."}, status=403)

    try:
        with database.transaction() as conn:
            cursor = conn.cursor()
            query = """
                SELECT mkt.*, m.division_id, m.round_number,
                       COALESCE(m.player1_team, 'Хозяева') as team1_name,
                       COALESCE(m.player2_team, 'Гости') as team2_name
                FROM markets mkt
                JOIN matches m ON mkt.match_id = m.id
                WHERE 1=1
            """
            params: list = []
            if division_id is not None:
                query += " AND m.division_id = ?"
                params.append(division_id)
            elif not _is_global_admin(actor_id):
                allowed = _get_division_admin_divisions(actor_id)
                placeholders = ",".join("?" * len(allowed))
                query += f" AND m.division_id IN ({placeholders})"
                params.extend(allowed)
            if status_filter:
                query += " AND mkt.status = ?"
                params.append(status_filter)
            query += " ORDER BY mkt.id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)
            markets = [dict(r) for r in cursor.fetchall()]

        return web.json_response({"status": "ok", "markets": markets, "count": len(markets)})
    except Exception as e:
        logger.exception("Error listing admin markets")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_transition_market(request: web.Request) -> web.Response:
    """
    POST /api/admin/markets/{id}/transition
    Body: {"new_status": "suspended"} (for "voided" also {"reason": "..."})
    Validates the state machine. "voided" refunds the market's bets through
    database.void_market; "settled" is refused — only settlement settles.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        market_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid market ID."}, status=400)

    if not _check_market_access(actor_id, market_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON."}, status=400)

    new_status = data.get("new_status", "").strip()
    if not new_status:
        return web.json_response({"status": "error", "message": "new_status is required."}, status=400)

    # Расчёт рынка делает только settlement: ручной 'settled' оставил бы
    # купоны на рынке висеть в pending навсегда.
    if new_status == "settled":
        return web.json_response({
            "status": "error", "error": "INVALID_TRANSITION",
            "message": "Markets are settled automatically; manual settlement is not allowed.",
        }, status=409)

    # Аннулирование — не просто смена статуса: ставки на рынке надо вернуть,
    # а это умеет только database.void_market.
    if new_status == "voided":
        reason = str(data.get("reason") or "").strip()
        if not reason:
            return web.json_response({"status": "error", "message": "Reason is required to void market."}, status=400)
        try:
            voided = await asyncio.to_thread(database.void_market, market_id, actor_id, reason)
        except ValueError as e:
            return web.json_response({"status": "error", "error": "INVALID_TRANSITION", "message": str(e)}, status=409)
        except Exception as e:
            logger.exception("Error voiding market via transition")
            return web.json_response({"status": "error", "message": str(e)}, status=500)
        if voided.get("already_voided"):
            return web.json_response({
                "status": "error", "error": "INVALID_TRANSITION",
                "message": f"Market #{market_id} is already voided.",
            }, status=409)
        return web.json_response({
            "status": "ok",
            "market": {"id": market_id, "old_status": voided["old_status"], "new_status": "voided"},
            "void": voided,
        })

    try:
        result = await asyncio.to_thread(database.transition_market_status, market_id, new_status, actor_id)
        return web.json_response({"status": "ok", "market": result})
    except ValueError as e:
        return web.json_response({"status": "error", "error": "INVALID_TRANSITION", "message": str(e)}, status=409)
    except Exception as e:
        logger.exception("Error transitioning market status")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_update_odds(request: web.Request) -> web.Response:
    """
    PUT /api/admin/markets/{id}/odds
    Body: {"selection_id": 123, "new_odd": 1.75}
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        market_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid market ID."}, status=400)

    if not _check_market_access(actor_id, market_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON."}, status=400)

    selection_id = data.get("selection_id")
    new_odd = data.get("new_odd")
    if not selection_id or not new_odd:
        return web.json_response({"status": "error", "message": "selection_id and new_odd are required."}, status=400)

    try:
        result = await asyncio.to_thread(database.update_selection_odds, int(selection_id), float(new_odd), actor_id)
        return web.json_response({"status": "ok", "update": result})
    except ValueError as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)
    except Exception as e:
        logger.exception("Error updating odds")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_list_bets(request: web.Request) -> web.Response:
    """
    GET /api/admin/bets?user_id=X&status=pending&division_id=X&limit=50
    List bets (admin view).
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not (_is_global_admin(actor_id) or _get_division_admin_divisions(actor_id)):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    user_id_filter = query_int(request, "user_id", None)
    status_filter = request.query.get("status")
    division_id = query_int(request, "division_id", None)
    limit = min(100, query_int(request, "limit", 50, minimum=1))
    offset = query_int(request, "offset", 0, minimum=0)

    # Division admins are scoped to their divisions
    if not _is_global_admin(actor_id):
        if division_id is not None and division_id not in _get_division_admin_divisions(actor_id):
            return web.json_response({"status": "error", "error": "forbidden", "message": "Access restricted to your division."}, status=403)

    try:
        with database.transaction() as conn:
            cursor = conn.cursor()
            query = """
                SELECT DISTINCT ub.*
                FROM user_bets ub
                LEFT JOIN bet_items bi ON ub.id = bi.bet_id
                LEFT JOIN matches m ON bi.match_id = m.id
                WHERE 1=1
            """
            params: list = []
            if user_id_filter is not None:
                query += " AND ub.user_id = ?"
                params.append(user_id_filter)
            if status_filter:
                query += " AND ub.status = ?"
                params.append(status_filter)
            if division_id is not None:
                query += " AND m.division_id = ?"
                params.append(division_id)
            elif not _is_global_admin(actor_id):
                allowed = _get_division_admin_divisions(actor_id)
                placeholders = ",".join("?" * len(allowed))
                query += f" AND m.division_id IN ({placeholders})"
                params.extend(allowed)
            query += " ORDER BY ub.id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)
            bets = [dict(r) for r in cursor.fetchall()]

        return web.json_response({"status": "ok", "bets": bets, "count": len(bets)})
    except Exception as e:
        logger.exception("Error listing admin bets")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_void_bet(request: web.Request) -> web.Response:
    """
    POST /api/admin/bets/{id}/void
    Void and refund a bet.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        bet_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid bet ID."}, status=400)

    if not _check_bet_access(actor_id, bet_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        result = await asyncio.to_thread(database.void_user_bet, bet_id, actor_id)
        return web.json_response({"status": "ok", "void": result, "message": f"Bet #{bet_id} voided and refunded {result['refunded_amount']} coins."})
    except ValueError as e:
        return web.json_response({"status": "error", "message": str(e)}, status=400)
    except Exception as e:
        logger.exception("Error voiding bet")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_audit_log(request: web.Request) -> web.Response:
    """
    GET /api/admin/audit-log?entity_type=market&division_id=X&limit=50
    Fetch betting audit log.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)
    if not (_is_global_admin(actor_id) or _get_division_admin_divisions(actor_id)):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    entity_type = request.query.get("entity_type")
    division_id = query_int(request, "division_id", None)
    limit = min(100, query_int(request, "limit", 50, minimum=1))
    offset = query_int(request, "offset", 0, minimum=0)

    # Division admins can only see their division's audit log
    if not _is_global_admin(actor_id):
        allowed = _get_division_admin_divisions(actor_id)
        if division_id is None:
            division_id = allowed[0] if allowed else None
        elif division_id not in allowed:
            return web.json_response({"status": "error", "error": "forbidden", "message": "Access restricted to your division."}, status=403)

    try:
        logs = await asyncio.to_thread(database.get_betting_audit_log, 
            limit=limit,
            offset=offset,
            entity_type=entity_type,
            division_id=division_id,
        )
        return web.json_response({"status": "ok", "audit_log": logs, "count": len(logs)})
    except Exception as e:
        logger.exception("Error fetching audit log")
        return web.json_response({"status": "error", "message": str(e)}, status=500)
