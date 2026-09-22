"""
api/routes_admin_live.py

Admin Live Center & Safety Controls (Phase 6):
1. GET /api/admin/live/overview - Live matches, provider status, stale matches, markets overview.
2. POST /api/admin/live/markets/{id}/suspend - Suspend market with mandatory reason and audit log.
3. POST /api/admin/live/markets/{id}/resume - Resume suspended market with reason.
4. POST /api/admin/live/markets/{id}/close - Force close market.
5. POST /api/admin/live/markets/{id}/void - Destructive void market with refunds and confirmation.
6. POST /api/admin/live/matches/{id}/correction - Result correction flow with audit log and confirmation.
7. POST /api/admin/live/matches/{id}/refresh - Force sync from provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

import database
from api.auth import get_authenticated_user
from api.params import body_int
import config
from services.sports_provider import get_sports_data_provider

logger = logging.getLogger(__name__)


def _get_actor_id(request: web.Request) -> int | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_info = get_authenticated_user(init_data)
    if not user_info or "id" not in user_info:
        return None
    return user_info["id"]


def _is_global_admin(actor_id: int) -> bool:
    return actor_id in config.ADMIN_IDS


def _get_division_admin_divisions(actor_id: int) -> list[int]:
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


def _can_manage_match(actor_id: int, match_id: int) -> bool:
    if _is_global_admin(actor_id):
        return True
    allowed_divisions = _get_division_admin_divisions(actor_id)
    if not allowed_divisions:
        return False
    try:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT division_id FROM matches WHERE id = ?", (match_id,))
            row = cursor.fetchone()
            return bool(row and row["division_id"] in allowed_divisions)
    except Exception:
        return False


def _can_manage_market(actor_id: int, market_id: int) -> bool:
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
            return bool(row and row["division_id"] in allowed_divisions)
    except Exception:
        return False


async def handle_admin_live_overview(request: web.Request) -> web.Response:
    """
    GET /api/admin/live/overview
    Lists live matches, active/suspended markets, provider status, and freshness.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    if not (_is_global_admin(actor_id) or _get_division_admin_divisions(actor_id)):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    allowed_divisions = _get_division_admin_divisions(actor_id) if not _is_global_admin(actor_id) else None

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Provider sync state
        cursor.execute("SELECT provider, last_sync_at, status, last_error FROM provider_sync_state")
        provider_rows = [dict(r) for r in cursor.fetchall()]

        # 2. Live matches
        query = """
            SELECT m.id, m.season_id, m.division_id, m.round_number,
                   m.player1_team, m.player2_team, m.status as match_status,
                   lms.period, lms.minute, lms.home_score, lms.away_score,
                   lms.last_updated_at, lms.provider,
                   strftime('%s', 'now', '+3 hours') - strftime('%s', lms.last_updated_at) as freshness_age_sec,
                   (SELECT COUNT(*) FROM markets WHERE match_id = m.id AND status = 'open') as open_markets,
                   (SELECT COUNT(*) FROM markets WHERE match_id = m.id AND status = 'suspended') as suspended_markets
            FROM matches m
            LEFT JOIN live_match_states lms ON m.id = lms.match_id
            -- live_match_states stores the state machine's upper-case statuses.
            -- Parenthesised so the division filter below scopes both branches.
            WHERE (m.status IN ('live', 'open') OR lms.status IN ('LIVE', 'HALFTIME'))
        """
        params: list[Any] = []
        if allowed_divisions is not None:
            placeholders = ",".join("?" for _ in allowed_divisions)
            query += f" AND m.division_id IN ({placeholders})"
            params.extend(allowed_divisions)

        query += " ORDER BY m.id DESC"
        cursor.execute(query, params)
        matches = []
        for r in cursor.fetchall():
            d = dict(r)
            freshness_age = d.get("freshness_age_sec")
            d["is_stale"] = bool(freshness_age is not None and int(freshness_age) > 180)
            matches.append(d)

    return web.json_response({
        "status": "ok",
        "provider_sync": provider_rows,
        "live_matches": matches
    })


async def handle_admin_suspend_market(request: web.Request) -> web.Response:
    """
    POST /api/admin/live/markets/{id}/suspend
    Body: {"reason": "VAR review"}
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        market_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid market ID."}, status=400)

    if not _can_manage_market(actor_id, market_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    reason = body.get("reason", "").strip()
    if not reason:
        return web.json_response({"status": "error", "message": "Reason is required for market suspension."}, status=400)

    try:
        res = await asyncio.to_thread(database.transition_market_status, market_id, "suspended", actor_id)
        await asyncio.to_thread(database.log_admin_action, 
            admin_id=actor_id,
            action="live_market_suspend",
            target_type="market",
            target_id=market_id,
            old_value=res.get("old_status"),
            new_value="suspended",
            reason=reason
        )
        return web.json_response({"status": "ok", "market": res})
    except ValueError as e:
        return web.json_response({"status": "error", "error": "INVALID_TRANSITION", "message": str(e)}, status=409)
    except Exception as e:
        logger.exception("Failed to suspend market %s", market_id)
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_resume_market(request: web.Request) -> web.Response:
    """
    POST /api/admin/live/markets/{id}/resume
    Body: {"reason": "Play resumed"}
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        market_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid market ID."}, status=400)

    if not _can_manage_market(actor_id, market_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    reason = body.get("reason", "").strip()
    if not reason:
        return web.json_response({"status": "error", "message": "Reason is required to resume market."}, status=400)

    try:
        res = await asyncio.to_thread(database.transition_market_status, market_id, "open", actor_id)
        await asyncio.to_thread(database.log_admin_action, 
            admin_id=actor_id,
            action="live_market_resume",
            target_type="market",
            target_id=market_id,
            old_value=res.get("old_status"),
            new_value="open",
            reason=reason
        )
        return web.json_response({"status": "ok", "market": res})
    except ValueError as e:
        return web.json_response({"status": "error", "error": "INVALID_TRANSITION", "message": str(e)}, status=409)
    except Exception as e:
        logger.exception("Failed to resume market %s", market_id)
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_close_market(request: web.Request) -> web.Response:
    """
    POST /api/admin/live/markets/{id}/close
    Body: {"reason": "Match 90th minute"}
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        market_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid market ID."}, status=400)

    if not _can_manage_market(actor_id, market_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    reason = body.get("reason", "").strip()
    if not reason:
        return web.json_response({"status": "error", "message": "Reason is required to close market."}, status=400)

    try:
        res = await asyncio.to_thread(database.transition_market_status, market_id, "closed", actor_id)
        await asyncio.to_thread(database.log_admin_action, 
            admin_id=actor_id,
            action="live_market_close",
            target_type="market",
            target_id=market_id,
            old_value=res.get("old_status"),
            new_value="closed",
            reason=reason
        )
        return web.json_response({"status": "ok", "market": res})
    except ValueError as e:
        return web.json_response({"status": "error", "error": "INVALID_TRANSITION", "message": str(e)}, status=409)
    except Exception as e:
        logger.exception("Failed to close market %s", market_id)
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_admin_void_market(request: web.Request) -> web.Response:
    """
    POST /api/admin/live/markets/{id}/void
    Body: {"reason": "Technical issue", "confirm": true}
    Destructive: аннулирует рынок и разбирает затронутые купоны — правила
    возврата, реестра и аудита принадлежат database.void_market (единый владелец).
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        market_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid market ID."}, status=400)

    if not _can_manage_market(actor_id, market_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON body."}, status=400)

    if not body.get("confirm"):
        return web.json_response({"status": "error", "message": "Confirmation is required to void market."}, status=400)

    reason = body.get("reason", "").strip()
    if not reason:
        return web.json_response({"status": "error", "message": "Reason is required to void market."}, status=400)

    try:
        result = await asyncio.to_thread(database.void_market, market_id, actor_id, reason)
    except ValueError as e:
        message = str(e)
        code = 404 if "not found" in message.lower() else 409
        return web.json_response({"status": "error", "message": message}, status=code)
    except Exception as e:
        logger.exception("Failed to void market %s", market_id)
        return web.json_response({"status": "error", "message": str(e)}, status=500)

    await asyncio.to_thread(database.log_admin_action,
        admin_id=actor_id,
        action="live_market_void",
        target_type="market",
        target_id=market_id,
        old_value=result["old_status"],
        new_value="voided",
        reason=reason,
        division_id=result["division_id"],
        season_id=result["season_id"]
    )

    refunded = result["refunded_bets"]
    return web.json_response({
        "status": "ok",
        "message": (f"Market {market_id} voided: {result['voided_legs']} leg(s) refunded, "
                    f"{len(refunded)} bet(s) returned ({result['refunded_stake']} coins), "
                    f"{result['pending_coupons']} bet(s) still in play."),
        "refunded_count": len(refunded),
        "voided_legs": result["voided_legs"],
        "refunded_stake": result["refunded_stake"],
        "pending_coupons": result["pending_coupons"],
        "already_voided": result["already_voided"]
    })


async def handle_admin_match_correction(request: web.Request) -> web.Response:
    """
    POST /api/admin/live/matches/{id}/correction
    Body: {
        "home_score": 2,
        "away_score": 1,
        "reason": "VAR confirmed goal correction",
        "confirm": true,
        "status": "finished" (optional)
    }
    Strict Result Correction Flow (Step 38).
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid match ID."}, status=400)

    if not _can_manage_match(actor_id, match_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON body."}, status=400)

    if not body.get("confirm"):
        return web.json_response({"status": "error", "message": "Explicit confirmation required for score correction."}, status=400)

    reason = body.get("reason", "").strip()
    if not reason:
        return web.json_response({"status": "error", "message": "Explicit reason is required for score correction."}, status=400)

    if "home_score" not in body or "away_score" not in body:
        return web.json_response({"status": "error", "message": "home_score and away_score are required."}, status=400)

    new_home = body_int(body, "home_score", minimum=0)
    new_away = body_int(body, "away_score", minimum=0)
    new_status = body.get("status")

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        match_row = cursor.fetchone()
        if not match_row:
            return web.json_response({"status": "error", "message": "Match not found."}, status=404)

        cursor.execute("SELECT * FROM live_match_states WHERE match_id = ?", (match_id,))
        lms_row = cursor.fetchone()

        old_state = {
            "player1_score": match_row["player1_score"],
            "player2_score": match_row["player2_score"],
            "status": match_row["status"]
        }
        new_state = {
            "player1_score": new_home,
            "player2_score": new_away,
            "status": new_status or match_row["status"],
            "reason": reason
        }

        # Update matches table
        if new_status:
            cursor.execute("""
                UPDATE matches
                SET player1_score = ?, player2_score = ?, status = ?
                WHERE id = ?
            """, (new_home, new_away, new_status, match_id))
        else:
            cursor.execute("""
                UPDATE matches
                SET player1_score = ?, player2_score = ?
                WHERE id = ?
            """, (new_home, new_away, match_id))

        # Update or insert live_match_states
        if lms_row:
            cursor.execute("""
                UPDATE live_match_states
                SET home_score = ?, away_score = ?, status = COALESCE(?, status),
                    version = version + 1, last_updated_at = datetime('now', '+3 hours')
                WHERE match_id = ?
            """, (new_home, new_away, new_status, match_id))
        else:
            cursor.execute("""
                INSERT INTO live_match_states (
                    match_id, season_id, division_id, status, period, minute,
                    home_score, away_score, provider, provider_match_id, version, last_updated_at
                ) VALUES (?, ?, ?, ?, 'regular', 90, ?, ?, 'manual_admin', ?, 1, datetime('now', '+3 hours'))
            """, (match_id, match_row["season_id"], match_row["division_id"],
                  new_status or "live", new_home, new_away, str(match_id)))

        # Audit logs — synchronous on purpose: transaction() is thread-local and a
        # to_thread hop inside this open transaction would deadlock on the write lock.
        database.log_admin_action(
            admin_id=actor_id,
            action="match_result_correction",
            target_type="match",
            target_id=match_id,
            old_value=json.dumps(old_state, ensure_ascii=False),
            new_value=json.dumps(new_state, ensure_ascii=False),
            reason=reason,
            division_id=match_row["division_id"],
            season_id=match_row["season_id"]
        )

        database.write_bet_audit_log(
            actor_id=actor_id,
            action="result_correction",
            entity_type="match",
            entity_id=match_id,
            old_value=old_state,
            new_value=new_state,
            division_id=match_row["division_id"],
            season_id=match_row["season_id"]
        )

    try:
        from services.season_progression import SeasonProgressionEngine
        SeasonProgressionEngine.recalculate_competitive_stats_for_match(match_id)
    except Exception as e:
        logger.warning(f"Error recalculating competitive stats on correction for match #{match_id}: {e}")

    logger.info("Admin %s applied result correction to match %s: %s:%s (reason: %s)",
                actor_id, match_id, new_home, new_away, reason)

    return web.json_response({
        "status": "ok",
        "match_id": match_id,
        "old_state": old_state,
        "new_state": new_state,
        "message": "Result correction applied and audited successfully."
    })


async def handle_admin_refresh_match(request: web.Request) -> web.Response:
    """
    POST /api/admin/live/matches/{id}/refresh
    Trigger a manual sync from provider.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    try:
        match_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"status": "error", "message": "Invalid match ID."}, status=400)

    if not _can_manage_match(actor_id, match_id):
        return web.json_response({"status": "error", "error": "forbidden"}, status=403)

    provider = get_sports_data_provider()
    sync_status = provider.get_sync_status()

    return web.json_response({
        "status": "ok",
        "match_id": match_id,
        "provider": provider.provider_name,
        "provider_status": sync_status,
        "message": f"Provider {provider.provider_name} status checked."
    })


async def handle_admin_sports_health(request: web.Request) -> web.Response:
    """
    GET /api/admin/sports/health
    Strictly Global Admin only (RBAC enforced).
    Returns provider status, health metrics, latency, rate-limit state, circuit breaker,
    and stale match counts. Never exposes API key or sensitive credentials.
    """
    actor_id = _get_actor_id(request)
    if not actor_id:
        return web.json_response({"status": "error", "error": "unauthorized"}, status=401)

    if not _is_global_admin(actor_id):
        return web.json_response({
            "status": "error",
            "error": "forbidden",
            "message": "Global Admin privilege required."
        }, status=403)

    provider = get_sports_data_provider()
    health_status = provider.get_provider_status()

    # Redact any sensitive information
    if isinstance(health_status, dict):
        for secret_field in ("api_key", "key", "token", "secret", "headers", "authorization"):
            health_status.pop(secret_field, None)

    # Count stale live matches
    try:
        health_status["stale_matches_count"] = await asyncio.to_thread(database.get_stale_provider_matches_count)
    except Exception as e:
        logger.warning("Failed to count stale provider matches: %s", e)
        health_status["stale_matches_count"] = 0

    return web.json_response({
        "status": "ok",
        "data": health_status
    })

