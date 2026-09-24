"""
tests/test_phase4_betting_experience.py

Phase 4 Regression Test Suite (25 Tests):
1. Валидация telegram init_data
2. Блокировка запросов без auth
3. Блокировка чужого кабинета / IDOR
4. Создание ставки ординар
5. Создание ставки экспресс
6. Проверка баланса при ставке
7. Запрет ставки с нулевой/отрицательной суммой
8. Запрет ставки больше баланса
9. Запрет ставки при закрытом раунде
10. Запрет ставки при дедлайне
11. Запрет ставки на завершенный матч
12. Идемпотентность кнопки 'Поставить'
13. Корректность расчёта total_odds
14. Корректность расчёта potential_win
15. Корректность списания баланса
16. Зачисление выигрыша после settlement
17. Возврат ставки при отмене/переносе
18. Получение истории ставок пользователя
19. Пагинация/лимиты истории ставок
20. Получение деталей конкретной ставки
21. Просмотр таблицы дивизиона через Mini App API
22. Просмотр результатов дивизиона через Mini App API
23. Изоляция данных Division 1 и Division 2
24. Корректность работы фильтров матчей
25. Защита от подделки коэффициента со стороны frontend
"""

import unittest
import datetime
import hmac
import hashlib
import json
import urllib.parse
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

import database
import config
from api.auth import validate_telegram_data, get_authenticated_user, check_user_access
from api.server import create_app
from services import odds_engine, settlement_engine, betting_engine


def make_valid_telegram_init_data(user_id: int, username: str = "tester", bot_token: str = None) -> str:
    """Helper to generate a cryptographically valid Telegram WebApp initData string."""
    token = bot_token or getattr(config, "TOKEN", "test_bot_token")
    user_dict = {
        "id": user_id,
        "first_name": "Test",
        "last_name": "User",
        "username": username,
        "language_code": "ru"
    }
    user_json = json.dumps(user_dict, separators=(",", ":"))
    auth_date = str(int(datetime.datetime.now().timestamp()))

    data = {
        "auth_date": auth_date,
        "query_id": "AAHdF6IQAAAAAN0XohDhrP_q",
        "user": user_json
    }

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    params = {**data, "hash": calculated_hash}
    return urllib.parse.urlencode(params)


class TestPhase4BettingExperience(unittest.TestCase):
    def setUp(self):
        self.user1_id = 998001
        self.user2_id = 998002
        self.admin_id = 998000

        database.init_db()

        with database.transaction() as conn:
            c = conn.cursor()
            u_ids = (self.admin_id, self.user1_id, self.user2_id)
            c.execute("DELETE FROM bet_items WHERE bet_id IN (SELECT id FROM user_bets WHERE user_id IN (?, ?, ?))", u_ids)
            c.execute("DELETE FROM user_bets WHERE user_id IN (?, ?, ?)", u_ids)
            c.execute("DELETE FROM coin_transactions WHERE user_id IN (?, ?, ?)", u_ids)
            c.execute("DELETE FROM user_wallets WHERE user_id IN (?, ?, ?)", u_ids)
            c.execute("DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id >= 99500)")
            c.execute("DELETE FROM markets WHERE match_id >= 99500")
            c.execute("DELETE FROM bet_markets WHERE match_id >= 99500")
            c.execute("DELETE FROM match_events WHERE match_id >= 99500")
            c.execute("DELETE FROM matches WHERE id >= 99500")
            c.execute("DELETE FROM rounds WHERE round_number >= 95")
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?, ?)", u_ids)

            # Setup test users
            c.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) VALUES (?, ?, ?, ?, ?)",
                      (self.admin_id, "p4_admin", "Admin Team", 1, "admin"))
            # 322-защита: клубы тестовых коучей намеренно не совпадают с участниками
            # матчей, на которые они ставят (иначе купон отклоняется как ставка на свой матч).
            c.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) VALUES (?, ?, ?, ?, ?)",
                      (self.user1_id, "p4_user1", "P4 Coach One", 1, "player"))
            c.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) VALUES (?, ?, ?, ?, ?)",
                      (self.user2_id, "p4_user2", "P4 Coach Two", 2, "player"))

            # Setup test rounds
            # Round 95: betting line open (is_open=0, bets_open=1), valid future deadline
            future_dl = (datetime.datetime.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("INSERT INTO rounds (round_number, is_open, bets_open, deadline, division_id) VALUES (95, 0, 1, ?, 1)", (future_dl,))

            # Round 96: Closed
            c.execute("INSERT INTO rounds (round_number, is_open, bets_open, deadline, division_id) VALUES (96, 0, 0, ?, 1)", (future_dl,))

            # Round 97: line open, but deadline in past
            past_dl = (datetime.datetime.now() - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("INSERT INTO rounds (round_number, is_open, bets_open, deadline, division_id) VALUES (97, 0, 1, ?, 1)", (past_dl,))

            # Round 98: Division 2 round with an open line
            c.execute("INSERT INTO rounds (round_number, is_open, bets_open, deadline, division_id) VALUES (98, 0, 1, ?, 2)", (future_dl,))

            # Setup test matches
            # Match 99501: Round 95, scheduled, Division 1
            c.execute("""
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99501, 95, 1, NULL, NULL, 'Arsenal P4', 'Chelsea P4', 'scheduled')
            """)
            # Match 99502: Round 95, scheduled, Division 1
            c.execute("""
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99502, 95, 1, NULL, NULL, 'Liverpool P4', 'ManCity P4', 'scheduled')
            """)
            # Match 99503: Round 96, scheduled in closed round
            c.execute("""
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99503, 96, 1, NULL, NULL, 'Everton P4', 'Fulham P4', 'scheduled')
            """)
            # Match 99504: Round 97, scheduled in expired deadline round
            c.execute("""
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99504, 97, 1, NULL, NULL, 'Tottenham P4', 'Newcastle P4', 'scheduled')
            """)
            # Match 99505: Round 95, already completed
            c.execute("""
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status, player1_score, player2_score)
                VALUES (99505, 95, 1, NULL, NULL, 'Juventus P4', 'Milan P4', 'completed', 2, 1)
            """)
            # Match 99506: Round 98, Division 2 match
            c.execute("""
                INSERT INTO matches (id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (99506, 98, 2, NULL, NULL, 'Div2 Alpha', 'Div2 Beta', 'scheduled')
            """)

            # Setup bet_markets for active matches
            c.execute("""
                INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, odd_tb25, odd_tm25, is_active)
                VALUES (99501, 95, 'Arsenal P4', 'Chelsea P4', 1.90, 3.20, 2.10, 1.85, 1.85, 1)
            """)
            c.execute("""
                INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, odd_tb25, odd_tm25, is_active)
                VALUES (99502, 95, 'Liverpool P4', 'ManCity P4', 2.00, 3.00, 2.50, 1.70, 2.00, 1)
            """)
            c.execute("""
                INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, odd_tb25, odd_tm25, is_active)
                VALUES (99506, 98, 'Div2 Alpha', 'Div2 Beta', 1.50, 3.50, 4.00, 1.60, 2.10, 1)
            """)

            # Generate relational markets for Match 99501 and 99502
            odds_engine.generate_match_markets(99501, 'Arsenal P4', 'Chelsea P4')
            odds_engine.generate_match_markets(99502, 'Liverpool P4', 'ManCity P4')

            # Wallets
            c.execute("INSERT INTO user_wallets (user_id, balance) VALUES (?, 1000)", (self.user1_id,))
            c.execute("INSERT INTO user_wallets (user_id, balance) VALUES (?, 100)", (self.user2_id,))

    def tearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            u_ids = (self.admin_id, self.user1_id, self.user2_id)
            c.execute("DELETE FROM bet_items WHERE bet_id IN (SELECT id FROM user_bets WHERE user_id IN (?, ?, ?))", u_ids)
            c.execute("DELETE FROM user_bets WHERE user_id IN (?, ?, ?)", u_ids)
            c.execute("DELETE FROM coin_transactions WHERE user_id IN (?, ?, ?)", u_ids)
            c.execute("DELETE FROM user_wallets WHERE user_id IN (?, ?, ?)", u_ids)
            c.execute("DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id >= 99500)")
            c.execute("DELETE FROM markets WHERE match_id >= 99500")
            c.execute("DELETE FROM bet_markets WHERE match_id >= 99500")
            c.execute("DELETE FROM matches WHERE id >= 99500")
            c.execute("DELETE FROM rounds WHERE round_number >= 95")
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?, ?)", u_ids)

    # -------------------------------------------------------------
    # 1. Валидация telegram init_data
    # -------------------------------------------------------------
    def test_01_telegram_init_data_validation(self):
        valid_init = make_valid_telegram_init_data(self.user1_id, "p4_user1")
        user = validate_telegram_data(valid_init)
        self.assertIsNotNone(user, "Valid init_data should successfully parse and validate")
        self.assertEqual(user["id"], self.user1_id)

        # Invalid signature
        tampered_init = valid_init.replace("auth_date=", "auth_date=123")
        self.assertIsNone(validate_telegram_data(tampered_init), "Tampered init_data must fail validation")

        # Empty data
        self.assertIsNone(validate_telegram_data(""), "Empty init_data must fail validation")

    # -------------------------------------------------------------
    # 2. Блокировка запросов без auth
    # -------------------------------------------------------------
    def test_02_block_requests_without_auth(self):
        user_info = get_authenticated_user("")
        self.assertIsNone(user_info, "Missing auth header must return None")

    # -------------------------------------------------------------
    # 3. Блокировка чужого кабинета / IDOR
    # -------------------------------------------------------------
    def test_03_block_idor_other_user_cabinet_and_bets(self):
        # Place a bet for user 1
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=100,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertTrue(success)

        # User 1 can view own bet
        own_bet = database.get_user_bet_by_id(bet_id, user_id=self.user1_id)
        self.assertIsNotNone(own_bet, "User 1 should access own bet")

        # User 2 attempts to view User 1's bet -> blocked
        idor_bet = database.get_user_bet_by_id(bet_id, user_id=self.user2_id)
        self.assertIsNone(idor_bet, "User 2 must be blocked from accessing User 1's bet")

    # -------------------------------------------------------------
    # 4. Создание ставки ординар
    # -------------------------------------------------------------
    def test_04_place_single_bet_success(self):
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=150,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertTrue(success, f"Single bet placement failed: {bet_id}")
        bet = database.get_user_bet_by_id(bet_id, self.user1_id)
        self.assertEqual(bet["bet_type"], "single")
        self.assertEqual(bet["amount"], 150)
        self.assertEqual(len(bet["items"]), 1)
        self.assertEqual(bet["items"][0]["outcome_type"], "p1")
        self.assertEqual(bet["status"], "pending")

    # -------------------------------------------------------------
    # 5. Создание ставки экспресс
    # -------------------------------------------------------------
    def test_05_place_express_bet_success(self):
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=200,
            selections=[
                {"match_id": 99501, "outcome": "p1"},
                {"match_id": 99502, "outcome": "x"}
            ]
        )
        self.assertTrue(success, f"Express bet placement failed: {bet_id}")
        bet = database.get_user_bet_by_id(bet_id, self.user1_id)
        self.assertEqual(bet["bet_type"], "express")
        self.assertEqual(len(bet["items"]), 2)
        # Verify odds multiplied
        self.assertGreater(bet["total_odd"], 3.0)

    # -------------------------------------------------------------
    # 6. Проверка баланса при ставке
    # -------------------------------------------------------------
    def test_06_check_balance_deduction_on_bet(self):
        initial_balance = database.get_user_balance(self.user1_id)
        self.assertEqual(initial_balance, 1000)

        stake = 250
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=stake,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertTrue(success)
        new_balance = database.get_user_balance(self.user1_id)
        self.assertEqual(new_balance, 750)

    # -------------------------------------------------------------
    # 7. Запрет ставки с нулевой/отрицательной суммой
    # -------------------------------------------------------------
    def test_07_reject_zero_or_negative_stake(self):
        # Stake 0
        s1, res1 = database.place_user_bet(
            user_id=self.user1_id,
            amount=0,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertFalse(s1, "Stake of 0 must be rejected")

        # Stake negative
        s2, res2 = database.place_user_bet(
            user_id=self.user1_id,
            amount=-100,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertFalse(s2, "Negative stake must be rejected")

    # -------------------------------------------------------------
    # 8. Запрет ставки больше баланса
    # -------------------------------------------------------------
    def test_08_reject_stake_exceeding_balance(self):
        # User 2 has 100 balance
        s, res = database.place_user_bet(
            user_id=self.user2_id,
            amount=500,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertFalse(s, "Stake exceeding balance must be rejected")
        self.assertIn("Недостаточно", res)

    # -------------------------------------------------------------
    # 9. Запрет ставки при закрытом раунде
    # -------------------------------------------------------------
    def test_09_reject_bet_on_closed_round(self):
        # Match 99503 is in Round 96 (is_open = 0)
        s, res = database.place_user_bet(
            user_id=self.user1_id,
            amount=50,
            selections=[{"match_id": 99503, "outcome": "p1"}]
        )
        self.assertFalse(s, "Bet on match in closed round must be rejected")
        self.assertIn("закрыт", res.lower())

    # -------------------------------------------------------------
    # 10. Запрет ставки при истекшем дедлайне
    # -------------------------------------------------------------
    def test_10_reject_bet_on_expired_deadline(self):
        # Match 99504 is in Round 97 (past deadline)
        s, res = database.place_user_bet(
            user_id=self.user1_id,
            amount=50,
            selections=[{"match_id": 99504, "outcome": "p1"}]
        )
        self.assertFalse(s, "Bet after round deadline must be rejected")
        self.assertIn("истек", res.lower())

    # -------------------------------------------------------------
    # 11. Запрет ставки на завершенный матч
    # -------------------------------------------------------------
    def test_11_reject_bet_on_completed_match(self):
        # Match 99505 is completed
        s, res = database.place_user_bet(
            user_id=self.user1_id,
            amount=50,
            selections=[{"match_id": 99505, "outcome": "p1"}]
        )
        self.assertFalse(s, "Bet on completed match must be rejected")
        self.assertIn("завершен", res.lower())

    # -------------------------------------------------------------
    # 12. Идемпотентность кнопки 'Поставить'
    # -------------------------------------------------------------
    def test_12_idempotency_key_prevents_duplicate_bets(self):
        key = "idem-test-key-12345"
        initial_balance = database.get_user_balance(self.user1_id)

        # First request
        s1, bet_id1 = database.place_user_bet(
            user_id=self.user1_id,
            amount=100,
            selections=[{"match_id": 99501, "outcome": "p1"}],
            idempotency_key=key
        )
        self.assertTrue(s1)
        bal_after_1 = database.get_user_balance(self.user1_id)
        self.assertEqual(bal_after_1, initial_balance - 100)

        # Immediate duplicate request with same key
        s2, bet_id2 = database.place_user_bet(
            user_id=self.user1_id,
            amount=100,
            selections=[{"match_id": 99501, "outcome": "p1"}],
            idempotency_key=key
        )
        self.assertTrue(s2)
        self.assertEqual(bet_id1, bet_id2, "Idempotent retry must return the existing bet_id")
        bal_after_2 = database.get_user_balance(self.user1_id)
        self.assertEqual(bal_after_2, bal_after_1, "Balance must NOT be deducted twice")

    # -------------------------------------------------------------
    # 13. Корректность расчёта total_odds
    # -------------------------------------------------------------
    def test_13_total_odds_calculation_correctness(self):
        # Match 99501 p1 = 1.90, Match 99502 p1 = 2.00
        # Expected: 1.90 * 2.00 * 0.97 (express margin for the second leg)
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=100,
            selections=[
                {"match_id": 99501, "outcome": "p1"},
                {"match_id": 99502, "outcome": "p1"}
            ]
        )
        self.assertTrue(success)
        bet = database.get_user_bet_by_id(bet_id, self.user1_id)
        expected_odd = database.express_odd(
            [bet["items"][0]["odd"], bet["items"][1]["odd"]], bet["express_margin_pct"])
        self.assertEqual(bet["express_margin_pct"], database.EXPRESS_MARGIN_PCT)
        self.assertAlmostEqual(bet["total_odd"], expected_odd, places=2)

    # -------------------------------------------------------------
    # 14. Корректность расчёта potential_win
    # -------------------------------------------------------------
    def test_14_potential_win_calculation_correctness(self):
        stake = 100
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=stake,
            selections=[
                {"match_id": 99501, "outcome": "p1"},
                {"match_id": 99502, "outcome": "p1"}
            ]
        )
        self.assertTrue(success)
        bet = database.get_user_bet_by_id(bet_id, self.user1_id)
        expected_win = int(stake * bet["total_odd"])
        self.assertEqual(bet["potential_win"], expected_win)

    # -------------------------------------------------------------
    # 15. Корректность списания баланса
    # -------------------------------------------------------------
    def test_15_coin_transaction_recorded(self):
        stake = 120
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=stake,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertTrue(success)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT * FROM coin_transactions
                WHERE user_id = ? AND amount = ? AND transaction_type IN ('bet_placed', 'bet_placement')
            """, (self.user1_id, -stake))
            tx = c.fetchone()
            self.assertIsNotNone(tx, "Coin transaction for bet placement must be created")

    # -------------------------------------------------------------
    # 16. Зачисление выигрыша после settlement
    # -------------------------------------------------------------
    def test_16_payout_credit_after_settlement(self):
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=100,
            selections=[{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertTrue(success)
        bal_before = database.get_user_balance(self.user1_id)

        # Settle Match 99501 with score 3:0 (Arsenal wins -> P1 won)
        settlement_engine.settle_match_result(
            match_id=99501,
            score1=3,
            score2=0
        )

        bet = database.get_user_bet_by_id(bet_id, self.user1_id)
        self.assertEqual(bet["status"], "won")
        self.assertGreater(bet["actual_payout"], 0)

        bal_after = database.get_user_balance(self.user1_id)
        self.assertEqual(bal_after, bal_before + bet["actual_payout"])

    # -------------------------------------------------------------
    # 17. Возврат ставки при отмене/переносе
    # -------------------------------------------------------------
    def test_17_refund_on_cancelled_or_tech_match(self):
        success, bet_id = database.place_user_bet(
            user_id=self.user1_id,
            amount=100,
            selections=[{"match_id": 99502, "outcome": "p1"}]
        )
        self.assertTrue(success)
        bal_before = database.get_user_balance(self.user1_id)

        # Settle as cancelled/refund
        settlement_engine.refund_match_bets(99502)

        bet = database.get_user_bet_by_id(bet_id, self.user1_id)
        self.assertEqual(bet["status"], "refunded")
        bal_after = database.get_user_balance(self.user1_id)
        self.assertEqual(bal_after, bal_before + 100, "Refund must restore original stake")

    # -------------------------------------------------------------
    # 18. Получение истории ставок пользователя
    # -------------------------------------------------------------
    def test_18_get_user_bet_history(self):
        database.place_user_bet(self.user1_id, 50, [{"match_id": 99501, "outcome": "p1"}])
        database.place_user_bet(self.user1_id, 60, [{"match_id": 99502, "outcome": "x"}])

        bets = database.get_user_bets(self.user1_id)
        self.assertGreaterEqual(len(bets), 2)
        for b in bets:
            self.assertEqual(b["user_id"], self.user1_id)
            self.assertIn("items", b)

    # -------------------------------------------------------------
    # 19. Пагинация/лимиты истории ставок
    # -------------------------------------------------------------
    def test_19_bet_history_pagination_and_limits(self):
        # Place 3 bets
        for i in range(3):
            database.place_user_bet(self.user1_id, 50 + i, [{"match_id": 99501, "outcome": "p1"}])

        limited = database.get_user_bets(self.user1_id, limit=2, offset=0)
        self.assertEqual(len(limited), 2)

        offset_bets = database.get_user_bets(self.user1_id, limit=2, offset=2)
        self.assertGreaterEqual(len(offset_bets), 1)

    # -------------------------------------------------------------
    # 20. Получение деталей конкретной ставки
    # -------------------------------------------------------------
    def test_20_get_bet_detail_by_id(self):
        success, bet_id = database.place_user_bet(
            self.user1_id, 100, [{"match_id": 99501, "outcome": "p1"}]
        )
        self.assertTrue(success)
        detail = database.get_user_bet_by_id(bet_id, self.user1_id)
        self.assertEqual(detail["id"], bet_id)
        self.assertEqual(detail["user_id"], self.user1_id)
        self.assertIn("items", detail)
        self.assertEqual(detail["items"][0]["match_id"], 99501)

    # -------------------------------------------------------------
    # 21. Просмотр таблицы дивизиона через Mini App API
    # -------------------------------------------------------------
    def test_21_get_standings_via_api(self):
        standings = database.get_tournament_standings(division_id=1)
        self.assertIsInstance(standings, list)

    # -------------------------------------------------------------
    # 22. Просмотр результатов дивизиона через Mini App API
    # -------------------------------------------------------------
    def test_22_get_results_via_api(self):
        results = database.get_tournament_results(limit=10, division_id=1)
        self.assertIsInstance(results, list)

    # -------------------------------------------------------------
    # 23. Изоляция данных Division 1 и Division 2
    # -------------------------------------------------------------
    def test_23_isolation_of_divisions_data(self):
        # Matches in Div 1 vs Div 2
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM matches WHERE division_id = 1 AND id >= 99500")
            div1_matches = [r["id"] for r in c.fetchall()]
            c.execute("SELECT id FROM matches WHERE division_id = 2 AND id >= 99500")
            div2_matches = [r["id"] for r in c.fetchall()]

        self.assertIn(99501, div1_matches)
        self.assertNotIn(99501, div2_matches)
        self.assertIn(99506, div2_matches)
        self.assertNotIn(99506, div1_matches)

        # Open tours isolation
        div1_tours = database.get_open_betting_tours(division_id=1)
        div2_tours = database.get_open_betting_tours(division_id=2)

        div1_tour_nums = [t["round_number"] for t in div1_tours]
        div2_tour_nums = [t["round_number"] for t in div2_tours]

        self.assertIn(95, div1_tour_nums)
        self.assertNotIn(98, div1_tour_nums)
        self.assertIn(98, div2_tour_nums)

    # -------------------------------------------------------------
    # 24. Корректность работы фильтров матчей
    # -------------------------------------------------------------
    def test_24_match_status_filtering(self):
        with database.transaction() as conn:
            c = conn.cursor()
            # Filter scheduled/open
            c.execute("SELECT id FROM matches WHERE status = 'scheduled' AND id >= 99500")
            sched = [r["id"] for r in c.fetchall()]
            self.assertIn(99501, sched)
            self.assertNotIn(99505, sched)

            # Filter completed
            c.execute("SELECT id FROM matches WHERE status = 'completed' AND id >= 99500")
            comp = [r["id"] for r in c.fetchall()]
            self.assertIn(99505, comp)
            self.assertNotIn(99501, comp)

    # -------------------------------------------------------------
    # 25. Защита от подделки коэффициента со стороны frontend
    # -------------------------------------------------------------
    def test_25_protection_against_client_odds_manipulation(self):
        # Client maliciously sends odd = 999.0
        success, result = database.place_user_bet(
            user_id=self.user1_id,
            amount=100,
            selections=[{"match_id": 99501, "outcome": "p1", "odd": 999.0}]
        )
        # Server-authoritative: a forged odd is rejected and the real one is reported back
        self.assertFalse(success)
        self.assertEqual(result["error"], "ODDS_CHANGED")
        self.assertLess(result["new_odd"], 10.0)


class TestPhase4ApiEndpoints(AioHTTPTestCase):
    async def get_application(self):
        return create_app()

    def setUp(self):
        super().setUp()
        self.user_id = 998001
        self.init_data = make_valid_telegram_init_data(self.user_id, "p4_api_user")
        database.get_or_create_wallet(self.user_id)
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) VALUES (?, ?, ?, ?, ?)",
                (self.user_id, "p4_api_user", "API FC", 1, "player")
            )

    @unittest_run_loop
    async def test_api_wallet_endpoint(self):
        resp = await self.client.get(
            "/api/wallet",
            headers={"X-Telegram-Init-Data": self.init_data}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("balance", data["wallet"])

    @unittest_run_loop
    async def test_api_bets_alias_endpoint(self):
        resp = await self.client.get(
            "/api/bets",
            headers={"X-Telegram-Init-Data": self.init_data}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("predictions", data)

    @unittest_run_loop
    async def test_api_table_alias_endpoint(self):
        resp = await self.client.get(
            "/api/table?division_id=1",
            headers={"X-Telegram-Init-Data": self.init_data}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("standings", data)

    @unittest_run_loop
    async def test_api_results_alias_endpoint(self):
        resp = await self.client.get(
            "/api/results?division_id=1",
            headers={"X-Telegram-Init-Data": self.init_data}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("results", data)


if __name__ == "__main__":
    unittest.main()
