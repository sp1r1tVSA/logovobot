"""
Автоматический пересчёт линии двигает коэффициенты матча не больше чем на ±15%
за одно изменение модели. В начале сезона таблица из 1–2 игр раскачивала
коэффициенты в разы за вечер (Ньюкасл — Боруссия, Ф2(−1.5): 13.45 → 5.89).
"""

import unittest
from unittest.mock import patch

import database
import services.betting_engine as betting_engine
import services.odds_engine as odds_engine
from services.odds_engine import MAX_REPRICE_STEP, smooth_match_repricing
from services.poisson_odds import calculate_poisson_market_odds as real_poisson

LIMIT = 1 + MAX_REPRICE_STEP


def _within_step(old: float, new: float) -> bool:
    # +0.01 на округление до сотых.
    return old / LIMIT - 0.01 <= new <= old * LIMIT + 0.01


class TestSmoothMatchRepricing(unittest.TestCase):
    CUR = {
        ("1x2", "p1"): (2.00, 2.00),
        ("1x2", "x"): (3.60, 3.60),
        ("1x2", "p2"): (3.80, 3.80),
    }

    def test_big_move_is_capped_for_every_selection(self):
        targets = {("1x2", "p1"): 1.30, ("1x2", "x"): 5.50, ("1x2", "p2"): 9.00}
        out = smooth_match_repricing(self.CUR, targets)
        for k, (cur, _) in self.CUR.items():
            self.assertTrue(_within_step(cur, out[k]), f"{k}: {cur} -> {out[k]}")
            # Движение в сторону модели, а не на месте.
            self.assertNotEqual(out[k], cur)
        # Самая дальняя по вероятности нога (П2: 3.80 → 9.00) упирается ровно в потолок.
        self.assertAlmostEqual(out[("1x2", "p2")], round(3.80 * LIMIT, 2), places=2)

    def test_capped_line_keeps_a_margin(self):
        """Общий шаг для всех исходов: сумма вероятностей не падает ниже 100%."""
        targets = {("1x2", "p1"): 1.30, ("1x2", "x"): 5.50, ("1x2", "p2"): 9.00}
        out = smooth_match_repricing(self.CUR, targets)
        self.assertGreater(sum(1 / o for o in out.values()), 1.0)

    def test_small_move_goes_all_the_way(self):
        targets = {("1x2", "p1"): 1.90, ("1x2", "x"): 3.70, ("1x2", "p2"): 4.10}
        self.assertEqual(smooth_match_repricing(self.CUR, targets), targets)

    def test_unchanged_model_does_not_keep_stepping(self):
        """Повторное открытие линии не должно дотягивать коэффициент до модели."""
        cur = {
            ("1x2", "p1"): (1.74, 1.30),
            ("1x2", "x"): (3.90, 5.50),
            ("1x2", "p2"): (4.30, 9.00),
        }
        targets = {k: model for k, (_, model) in cur.items()}
        out = smooth_match_repricing(cur, targets)
        self.assertEqual(out, {k: odd for k, (odd, _) in cur.items()})

    def test_new_selection_takes_the_model_price(self):
        targets = {("btts", "btts_yes"): 1.62}
        self.assertEqual(smooth_match_repricing({}, targets), targets)


class TestRepricingPipeline(unittest.TestCase):
    match_id = 889961
    user_id = 998861

    def setUp(self):
        database.init_db()
        self._cleanup()
        with database.transaction() as conn:
            conn.execute("INSERT INTO users (telegram_id, username, role) VALUES (?, 'smooth_user', 'user')", (self.user_id,))
            conn.execute("""
                INSERT INTO matches (id, tournament_id, round_number, player1_team, player2_team, status)
                VALUES (?, 1, 1, 'Спортинг', 'Бенфика', 'scheduled')
            """, (self.match_id,))
            conn.execute("INSERT OR REPLACE INTO rounds (round_number, is_open, bets_open, deadline) VALUES (1, 0, 1, '2099-01-01 23:59')")
        database.get_or_create_wallet(self.user_id)
        database.save_bet_market(self.match_id, 1, "Спортинг", "Бенфика", 2, 3, 4, 2, 2, 2, 2)

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM user_bets WHERE user_id = ?", (self.user_id,))
            conn.execute("DELETE FROM coin_transactions WHERE user_id = ?", (self.user_id,))
            conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (self.user_id,))
            conn.execute("DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id = ?)", (self.match_id,))
            conn.execute("DELETE FROM markets WHERE match_id = ?", (self.match_id,))
            conn.execute("DELETE FROM bet_markets WHERE match_id = ?", (self.match_id,))
            conn.execute("DELETE FROM matches WHERE id = ?", (self.match_id,))
            conn.execute("DELETE FROM users WHERE telegram_id = ?", (self.user_id,))

    def _reprice_with(self, s1: float, s2: float):
        """Пересчёт линии так, будто модель оценила команды как s1 и s2."""
        def fake(_s1, _s2, margin):
            return real_poisson(s1, s2, margin=margin)

        with patch("services.poisson_odds.calculate_poisson_market_odds", fake), \
             patch("services.betting_engine.calculate_poisson_market_odds", fake):
            betting_engine.regenerate_all_active_markets()

    def _selections(self) -> dict:
        return {
            (m["market_key"], s["selection_key"]): s
            for m in odds_engine.get_match_markets(self.match_id)
            for s in m["selections"]
        }

    def test_model_jump_moves_every_odd_at_most_15_percent(self):
        self._reprice_with(10, 10)
        before = {k: s["odds_value"] for k, s in self._selections().items()}

        self._reprice_with(22, 6)  # «одна победа 5:1» — модель резко меняет мнение
        after = self._selections()
        for k, old in before.items():
            self.assertTrue(_within_step(old, after[k]["odds_value"]), f"{k}: {old} -> {after[k]['odds_value']}")
        self.assertLess(after[("1x2", "p1")]["odds_value"], before[("1x2", "p1")])

    def test_refetching_the_line_does_not_step_further(self):
        self._reprice_with(10, 10)
        self._reprice_with(22, 6)
        once = {k: (s["odds_value"], s["odds_version"]) for k, s in self._selections().items()}
        self._reprice_with(22, 6)
        self._reprice_with(22, 6)
        self.assertEqual(once, {k: (s["odds_value"], s["odds_version"]) for k, s in self._selections().items()})

    def test_line_tile_shows_the_smoothed_odds_and_accepts_the_bet(self):
        self._reprice_with(10, 10)
        self._reprice_with(22, 6)
        sel = self._selections()
        line = next(m for m in database.get_active_bet_markets() if m["match_id"] == self.match_id)
        for field, key in betting_engine._TILE_SELECTIONS.items():
            self.assertAlmostEqual(line[field], sel[key]["odds_value"], places=2, msg=field)

        ok, result = database.place_user_bet(
            user_id=self.user_id,
            amount=100,
            selections=[{"match_id": self.match_id, "outcome": "p1", "odd": line["odd_p1"]}],
        )
        self.assertTrue(ok, f"Line pick rejected: {result}")


if __name__ == "__main__":
    unittest.main()
