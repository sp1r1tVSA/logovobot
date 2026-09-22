"""
tests/test_fix10a_legacy_null_risk_scope.py

FIX-10A — один effective division scope для betting gate и RiskEngine.

Инвариант: `place_user_bet` обязан передать в `RiskEngine.evaluate_bet` ровно тот
же effective `division_id`, который он передаёт в `evaluate_round_betting_gate`.
До фикса гейт нормализовал `matches.division_id IS NULL → 1`
(`database.py:5970`, `:9063`), а контекст для риск-движка брался без нормализации
(`div_id` оставался `None`), из-за чего для legacy-матча
`BettingLimitsService.get_user_effective_limits` возвращал system limits вместо
лимитов дивизиона (`services/betting_limits.py:83`), а ветка
`division_exposure_limit` не выполнялась вовсе (`services/risk_engine.py:459`).

Само соглашение «NULL = дивизион 1» этим файлом НЕ меняется — оно проверяется как
неизменное (TEST F).

 A. Legacy-матч (division_id IS NULL): гейт пропускает, RiskEngine получает 1,
    лимиты дивизиона 1 применяются.
 B. Матч Д2: RiskEngine получает 2, применяются лимиты Д2.
 C. Матч Д5: RiskEngine получает 5.
 D. division_exposure_limit Д1 действительно ограничивает ставку на legacy-матч.
 E. Cross-division изоляция лимитов в обе стороны (Д1 ↔ Д2).
 F. Гейт для NULL-матча не изменился: линия Д1 решает, линия Д2 не помогает.
 G. Non-vacuity: то же тело купона при division_id=None (старое значение) проходит
    там, где при division_id=1 отказывает — тест не пустой.
"""

import contextlib
import os
import tempfile
import unittest
from unittest import mock

import database
from services.betting_limits import (
    BettingLimitsService,
    DEFAULT_DIVISION_EXPOSURE_LIMIT,
    DEFAULT_MAX_BET,
)
from services.risk_engine import RiskEngine


USER = 984001
ROUND = 5
SCOPES = ("legacy", "d1", "d2", "d5")
# division_id строки матча; «legacy» — намеренно NULL, как в боевой базе до миграции 003.
SCOPE_DIVISIONS = {"legacy": None, "d1": 1, "d2": 2, "d5": 5}
ODDS = 2.00


class TestFix10aLegacyNullRiskScope(unittest.TestCase):
    """Свой файл БД на тест — лимиты дивизионов и купоны не затекают между тестами."""

    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()

        self.match_ids = {}
        self.market_ids = {}
        self.selection_ids = {}

        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) "
                "VALUES (?, 'fix10a_bettor', 'Fix10a FC', 1, 'user')",
                (USER,)
            )
            c.execute("INSERT INTO seasons (name, status) VALUES ('FIX-10A Season', 'active')")
            self.season_id = c.lastrowid

            # Один номер тура в трёх дивизионах, линии открыты (is_open = 0, bets_open = 1).
            for d_id in (1, 2, 5):
                c.execute(
                    "INSERT INTO rounds (round_number, division_id, season_id, is_open, bets_open, deadline) "
                    "VALUES (?, ?, ?, 0, 1, NULL)",
                    (ROUND, d_id, self.season_id)
                )

            for i, name in enumerate(SCOPES):
                m_id, mk_id, sel_id = 984101 + i, 984201 + i, 984301 + i
                c.execute(
                    "INSERT INTO matches (id, round_number, division_id, season_id, "
                    "player1_team, player2_team, status) VALUES (?, ?, ?, ?, ?, ?, 'scheduled')",
                    (m_id, ROUND, SCOPE_DIVISIONS[name], self.season_id,
                     f"{name} Home", f"{name} Away")
                )
                c.execute(
                    "INSERT INTO markets (id, match_id, market_key, market_name, status, created_at) "
                    "VALUES (?, ?, 'match_result', 'Match Winner', 'open', datetime('now', '+3 hours'))",
                    (mk_id, m_id)
                )
                c.execute(
                    "INSERT INTO market_selections (id, market_id, selection_key, selection_name, "
                    "odds_value, status, odds_version, updated_at) "
                    "VALUES (?, ?, 'p1', 'Home', ?, 'active', 1, datetime('now', '+3 hours'))",
                    (sel_id, mk_id, ODDS)
                )
                c.execute(
                    "INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, "
                    "odd_p2, is_active, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 3.0, 3.5, 1, datetime('now', '+3 hours'))",
                    (m_id, ROUND, f"{name} Home", f"{name} Away", ODDS)
                )
                self.match_ids[name] = m_id
                self.market_ids[name] = mk_id
                self.selection_ids[name] = sel_id

        database.get_or_create_wallet(USER)
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE user_wallets SET balance = 500000 WHERE user_id = ?", (USER,)
            )

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    # --- helpers -----------------------------------------------------------

    def _selection(self, scope):
        return [{
            "match_id": self.match_ids[scope],
            "market_id": self.market_ids[scope],
            "selection_id": self.selection_ids[scope],
            "outcome": "p1",
        }]

    def _bet(self, scope, amount, key):
        return database.place_user_bet(
            user_id=USER, amount=amount,
            selections=self._selection(scope), idempotency_key=key,
        )

    def _error(self, result):
        return result.get("error") if isinstance(result, dict) else str(result)

    def _set_division_limit(self, division_id, key, value):
        BettingLimitsService.set_limit("division", division_id, key, value)

    def _set_line(self, division_id, bets_open):
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE rounds SET bets_open = ? "
                "WHERE round_number = ? AND division_id = ? AND season_id = ?",
                (1 if bets_open else 0, ROUND, division_id, self.season_id)
            )

    @contextlib.contextmanager
    def _risk_division_spy(self):
        """Записать division_id, который place_user_bet реально передал в RiskEngine."""
        seen = []
        real = RiskEngine.evaluate_bet

        def spy(**kwargs):
            seen.append(kwargs.get("division_id"))
            return real(**kwargs)

        with mock.patch.object(RiskEngine, "evaluate_bet", side_effect=spy):
            yield seen

    def _gate(self, division_id):
        with database.transaction() as conn:
            return database.evaluate_round_betting_gate(
                conn.cursor(), ROUND, division_id, self.season_id,
                match_id=self.match_ids["legacy"],
            )

    # --- A. Legacy NULL-матч ------------------------------------------------

    def test_a_legacy_null_match_goes_to_risk_engine_as_division_1(self):
        self._set_division_limit(1, "max_bet", 1000)

        with self._risk_division_spy() as seen:
            ok, res = self._bet("legacy", 500, "a1")
        self.assertTrue(ok, f"Ставка на legacy-матч должна пройти: {res}")
        self.assertEqual(seen, [1], "RiskEngine должен получить effective division_id=1")

        # Лимит дивизиона 1 применён именно к legacy-матчу: 5000 > 1000 (Д1),
        # хотя system limit — DEFAULT_MAX_BET.
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(USER, division_id=1)["max_bet"], 1000
        )
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(USER, division_id=None)["max_bet"],
            DEFAULT_MAX_BET,
        )
        ok2, res2 = self._bet("legacy", 5000, "a2")
        self.assertFalse(ok2, "Лимит max_bet дивизиона 1 должен отклонить ставку")
        self.assertEqual(self._error(res2), "MAX_BET_EXCEEDED")

    # --- B. Явный Д2 --------------------------------------------------------

    def test_b_explicit_division_two_goes_to_risk_engine_as_two(self):
        self._set_division_limit(2, "max_bet", 1000)

        with self._risk_division_spy() as seen:
            ok, res = self._bet("d2", 500, "b1")
        self.assertTrue(ok, f"Ставка на матч Д2 должна пройти: {res}")
        self.assertEqual(seen, [2])

        ok2, res2 = self._bet("d2", 5000, "b2")
        self.assertFalse(ok2, "Лимит max_bet дивизиона 2 должен отклонить ставку")
        self.assertEqual(self._error(res2), "MAX_BET_EXCEEDED")

    # --- C. Явный Д5 --------------------------------------------------------

    def test_c_explicit_division_five_goes_to_risk_engine_as_five(self):
        self._set_division_limit(5, "max_bet", 1000)

        with self._risk_division_spy() as seen:
            ok, res = self._bet("d5", 500, "c1")
        self.assertTrue(ok, f"Ставка на матч Д5 должна пройти: {res}")
        self.assertEqual(seen, [5])

        ok2, res2 = self._bet("d5", 5000, "c2")
        self.assertFalse(ok2, "Лимит max_bet дивизиона 5 должен отклонить ставку")
        self.assertEqual(self._error(res2), "MAX_BET_EXCEEDED")

    # --- D. division exposure для legacy-матча ------------------------------

    def test_d_division_exposure_limit_enforced_for_legacy_null_match(self):
        self._set_division_limit(1, "division_exposure_limit", 3000)
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(USER, division_id=1)
            ["division_exposure_limit"], 3000
        )

        # 1000 @ 2.00 → potential_win 2000 ≤ 3000: первый купон проходит и сам
        # создаёт ответственность дивизиона 1 (net = 2000 - 1000 = 1000).
        ok1, res1 = self._bet("d1", 1000, "d-first")
        self.assertTrue(ok1, f"Первая ставка в Д1 должна пройти: {res1}")

        # 1200 @ 2.00 → 1000 + 2400 > 3000. Матч legacy (division_id IS NULL),
        # значит отказ возможен только если риск-движок увидел дивизион 1.
        ok2, res2 = self._bet("legacy", 1200, "d-legacy")
        self.assertFalse(ok2, "division_exposure_limit Д1 должен отклонить ставку на legacy-матч")
        self.assertEqual(self._error(res2), "DIVISION_EXPOSURE_LIMIT")

    # --- E. Изоляция лимитов между дивизионами ------------------------------

    def test_e_division_limits_do_not_cross(self):
        self._set_division_limit(1, "max_bet", 1000)
        self._set_division_limit(2, "max_bet", 20000)

        ok_d1, res_d1 = self._bet("d1", 4000, "e1-d1")
        self.assertFalse(ok_d1, "Д1 с узким лимитом: ставка обязана быть отклонена")
        self.assertEqual(self._error(res_d1), "MAX_BET_EXCEEDED")

        ok_d2, res_d2 = self._bet("d2", 4000, "e1-d2")
        self.assertTrue(ok_d2, f"Лимит Д1 не должен действовать на Д2: {res_d2}")

        # Зеркало: узкий лимит теперь у Д2.
        self._set_division_limit(1, "max_bet", 20000)
        self._set_division_limit(2, "max_bet", 1000)

        ok_d2b, res_d2b = self._bet("d2", 4000, "e2-d2")
        self.assertFalse(ok_d2b, "Д2 с узким лимитом: ставка обязана быть отклонена")
        self.assertEqual(self._error(res_d2b), "MAX_BET_EXCEEDED")

        ok_d1b, res_d1b = self._bet("d1", 4000, "e2-d1")
        self.assertTrue(ok_d1b, f"Лимит Д2 не должен действовать на Д1: {res_d1b}")

    # --- F. Гейт для NULL-матча не изменился --------------------------------

    def test_f_gate_still_resolves_legacy_null_to_division_one_line(self):
        # Совпадение с legacy-соглашением: NULL и явная 1 дают один и тот же ответ.
        self._set_line(1, True)
        self._set_line(2, False)
        allowed_null, reason_null, _ = self._gate(None)
        allowed_one, reason_one, _ = self._gate(1)
        self.assertTrue(allowed_null, "Открытая линия Д1 должна принимать legacy-матч")
        self.assertEqual((allowed_null, reason_null), (allowed_one, reason_one))

        ok, res = self._bet("legacy", 700, "f-open")
        self.assertTrue(ok, f"Ставка по открытой линии Д1 должна пройти: {res}")

        # Линия Д1 закрыта — ставка отказывает, даже когда линия Д2 того же тура открыта.
        self._set_line(1, False)
        self._set_line(2, True)
        allowed, reason, _ = self._gate(None)
        self.assertFalse(allowed, "Закрытая линия Д1 должна отклонять legacy-матч")
        self.assertEqual(reason, "LINE_CLOSED")
        self.assertTrue(self._gate(2)[0], "Линия Д2 при этом открыта — она не «спасает» и не портит гейт")

        ok2, res2 = self._bet("legacy", 700, "f-closed")
        self.assertFalse(ok2, "После закрытия линии Д1 купон на legacy-матч не принимается")

    # --- G. Non-vacuity ------------------------------------------------------

    def test_g_nonvacuity_old_none_value_would_pass_the_rejected_bet(self):
        """Старое значение аргумента обязан ловить именно этот тест."""
        self._set_division_limit(1, "division_exposure_limit", 3000)
        ok1, res1 = self._bet("d1", 1000, "g-first")
        self.assertTrue(ok1, f"Первая ставка в Д1 должна пройти: {res1}")

        # Со старым значением аргумента (None) лимит берётся из системного
        # значения, а ветка 8c не выполняется вовсе. Если бы при None лимит тоже
        # был 3000 — нижеприведённые проверки ничего не различали бы.
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(USER, division_id=1)["division_exposure_limit"],
            3000,
        )
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(USER, division_id=None)["division_exposure_limit"],
            DEFAULT_DIVISION_EXPOSURE_LIMIT,
            "Без division scope лимит обязан оставаться системным — иначе тест пустой",
        )

        # Старое поведение place_user_bet: тот же купон и та же сумма проходят.
        old = RiskEngine.evaluate_bet(
            user_id=USER, amount=1200,
            selections=self._selection("legacy"),
            division_id=None,
        )
        self.assertTrue(old.allowed, "Со старым division_id=None ставка должна проходить")

        # Новое поведение: division_id=1 — отказ по лимиту дивизиона.
        new = RiskEngine.evaluate_bet(
            user_id=USER, amount=1200,
            selections=self._selection("legacy"),
            division_id=1,
        )
        self.assertFalse(new.allowed, "С division_id=1 та же ставка должна быть отклонена")
        self.assertEqual(new.reason, "DIVISION_EXPOSURE_LIMIT")

        ok_live, res_live = self._bet("legacy", 1200, "g-live")
        self.assertFalse(ok_live, "Сквозной путь place_user_bet обязан наследовать новое поведение")
        self.assertEqual(self._error(res_live), "DIVISION_EXPOSURE_LIMIT")


if __name__ == "__main__":
    unittest.main()
