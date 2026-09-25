"""
tests/test_bet_bans.py

Запреты видов ставок: строки `ban_<группа>` в `risk_limits_config`.

Запрет ставит администратор панели — на всю лигу или на дивизион. На матч
действуют глобальные запреты ∪ запреты его дивизиона; кубковый матч берёт
дивизион у владельца этапа (`cup_stages.division_id`), а у общего кубка
владельца нет — только глобальные. `express` рынков не имеет и закрывает
купон из нескольких событий, если в нём есть матч под запретом.

Проверяет `RiskEngine` — значит, отказ одинаков для Telegram, Mini App и REST.
"""

import os
import tempfile
import unittest

import database
from services.betting_limits import BettingLimitsService

USER = 962001
# (match_id, division_id, [(market_id, market_key, selection_id, selection_key)])
MATCHES = {
    "d1": (962101, 1, [(962201, "1x2", 962301, "p1"), (962202, "total_goals", 962302, "over_2.5")]),
    "d1b": (962102, 1, [(962203, "1x2", 962303, "p1")]),
    "d2": (962103, 2, [(962204, "1x2", 962304, "p1")]),
}


class BetBansCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'ban_user', 'user')",
                (USER,),
            )
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Bans Season', 'active')")
            self.season = cursor.lastrowid
            for div in (1, 2):
                cursor.execute(
                    "INSERT INTO rounds (division_id, round_number, season_id, is_open, bets_open) "
                    "VALUES (?, 1, ?, 0, 1)",
                    (div, self.season),
                )
            for key, (match_id, div, markets) in MATCHES.items():
                cursor.execute("""
                    INSERT INTO matches (id, division_id, season_id, round_number, player1_team, player2_team, status)
                    VALUES (?, ?, ?, 1, ?, ?, 'open')
                """, (match_id, div, self.season, f"Home {key}", f"Away {key}"))
                for market_id, market_key, selection_id, selection_key in markets:
                    cursor.execute("""
                        INSERT INTO markets (id, match_id, market_key, market_name, status)
                        VALUES (?, ?, ?, ?, 'open')
                    """, (market_id, match_id, market_key, market_key))
                    cursor.execute("""
                        INSERT INTO market_selections (id, market_id, selection_key, selection_name,
                                                       odds_value, status, odds_version)
                        VALUES (?, ?, ?, ?, 1.80, 'active', 1)
                    """, (selection_id, market_id, selection_key, selection_key))

        database.get_or_create_wallet(USER)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE user_wallets SET balance = 100000 WHERE user_id = ?", (USER,))
        self._counter = 0

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    # ─── helpers ───

    def _leg(self, key: str, index: int = 0) -> dict:
        match_id, _div, markets = MATCHES[key]
        market_id, _mk, selection_id, selection_key = markets[index]
        return {"match_id": match_id, "market_id": market_id, "selection_id": selection_id,
                "outcome": selection_key, "odds": 1.80}

    def _place(self, *legs: dict):
        self._counter += 1
        return database.place_user_bet(
            user_id=USER, amount=100, selections=list(legs),
            idempotency_key=f"bans-{self._counter}",
        )

    @staticmethod
    def _ban(group: str, scope_type: str = "division", scope_id: int = 1) -> None:
        BettingLimitsService.set_limit(scope_type, scope_id, database.BET_BAN_PREFIX + group, 1)


class TestBanGroups(unittest.TestCase):
    def test_groups_by_market_key(self):
        cases = {
            "1x2": "result", "match_result": "result", "double_chance": "double",
            "total_goals": "total", "individual_total_2": "itotal", "handicap": "handicap",
            "btts": "btts", "both_teams_to_score": "btts", "correct_score": "correct_score",
        }
        for market_key, group in cases.items():
            with self.subTest(market_key=market_key):
                self.assertEqual(database.bet_ban_group(market_key), group)

    def test_groups_by_outcome_without_a_market(self):
        cases = {
            "p1": "result", "1": "result", "draw": "result", "home": "result",
            "x2": "double", "tb25": "total", "under_3.5": "total", "yes": "btts",
            "it1_over_1.5": "itotal", "h2_plus_1.5": "handicap", "cs_2_1": "correct_score",
        }
        for outcome, group in cases.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(database.bet_ban_group(None, outcome), group)

    def test_market_key_wins_over_outcome_and_unknown_is_none(self):
        self.assertEqual(database.bet_ban_group("total_goals", "p1"), "total")
        self.assertIsNone(database.bet_ban_group("weird_market", "weird_outcome"))
        self.assertIsNone(database.bet_ban_group())

    def test_every_group_has_a_label_and_key(self):
        for group, (label, _keys) in database.BET_BAN_GROUPS.items():
            self.assertTrue(label)
            self.assertIn(database.BET_BAN_PREFIX + group, database.BET_BAN_KEYS)


class TestBanPlacement(BetBansCase):
    def test_division_ban_rejects_with_a_russian_message(self):
        self._ban("result")
        ok, payload = self._place(self._leg("d1"))
        self.assertFalse(ok)
        self.assertEqual(payload["error"], "BET_TYPE_BANNED")
        self.assertEqual(payload["group"], "result")
        self.assertIn("«Исход»", payload["message"])

    def test_division_ban_leaves_other_divisions_and_groups_open(self):
        self._ban("result")
        ok, payload = self._place(self._leg("d2"))
        self.assertTrue(ok, payload)
        ok, payload = self._place(self._leg("d1", 1))
        self.assertTrue(ok, payload)

    def test_global_ban_hits_every_division(self):
        self._ban("result", "global", 0)
        for key in ("d1", "d2"):
            with self.subTest(match=key):
                ok, payload = self._place(self._leg(key))
                self.assertFalse(ok)
                self.assertEqual(payload["error"], "BET_TYPE_BANNED")

    def test_express_ban_closes_multi_leg_coupons_only(self):
        self._ban("express")
        ok, payload = self._place(self._leg("d1"), self._leg("d1b"))
        self.assertFalse(ok)
        self.assertEqual(payload["error"], "EXPRESS_BANNED")
        ok, payload = self._place(self._leg("d1"))
        self.assertTrue(ok, payload)
        # Экспресс из матчей другого дивизиона запрет не трогает.
        with database.transaction() as conn:
            conn.execute("UPDATE matches SET division_id = 2 WHERE id = ?", (MATCHES["d1b"][0],))
        ok, payload = self._place(self._leg("d2"), self._leg("d1b"))
        self.assertTrue(ok, payload)

    def test_express_with_one_banned_match_is_rejected(self):
        self._ban("express", "division", 2)
        ok, payload = self._place(self._leg("d1"), self._leg("d2"))
        self.assertFalse(ok)
        self.assertEqual(payload["error"], "EXPRESS_BANNED")
        self.assertEqual(payload["match_id"], MATCHES["d2"][0])

    def test_lifting_the_ban_reopens_betting(self):
        self._ban("result")
        self.assertFalse(self._place(self._leg("d1"))[0])
        database.delete_risk_limit_override("division", 1, "ban_result")
        ok, payload = self._place(self._leg("d1"))
        self.assertTrue(ok, payload)

    def test_zero_value_is_not_a_ban(self):
        BettingLimitsService.set_limit("division", 1, "ban_result", 0)
        self.assertEqual(database.get_bet_bans(1), set())


class TestBanScope(BetBansCase):
    def _cup_match(self, match_id: int, division_id: int | None) -> None:
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO cup_stages (season_id, stage, stage_order, division_id) VALUES (?, ?, 1, ?)",
                (self.season, f"1/8@D{division_id}" if division_id else "1/8", division_id),
            )
            stage_id = cursor.lastrowid
            cursor.execute("""
                INSERT INTO matches (id, division_id, season_id, round_number, player1_team, player2_team,
                                     status, tournament_type, stage_id)
                VALUES (?, 0, ?, 1, 'Кубок А', 'Кубок Б', 'open', 'cup', ?)
            """, (match_id, self.season, stage_id))

    def test_bans_union_global_and_division(self):
        self._ban("result")
        self._ban("btts", "global", 0)
        self._ban("total", "division", 2)
        self.assertEqual(database.get_bet_bans(1), {"result", "btts"})
        self.assertEqual(database.get_bet_bans(None), {"btts"})

    def test_division_cup_follows_its_division(self):
        self._cup_match(962501, 1)
        self._cup_match(962502, None)
        self._ban("result")
        self.assertEqual(database.get_match_bet_bans(962501), {"result"})
        self.assertEqual(database.get_match_bet_bans(962502), set())

        self._ban("express", "global", 0)
        self.assertEqual(database.get_match_bet_bans(962502), {"express"})
        self.assertEqual(database.get_match_bet_bans(962501), {"result", "express"})

    def test_league_match_uses_its_division(self):
        self._ban("result", "division", 2)
        self.assertEqual(database.get_match_bet_bans(MATCHES["d2"][0]), {"result"})
        self.assertEqual(database.get_match_bet_bans(MATCHES["d1"][0]), set())
        self.assertEqual(database.get_match_bet_bans(999999), set())


if __name__ == "__main__":
    unittest.main()
