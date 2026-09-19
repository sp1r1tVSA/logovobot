"""
services/settlement_engine.py

Logovo.bet — Settlement Engine & Prediction Payouts (v2.0).
Handles atomic, idempotent settlement for:
1. Relational markets & selections (`markets`, `market_selections`).
2. Legacy `bet_markets`.
3. Single & Express prediction coupons (`user_bets`, `bet_items`).
4. Automatic wallet crediting, transaction auditing, and notification queues.
"""

import html
import math
import logging
from typing import Optional
import database
from services.bet_outcome_text import describe_selection
from services.market_settler import evaluate_market_selection

logger = logging.getLogger(__name__)


def _coins(amount: int) -> str:
    return f"{int(amount):,}".replace(",", " ")


def _bet_type_label(bet_type: Optional[str]) -> str:
    return "Экспресс" if bet_type == "express" else "Ординар"


_LEG_STATUS_EMOJI = {"won": "✅", "lost": "❌", "refunded": "🔄", "voided": "🔄", "pending": "⏳"}
_MAX_NOTICE_LEGS = 10


def _leg_lines(cursor, bet_id: int) -> tuple[list[str], bool]:
    """Строки событий купона и признак, что среди них есть отменённые.

    Уведомление — не повод откатывать расчёт: при любой ошибке события просто
    не попадут в текст.
    """
    try:
        legs = database.get_bet_legs_for_notice(cursor, bet_id)
    except Exception as e:
        logger.warning("Could not load legs for bet #%s notice: %s", bet_id, e)
        return [], False

    lines = []
    for leg in legs[:_MAX_NOTICE_LEGS]:
        t1, t2 = leg["team1_name"], leg["team2_name"]
        s1, s2 = leg["player1_score"], leg["player2_score"]
        match = f"{t1} {s1}:{s2} {t2}" if s1 is not None and s2 is not None else f"{t1} — {t2}"
        pick = describe_selection(leg["outcome_type"], t1, t2,
                                  market_key=leg["market_key"], selection_name=leg["selection_name"])
        status = leg["status"]
        odd = "возврат" if status in ("refunded", "voided") else f"@{float(leg['odd'] or 1.0):.2f}"
        lines.append(f"⚽ <b>{html.escape(match)}</b>\n"
                     f"   {_LEG_STATUS_EMOJI.get(status, '•')} {html.escape(pick)} · {odd}")
    if len(legs) > _MAX_NOTICE_LEGS:
        lines.append(f"<i>…и ещё {len(legs) - _MAX_NOTICE_LEGS} событ.</i>")
    has_voided = any(leg["status"] in ("refunded", "voided") for leg in legs)
    return lines, has_voided


def _notify_bet_won(cursor, user_id: int, bet_id: int, bet_type: Optional[str], odd: float, payout: int,
                    stake: int, balance_after: int, resettle: bool = False) -> None:
    head = "пересчитана и сыграла" if resettle else "сыграла"
    legs, has_voided = _leg_lines(cursor, bet_id)

    parts = []
    if resettle:
        parts.append("<i>Счёт матча исправлен — ставка рассчитана заново.</i>")
    if legs:
        parts.append("\n".join(legs))
    money = [
        f"💵 Ставка: <b>{_coins(stake)} 🪙</b> × {odd:.2f}",
        f"💸 Выигрыш: <b>+{_coins(payout)} 🪙</b> зачислен на баланс!",
        f"📈 Чистая прибыль: <b>+{_coins(payout - stake)} 🪙</b>",
        f"👛 Баланс: <b>{_coins(balance_after)} 🪙</b>",
    ]
    if has_voided:
        money.append("<i>Отменённые события посчитаны с кэфом 1.00.</i>")
    parts.append("\n".join(money))
    parts.append("<i>Темшик поздравляет с победным прогнозом! 🎰</i>")

    database.enqueue_bet_settled_notice(
        cursor, user_id, bet_id,
        title=f"🎉 Ваша ставка #{bet_id} ({_bet_type_label(bet_type)}) {head}!",
        body="\n\n".join(parts),
        resettle=resettle,
    )


def _notify_bet_refunded(cursor, user_id: int, bet_id: int, bet_type: Optional[str], stake: int,
                         balance_after: int, resettle: bool = False) -> None:
    legs, _ = _leg_lines(cursor, bet_id)
    parts = []
    if legs:
        parts.append("\n".join(legs))
    parts.append(f"Матч аннулирован — ставка <b>{_coins(stake)} 🪙</b> вернулась на баланс.\n"
                 f"👛 Баланс: <b>{_coins(balance_after)} 🪙</b>")
    database.enqueue_bet_settled_notice(
        cursor, user_id, bet_id,
        title=f"🔄 Возврат по ставке #{bet_id} ({_bet_type_label(bet_type)})",
        body="\n\n".join(parts),
        resettle=resettle,
    )


def settle_match_predictions(
    match_id: int,
    score1: int,
    score2: int,
    match_status: str = "finished",
    ht_score1: Optional[int] = None,
    ht_score2: Optional[int] = None
) -> list[dict]:
    """
    Settle all markets, selections, and user prediction coupons for a match.
    Returns list of payout/settlement notifications.
    """
    payout_notifications = []

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Check if match exists and update its scores / status
        cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        match_row = cursor.fetchone()
        if not match_row:
            logger.warning(f"Cannot settle match #{match_id}: match not found.")
            return []

        target_status = "confirmed" if match_row["status"] in ("confirmed", "completed") else match_status
        cursor.execute("""
            UPDATE matches
            SET player1_score = ?, player2_score = ?, ht_score1 = ?, ht_score2 = ?,
                status = ?, played_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (score1, score2, ht_score1, ht_score2, target_status, match_id))

        # 2. Settle relational markets & selections
        cursor.execute("SELECT * FROM markets WHERE match_id = ?", (match_id,))
        markets = cursor.fetchall()

        for m in markets:
            cursor.execute("SELECT * FROM market_selections WHERE market_id = ?", (m["id"],))
            selections = cursor.fetchall()
            for s in selections:
                sel_result = evaluate_market_selection(
                    market_key=m["market_key"],
                    selection_key=s["selection_key"],
                    score1=score1,
                    score2=score2,
                    match_status=match_status,
                    ht_score1=ht_score1,
                    ht_score2=ht_score2
                )
                # Keep active/voided status or store result
                new_sel_status = "voided" if sel_result == "voided" else "locked"
                cursor.execute("""
                    UPDATE market_selections
                    SET status = ?
                    WHERE id = ?
                """, (new_sel_status, s["id"]))

            cursor.execute("""
                UPDATE markets
                SET status = 'settled'
                WHERE id = ?
            """, (m["id"],))

        # 3. Settle legacy bet_markets if present
        cursor.execute("UPDATE bet_markets SET is_active = 0 WHERE match_id = ?", (match_id,))

        # 4. Settle individual bet_items for this match
        cursor.execute("""
            SELECT bi.*, m.market_key
            FROM bet_items bi
            LEFT JOIN markets m ON bi.market_id = m.id
            WHERE bi.match_id = ? AND bi.status = 'pending'
        """, (match_id,))
        pending_items = cursor.fetchall()

        for item in pending_items:
            m_key = item["market_key"] or "1x2"
            outcome_type = item["outcome_type"]

            # Map legacy outcome types to standard market keys if needed
            if outcome_type in ("p1", "x", "p2"):
                m_key = "1x2"
            elif outcome_type in ("tb25", "tm25", "over_2.5", "under_2.5"):
                m_key = "total_goals"
            elif outcome_type in ("btts_yes", "btts_no"):
                m_key = "btts"

            item_result = evaluate_market_selection(
                market_key=m_key,
                selection_key=outcome_type,
                score1=score1,
                score2=score2,
                match_status=match_status,
                ht_score1=ht_score1,
                ht_score2=ht_score2
            )

            # SQLite check constraint on bet_items expects 'refunded' instead of 'voided'
            db_item_status = "refunded" if item_result == "voided" else item_result

            cursor.execute("""
                UPDATE bet_items
                SET status = ?
                WHERE id = ?
            """, (db_item_status, item["id"]))

        # 5. Settle user_bets that have unsettled status
        cursor.execute("""
            SELECT DISTINCT b.*
            FROM user_bets b
            JOIN bet_items bi ON b.id = bi.bet_id
            WHERE bi.match_id = ? AND b.status = 'pending' AND b.settled_at IS NULL
        """, (match_id,))
        affected_bets = cursor.fetchall()

        for bet in affected_bets:
            b_id = bet["id"]
            u_id = bet["user_id"]
            stake = bet["amount"]

            cursor.execute("SELECT * FROM bet_items WHERE bet_id = ?", (b_id,))
            all_items = cursor.fetchall()

            has_lost = any(i["status"] == "lost" for i in all_items)
            has_pending = any(i["status"] == "pending" for i in all_items)
            all_voided = all(i["status"] in ("voided", "refunded") for i in all_items)

            if has_lost:
                # Bet is LOST immediately
                cursor.execute("""
                    UPDATE user_bets
                    SET status = 'lost', actual_payout = 0, settled_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND settled_at IS NULL
                """, (b_id,))
                if cursor.rowcount > 0:
                    try:
                        from services.player_rating import PlayerRatingEngine
                        from services.streak_engine import StreakEngine
                        from services.leaderboard_service import invalidate_leaderboard_cache
                        PlayerRatingEngine.process_bet_settlement(
                            user_id=u_id, outcome="lost", total_odd=float(bet["total_odd"] or 1.0),
                            stake=stake, payout=0
                        )
                        StreakEngine.process_bet_outcome(u_id, "lost")
                        invalidate_leaderboard_cache()
                    except Exception as e:
                        logger.warning(f"Error updating loss rating/streak for user #{u_id}: {e}")
                continue

            if has_pending:
                # Other legs in express are still waiting for their matches to finish
                continue

            if all_voided:
                # Full refund of stake
                cursor.execute("""
                    UPDATE user_bets
                    SET status = 'refunded', actual_payout = ?, settled_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND settled_at IS NULL
                """, (stake, b_id))

                if cursor.rowcount == 0:
                    continue

                database.get_or_create_wallet(u_id)
                cursor.execute("""
                    UPDATE user_wallets
                    SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (stake, u_id))

                cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
                bal_after = cursor.fetchone()["balance"]

                cursor.execute("""
                    INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after)
                    VALUES (?, ?, 'refund', ?, 'bet', ?)
                """, (u_id, stake, b_id, bal_after))
                _notify_bet_refunded(cursor, u_id, b_id, bet["bet_type"], stake, bal_after)

                try:
                    from services.player_rating import PlayerRatingEngine
                    from services.streak_engine import StreakEngine
                    from services.leaderboard_service import invalidate_leaderboard_cache
                    PlayerRatingEngine.process_bet_settlement(
                        user_id=u_id, outcome="refunded", total_odd=float(bet["total_odd"] or 1.0),
                        stake=stake, payout=stake
                    )
                    StreakEngine.process_bet_outcome(u_id, "refunded")
                    invalidate_leaderboard_cache()
                except Exception as e:
                    logger.warning(f"Error updating refund rating/streak for user #{u_id}: {e}")

                payout_notifications.append({
                    "user_id": u_id,
                    "bet_id": b_id,
                    "status": "refunded",
                    "payout": stake,
                    "message": f"🔄 Прогноз #{b_id} возвращен (+{stake} 🪙)"
                })
                continue

            # All legs are settled and 0 lost -> Bet is WON!
            # Calculate combined odds of winning legs (voided legs count as 1.00)
            effective_odd = 1.0
            for i in all_items:
                if i["status"] == "won":
                    effective_odd *= float(i["odd"])

            effective_odd_rounded = round(effective_odd, 2)
            payout = int(round(stake * effective_odd_rounded))

            cursor.execute("""
                UPDATE user_bets
                SET status = 'won', actual_payout = ?, settled_at = CURRENT_TIMESTAMP
                WHERE id = ? AND settled_at IS NULL
            """, (payout, b_id))

            if cursor.rowcount == 0:
                continue

            database.get_or_create_wallet(u_id)
            cursor.execute("""
                UPDATE user_wallets
                SET balance = balance + ?,
                    total_won = total_won + ?,
                    bets_won = bets_won + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (payout, payout, u_id))

            cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
            bal_after = cursor.fetchone()["balance"]

            cursor.execute("""
                INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after)
                VALUES (?, ?, 'bet_won', ?, 'bet', ?)
            """, (u_id, payout, b_id, bal_after))
            _notify_bet_won(cursor, u_id, b_id, bet["bet_type"], effective_odd_rounded, payout, stake, bal_after)

            # Trigger progression, streak, rating & achievement hooks
            try:
                from services.player_rating import PlayerRatingEngine
                from services.streak_engine import StreakEngine
                from services.leaderboard_service import invalidate_leaderboard_cache
                database.add_user_xp(u_id, 100)
                database.evaluate_betting_achievements(u_id, dict(bet))
                PlayerRatingEngine.process_bet_settlement(
                    user_id=u_id, outcome="won", total_odd=effective_odd,
                    stake=stake, payout=payout
                )
                StreakEngine.process_bet_outcome(u_id, "won")
                invalidate_leaderboard_cache()
            except Exception as e:
                logger.warning(f"Error triggering win hooks for user #{u_id}: {e}")

            payout_notifications.append({
                "user_id": u_id,
                "bet_id": b_id,
                "status": "won",
                "bet_type": bet["bet_type"],
                "amount": stake,
                "total_odd": round(effective_odd, 2),
                "payout": payout,
                "message": f"🎉 Прогноз #{b_id} ВЫИГРАЛ (+{payout} 🪙)!"
            })

    return payout_notifications


def refund_match_bets(match_id: int) -> list[dict]:
    """
    Cancel/refund all pending bets for a specific match.
    Marks bet_items as 'refunded'.
    For single bets: marks user_bets as 'refunded', refunds full stake to user wallet.
    """
    refund_notifications = []
    with database.transaction() as conn:
        cursor = conn.cursor()

        # Update matches status to cancelled
        cursor.execute("UPDATE matches SET status = 'cancelled' WHERE id = ?", (match_id,))

        # Update bet items
        cursor.execute("UPDATE bet_items SET status = 'refunded' WHERE match_id = ? AND status = 'pending'", (match_id,))

        # Find all pending user_bets that have items from this match
        cursor.execute("""
            SELECT DISTINCT b.*
            FROM user_bets b
            JOIN bet_items bi ON b.id = bi.bet_id
            WHERE bi.match_id = ? AND b.status = 'pending'
        """, (match_id,))
        bets = [dict(r) for r in cursor.fetchall()]

        for bet in bets:
            b_id = bet["id"]
            u_id = bet["user_id"]
            stake = bet["amount"]

            cursor.execute("SELECT * FROM bet_items WHERE bet_id = ?", (b_id,))
            items = cursor.fetchall()
            all_refunded = all(it["status"] == "refunded" for it in items)

            if bet["bet_type"] == "single" or all_refunded:
                cursor.execute("""
                    UPDATE user_bets
                    SET status = 'refunded', actual_payout = ?, settled_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND settled_at IS NULL
                """, (stake, b_id))

                if cursor.rowcount > 0:
                    database.get_or_create_wallet(u_id)
                    cursor.execute("""
                        UPDATE user_wallets
                        SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = ?
                    """, (stake, u_id))

                    cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
                    bal_after = cursor.fetchone()["balance"]

                    cursor.execute("""
                        INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after)
                        VALUES (?, ?, 'bet_refund', ?, 'bet', ?)
                    """, (u_id, stake, b_id, bal_after))
                    _notify_bet_refunded(cursor, u_id, b_id, bet["bet_type"], stake, bal_after)

                    refund_notifications.append({
                        "user_id": u_id,
                        "bet_id": b_id,
                        "status": "refunded",
                        "amount": stake,
                        "payout": stake
                    })

    return refund_notifications


settle_match_result = settle_match_predictions


def resettle_match_predictions(
    match_id: int,
    score1: int,
    score2: int,
    match_status: str = "finished",
    ht_score1: Optional[int] = None,
    ht_score2: Optional[int] = None
) -> list[dict]:
    """
    Idempotent resettle routine for disputed or corrected match results (LB-14).
    1. Reverts previous payouts/refunds for affected bets (except cashed_out bets).
    2. Re-evaluates market selections and bet items with the corrected score.
    3. Settles user bets to their new state (won, lost, refunded, or pending).
    4. Audits all financial balance adjustments with 'resettle_reversal' and 'resettle_payout'.
    """
    notifications = []

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Update match scores and status
        cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
        match_row = cursor.fetchone()
        if not match_row:
            logger.warning(f"Cannot resettle match #{match_id}: match not found.")
            return []

        target_status = "confirmed" if match_row["status"] in ("confirmed", "completed") else match_status
        cursor.execute("""
            UPDATE matches
            SET player1_score = ?, player2_score = ?, ht_score1 = ?, ht_score2 = ?,
                status = ?, played_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (score1, score2, ht_score1, ht_score2, target_status, match_id))

        # 2. Re-evaluate relational markets and market selections
        cursor.execute("SELECT * FROM markets WHERE match_id = ?", (match_id,))
        markets = cursor.fetchall()
        for m in markets:
            cursor.execute("SELECT * FROM market_selections WHERE market_id = ?", (m["id"],))
            selections = cursor.fetchall()
            for s in selections:
                sel_result = evaluate_market_selection(
                    market_key=m["market_key"],
                    selection_key=s["selection_key"],
                    score1=score1,
                    score2=score2,
                    match_status=match_status,
                    ht_score1=ht_score1,
                    ht_score2=ht_score2
                )
                new_sel_status = "voided" if sel_result == "voided" else "locked"
                cursor.execute("""
                    UPDATE market_selections
                    SET status = ?
                    WHERE id = ?
                """, (new_sel_status, s["id"]))

        # 3. Re-evaluate bet_items for this match
        cursor.execute("""
            SELECT bi.*, m.market_key
            FROM bet_items bi
            LEFT JOIN markets m ON bi.market_id = m.id
            WHERE bi.match_id = ?
        """, (match_id,))
        match_items = cursor.fetchall()

        for item in match_items:
            m_key = item["market_key"] or "1x2"
            outcome_type = item["outcome_type"]
            if outcome_type in ("p1", "x", "p2"):
                m_key = "1x2"
            elif outcome_type in ("tb25", "tm25", "over_2.5", "under_2.5"):
                m_key = "total_goals"
            elif outcome_type in ("btts_yes", "btts_no"):
                m_key = "btts"

            item_result = evaluate_market_selection(
                market_key=m_key,
                selection_key=outcome_type,
                score1=score1,
                score2=score2,
                match_status=match_status,
                ht_score1=ht_score1,
                ht_score2=ht_score2
            )
            db_item_status = "refunded" if item_result == "voided" else item_result
            cursor.execute("""
                UPDATE bet_items
                SET status = ?
                WHERE id = ?
            """, (db_item_status, item["id"]))

        # 4. Find all affected user_bets (exclude early cashout bets)
        cursor.execute("""
            SELECT DISTINCT b.*
            FROM user_bets b
            JOIN bet_items bi ON b.id = bi.bet_id
            WHERE bi.match_id = ? AND b.status != 'cashed_out' AND b.cashout_at IS NULL
        """, (match_id,))
        affected_bets = cursor.fetchall()

        for bet in affected_bets:
            b_id = bet["id"]
            u_id = bet["user_id"]
            stake = bet["amount"]
            prev_status = bet["status"]
            prev_payout = bet["actual_payout"] or 0

            # 4a. Revert previous settlement if bet was already settled
            if bet["settled_at"] is not None and prev_payout > 0:
                database.get_or_create_wallet(u_id)
                cursor.execute("""
                    UPDATE user_wallets
                    SET balance = balance - ?,
                        total_won = CASE WHEN ? = 'won' THEN max(0, total_won - ?) ELSE total_won END,
                        bets_won = CASE WHEN ? = 'won' THEN max(0, bets_won - 1) ELSE bets_won END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (prev_payout, prev_status, prev_payout, prev_status, u_id))

                cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
                bal_rev = cursor.fetchone()["balance"]
                cursor.execute("""
                    INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after)
                    VALUES (?, ?, 'resettle_reversal', ?, 'bet', ?)
                """, (u_id, -prev_payout, b_id, bal_rev))

            # 4b. Re-evaluate bet legs
            cursor.execute("SELECT * FROM bet_items WHERE bet_id = ?", (b_id,))
            all_items = cursor.fetchall()

            has_lost = any(i["status"] == "lost" for i in all_items)
            has_pending = any(i["status"] == "pending" for i in all_items)
            all_voided = all(i["status"] in ("voided", "refunded") for i in all_items)

            if has_lost:
                cursor.execute("""
                    UPDATE user_bets
                    SET status = 'lost', actual_payout = 0, settled_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (b_id,))
                if prev_status != "lost":
                    database.enqueue_bet_settled_notice(
                        cursor, u_id, b_id,
                        title=f"⚖️ Ставка #{b_id} ({_bet_type_label(bet['bet_type'])}) пересчитана: проигрыш",
                        body=(f"Счёт матча исправлен. Ранее начисленные <b>{_coins(prev_payout)} 🪙</b> списаны."
                              if prev_payout > 0 else "Счёт матча исправлен."),
                        resettle=True,
                    )
                notifications.append({
                    "user_id": u_id,
                    "bet_id": b_id,
                    "status": "lost",
                    "payout": 0,
                    "message": f"⚖️ Прогноз #{b_id} пересчитан: Проигрыш"
                })
                continue

            if has_pending:
                cursor.execute("""
                    UPDATE user_bets
                    SET status = 'pending', actual_payout = 0, settled_at = NULL
                    WHERE id = ?
                """, (b_id,))
                if prev_status != "pending":
                    database.enqueue_bet_settled_notice(
                        cursor, u_id, b_id,
                        title=f"⚖️ Ставка #{b_id} ({_bet_type_label(bet['bet_type'])}) пересчитана: снова в игре",
                        body=(f"Счёт матча исправлен. Ранее начисленные <b>{_coins(prev_payout)} 🪙</b> списаны до расчёта."
                              if prev_payout > 0 else "Счёт матча исправлен."),
                        resettle=True,
                    )
                notifications.append({
                    "user_id": u_id,
                    "bet_id": b_id,
                    "status": "pending",
                    "payout": 0,
                    "message": f"⚖️ Прогноз #{b_id} пересчитан: В игре"
                })
                continue

            if all_voided:
                cursor.execute("""
                    UPDATE user_bets
                    SET status = 'refunded', actual_payout = ?, settled_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (stake, b_id))

                database.get_or_create_wallet(u_id)
                cursor.execute("""
                    UPDATE user_wallets
                    SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (stake, u_id))

                cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
                bal_after = cursor.fetchone()["balance"]
                cursor.execute("""
                    INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after)
                    VALUES (?, ?, 'resettle_refund', ?, 'bet', ?)
                """, (u_id, stake, b_id, bal_after))
                if (prev_status, prev_payout) != ("refunded", stake):
                    _notify_bet_refunded(cursor, u_id, b_id, bet["bet_type"], stake, bal_after, resettle=True)

                notifications.append({
                    "user_id": u_id,
                    "bet_id": b_id,
                    "status": "refunded",
                    "payout": stake,
                    "message": f"🔄 Прогноз #{b_id} пересчитан: Возврат (+{stake} 🪙)"
                })
                continue

            # Won
            effective_odd = 1.0
            for i in all_items:
                if i["status"] == "won":
                    effective_odd *= float(i["odd"])
            effective_odd_rounded = round(effective_odd, 2)
            payout = int(round(stake * effective_odd_rounded))

            cursor.execute("""
                UPDATE user_bets
                SET status = 'won', actual_payout = ?, settled_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (payout, b_id))

            database.get_or_create_wallet(u_id)
            cursor.execute("""
                UPDATE user_wallets
                SET balance = balance + ?,
                    total_won = total_won + ?,
                    bets_won = bets_won + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (payout, payout, u_id))

            cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
            bal_after = cursor.fetchone()["balance"]
            cursor.execute("""
                INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after)
                VALUES (?, ?, 'resettle_payout', ?, 'bet', ?)
            """, (u_id, payout, b_id, bal_after))
            if (prev_status, prev_payout) != ("won", payout):
                _notify_bet_won(cursor, u_id, b_id, bet["bet_type"], effective_odd_rounded, payout, stake, bal_after,
                                resettle=True)

            notifications.append({
                "user_id": u_id,
                "bet_id": b_id,
                "status": "won",
                "payout": payout,
                "message": f"🎉 Прогноз #{b_id} пересчитан: Победа (+{payout} 🪙)"
            })

    return notifications

