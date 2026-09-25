"""
tests/test_outright_markets.py

Долгосрочные рынки на базе: пересчёт (`services/outright_service.refresh_outrights`),
приём ставки (`database.place_outright_bet`), запрет тренеру на свой дивизион
и свой кубок, ручная цена админа, dead heat, аннулирование и авторасчёт.
"""

import os
import tempfile
import unittest
from itertools import combinations

import config
import database
from services import outright_service

BETTOR = 971001
COACH_BASE = 971100


class OutrightCase(unittest.TestCase):
    """Дивизион 2: четыре клуба, круг из шести матчей в трёх турах, ничего не сыграно."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.div = next(d for d in database.get_divisions() if d["code"] == "DIV_2")["id"]
        self.clubs = list(config.DIVISION_CLUBS["DIV_2"][:4])
        self.coaches = {club: COACH_BASE + i for i, club in enumerate(self.clubs)}
        self.matches = []

        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE seasons SET status = 'finished'")
            cur.execute("INSERT INTO seasons (name, status) VALUES ('Outright Season', 'active')")
            self.season = cur.lastrowid
            cur.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'bettor', 'user')",
                        (BETTOR,))
            for club, uid in self.coaches.items():
                cur.execute("""
                    INSERT OR REPLACE INTO users (telegram_id, username, role, team_name, division_id)
                    VALUES (?, ?, 'player', ?, ?)
                """, (uid, f"coach{uid}", club, self.div))
            for rnd in (1, 2, 3):
                cur.execute("""
                    INSERT INTO rounds (season_id, division_id, round_number, is_open, status)
                    VALUES (?, ?, ?, 1, 'open')
                """, (self.season, self.div, rnd))
            for n, (a, b) in enumerate(combinations(self.clubs, 2)):
                cur.execute("""
                    INSERT INTO matches (division_id, season_id, round_number, tournament_type,
                                         player1_id, player2_id, player1_team, player2_team, status)
                    VALUES (?, ?, ?, 'league', ?, ?, ?, ?, 'pending')
                """, (self.div, self.season, n // 2 + 1, self.coaches[a], self.coaches[b], a, b))
                self.matches.append((cur.lastrowid, a, b))

        for uid in (BETTOR, *self.coaches.values()):
            database.get_or_create_wallet(uid)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_wallets SET balance = 100000")

    def tearDown(self):
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    # ─── helpers ───

    def _market(self, market_type: str, scope: str) -> dict | None:
        return next((m for m in database.get_outright_markets(self.season)
                     if m["market_type"] == market_type and m["scope_key"] == scope), None)

    def _winner_market(self) -> dict:
        return self._market("division_winner", f"D{self.div}")

    def _selection(self, market: dict, name: str) -> dict:
        return next(s for s in market["selections"] if s["name"] == name or s["team_name"] == name)

    def _confirm(self, index: int, s1: int, s2: int, scorer: tuple[str, str, int] | None = None) -> None:
        match_id, a, b = self.matches[index]
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'confirmed', player1_score = ?, player2_score = ? WHERE id = ?",
                        (s1, s2, match_id))
            if scorer:
                team, player, goals = scorer
                cur.execute("INSERT INTO match_events (match_id, team_name, player_name, event_type, count) "
                            "VALUES (?, ?, ?, 'goal', ?)", (match_id, team, player, goals))

    def _finish_league(self) -> None:
        for i in range(len(self.matches)):
            match_id = self.matches[i][0]
            with database.transaction() as conn:
                row = conn.cursor().execute("SELECT status FROM matches WHERE id = ?", (match_id,)).fetchone()
            if row["status"] != "confirmed":
                # Первый клуб списка выигрывает всё — у дивизиона один чемпион.
                a_first = self.matches[i][1] == self.clubs[0]
                self._confirm(i, 2 if a_first else 1, 0 if a_first else 1)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE rounds SET status = 'closed', is_open = 0 WHERE season_id = ?",
                                  (self.season,))

    def _balance(self, uid: int) -> int:
        return database.get_wallet_balance(uid)


class TestRefresh(OutrightCase):
    def test_division_winner_market_is_created_and_priced(self):
        summary = outright_service.refresh_outrights()
        self.assertGreaterEqual(summary["priced"], 1)
        market = self._winner_market()
        self.assertIsNotNone(market)
        self.assertEqual(market["status"], "open")
        self.assertEqual(len(market["selections"]), 4)
        self.assertAlmostEqual(sum(s["probability"] for s in market["selections"]), 1.0, places=6)
        for s in market["selections"]:
            self.assertGreater(s["odds_value"], 1.0)
            self.assertEqual(s["division_id"], self.div)

    def test_unchanged_state_is_not_repriced(self):
        outright_service.refresh_outrights()
        again = outright_service.refresh_outrights()
        self.assertEqual(again["priced"], 0)
        self.assertGreaterEqual(again["unchanged"], 1)

    def test_no_scorer_market_until_somebody_scores(self):
        outright_service.refresh_outrights()
        self.assertIsNone(self._market("division_top_scorer", f"D{self.div}"))
        self.assertIsNone(self._market("league_top_scorer", "LEAGUE"))

    def test_scorer_markets_carry_an_other_player_selection(self):
        self._confirm(0, 3, 0, scorer=(self.matches[0][1], "Иван Бомбардиров", 3))
        outright_service.refresh_outrights()
        for mtype, scope in (("division_top_scorer", f"D{self.div}"), ("league_top_scorer", "LEAGUE")):
            with self.subTest(mtype=mtype):
                market = self._market(mtype, scope)
                self.assertIsNotNone(market)
                keys = {s["selection_key"] for s in market["selections"]}
                self.assertIn(database.OUTRIGHT_OTHER_KEY, keys)
                named = self._selection(market, "Иван Бомбардиров")
                self.assertEqual(named["division_id"], self.div)
                self.assertAlmostEqual(sum(s["probability"] for s in market["selections"]), 1.0, places=6)

    def test_confirmed_match_moves_the_price_and_writes_history(self):
        outright_service.refresh_outrights()
        before = self._selection(self._winner_market(), self.clubs[0])
        self._confirm(0, 5, 0)
        self._confirm(1, 5, 0)
        outright_service.refresh_outrights()
        after = self._selection(self._winner_market(), self.clubs[0])
        self.assertLess(after["odds_value"], before["odds_value"])
        history = database.get_outright_history(self._winner_market()["id"])
        self.assertGreaterEqual(len(history[after["id"]]), 2)


class TestPlacement(OutrightCase):
    def setUp(self):
        super().setUp()
        outright_service.refresh_outrights()
        self.market = self._winner_market()
        self.sel = self._selection(self.market, self.clubs[1])

    def test_bet_is_accepted_and_debited(self):
        ok, res = database.place_outright_bet(BETTOR, self.sel["id"], 100, client_odd=self.sel["odds_value"])
        self.assertTrue(ok, res)
        self.assertEqual(res["potential_win"], int(round(100 * self.sel["odds_value"])))
        self.assertEqual(self._balance(BETTOR), 100000 - 100)
        self.assertEqual(database.count_user_open_outright_bets(BETTOR), 1)

    def test_idempotency_key_returns_the_same_bet(self):
        ok1, first = database.place_outright_bet(BETTOR, self.sel["id"], 100, idempotency_key="k1")
        ok2, second = database.place_outright_bet(BETTOR, self.sel["id"], 100, idempotency_key="k1")
        self.assertTrue(ok1 and ok2)
        self.assertEqual(first["bet_id"], second["bet_id"])
        self.assertEqual(self._balance(BETTOR), 100000 - 100)

    def test_changed_client_odd_is_rejected(self):
        ok, res = database.place_outright_bet(BETTOR, self.sel["id"], 100, client_odd=self.sel["odds_value"] + 1)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "ODDS_CHANGED")

    def test_stale_price_is_rejected_until_repriced(self):
        self._confirm(0, 2, 1)
        ok, res = database.place_outright_bet(BETTOR, self.sel["id"], 100)
        self.assertFalse(ok)
        self.assertEqual(res["error"], database.OUTRIGHT_REPRICING_ERROR)
        outright_service.refresh_outrights()
        ok, res = database.place_outright_bet(BETTOR, self.sel["id"], 100)
        self.assertTrue(ok, res)

    def test_coach_cannot_bet_on_their_own_division(self):
        coach = self.coaches[self.clubs[0]]
        ok, res = database.place_outright_bet(coach, self.sel["id"], 100)
        self.assertFalse(ok)
        self.assertEqual(res["error"], database.OUTRIGHT_OWN_SCOPE_ERROR)
        self.assertEqual(self._balance(coach), 100000)

    def test_suspended_market_and_selection_reject(self):
        database.set_outright_selection_status(self.sel["id"], "suspended")
        ok, res = database.place_outright_bet(BETTOR, self.sel["id"], 100)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "MARKET_SUSPENDED")
        database.set_outright_selection_status(self.sel["id"], "active")
        database.set_outright_market_status(self.market["id"], "suspended")
        ok, res = database.place_outright_bet(BETTOR, self.sel["id"], 100)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "MARKET_SUSPENDED")

    def test_payout_cap_counts_bets_already_held_on_the_selection(self):
        stake = int(9000 / self.sel["odds_value"])
        ok, _ = database.place_outright_bet(BETTOR, self.sel["id"], stake)
        self.assertTrue(ok)
        ok, res = database.place_outright_bet(BETTOR, self.sel["id"], stake)
        self.assertFalse(ok)
        self.assertEqual(res["error"], "MAX_PAYOUT_EXCEEDED")
        self.assertLess(res["max_allowed_stake"], stake)

    def test_admin_override_survives_a_reprice(self):
        ok, res = database.set_outright_odds_override(self.sel["id"], 7.5)
        self.assertTrue(ok, res)
        self._confirm(0, 1, 1)
        outright_service.refresh_outrights()
        sel = self._selection(self._winner_market(), self.clubs[1])
        self.assertEqual(sel["odds_value"], 7.5)
        self.assertEqual(sel["odds_override"], 7.5)
        ok, res = database.place_outright_bet(BETTOR, sel["id"], 100)
        self.assertTrue(ok, res)
        self.assertEqual(res["odd"], 7.5)


class TestSettlement(OutrightCase):
    def setUp(self):
        super().setUp()
        outright_service.refresh_outrights()
        self.market = self._winner_market()

    def test_dead_heat_pays_the_share(self):
        a = self._selection(self.market, self.clubs[0])
        b = self._selection(self.market, self.clubs[1])
        ok, bet_a = database.place_outright_bet(BETTOR, a["id"], 100)
        self.assertTrue(ok)
        ok, bet_b = database.place_outright_bet(BETTOR, b["id"], 100)
        self.assertTrue(ok)
        start = self._balance(BETTOR)
        ok, res = database.settle_outright_market(self.market["id"], {a["id"]: 0.5})
        self.assertTrue(ok, res)
        self.assertEqual(self._balance(BETTOR) - start, int(round(100 * 0.5 * bet_a["odd"])))
        bets = {x["id"]: x for x in database.get_user_outright_bets(BETTOR)}
        self.assertEqual(bets[bet_a["bet_id"]]["status"], "won")
        self.assertEqual(bets[bet_a["bet_id"]]["dead_heat_factor"], 0.5)
        self.assertEqual(bets[bet_b["bet_id"]]["status"], "lost")

    def test_settled_market_cannot_be_settled_again_or_repriced(self):
        a = self._selection(self.market, self.clubs[0])
        self.assertTrue(database.settle_outright_market(self.market["id"], {a["id"]: 1.0})[0])
        self.assertFalse(database.settle_outright_market(self.market["id"], {a["id"]: 1.0})[0])
        self._confirm(0, 3, 0)
        outright_service.refresh_outrights()
        self.assertEqual(self._winner_market()["status"], "settled")

    def test_shares_above_one_are_rejected(self):
        a = self._selection(self.market, self.clubs[0])
        b = self._selection(self.market, self.clubs[1])
        ok, _ = database.settle_outright_market(self.market["id"], {a["id"]: 1.0, b["id"]: 0.5})
        self.assertFalse(ok)

    def test_void_refunds_every_open_bet(self):
        sel = self._selection(self.market, self.clubs[2])
        self.assertTrue(database.place_outright_bet(BETTOR, sel["id"], 250)[0])
        ok, res = database.void_outright_market(self.market["id"], "тест")
        self.assertTrue(ok, res)
        self.assertEqual(res["refunded"], 1)
        self.assertEqual(self._balance(BETTOR), 100000)

    def test_finished_league_settles_automatically(self):
        champion = self._selection(self.market, self.clubs[0])
        loser = self._selection(self.market, self.clubs[3])
        ok, bet = database.place_outright_bet(BETTOR, champion["id"], 100)
        self.assertTrue(ok)
        self.assertTrue(database.place_outright_bet(BETTOR, loser["id"], 100)[0])
        start = self._balance(BETTOR)
        self._finish_league()
        summary = outright_service.refresh_outrights()
        self.assertGreaterEqual(summary["settled"], 1)
        market = self._winner_market()
        self.assertEqual(market["status"], "settled")
        self.assertEqual(self._selection(market, self.clubs[0])["status"], "won")
        self.assertEqual(self._balance(BETTOR) - start, bet["potential_win"])

    def test_open_round_holds_the_settlement(self):
        self._finish_league()
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE rounds SET status = 'open' WHERE round_number = 3 AND season_id = ?",
                                  (self.season,))
        outright_service.refresh_outrights()
        self.assertNotEqual(self._winner_market()["status"], "settled")


class TestCupMarket(OutrightCase):
    def setUp(self):
        super().setUp()
        database.create_cup_series("1/2", [(self.clubs[0], self.clubs[1]), (self.clubs[2], "Сельта Б")],
                                   season_id=self.season)
        outright_service.refresh_outrights()
        self.market = self._market("cup_winner", "CUP")

    def test_general_cup_market_covers_the_bracket(self):
        self.assertIsNotNone(self.market)
        self.assertEqual(len(self.market["selections"]), 4)
        self.assertAlmostEqual(sum(s["probability"] for s in self.market["selections"]), 1.0, places=6)

    def test_coach_in_the_bracket_is_locked_out_of_the_general_cup(self):
        sel = self._selection(self.market, self.clubs[2])
        in_cup = self.coaches[self.clubs[0]]
        ok, res = database.place_outright_bet(in_cup, sel["id"], 100)
        self.assertFalse(ok)
        self.assertEqual(res["error"], database.OUTRIGHT_OWN_SCOPE_ERROR)
        self.assertTrue(database.get_outright_coach_scope(in_cup)["in_general_cup"])

    def test_coach_outside_the_bracket_may_bet(self):
        outside = self.coaches[self.clubs[3]]
        self.assertFalse(database.get_outright_coach_scope(outside)["in_general_cup"])
        sel = self._selection(self.market, self.clubs[0])
        ok, res = database.place_outright_bet(outside, sel["id"], 100)
        self.assertTrue(ok, res)

    def test_final_winner_settles_the_cup(self):
        sel = self._selection(self.market, self.clubs[0])
        self.assertTrue(database.place_outright_bet(BETTOR, sel["id"], 100)[0])
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE cup_series SET winner_name = ?, team1_wins = 2, status = 'finished' "
                        "WHERE team1_name = ?", (self.clubs[0], self.clubs[0]))
            cur.execute("UPDATE cup_series SET winner_name = ?, team1_wins = 2, status = 'finished' "
                        "WHERE team1_name = ?", (self.clubs[2], self.clubs[2]))
        database.create_cup_series("final", [(self.clubs[0], self.clubs[2])], season_id=self.season)
        outright_service.refresh_outrights()
        market = self._market("cup_winner", "CUP")
        self.assertEqual(market["status"], "open")
        self.assertEqual(self._selection(market, self.clubs[1])["status"], "eliminated")
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE cup_series SET winner_name = ?, team1_wins = 2 "
                                  "WHERE stage = 'final'", (self.clubs[0],))
        outright_service.refresh_outrights()
        market = self._market("cup_winner", "CUP")
        self.assertEqual(market["status"], "settled")
        self.assertEqual(self._selection(market, self.clubs[0])["status"], "won")


if __name__ == "__main__":
    unittest.main()
