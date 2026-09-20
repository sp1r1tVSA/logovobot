"""
services/streak_engine.py

Deterministic Streak Tracking & Milestone Recognition for Logovo.bet.
Strict Invariants:
1. Streaks are strictly evaluated on SETTLED bets.
   Pending bets never increment or reset streaks.
2. Void / Refunded bets are neutral:
   They neither increment win streaks nor break/reset them.
3. Wins increment current streak and update all-time and seasonal best streaks.
4. Losses reset current streak to 0.
5. Thread-safe and idempotent.
"""

import logging
from typing import Optional
import database

logger = logging.getLogger(__name__)


class StreakEngine:
    """Evaluates and persists win streaks across user progression and seasonal records."""

    @staticmethod
    def process_bet_outcome(
        user_id: int,
        outcome: str,
        season_id: Optional[int] = None,
        division_id: Optional[int] = None
    ) -> dict:
        """
        Process settled bet outcome for streaks.
        Returns dict with updated current_streak, best_streak, and any streak achievements unlocked.
        """
        if outcome in ("pending", "created"):
            # Pending wagers have zero effect on streak
            prog = database.get_or_create_progression(user_id)
            return {
                "current_streak": prog.get("current_streak", 0),
                "best_streak": prog.get("best_streak", 0),
                "changed": False
            }

        with database.transaction() as conn:
            cursor = conn.cursor()
            prog = database.get_or_create_progression(user_id)
            s_stats = database.get_or_create_season_stats(user_id, season_id, division_id)

            cur_streak = prog.get("current_streak", 0)
            best_streak = prog.get("best_streak", 0)
            s_cur_streak = s_stats.get("current_streak", 0)
            s_best_streak = s_stats.get("best_streak", 0)

            unlocked_achievements = []

            if outcome == "won":
                cursor.execute("""
                    UPDATE user_progression
                    SET current_streak = current_streak + 1,
                        best_streak = MAX(best_streak, current_streak + 1),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (user_id,))
                cursor.execute("""
                    UPDATE season_player_stats
                    SET current_streak = current_streak + 1,
                        best_streak = MAX(best_streak, current_streak + 1),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND season_id = ? AND division_id = ?
                """, (user_id, s_stats["season_id"], s_stats["division_id"]))
            elif outcome == "lost":
                cursor.execute("""
                    UPDATE user_progression
                    SET current_streak = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                """, (user_id,))
                cursor.execute("""
                    UPDATE season_player_stats
                    SET current_streak = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND season_id = ? AND division_id = ?
                """, (user_id, s_stats["season_id"], s_stats["division_id"]))
            elif outcome in ("refunded", "voided", "cancelled"):
                pass

            cursor.execute("SELECT current_streak, best_streak FROM user_progression WHERE user_id = ?", (user_id,))
            p_row = cursor.fetchone()
            cur_streak = p_row["current_streak"] if p_row else 0
            best_streak = p_row["best_streak"] if p_row else 0

            if outcome == "won":
                # Milestone checks
                if cur_streak >= 3:
                    if database.unlock_achievement(user_id, "ACH_STREAK_3"):
                        unlocked_achievements.append("ACH_STREAK_3")
                if cur_streak >= 5:
                    # ACH_HOT_STREAK выдавался здесь же и с тем же условием, то
                    # есть платил за одну серию из 5 побед дважды. Он снят с
                    # каталога миграцией 016 и больше не выдаётся.
                    if database.unlock_achievement(user_id, "ACH_STREAK_5"):
                        unlocked_achievements.append("ACH_STREAK_5")
                if cur_streak >= 7:
                    if database.unlock_achievement(user_id, "ACH_NO_LOSS_STREAK"):
                        unlocked_achievements.append("ACH_NO_LOSS_STREAK")
                if cur_streak >= 10:
                    if database.unlock_achievement(user_id, "ACH_STREAK_10"):
                        unlocked_achievements.append("ACH_STREAK_10")

            return {
                "current_streak": cur_streak,
                "best_streak": best_streak,
                "season_current_streak": s_cur_streak,
                "season_best_streak": s_best_streak,
                "unlocked_achievements": unlocked_achievements,
                "changed": outcome in ("won", "lost")
            }
