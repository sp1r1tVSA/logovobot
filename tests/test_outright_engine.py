"""
tests/test_outright_engine.py

Модель долгосрочных рынков (`services/outright_engine.py`): цена, серия до двух
побед, сетка кубка, Монте-Карло чемпионата и бомбардира, dead heat.
"""

import unittest

from services import outright_engine as engine


def _row(points=0, gf=0, ga=0, wins=0):
    return {"points": points, "goals_scored": gf, "goals_conceded": ga, "wins": wins}


class TestPrice(unittest.TestCase):
    def test_price_applies_the_margin_and_the_bounds(self):
        self.assertAlmostEqual(engine.price(0.5), round(1 / (0.5 * engine.OUTRIGHT_MARGIN), 2))
        self.assertEqual(engine.price(1.0), max(engine.OUTRIGHT_MIN_ODD, round(1 / engine.OUTRIGHT_MARGIN, 2)))
        self.assertEqual(engine.price(0.0), engine.OUTRIGHT_MAX_ODD)
        self.assertEqual(engine.price(1e-6), engine.OUTRIGHT_MAX_ODD)


class TestBestOfThree(unittest.TestCase):
    def test_even_game_is_an_even_series(self):
        self.assertAlmostEqual(engine.bo3_win_prob(0.5), 0.5)

    def test_series_resumes_from_its_score(self):
        self.assertAlmostEqual(engine.bo3_win_prob(0.5, 1, 0), 0.75)
        self.assertAlmostEqual(engine.bo3_win_prob(0.5, 0, 1), 0.25)
        self.assertAlmostEqual(engine.bo3_win_prob(0.6, 1, 1), 0.6)
        self.assertEqual(engine.bo3_win_prob(0.1, 2, 0), 1.0)
        self.assertEqual(engine.bo3_win_prob(0.9, 0, 2), 0.0)

    def test_series_amplifies_the_favourite(self):
        self.assertGreater(engine.bo3_win_prob(0.6), 0.6)


class TestCupBracket(unittest.TestCase):
    def test_complete_bracket_needs_a_power_of_two_to_the_final(self):
        self.assertTrue(engine.cup_bracket_complete(1, 0))   # финал
        self.assertTrue(engine.cup_bracket_complete(4, 2))   # 1/4
        self.assertTrue(engine.cup_bracket_complete(16, 4))  # 1/16
        # 1/64 общего кубка: 16 серий, а до финала нужно 64 — клубы вступают позже.
        self.assertFalse(engine.cup_bracket_complete(16, 6))
        self.assertFalse(engine.cup_bracket_complete(0, 0))

    def test_winner_probabilities_sum_to_one(self):
        series = [
            {"team1_name": "A", "team2_name": "B"},
            {"team1_name": "C", "team2_name": "D"},
            {"team1_name": "E", "team2_name": "F"},
            {"team1_name": "G", "team2_name": "H"},
        ]
        strength = dict(zip("ABCDEFGH", (9, 1, 5, 5, 3, 7, 2, 8)))
        probs = engine.cup_winner_probs(series, lambda a, b: strength[a] / (strength[a] + strength[b]))
        self.assertAlmostEqual(sum(probs.values()), 1.0)
        self.assertEqual(max(probs, key=probs.get), "A")

    def test_decided_series_sends_only_its_winner_on(self):
        series = [
            {"team1_name": "A", "team2_name": "B", "winner_name": "B"},
            {"team1_name": "C", "team2_name": "D"},
        ]
        probs = engine.cup_winner_probs(series, lambda a, b: 0.5)
        self.assertNotIn("A", probs)
        self.assertAlmostEqual(probs["B"], 0.5)
        self.assertAlmostEqual(probs["C"] + probs["D"], 0.5)

    def test_series_in_progress_counts_its_score(self):
        leading = engine.cup_winner_probs([{"team1_name": "A", "team2_name": "B", "team1_wins": 1}],
                                          lambda a, b: 0.5)
        self.assertAlmostEqual(leading["A"], 0.75)


class TestLeague(unittest.TestCase):
    def test_finished_league_goes_to_the_leader(self):
        table = {"A": _row(9, 6, 2, 3), "B": _row(6), "C": _row(0)}
        probs = engine.simulate_league_winner(table, [], n=100, seed=1)
        self.assertEqual(probs, {"A": 1.0, "B": 0.0, "C": 0.0})

    def test_full_tie_is_split(self):
        table = {"A": _row(3, 2, 1, 1), "B": _row(3, 2, 1, 1)}
        probs = engine.simulate_league_winner(table, [], n=10, seed=1)
        self.assertAlmostEqual(probs["A"], 0.5)

    def test_goal_difference_breaks_a_points_tie(self):
        table = {"A": _row(3, 5, 1, 1), "B": _row(3, 2, 1, 1)}
        probs = engine.simulate_league_winner(table, [], n=10, seed=1)
        self.assertEqual(probs["A"], 1.0)

    def test_simulation_sums_to_one_and_is_reproducible(self):
        table = {t: _row() for t in "ABCD"}
        fixtures = [("A", "B", 2.0, 0.8), ("C", "D", 1.2, 1.2), ("A", "C", 1.8, 1.0), ("B", "D", 1.1, 1.3)]
        first = engine.simulate_league_winner(table, fixtures, n=5_000, seed=7)
        again = engine.simulate_league_winner(table, fixtures, n=5_000, seed=7)
        self.assertEqual(first, again)
        self.assertAlmostEqual(sum(first.values()), 1.0)
        self.assertEqual(max(first, key=first.get), "A")

    def test_eliminated_only_when_the_leader_is_out_of_reach(self):
        table = {"A": _row(10), "B": _row(7), "C": _row(4)}
        out = engine.league_eliminated(table, {"A": 1, "B": 1, "C": 1})
        # C максимум 7 < 10, B догоняет по очкам — равенство решают тай-брейки.
        self.assertEqual(out, {"C"})


class TestTopScorer(unittest.TestCase):
    def test_shares_sum_to_one_and_favour_the_leader(self):
        players = [{"goals": 10, "rate": 1.0, "remaining": 3},
                   {"goals": 6, "rate": 1.0, "remaining": 3},
                   {"goals": 0, "rate": 0.2, "remaining": 3}]
        shares = engine.simulate_top_scorer(players, n=5_000, seed=3)
        self.assertAlmostEqual(sum(shares), 1.0)
        self.assertGreater(shares[0], shares[1])
        self.assertEqual(shares, engine.simulate_top_scorer(players, n=5_000, seed=3))

    def test_finished_tie_is_a_dead_heat(self):
        players = [{"goals": 8, "rate": 1, "remaining": 0}, {"goals": 8, "rate": 1, "remaining": 0},
                   {"goals": 3, "rate": 1, "remaining": 0}]
        self.assertEqual(engine.simulate_top_scorer(players, n=50, seed=1), [0.5, 0.5, 0.0])

    def test_chunking_does_not_change_the_answer_shape(self):
        players = [{"goals": i % 5, "rate": 0.4, "remaining": 2} for i in range(300)]
        shares = engine.simulate_top_scorer(players, n=20_000, seed=5)
        self.assertEqual(len(shares), 300)
        self.assertAlmostEqual(sum(shares), 1.0, places=6)

    def test_rate_uncertainty_keeps_an_early_leader_catchable(self):
        # 13 голов за 5 матчей против 9 — но впереди 25 туров: лидер не «гарантирован».
        def player(goals, played):
            shape, scale = engine.scorer_rate_posterior(goals, played)
            return {"goals": goals, "remaining": 25, "shape": shape, "scale": scale}
        def point(goals, played):
            return {"goals": goals, "remaining": 25, "rate": engine.scorer_rate(goals, played)}
        field = [(13, 5), (9, 5), (8, 5)]
        spread = engine.simulate_top_scorer([player(*g) for g in field], n=20_000, seed=11)
        fixed = engine.simulate_top_scorer([point(*g) for g in field], n=20_000, seed=11)
        self.assertGreater(spread[0], spread[1])
        self.assertLess(spread[0], fixed[0] - 0.1)
        self.assertGreater(spread[1], 0.08)

    def test_leaders_and_dead_heat_factor(self):
        self.assertEqual(engine.top_scorer_leaders([("a", 5), ("b", 5), ("c", 2)]), ["a", "b"])
        self.assertEqual(engine.top_scorer_leaders([("a", 0)]), [])
        self.assertEqual(engine.dead_heat_factor(2), 0.5)
        self.assertEqual(engine.dead_heat_factor(0), 0.0)

    def test_scorer_rate_is_shrunk_early_in_the_season(self):
        self.assertLess(engine.scorer_rate(6, 2), 3.0)
        self.assertAlmostEqual(engine.scorer_rate(0, 0), engine.SCORER_PRIOR_RATE)


if __name__ == "__main__":
    unittest.main()
