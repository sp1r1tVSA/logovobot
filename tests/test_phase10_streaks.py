"""
tests/test_phase10_streaks.py

Phase 10: Deterministic Win Streak Engine Tests.
Strict Invariants:
1. Streaks are strictly evaluated on settled bets.
2. Won outcomes increment current streak and update all-time best streak.
3. Lost outcomes reset current streak to 0 while preserving best streak.
4. Refunded or voided outcomes are neutral: they neither increment nor reset streak.
5. Streak milestone achievements unlock at 3, 5, 7, and 10 wins.
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
import database
from services.streak_engine import StreakEngine


@pytest.fixture(autouse=True)
def clean_streak_user():
    database.init_db()
    with database.transaction() as conn:
        conn.execute("DELETE FROM user_progression WHERE user_id = 8001")
        conn.execute("DELETE FROM user_achievements WHERE user_id = 8001")
        conn.execute("DELETE FROM season_player_stats WHERE user_id = 8001")
        conn.execute("DELETE FROM users WHERE telegram_id = 8001")
        conn.execute("INSERT OR IGNORE INTO users (telegram_id, username, role, division_id) VALUES (8001, 'user_8001', 'user', 1)")
    yield
    with database.transaction() as conn:
        conn.execute("DELETE FROM user_progression WHERE user_id = 8001")
        conn.execute("DELETE FROM user_achievements WHERE user_id = 8001")
        conn.execute("DELETE FROM season_player_stats WHERE user_id = 8001")
        conn.execute("DELETE FROM users WHERE telegram_id = 8001")


def test_01_win_increments_streak_and_updates_best():
    """Verify winning consecutive bets increments current_streak and best_streak."""
    database.get_or_create_progression(8001)
    with database.transaction() as conn:
        conn.execute("UPDATE user_progression SET current_streak = 0, best_streak = 0 WHERE user_id = 8001")

    r1 = StreakEngine.process_bet_outcome(8001, "won", season_id=1, division_id=1)
    assert r1["current_streak"] == 1
    assert r1["best_streak"] == 1

    r2 = StreakEngine.process_bet_outcome(8001, "won", season_id=1, division_id=1)
    assert r2["current_streak"] == 2
    assert r2["best_streak"] == 2


def test_02_loss_resets_current_streak_to_zero():
    """Verify losing a bet resets current_streak to 0 but retains best_streak."""
    database.get_or_create_progression(8001)
    with database.transaction() as conn:
        conn.execute("UPDATE user_progression SET current_streak = 4, best_streak = 4 WHERE user_id = 8001")

    res = StreakEngine.process_bet_outcome(8001, "lost", season_id=1, division_id=1)
    assert res["current_streak"] == 0
    assert res["best_streak"] == 4


def test_03_refund_or_void_is_neutral_to_streak():
    """Verify a refund or void outcome keeps the current streak intact without incrementing."""
    database.get_or_create_progression(8001)
    with database.transaction() as conn:
        conn.execute("UPDATE user_progression SET current_streak = 3, best_streak = 3 WHERE user_id = 8001")

    r_refund = StreakEngine.process_bet_outcome(8001, "refunded", season_id=1, division_id=1)
    assert r_refund["current_streak"] == 3
    assert r_refund["best_streak"] == 3

    r_void = StreakEngine.process_bet_outcome(8001, "voided", season_id=1, division_id=1)
    assert r_void["current_streak"] == 3


def test_04_pending_bet_has_no_effect_on_streak():
    """Verify pending bets do not alter streak values."""
    database.get_or_create_progression(8001)
    with database.transaction() as conn:
        conn.execute("UPDATE user_progression SET current_streak = 2, best_streak = 5 WHERE user_id = 8001")

    res = StreakEngine.process_bet_outcome(8001, "pending", season_id=1, division_id=1)
    assert res["current_streak"] == 2
    assert res["best_streak"] == 5
    assert res["changed"] is False


def test_05_streak_milestone_achievements_unlock():
    """Verify reaching 3, 5, 7 consecutive wins unlocks appropriate achievements."""
    database.get_or_create_progression(8001)
    with database.transaction() as conn:
        conn.execute("UPDATE user_progression SET current_streak = 2, best_streak = 2 WHERE user_id = 8001")

    # 3rd win
    r3 = StreakEngine.process_bet_outcome(8001, "won")
    assert "ACH_STREAK_3" in r3["unlocked_achievements"]

    # 4th win
    StreakEngine.process_bet_outcome(8001, "won")

    # 5th win
    r5 = StreakEngine.process_bet_outcome(8001, "won")
    assert "ACH_STREAK_5" in r5["unlocked_achievements"]
    # ACH_HOT_STREAK — снятый дубль ACH_STREAK_5, он больше не выдаётся
    assert "ACH_HOT_STREAK" not in r5["unlocked_achievements"]
