"""
services/sports/freshness.py

Logovo.bet — Stale Data Protection & Freshness Policy (Phase 8).
Classifies live match updates into FRESH, STALE, or EXPIRED.
Enforces that AI confidence and recommendations degrade gracefully when data is delayed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import config
from time_utils import MSK

logger = logging.getLogger(__name__)

FRESH = "FRESH"
STALE = "STALE"
EXPIRED = "EXPIRED"
UNAVAILABLE = "UNAVAILABLE"


def evaluate_match_freshness(
    last_updated_at: Optional[str | datetime],
    stale_threshold: Optional[int] = None,
    expired_threshold: Optional[int] = None
) -> dict[str, Any]:
    """
    Evaluate the freshness state of a live match update.
    Returns status ('FRESH', 'STALE', 'EXPIRED', 'UNAVAILABLE'), UI badge, age in seconds,
    and a confidence multiplier for AI predictions.
    """
    if not last_updated_at:
        return {
            "status": UNAVAILABLE,
            "badge": "🔴 LIVE DATA UNAVAILABLE",
            "age_seconds": None,
            "is_stale": True,
            "is_expired": True,
            "confidence_multiplier": 0.0,
            "warning": "Нет актуальных данных реального времени по матчу."
        }

    stale_sec = stale_threshold or getattr(config, "LIVE_DATA_STALE_AFTER_SECONDS", 120)
    expired_sec = expired_threshold or getattr(config, "LIVE_DATA_EXPIRED_AFTER_SECONDS", 300)

    try:
        if isinstance(last_updated_at, str):
            clean_str = last_updated_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_str)
        else:
            dt = last_updated_at

        if dt.tzinfo is None:
            # Наивная строка приходит из базы, а база живёт по Москве (time_utils).
            dt = dt.replace(tzinfo=MSK)

        now = datetime.now(timezone.utc)
        age = max(0.0, (now - dt).total_seconds())

        if age <= stale_sec:
            return {
                "status": FRESH,
                "badge": "🟢 LIVE DATA FRESH",
                "age_seconds": round(age, 1),
                "is_stale": False,
                "is_expired": False,
                "confidence_multiplier": 1.0,
                "warning": None
            }
        elif age <= expired_sec:
            return {
                "status": STALE,
                "badge": "🟡 DATA DELAYED",
                "age_seconds": round(age, 1),
                "is_stale": True,
                "is_expired": False,
                "confidence_multiplier": 0.70,
                "warning": f"Данные матча задерживаются ({int(age)}с). Коэффициенты и рекомендации могут быть скорректированы."
            }
        else:
            return {
                "status": EXPIRED,
                "badge": "🔴 LIVE DATA UNAVAILABLE",
                "age_seconds": round(age, 1),
                "is_stale": True,
                "is_expired": True,
                "confidence_multiplier": 0.40,
                "warning": f"Данные реального времени устарели ({int(age)}с). Торговля приостановлена до подтверждения."
            }
    except Exception as e:
        logger.warning(f"Error parsing last_updated_at '{last_updated_at}': {e}")
        return {
            "status": UNAVAILABLE,
            "badge": "🔴 LIVE DATA UNAVAILABLE",
            "age_seconds": None,
            "is_stale": True,
            "is_expired": True,
            "confidence_multiplier": 0.0,
            "warning": "Ошибка проверки времени обновления данных."
        }
