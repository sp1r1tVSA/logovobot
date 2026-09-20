"""
services/gamification_notifications.py

Responsible Notification System for Progression & Seasonal Events.
Strict Invariants:
1. No Coercive Language: Never push players to increase stakes, chase losses, or act under artificial scarcity.
2. Cooldown & Deduplication: Prevents alert fatigue and spamming.
3. Clean HTML and formatted message payloads.
"""

import time
import logging
from typing import Optional, Any
import database

logger = logging.getLogger(__name__)

# In-memory deduplication window (event_key -> timestamp)
_notification_cooldowns: dict[str, float] = {}
COOLDOWN_SECONDS = 300.0  # 5 minutes per user+event


class GamificationNotifications:
    """Manages progression notifications with cooldowns and deduplication."""

    @staticmethod
    def send_event_notification(
        user_id: int,
        event_type: str,
        title: str,
        message: str,
        data: Optional[dict[str, Any]] = None
    ) -> bool:
        """
        Record and queue a user notification if outside cooldown window.
        """
        now = time.time()
        dedup_key = f"{user_id}:{event_type}"
        last_sent = _notification_cooldowns.get(dedup_key, 0.0)

        if (now - last_sent) < COOLDOWN_SECONDS and event_type not in ("ACHIEVEMENT_UNLOCKED", "REWARD_RECEIVED"):
            logger.debug(f"Notification suppressed for user #{user_id} ({event_type}) due to cooldown.")
            return False

        _notification_cooldowns[dedup_key] = now

        try:
            with database.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO notifications (user_id, type, title, body, is_read, created_at)
                    VALUES (?, ?, ?, ?, 0, datetime('now', '+3 hours'))
                """, (user_id, event_type.lower(), title, message))
            return True
        except Exception as e:
            logger.warning(f"Failed to queue notification for #{user_id}: {e}")
            return False

    @classmethod
    def notify_achievement(cls, user_id: int, achievement_name: str, coins: int, xp: int) -> bool:
        title = "🏆 Новое Достижение!"
        msg = f"Вы разблокировали достижение <b>{achievement_name}</b>! Получено: +{coins} 🪙 и +{xp} XP."
        return cls.send_event_notification(user_id, "ACHIEVEMENT_UNLOCKED", title, msg)

    @classmethod
    def notify_season_reward(cls, user_id: int, reward_name: str, coins: int, xp: int) -> bool:
        title = "🎁 Сезонная Награда Получена"
        msg = f"За успехи в сезоне вам начислена награда <b>{reward_name}</b> (+{coins} 🪙, +{xp} XP)."
        return cls.send_event_notification(user_id, "REWARD_RECEIVED", title, msg)

    @classmethod
    def notify_promotion(cls, user_id: int, new_division_name: str) -> bool:
        title = "🚀 Повышение в Классе!"
        msg = f"Поздравляем! По итогам сезона вы переходите в <b>{new_division_name}</b>!"
        return cls.send_event_notification(user_id, "PROMOTED", title, msg)

    @classmethod
    def notify_relegation(cls, user_id: int, new_division_name: str) -> bool:
        title = "⚽ Изменение Дивизиона"
        msg = f"В следующем сезоне вы выступаете в <b>{new_division_name}</b>. Удачи в новых матчах!"
        return cls.send_event_notification(user_id, "RELEGATED", title, msg)
