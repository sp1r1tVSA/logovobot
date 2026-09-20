"""
tests/test_open_bets_limit.py

Лимит на число одновременно открытых купонов.

Считаются именно купоны, а не исходы: экспресс из пяти матчей занимает один
слот, как и ординар. Купоны, сделанные до введения лимита, не отменяются, но
слоты занимают — пока они не рассчитаются, новых пари не будет. Это отличает
лимит количества от лимита суммы, где флаг `legacy_limits` выводит старые
купоны из-под потолка выплат.
"""

import os
import tempfile
import unittest

import database
from services.betting_limits import BettingLimitsService, DEFAULT_MAX_OPEN_BETS

MATCH_COUNT = 10


class OpenBetsLimitCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.user_id = 960001
        self.matches = []

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'slots_user', 'user')",
                (self.user_id,)
            )
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Slots Season', 'active')")
            season_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) VALUES (1, 1, ?, 0, 1)",
                (season_id,)
            )
            for n in range(MATCH_COUNT):
                match_id, market_id, selection_id = 960100 + n, 960200 + n, 960300 + n
                cursor.execute("""
                    INSERT INTO matches (id, division_id, season_id, round_number, player1_team, player2_team, status)
                    VALUES (?, 1, ?, 1, ?, ?, 'open')
                """, (match_id, season_id, f"Team A{n}", f"Team B{n}"))
                cursor.execute("""
                    INSERT INTO markets (id, match_id, market_key, market_name, status)
                    VALUES (?, ?, 'match_result', 'Match Winner', 'open')
                """, (market_id, match_id))
                cursor.execute("""
                    INSERT INTO market_selections (id, market_id, selection_key, selection_name, odds_value, status, odds_version)
                    VALUES (?, ?, 'home', ?, 1.50, 'active', 1)
                """, (selection_id, market_id, f"Team A{n}"))
                self.matches.append((match_id, market_id, selection_id))

        database.get_or_create_wallet(self.user_id)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_wallets SET balance = 100000 WHERE user_id = ?", (self.user_id,))

    def tearDown(self):
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    # ─── helpers ───

    def _selection(self, index: int) -> dict:
        match_id, market_id, selection_id = self.matches[index]
        return {
            "match_id": match_id, "market_id": market_id,
            "selection_id": selection_id, "outcome": "home", "odds": 1.50,
        }

    def _place(self, *indexes: int, amount: int = 100):
        joined = "-".join(str(i) for i in indexes)
        return database.place_user_bet(
            user_id=self.user_id,
            amount=amount,
            selections=[self._selection(i) for i in indexes],
            idempotency_key=f"slots-{joined}-{amount}"
        )

    def _settle(self, bet_id: int, status: str = "won") -> None:
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_bets SET status = ? WHERE id = ?", (status, bet_id))


class TestOpenBetsCounter(OpenBetsLimitCase):
    """`get_user_open_bets_count` считает купоны, а не исходы."""

    def test_counts_nothing_for_a_fresh_player(self):
        self.assertEqual(database.get_user_open_bets_count(self.user_id), 0)

    def test_a_single_takes_one_slot(self):
        ok, _ = self._place(0)
        self.assertTrue(ok)
        self.assertEqual(database.get_user_open_bets_count(self.user_id), 1)

    def test_an_express_of_five_still_takes_one_slot(self):
        ok, _ = self._place(0, 1, 2, 3, 4)
        self.assertTrue(ok)
        self.assertEqual(database.get_user_open_bets_count(self.user_id), 1)

    def test_settled_bets_free_their_slot(self):
        ok, bet_id = self._place(0)
        self.assertTrue(ok)
        for status in ("won", "lost", "refunded", "cancelled", "cashed_out"):
            with self.subTest(status=status):
                self._settle(bet_id, status)
                self.assertEqual(database.get_user_open_bets_count(self.user_id), 0)

    def test_another_players_bets_do_not_count(self):
        ok, bet_id = self._place(0)
        self.assertTrue(ok)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_bets SET user_id = 960099 WHERE id = ?", (bet_id,))
        self.assertEqual(database.get_user_open_bets_count(self.user_id), 0)


class TestLimitEnforcement(OpenBetsLimitCase):
    """Шестой купон не принимается, пока не рассчитан один из пяти."""

    def test_default_ceiling_is_five(self):
        self.assertEqual(DEFAULT_MAX_OPEN_BETS, 5)
        self.assertEqual(BettingLimitsService.get_system_limits()["max_open_bets"], 5)

    def test_sixth_coupon_is_rejected(self):
        for n in range(5):
            ok, _ = self._place(n)
            self.assertTrue(ok, f"купон #{n + 1} должен был пройти")

        ok, err = self._place(5)
        self.assertFalse(ok)
        self.assertIsInstance(err, dict)
        self.assertEqual(err["error"], "OPEN_BETS_LIMIT")
        self.assertEqual(err["max_open_bets"], 5)
        self.assertEqual(err["open_bets"], 5)
        self.assertIn("5", err["message"])

    def test_rejected_coupon_costs_nothing(self):
        for n in range(5):
            self._place(n)
        balance_before = database.get_or_create_wallet(self.user_id)["balance"]

        ok, _ = self._place(5)
        self.assertFalse(ok)
        self.assertEqual(database.get_or_create_wallet(self.user_id)["balance"], balance_before)
        self.assertEqual(database.get_user_open_bets_count(self.user_id), 5)

    def test_a_settled_coupon_reopens_the_slot(self):
        bet_ids = []
        for n in range(5):
            ok, bet_id = self._place(n)
            bet_ids.append(bet_id)

        self.assertFalse(self._place(5)[0])
        self._settle(bet_ids[0], "won")
        self.assertTrue(self._place(5)[0], "после расчёта слот должен освободиться")

    def test_an_express_leaves_room_for_four_more(self):
        """Пять матчей в одном купоне — один слот, а не пять."""
        ok, _ = self._place(0, 1, 2, 3, 4)
        self.assertTrue(ok)
        for n in range(5, 9):
            self.assertTrue(self._place(n)[0], f"ординар #{n} должен был пройти")
        self.assertFalse(self._place(9)[0])

    def test_old_coupons_count_but_are_not_cancelled(self):
        """Купоны, принятые до лимита, остаются открытыми и занимают слоты."""
        for _ in range(8):
            with database.transaction() as conn:
                conn.cursor().execute("""
                    INSERT INTO user_bets (user_id, amount, total_odd, potential_win, status, bet_type, created_at)
                    VALUES (?, 100, 1.5, 150, 'pending', 'single', datetime('now', '+3 hours'))
                """, (self.user_id,))

        self.assertEqual(database.get_user_open_bets_count(self.user_id), 8)

        ok, err = self._place(0)
        self.assertFalse(ok)
        self.assertEqual(err["open_bets"], 8)

        # Ни один старый купон не тронут.
        with database.transaction() as conn:
            still_open = conn.cursor().execute(
                "SELECT COUNT(*) AS c FROM user_bets WHERE user_id = ? AND status = 'pending'",
                (self.user_id,)
            ).fetchone()["c"]
        self.assertEqual(still_open, 8)

    def test_legacy_flag_does_not_exempt_a_coupon_from_the_count(self):
        """`legacy_limits` выводит купон из лимита суммы, но не из лимита количества."""
        for n in range(5):
            ok, bet_id = self._place(n)
            with database.transaction() as conn:
                conn.cursor().execute("UPDATE user_bets SET legacy_limits = 1 WHERE id = ?", (bet_id,))

        self.assertEqual(database.get_user_open_bets_count(self.user_id), 5)
        self.assertFalse(self._place(5)[0])


class TestLimitHierarchy(OpenBetsLimitCase):
    """Потолок настраивается по игроку и по дивизиону, как остальные лимиты."""

    def test_user_override_lowers_the_ceiling(self):
        BettingLimitsService.set_limit("user", self.user_id, "max_open_bets", 2)
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(self.user_id)["max_open_bets"], 2
        )

        self.assertTrue(self._place(0)[0])
        self.assertTrue(self._place(1)[0])
        ok, err = self._place(2)
        self.assertFalse(ok)
        self.assertEqual(err["max_open_bets"], 2)

    def test_user_override_can_raise_the_ceiling_above_the_default(self):
        BettingLimitsService.set_limit("global", 0, "max_open_bets", 7)
        BettingLimitsService.set_limit("user", self.user_id, "max_open_bets", 7)
        for n in range(7):
            self.assertTrue(self._place(n)[0], f"купон #{n + 1} должен был пройти")
        self.assertFalse(self._place(7)[0])

    def test_division_override_is_read(self):
        BettingLimitsService.set_limit("division", 1, "max_open_bets", 3)
        self.assertEqual(BettingLimitsService.get_division_limits(1)["max_open_bets"], 3)

    def test_the_telegram_coupon_reads_the_same_pair(self):
        """Счётчик в купоне Telegram берёт то же число, что и проверка при приёме."""
        from handlers.betting import _open_bets_state

        BettingLimitsService.set_limit("user", self.user_id, "max_open_bets", 3)
        self.assertEqual(_open_bets_state(self.user_id), (0, 3))
        self._place(0)
        self.assertEqual(_open_bets_state(self.user_id), (1, 3))

    def test_the_stricter_of_user_and_global_wins(self):
        BettingLimitsService.set_limit("global", 0, "max_open_bets", 9)
        BettingLimitsService.set_limit("user", self.user_id, "max_open_bets", 1)
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(self.user_id)["max_open_bets"], 1
        )


if __name__ == "__main__":
    unittest.main()
