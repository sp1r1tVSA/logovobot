"""
services/background_sync.py

Phase 6 Background Jobs for Live Sync, Intelligence, and Notification Delivery.
Uses telegram.ext.JobQueue compatible coroutines:
1. sync_live_provider_job: Safe, idempotent sync with external sports provider.
2. sync_intelligence_cache_job: Pre-computes hot scores and intelligence snapshots.
3. process_notification_queue_job: Dispatches pending notification_events via Telegram bot.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

import config
import database
from services.intelligence_engine import IntelligenceEngine
from services.notification_service import mark_notification_sent
from services.sports_provider import get_sports_data_provider

logger = logging.getLogger(__name__)


async def sync_live_provider_job(context: Any) -> None:
    """
    Periodic job to sync live matches from external sports data provider.
    Runs every 30-60 seconds.
    If provider is Null or unconfigured, records status and exits cleanly (ZERO fake data).
    """
    try:
        provider = get_sports_data_provider()
        sync_status = provider.get_sync_status()

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO provider_sync_state (provider, last_sync_at, status, last_error)
                VALUES (?, CURRENT_TIMESTAMP, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    last_sync_at = CURRENT_TIMESTAMP,
                    status = excluded.status,
                    last_error = excluded.last_error
            """, (provider.provider_name, sync_status.get("status", "unknown"), sync_status.get("last_error")))

        if not provider.is_connected:
            logger.debug("Live sync skipped: provider '%s' is not connected.", provider.provider_name)
            return

        # Connected provider ingestion
        live_matches = await provider.get_live_matches()
        logger.info("Live provider synced %d active matches.", len(live_matches))
    except Exception as e:
        logger.error("Error in sync_live_provider_job: %s", e, exc_info=True)


async def sync_intelligence_cache_job(context: Any) -> None:
    """
    Periodic job to refresh intelligence calculations for open and live matches.
    Runs every 5 minutes.
    """
    try:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM matches
                WHERE status IN ('open', 'live')
                ORDER BY id DESC
                LIMIT 20
            """)
            match_ids = [r["id"] for r in cursor.fetchall()]

        for mid in match_ids:
            try:
                # Precompute intelligence
                IntelligenceEngine.get_match_intelligence(mid)
            except Exception as e:
                logger.debug("Failed precomputing intelligence for match %s: %s", mid, e)
    except Exception as e:
        logger.error("Error in sync_intelligence_cache_job: %s", e)


async def process_notification_queue_job(context: Any) -> None:
    """
    Dispatches pending notifications to users via Telegram bot.
    Runs every 10-15 seconds.

    Bet settlement notices (BET_SETTLED) are always delivered: they are the
    only way a user learns a bet won or was refunded. The rest of the smart
    notifications stay behind SMART_NOTIFICATIONS_ENABLED.
    """
    if not hasattr(context, "bot") or context.bot is None:
        return

    event_types = None
    if not getattr(config, "SMART_NOTIFICATIONS_ENABLED", False):
        event_types = (database.BET_SETTLED_EVENT,)

    try:
        pending = await asyncio.to_thread(database.get_pending_notification_events, 25, event_types)

        for item in pending:
            ev_id = item["id"]
            uid = item["user_id"]
            text = f"<b>{item['title']}</b>\n{item.get('body') or ''}"
            if not uid or uid <= 0:
                # Pre-registered coaches hold placeholder negative ids — no chat to reach.
                database.mark_notification_event_failed(ev_id)
                continue
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML"
                )
                mark_notification_sent(ev_id)
            except RetryAfter as ra:
                # Flood control: leave the rest pending for the next tick.
                logger.warning("Notification queue throttled for %ss; pausing", ra.retry_after)
                break
            except BadRequest as br:
                # Permanent (chat not found, bad markup) — BadRequest subclasses
                # NetworkError in PTB, so it must be caught first.
                logger.warning("Failed to send notification %s to user %s: %s", ev_id, uid, br)
                database.mark_notification_event_failed(ev_id)
            except NetworkError as ne:
                # Transient (timeout, connection reset) — keep it pending and retry.
                logger.warning("Network error sending notification %s: %s", ev_id, ne)
                break
            except TelegramError as te:
                logger.warning("Failed to send notification %s to user %s: %s", ev_id, uid, te)
                # Mark failed or leave pending with attempt limit
                database.mark_notification_event_failed(ev_id)
            except Exception as e:
                logger.warning("Unexpected error sending notification %s: %s", ev_id, e)
                database.mark_notification_event_failed(ev_id)
    except Exception as e:
        logger.error("Error in process_notification_queue_job: %s", e)


async def settle_finished_bets_job(context: Any) -> None:
    """
    Settle bets on matches that have finished.

    Previously this sweep ran synchronously inside the Mini App request handlers
    (GET /api/predictions and the wallet bootstrap). Because the API server shares
    the bot's event loop, that stalled Telegram handling on every request. It now
    runs here on a fixed schedule, off the loop.
    """
    try:
        settled = await asyncio.to_thread(database.settle_all_pending_finished_matches)
        if settled:
            logger.info("Settled %d finished bet(s).", len(settled))
    except Exception as e:
        logger.error("Error in settle_finished_bets_job: %s", e, exc_info=True)


async def scan_integrity_job(context: Any) -> None:
    """
    Детектор договорных матчей: два прохода по ставкам.

    1. Онлайн — новые ноги ставок, для которых дела ещё нет.
    2. Постматчевый — дела, чей матч уже подтверждён, а нога рассчитана.

    Джоба намеренно живёт здесь, а не в `place_user_bet`: размещение
    синхронно и работает под локом, а прогон ансамбля внутри лока подвесил бы
    ставки всем. Ничего не блокируется, поэтому задержка до двух минут ничего
    не стоит.
    """
    if not getattr(config, "INTEGRITY_ENABLED", True):
        return

    try:
        import json

        from services.integrity_engine import CASE_MIN_SCORE, score_case

        # Ансамбль тяжёлый, а матчи в экспрессах и у разных игроков
        # повторяются: считаем прогноз один раз на матч за прогон.
        pred_cache: dict[int, dict | None] = {}

        def _predict(match_id: int) -> dict | None:
            if match_id in pred_cache:
                return pred_cache[match_id]
            try:
                from services.ensemble_engine import EnsemblePredictionEngine
                pred_cache[match_id] = EnsemblePredictionEngine.predict_match(match_id)
            except Exception as e:
                logger.debug("Integrity: no prediction for match %s: %s", match_id, e)
                pred_cache[match_id] = None
            return pred_cache[match_id]

        def _process(items: list[dict], resolved: bool) -> int:
            reported = 0
            for item in items:
                try:
                    pred = _predict(item["match_id"])
                    profile = database.get_user_bet_profile(
                        item["user_id"], before=item.get("placed_at")
                    )
                    market_odds = database.get_market_odds_snapshot(item.get("market_id"))
                    volume = None
                    if resolved:
                        volume = database.get_selection_volume(
                            item["match_id"], item.get("selection_id"), item["user_id"]
                        )

                    case = score_case(
                        item, profile, pred,
                        market_odds=market_odds, volume=volume, resolved=resolved,
                    )
                    database.upsert_integrity_case(
                        bet_id=case["bet_id"],
                        bet_item_id=case["bet_item_id"],
                        user_id=case["user_id"],
                        match_id=case["match_id"],
                        division_id=case["division_id"],
                        season_id=case["season_id"],
                        online_score=case["online_score"],
                        post_score=case["post_score"],
                        total_score=case["total_score"],
                        severity=case["severity"],
                        stage=case["stage"],
                        low_confidence=case["low_confidence"],
                        features=json.dumps(case["features"], ensure_ascii=False),
                    )
                    if case["reportable"]:
                        reported += 1
                        _mirror_to_risk_feed(case, item)
                except Exception as e:
                    logger.warning(
                        "Integrity scoring failed for bet item %s: %s",
                        item.get("bet_item_id"), e
                    )
            return reported

        online_items = await asyncio.to_thread(database.get_unscored_bet_items, 50)
        online_flagged = await asyncio.to_thread(_process, online_items, False)

        resolvable = await asyncio.to_thread(database.get_resolvable_integrity_cases, 50)
        post_flagged = await asyncio.to_thread(_process, resolvable, True)

        if online_flagged or post_flagged:
            logger.info(
                "Integrity scan: %d new + %d resolved case(s) above %.0f.",
                online_flagged, post_flagged, CASE_MIN_SCORE
            )
    except Exception as e:
        logger.error("Error in scan_integrity_job: %s", e, exc_info=True)


def _mirror_to_risk_feed(case: dict, item: dict) -> None:
    """
    Дублировать тяжёлое дело в общую ленту рисков.

    Источник истины — integrity_cases: у create_risk_alert дедуп на пять минут
    без user_id и bet_id, поэтому две разные подозрительные ставки на один матч
    там схлопнулись бы в один алерт. Сюда попадают только high/critical.
    """
    threshold = getattr(config, "INTEGRITY_ALERT_THRESHOLD", 70)
    if case["total_score"] < threshold:
        return
    try:
        from services.risk_alerts import create_risk_alert

        who = item.get("user_team") or item.get("username") or f"ID {item.get('user_id')}"
        teams = f"{item.get('player1_team') or '?'} — {item.get('player2_team') or '?'}"
        create_risk_alert(
            alert_type="SUSPICIOUS_ACTIVITY",
            severity=case["severity"],
            message=(
                f"Индекс подозрительности {case['total_score']:.0f}/100: "
                f"{who}, матч {teams}, исход "
                f"{item.get('selection_name') or item.get('outcome_type') or '?'}"
            ),
            division_id=case.get("division_id"),
            match_id=case.get("match_id"),
            market_id=item.get("market_id"),
            selection_id=item.get("selection_id"),
            details={
                "bet_id": case["bet_id"],
                "bet_item_id": case["bet_item_id"],
                "user_id": case["user_id"],
                "total_score": case["total_score"],
                "stage": case["stage"],
            },
        )
    except Exception as e:
        logger.debug("Integrity: could not mirror case to risk feed: %s", e)
