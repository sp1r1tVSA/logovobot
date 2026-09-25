"""
tests/test_gamification.py
Unit tests for LOGOVO.BET Progression, Streaks & Achievements (Secondary gamification).
"""

import unittest
import database
from config import INITIAL_WALLET_BALANCE as START


class TestGamificationEngine(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self.user_id = 999901
        with database.transaction() as conn:
            conn.execute("DELETE FROM user_progression WHERE user_id = ?", (self.user_id,))
            conn.execute("DELETE FROM user_achievements WHERE user_id = ?", (self.user_id,))
            conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (self.user_id,))

    def test_progression_and_level_up(self):
        # 1. Initial progression
        p = database.get_or_create_progression(self.user_id)
        self.assertEqual(p["level"], 1)
        self.assertEqual(p["current_xp"], 0)

        # 2. Add XP (e.g. 500 XP -> should level up)
        res = database.add_user_xp(self.user_id, 500)
        self.assertTrue(res["leveled_up"])
        self.assertGreater(res["level"], 1)
        self.assertGreater(res["reward_coins"], 0)

        # 3. Check wallet got level up coins
        w = database.get_or_create_wallet(self.user_id)
        self.assertGreaterEqual(w["balance"], START + res["reward_coins"])

    def test_login_streak(self):
        streak_info = database.check_and_update_login_streak(self.user_id)
        self.assertGreaterEqual(streak_info["streak"], 1)
        self.assertGreaterEqual(streak_info["streak_shield_count"], 1)

    def test_achievements_unlock_and_claim(self):
        # 1. Unlock achievement
        database.unlock_achievement(self.user_id, "ACH_FIRST_BET")
        achievements = database.get_user_achievements(self.user_id)
        first_bet_ach = next((a for a in achievements if a["id"] == "ACH_FIRST_BET"), None)
        self.assertIsNotNone(first_bet_ach)
        self.assertEqual(first_bet_ach["is_unlocked"], 1)
        self.assertEqual(first_bet_ach["is_claimed"], 0)

        # 2. Claim achievement
        success, msg, reward = database.claim_achievement_reward(self.user_id, "ACH_FIRST_BET")
        self.assertTrue(success)
        self.assertGreater(reward["coins"], 0)

    def test_claiming_a_skill_achievement_grants_a_freebet(self):
        # Фрибеты (миграция 029) — награда только для достижений уровня
        # skill/parlays/seasonal, отдельная от монет и XP.
        with database.transaction() as conn:
            conn.execute("DELETE FROM user_freebets WHERE user_id = ?", (self.user_id,))
        database.unlock_achievement(self.user_id, "ACH_POSITIVE_ROI")
        success, msg, reward = database.claim_achievement_reward(self.user_id, "ACH_POSITIVE_ROI")
        self.assertTrue(success, msg)
        self.assertEqual(reward["freebet"], 250)
        self.assertIn("фрибет", msg.lower())

        with database.transaction() as conn:
            row = conn.execute(
                "SELECT amount, status, source, source_id FROM user_freebets WHERE user_id = ?",
                (self.user_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["amount"], 250)
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["source"], "achievement")
        self.assertEqual(row["source_id"], "ACH_POSITIVE_ROI")

    def test_claiming_an_achievement_without_a_freebet_grants_none(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM user_freebets WHERE user_id = ?", (self.user_id,))
        database.unlock_achievement(self.user_id, "ACH_FIRST_BET")
        success, _, reward = database.claim_achievement_reward(self.user_id, "ACH_FIRST_BET")
        self.assertTrue(success)
        self.assertEqual(reward["freebet"], 0)
        with database.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM user_freebets WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)


class TestLoginStreakIsolation(unittest.TestCase):
    """
    Серия входов и серия побед по ставкам — разные счётчики.

    Пока они жили в одной колонке `current_streak`, пять выигранных ставок
    читались как пять дней подряд и выдавали «📅 Разминка» тому, кто зашёл
    впервые.
    """

    def setUp(self):
        database.init_db()
        self.user_id = 999931
        with database.transaction() as conn:
            conn.execute("DELETE FROM user_progression WHERE user_id = ?", (self.user_id,))
            conn.execute("DELETE FROM user_achievements WHERE user_id = ?", (self.user_id,))

    def test_win_streak_does_not_grant_login_achievement(self):
        database.get_or_create_progression(self.user_id)
        with database.transaction() as conn:
            conn.execute(
                "UPDATE user_progression SET current_streak = 9, best_streak = 9 WHERE user_id = ?",
                (self.user_id,)
            )

        info = database.check_and_update_login_streak(self.user_id)
        self.assertEqual(info["streak"], 1)

        unlocked = {a["id"] for a in database.get_user_achievements(self.user_id) if a["is_unlocked"]}
        self.assertNotIn("ACH_LOGIN_3", unlocked)
        self.assertNotIn("ACH_LOGIN_7", unlocked)

    def test_login_streak_does_not_reset_win_streak(self):
        database.get_or_create_progression(self.user_id)
        with database.transaction() as conn:
            conn.execute(
                "UPDATE user_progression SET current_streak = 4, best_streak = 6 WHERE user_id = ?",
                (self.user_id,)
            )

        database.check_and_update_login_streak(self.user_id)

        p = database.get_or_create_progression(self.user_id)
        self.assertEqual(p["current_streak"], 4)
        self.assertEqual(p["best_streak"], 6)

    def test_login_achievement_needs_three_real_days(self):
        # Отматываем last_active_date на вчера перед каждым заходом — так
        # выглядят три дня подряд для функции, которая считает по датам.
        for expected in (1, 2, 3):
            info = database.check_and_update_login_streak(self.user_id)
            self.assertEqual(info["streak"], expected)
            unlocked = {a["id"] for a in database.get_user_achievements(self.user_id) if a["is_unlocked"]}
            self.assertEqual("ACH_LOGIN_3" in unlocked, expected >= 3)
            if expected < 3:
                with database.transaction() as conn:
                    conn.execute("""
                        UPDATE user_progression
                        SET last_active_date = date('now', '+3 hours', '-1 day')
                        WHERE user_id = ?
                    """, (self.user_id,))

    def test_new_progression_starts_without_streaks(self):
        p = database.get_or_create_progression(self.user_id)
        self.assertEqual(p["current_streak"], 0)
        self.assertEqual(p["login_streak"], 0)

    def test_migration_015_revokes_unclaimed_login_rewards(self):
        # Свежая база проходит миграцию на пустых таблицах, поэтому прогоняем её
        # ещё раз — на строках, которые в бою достались от старой схемы.
        with database.transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO user_progression (user_id, login_streak, best_login_streak, last_active_date)
                VALUES (?, 9, 9, '2026-09-01')
            """, (self.user_id,))
            conn.execute("""
                INSERT OR REPLACE INTO user_achievements (user_id, achievement_id, is_claimed)
                VALUES (?, 'ACH_LOGIN_3', 0), (?, 'ACH_LOGIN_7', 1), (?, 'ACH_FIRST_BET', 0)
            """, (self.user_id, self.user_id, self.user_id))
            conn.execute("DELETE FROM schema_migrations WHERE version = '015_split_login_and_win_streaks'")

        database.init_db()

        owned = {a["id"] for a in database.get_user_achievements(self.user_id) if a["is_unlocked"]}
        self.assertNotIn("ACH_LOGIN_3", owned)   # не выдана — отзывается
        self.assertIn("ACH_LOGIN_7", owned)      # уже оплачена — не трогаем
        self.assertIn("ACH_FIRST_BET", owned)    # к входам отношения не имеет

        p = database.get_or_create_progression(self.user_id)
        self.assertEqual(p["login_streak"], 1)   # даты прошлых входов не восстановить


class TestAchievementsCatalog(unittest.TestCase):
    RARITY_COIN_CAP = {"common": 300, "rare": 1000, "epic": 1500, "legendary": 5000}

    def setUp(self):
        database.init_db()
        self.user_id = 999932
        with database.transaction() as conn:
            conn.execute("DELETE FROM user_achievements WHERE user_id = ?", (self.user_id,))

    def _active(self):
        with database.transaction() as conn:
            rows = conn.execute("SELECT * FROM achievements_catalog WHERE is_active = 1").fetchall()
        return [dict(r) for r in rows]

    def test_retired_achievements_are_out_of_the_catalog(self):
        active = {a["id"] for a in self._active()}
        for dead in ("ACH_DUEL_FIRST", "ACH_DUEL_5_WINS", "ACH_HOT_STREAK"):
            self.assertNotIn(dead, active)

        listed = {a["id"] for a in database.get_user_achievements(self.user_id)}
        self.assertNotIn("ACH_HOT_STREAK", listed)

    def test_retired_achievement_stays_visible_to_its_owner(self):
        # Строка снята с каталога, но у кого-то она уже открыта — из профиля
        # такая карточка пропадать не должна. В свежей базе снятых строк нет
        # вовсе (сид их больше не пишет), поэтому боевую ситуацию собираем сами.
        with database.transaction() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO achievements_catalog
                    (id, name, description, category, rarity, reward_xp, reward_coins, badge_icon, is_active)
                VALUES ('ACH_HOT_STREAK', '🔥 Горячая серия', 'Снятое достижение', 'streaks', 'rare', 300, 700, '🔥', 0)
            """)
        database.unlock_achievement(self.user_id, "ACH_HOT_STREAK")

        listed = {a["id"] for a in database.get_user_achievements(self.user_id)}
        self.assertIn("ACH_HOT_STREAK", listed)

        stranger = {a["id"] for a in database.get_user_achievements(self.user_id + 1)}
        self.assertNotIn("ACH_HOT_STREAK", stranger)

    def test_every_active_achievement_pays_something(self):
        for a in self._active():
            self.assertGreater(a["reward_coins"], 0, a["id"])
            self.assertGreater(a["reward_xp"], 0, a["id"])

    def test_rewards_stay_inside_their_rarity_band(self):
        for a in self._active():
            cap = self.RARITY_COIN_CAP.get(a["rarity"])
            self.assertIsNotNone(cap, f"{a['id']}: неизвестная редкость {a['rarity']}")
            self.assertLessEqual(a["reward_coins"], cap, a["id"])
            # XP примерно вдвое дешевле монет: уровень сам доплачивает монетами.
            self.assertLessEqual(a["reward_xp"], a["reward_coins"], a["id"])

    def test_full_completion_stays_in_scale_with_the_economy(self):
        # Ориентир — стартовый кошелёк 677 🪙 и дейлик 250 🪙/день: 100%
        # достижений не должно стоить дороже примерно полугода ежедневных
        # заходов, иначе каталог печатает монеты быстрее самой игры.
        total = sum(a["reward_coins"] for a in self._active())
        self.assertLessEqual(total, 45_000)

    def test_freebet_rewards_are_rare_and_scale_with_rarity(self):
        # Фрибет — бонус поверх монет для skill/parlays/seasonal, не для каждого
        # достижения, и его шкала (миграция 029) не завязана на монетную.
        band = {"rare": 250, "epic": 500, "legendary": 1000}
        active = self._active()
        with_freebet = [a for a in active if a["reward_freebet"] > 0]
        self.assertGreater(len(with_freebet), 0)
        self.assertLess(len(with_freebet), len(active))
        for a in with_freebet:
            self.assertEqual(a["reward_freebet"], band.get(a["rarity"]), a["id"])


if __name__ == "__main__":
    unittest.main()
