"""
services/cashout_engine.py

Logovo.bet — Dynamic Cashout Calculation & Execution Engine.
Strict Invariants:
1. Only pending bets with settled_at IS NULL can be cashed out.
2. If any leg is lost or cancelled, cashout offer is 0 or unavailable.
3. If any market is suspended or closed, cashout is temporarily disabled.
4. Payout is bounded in [1, potential_win].
5. Atomic wallet crediting and transaction logging via Settlement integration.
6. Absolute idempotency: duplicate cashout requests result in exactly one payout.
"""

import math
import logging
from typing import Any, Optional
import database

logger = logging.getLogger(__name__)

DEFAULT_CASHOUT_MARGIN: float = 0.08  # 8% bookmaker margin on early cashout


def calculate_cashout_offer(
    stake: int,
    potential_win: int,
    items: list[dict[str, Any]],
    margin: float = DEFAULT_CASHOUT_MARGIN
) -> tuple[bool, int, Optional[str]]:
    """
    Calculate fair cashout offer based on current market odds vs initial odds.
    Returns (is_available, offer_amount, reason).
    """
    if not items:
        return False, 0, "NO_ITEMS"

    ratio_product = 1.0

    for item in items:
        item_status = item.get("status", "pending")
        if item_status == "lost":
            return False, 0, "LEG_LOST"

        if item_status in ("won", "refunded"):
            continue  # Выигравшая нога хранит полную стоимость, аннулированная даёт 1.00

        # Pending leg: compute relative odds ratio
        orig_odd = float(item.get("odds_at_placement") or item.get("odd") or 1.0)
        curr_odd = item.get("current_odd")

        if curr_odd is None or not math.isfinite(curr_odd) or curr_odd <= 1.0:
            return False, 0, "ODDS_UNAVAILABLE"

        # If current market or selection is suspended
        if item.get("market_status") in ("suspended", "closed", "settled") or item.get("sel_status") in ("suspended", "locked", "settled"):
            return False, 0, "MARKET_SUSPENDED"

        leg_ratio = orig_odd / curr_odd
        ratio_product *= leg_ratio

    # Fair value before margin
    fair_value = stake * ratio_product
    offer = int(round(fair_value * (1.0 - margin)))

    # Bound offer: minimum 1 coin, maximum potential_win
    offer = max(1, min(potential_win, offer))

    return True, offer, None


def quote_cashout(user_id: int, bet_id: int) -> dict[str, Any]:
    """
    Generate live cashout quotation for an active bet slip.
    """
    from config import is_global_lockdown_enabled
    if is_global_lockdown_enabled():
        from handlers.base import is_global_admin
        if not is_global_admin(user_id):
            return {"available": False, "reason": "LOGOVO_LOCKDOWN", "offer": 0}

    with database.transaction() as conn:
        cursor = conn.cursor()

        # Fetch bet record
        cursor.execute("SELECT * FROM user_bets WHERE id = ? AND user_id = ?", (bet_id, user_id))
        bet = cursor.fetchone()
        if not bet:
            return {"available": False, "reason": "BET_NOT_FOUND", "offer": 0}

        if bet["settled_at"] is not None or bet["status"] != "pending":
            return {"available": False, "reason": "ALREADY_SETTLED", "offer": 0}

        # Fetch bet items with current market and selection statuses
        cursor.execute("""
            SELECT bi.*, 
                   ms.odds_value as current_odd,
                   ms.status as sel_status,
                   m.status as market_status,
                   mat.status as match_status
            FROM bet_items bi
            LEFT JOIN market_selections ms ON bi.selection_id = ms.id
            LEFT JOIN markets m ON bi.market_id = m.id
            LEFT JOIN matches mat ON bi.match_id = mat.id
            WHERE bi.bet_id = ?
        """, (bet_id,))
        items = [dict(r) for r in cursor.fetchall()]

        # Resolve current odds fallback if selection_id is NULL
        for it in items:
            if it.get("current_odd") is None:
                cursor.execute("""
                    SELECT ms.odds_value, ms.status as sel_status, m.status as market_status
                    FROM market_selections ms
                    JOIN markets m ON ms.market_id = m.id
                    WHERE m.match_id = ? AND ms.selection_key = ?
                """, (it["match_id"], it["outcome_type"]))
                ms_r = cursor.fetchone()
                if ms_r:
                    it["current_odd"] = float(ms_r["odds_value"])
                    it["sel_status"] = ms_r["sel_status"]
                    it["market_status"] = ms_r["market_status"]
                else:
                    cursor.execute("SELECT * FROM bet_markets WHERE match_id = ? AND is_active = 1", (it["match_id"],))
                    bm_r = cursor.fetchone()
                    if bm_r:
                        bm_map = {
                            "p1": "odd_p1", "x": "odd_x", "p2": "odd_p2",
                            "over_2.5": "odd_tb25", "tb25": "odd_tb25",
                            "under_2.5": "odd_tm25", "tm25": "odd_tm25",
                            "btts_yes": "odd_btts_yes", "btts_no": "odd_btts_no"
                        }
                        col = bm_map.get(it["outcome_type"])
                        if col and bm_r[col]:
                            it["current_odd"] = float(bm_r[col])
                            it["sel_status"] = "active"
                            it["market_status"] = "open"

        # Check for completed or terminal matches
        for it in items:
            if it.get("match_status") in ("completed", "confirmed", "cancelled"):
                return {"available": False, "reason": "MATCH_TERMINAL", "offer": 0}

        available, offer, reason = calculate_cashout_offer(
            stake=bet["amount"],
            potential_win=bet["potential_win"],
            items=items
        )

        return {
            "available": available,
            "bet_id": bet_id,
            "offer": offer if available else 0,
            "stake": bet["amount"],
            "potential_win": bet["potential_win"],
            "reason": reason
        }


def execute_cashout(user_id: int, bet_id: int, idempotency_key: Optional[str] = None) -> tuple[bool, dict | str]:
    """
    Execute atomic early cashout settlement.
    Delegates to database transaction with strict row locking.
    """
    return database.execute_cashout(user_id=user_id, bet_id=bet_id, idempotency_key=idempotency_key)
