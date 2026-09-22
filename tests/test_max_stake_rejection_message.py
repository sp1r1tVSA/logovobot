"""
Аудит 5a — отказ по лимиту ставки обязан показывать применённый лимит.

`RiskEngine.evaluate_bet` возвращает в `details["max_bet"]` ТОТ лимит, который
сработал (`services/risk_engine.py:96-103`) — то есть результат цепочки
user → division → global из `BettingLimitsService`. Ветка отказа в
`place_user_bet` (`database.py`) раньше печатала вместо него хардкодный
`_MAX_BET = 50 000`, из-за чего игроку с узким лимитом дивизиона показывали
потолок, в который он даже не упирался. Соседняя ветка `MAX_PAYOUT` (`:9624`)
к тому моменту уже вела себя правильно: `details.get("max_payout", _MAX_PAYOUT)`.

Правило, которое здесь проверяется:
  1. число в `payload["max_bet"]` и в `payload["message"]` — применённый лимит;
  2. `_MAX_BET` остаётся фолбэком, когда `details` пуст;
  3. ранний глобальный потолок `amount > _MAX_BET` (`database.py:9464`) не тронут —
     ставка больше 50 000 отказывает и не доходя до риск-движка.
"""
import unittest
from unittest import mock

import database
from services.betting_limits import BettingLimitsService, DEFAULT_MAX_BET
from services.risk_engine import RiskDecision, RiskEngine

USER = 985001
ROUND = 6
ODDS = 2.00
MATCH_ID = 985101
MARKET_ID = 985201
SELECTION_ID = 985301


class TestMaxStakeRejectionMessage(unittest.TestCase):

    def setUp(self):
        database.init_db()
        database.ensure_canonical_divisions()
        # Лимиты из других файлов этого процесса нам не нужны, а свои ставим явно.
        with database.transaction() as conn:
            conn.cursor().execute("DELETE FROM risk_limits_config")
            conn.cursor().execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) "
                "VALUES (?, 'limit_msg_bettor', 'LimitMsg FC', 1, 'user')",
                (USER,),
            )
            conn.cursor().execute("UPDATE seasons SET status = 'active' WHERE id = 1")
            # init_db сеет календарь сезона 1, поэтому тур берётся «если его нет»
            # и явно открывается: ставке нужна открытая линия Д1 этого тура.
            conn.cursor().execute(
                "INSERT OR IGNORE INTO rounds (round_number, division_id, season_id, is_open, bets_open, deadline) "
                "VALUES (?, 1, 1, 0, 1, NULL)",
                (ROUND,),
            )
            conn.cursor().execute(
                "UPDATE rounds SET is_open = 0, bets_open = 1, deadline = NULL "
                "WHERE round_number = ? AND division_id = 1 AND season_id = 1",
                (ROUND,),
            )
            conn.cursor().execute(
                "INSERT OR REPLACE INTO matches (id, round_number, division_id, season_id, "
                "player1_team, player2_team, status) "
                "VALUES (?, ?, 1, 1, 'Home', 'Away', 'scheduled')",
                (MATCH_ID, ROUND),
            )
            conn.cursor().execute(
                "INSERT OR REPLACE INTO markets (id, match_id, market_key, market_name, status, created_at) "
                "VALUES (?, ?, 'match_result', 'Match Winner', 'open', datetime('now', '+3 hours'))",
                (MARKET_ID, MATCH_ID),
            )
            conn.cursor().execute(
                "INSERT OR REPLACE INTO market_selections (id, market_id, selection_key, selection_name, "
                "odds_value, status, odds_version, updated_at) "
                "VALUES (?, ?, 'p1', 'Home', ?, 'active', 1, datetime('now', '+3 hours'))",
                (SELECTION_ID, MARKET_ID, ODDS),
            )

        database.get_or_create_wallet(USER)
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE user_wallets SET balance = 500000 WHERE user_id = ?", (USER,)
            )

    def tearDown(self):
        with database.transaction() as conn:
            conn.cursor().execute("DELETE FROM risk_limits_config")

    # --- helpers ----------------------------------------------------------

    def _bet(self, amount: int, key: str):
        return database.place_user_bet(
            user_id=USER,
            amount=amount,
            selections=[{
                "match_id": MATCH_ID,
                "market_id": MARKET_ID,
                "selection_id": SELECTION_ID,
                "outcome": "p1",
            }],
            idempotency_key=key,
        )

    def _rejection(self, amount: int, key: str) -> dict:
        ok, res = self._bet(amount, key)
        self.assertFalse(ok, f"Ставка {amount} должна быть отклонена")
        self.assertIsInstance(res, dict, f"Ожидался структурированный отказ, получено {res!r}")
        self.assertEqual(res.get("error"), "MAX_BET_EXCEEDED")
        return res

    # --- A. применённый лимит дивизиона -----------------------------------

    def test_division_limit_is_the_number_the_player_sees(self):
        BettingLimitsService.set_limit("division", 1, "max_bet", 1000)
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(USER, division_id=1)["max_bet"], 1000
        )

        res = self._rejection(5000, "msg-division")

        self.assertEqual(res["max_bet"], 1000)
        self.assertIn(f"{1000:,}", res["message"])
        self.assertNotIn(f"{DEFAULT_MAX_BET:,}", res["message"],
                         "Глобальный потолок показан вместо лимита дивизиона")

    def test_payload_number_and_message_number_agree(self):
        """Число в контракте ответа и в тексте — одно и то же."""
        BettingLimitsService.set_limit("division", 1, "max_bet", 2500)
        res = self._rejection(5000, "msg-consistent")

        self.assertIsInstance(res["max_bet"], int)
        self.assertIn(f"{res['max_bet']:,}", res["message"])

    # --- B. user override поверх дивизионного -----------------------------

    def test_user_override_is_the_number_the_player_sees(self):
        BettingLimitsService.set_limit("division", 1, "max_bet", 1000)
        BettingLimitsService.set_limit("user", USER, "max_bet", 700)
        self.assertEqual(
            BettingLimitsService.get_user_effective_limits(USER, division_id=1)["max_bet"], 700
        )

        res = self._rejection(5000, "msg-user")

        self.assertEqual(res["max_bet"], 700)
        self.assertIn(f"{700:,}", res["message"])

    # --- C. фолбэк на константу -------------------------------------------

    def test_falls_back_to_global_constant_when_engine_gives_no_details(self):
        """Если RiskEngine вернул отказ без details — печатается `_MAX_BET`, а не 0/None."""
        decision = RiskDecision(decision="REJECT", allowed=False, reason="MAX_STAKE",
                                message="x", details={})
        with mock.patch.object(RiskEngine, "evaluate_bet", return_value=decision):
            ok, res = self._bet(500, "msg-fallback")

        self.assertFalse(ok)
        self.assertEqual(res["error"], "MAX_BET_EXCEEDED")
        self.assertEqual(res["max_bet"], DEFAULT_MAX_BET)
        self.assertIn(f"{DEFAULT_MAX_BET:,}", res["message"])

    # --- D. глобальный потолок не ослаблен --------------------------------

    def test_global_ceiling_still_rejects_before_the_risk_engine(self):
        """Ранний gate `amount > _MAX_BET` (database.py:9464) жив и не зависит от лимитов."""
        BettingLimitsService.set_limit("division", 1, "max_bet", 200000)

        seen = []
        real = RiskEngine.evaluate_bet

        def spy(**kwargs):
            seen.append(kwargs.get("amount"))
            return real(**kwargs)

        with mock.patch.object(RiskEngine, "evaluate_bet", side_effect=spy):
            res = self._rejection(60000, "msg-global")

        self.assertEqual(seen, [], "Ставка выше глобального потолка не должна доходить до риск-движка")
        self.assertEqual(res["max_bet"], DEFAULT_MAX_BET)
        self.assertIn(f"{DEFAULT_MAX_BET:,}", res["message"])


if __name__ == "__main__":
    unittest.main()
