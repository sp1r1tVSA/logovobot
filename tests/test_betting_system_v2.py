"""
tests/test_betting_system_v2.py

Приёмочные тесты системы ставок Logovo.bet v2 (ординары + экспрессы).

1. test_top_4_matches_selection — тур из 8 матчей даёт ровно 4 рынка:
   в линию выставляются только центральные пары.
2. test_express_length_limits — 1 событие = ординар, 2..15 = экспресс,
   16-е событие отклоняется валидацией.
3. test_two_by_two_round_lifecycle — парный цикл «два через два»:
   линия стоит на Турах 1-2, после их открытия для игры уезжает на 3-4, затем на 5-6.
4. test_technical_result_voids_bets — ТП/ТН: 100% возврат по ординару,
   коэффициент 1.00 по ноге экспресса, купон при этом не сгорает.
5. test_debt_holds_bets — матч в долге не рассчитывается досрочно,
   ставки по нему остаются pending, а приём новых прогнозов закрыт.
"""

import asyncio
import unittest

import database
from services.betting_engine import (
    CENTRAL_MATCHES_PER_ROUND,
    generate_round_markets,
    select_top_round_matches,
)


DIV_ID = 1
SEASON_ID = 1

# Туры 1-6 отданы парному циклу: `_open_preseason_line` выставляет линию
# на Туры 1 и 2 по фиксированным номерам, поэтому подменить их нельзя.
LIFECYCLE_ROUNDS = (1, 2, 3, 4, 5, 6)

ROUND_TABLE_A = 11       # сыгранный тур — формирует таблицу
ROUND_TABLE_B = 12       # второй сыгранный тур
ROUND_CENTRAL = 13       # 8 матчей, в линию должны уйти 4
ROUND_EXPRESS = 21       # 16 матчей для проверки длины экспресса
ROUND_TECH = 22          # технический результат
ROUND_DEBT = 23          # долг

MATCH_ID_MIN = 99800
MATCH_ID_MAX = 99900

USER_ID_MIN = 998400
USER_ID_MAX = 998500
BETTOR_ID = 998450

FUTURE_DEADLINE = "2099-01-01 23:59"
PAST_DEADLINE = "2000-01-01 23:59"

# 16 клубов дивизиона: имена заведомо уникальны и не пересекаются с реестром КПЛ.
CLUBS = [f"Логово Тест Клуб {i:02d}" for i in range(1, 17)]
BETTOR_CLUB = "Логово Тест Зритель"


class TestBettingSystemV2(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    # ─── фикстуры ────────────────────────────────────────────────────────

    def _cleanup(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "DELETE FROM bet_items WHERE bet_id IN "
                "(SELECT id FROM user_bets WHERE user_id BETWEEN ? AND ?)",
                (USER_ID_MIN, USER_ID_MAX)
            )
            c.execute("DELETE FROM bet_items WHERE match_id BETWEEN ? AND ?", (MATCH_ID_MIN, MATCH_ID_MAX))
            c.execute("DELETE FROM user_bets WHERE user_id BETWEEN ? AND ?", (USER_ID_MIN, USER_ID_MAX))
            c.execute("DELETE FROM coin_transactions WHERE user_id BETWEEN ? AND ?", (USER_ID_MIN, USER_ID_MAX))
            c.execute("DELETE FROM user_wallets WHERE user_id BETWEEN ? AND ?", (USER_ID_MIN, USER_ID_MAX))
            c.execute(
                "DELETE FROM market_selections WHERE market_id IN "
                "(SELECT id FROM markets WHERE match_id BETWEEN ? AND ?)",
                (MATCH_ID_MIN, MATCH_ID_MAX)
            )
            c.execute("DELETE FROM markets WHERE match_id BETWEEN ? AND ?", (MATCH_ID_MIN, MATCH_ID_MAX))
            c.execute("DELETE FROM bet_markets WHERE match_id BETWEEN ? AND ?", (MATCH_ID_MIN, MATCH_ID_MAX))
            c.execute("DELETE FROM matches WHERE id BETWEEN ? AND ?", (MATCH_ID_MIN, MATCH_ID_MAX))
            c.execute("DELETE FROM rounds WHERE round_number BETWEEN 1 AND 30 AND division_id = ?", (DIV_ID,))
            c.execute("DELETE FROM users WHERE telegram_id BETWEEN ? AND ?", (USER_ID_MIN, USER_ID_MAX))

    def _add_clubs(self):
        """16 клубов дивизиона — из них строится турнирная таблица."""
        with database.transaction() as conn:
            c = conn.cursor()
            for idx, club in enumerate(CLUBS, start=1):
                c.execute(
                    "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) "
                    "VALUES (?, ?, ?, ?, 'player')",
                    (USER_ID_MIN + idx, f"club_owner_{idx:02d}", club, DIV_ID)
                )

    def _add_bettor(self, coins: int = 10_000) -> int:
        """Игрок, который ставит: его клуб не участвует ни в одном тестовом матче."""
        with database.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, division_id, role) "
                "VALUES (?, ?, ?, ?, 'player')",
                (BETTOR_ID, "v2_bettor", BETTOR_CLUB, DIV_ID)
            )
        database.get_or_create_wallet(BETTOR_ID)
        if coins:
            database.add_coins(BETTOR_ID, coins, "test_deposit")
        return database.get_wallet_balance(BETTOR_ID)

    def _add_round(self, round_number: int, *, is_open: int = 0, bets_open: int = 0,
                   deadline: str | None = None):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO rounds (round_number, is_open, bets_open, deadline, division_id, season_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (round_number, is_open, bets_open, deadline, DIV_ID, SEASON_ID)
            )

    def _add_match(self, match_id: int, round_number: int, team1: str, team2: str,
                   status: str = "pending", score1: int | None = None, score2: int | None = None):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO matches (id, round_number, division_id, season_id, "
                "player1_id, player2_id, player1_team, player2_team, player1_score, player2_score, status) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)",
                (match_id, round_number, DIV_ID, SEASON_ID, team1, team2, score1, score2, status)
            )

    def _add_market(self, match_id: int, round_number: int, team1: str, team2: str,
                    odd_p1: float = 1.80, odd_x: float = 3.40, odd_p2: float = 3.20):
        database.save_bet_market(
            match_id, round_number, team1, team2,
            odd_p1, odd_x, odd_p2, 1.75, 1.95, 1.70, 2.05
        )

    def _bet_status(self, bet_id: int) -> dict:
        with database.transaction() as conn:
            row = conn.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,)).fetchone()
        return dict(row)

    def _bet_items(self, bet_id: int) -> dict[int, dict]:
        with database.transaction() as conn:
            rows = conn.execute("SELECT * FROM bet_items WHERE bet_id = ?", (bet_id,)).fetchall()
        return {r["match_id"]: dict(r) for r in rows}

    # ─── 1. Четыре центральных матча тура ────────────────────────────────

    def test_top_4_matches_selection(self):
        """Тур из 8 матчей → ровно 4 рынка: в линию идут только центральные пары."""
        self._add_clubs()

        # Два сыгранных тура: клубы с нечётным номером выигрывают оба раза (6 очков),
        # клубы с чётным номером остаются на нуле. Таблица становится «настоящей»,
        # и отбор переключается с базовой силы на формулу очков и мест.
        match_id = MATCH_ID_MIN + 11
        for rnd in (ROUND_TABLE_A, ROUND_TABLE_B):
            for i in range(0, 16, 2):
                self._add_match(
                    match_id, rnd, CLUBS[i], CLUBS[i + 1],
                    status="confirmed", score1=1, score2=0
                )
                match_id += 1

        standings = database.get_standings(division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(max(r["played"] for r in standings), 2, "Таблица должна знать о двух сыгранных турах")

        # Тур 13: сначала четыре пары аутсайдеров (меньшие id), затем четыре пары
        # лидеров. Порядок намеренно обратный — если отбор перестанет считать
        # статусность, он возьмёт первые четыре по id и тест это поймает.
        weak_ids = [MATCH_ID_MIN + 31 + i for i in range(4)]
        top_ids = [MATCH_ID_MIN + 35 + i for i in range(4)]
        for n, m_id in enumerate(weak_ids):
            self._add_match(m_id, ROUND_CENTRAL, CLUBS[1 + 4 * n], CLUBS[3 + 4 * n])
        for n, m_id in enumerate(top_ids):
            self._add_match(m_id, ROUND_CENTRAL, CLUBS[0 + 4 * n], CLUBS[2 + 4 * n])

        all_round_matches = database.get_matches_by_round(ROUND_CENTRAL, division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(len(all_round_matches), 8, "В туре должно быть 8 матчей")

        selected = select_top_round_matches(ROUND_CENTRAL, division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(len(selected), CENTRAL_MATCHES_PER_ROUND)
        self.assertEqual(
            sorted(m["id"] for m in selected), sorted(top_ids),
            "В линию должны попасть матчи лидеров, а не первые четыре по id"
        )

        # Линия тура: котировки выставляются ровно на четыре центральных матча.
        self._add_round(ROUND_CENTRAL)
        self.assertTrue(database.set_round_bets_open(ROUND_CENTRAL, True, division_id=DIV_ID, season_id=SEASON_ID))

        markets = database.get_active_bet_markets(ROUND_CENTRAL, division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(len(markets), CENTRAL_MATCHES_PER_ROUND, "В линии тура ровно 4 матча")
        self.assertEqual(sorted(m["match_id"] for m in markets), sorted(top_ids))

        # Повторная генерация идемпотентна: линия не разрастается.
        regenerated = generate_round_markets(ROUND_CENTRAL, division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(len(regenerated), CENTRAL_MATCHES_PER_ROUND)
        markets_again = database.get_active_bet_markets(ROUND_CENTRAL, division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(len(markets_again), CENTRAL_MATCHES_PER_ROUND)

        # И каждый рынок несёт все семь исходов линии.
        for m in markets_again:
            for col in ("odd_p1", "odd_x", "odd_p2", "odd_tb25", "odd_tm25", "odd_btts_yes", "odd_btts_no"):
                self.assertGreater(m[col], 1.0, f"Коэффициент {col} должен быть больше 1.00")

    # ─── 2. Длина купона: ординар и экспресс 2..15 ───────────────────────

    def test_express_length_limits(self):
        """1 событие — ординар, 2..15 — экспресс, 16-е событие не принимается."""
        self.assertEqual(database.MAX_EXPRESS_EVENTS, 15)
        self._add_round(ROUND_EXPRESS, bets_open=1, deadline=FUTURE_DEADLINE)
        # На одно событие больше потолка. Кэф 1.20: экспресс из 15 событий
        # даёт ~15.4 и при ставке 100 укладывается в потолок выплаты 10 000.
        match_ids = [MATCH_ID_MIN + 70 + i for i in range(database.MAX_EXPRESS_EVENTS + 1)]
        for n, m_id in enumerate(match_ids):
            t1, t2 = f"Логово Лимит {2 * n + 1}", f"Логово Лимит {2 * n + 2}"
            self._add_match(m_id, ROUND_EXPRESS, t1, t2)
            self._add_market(m_id, ROUND_EXPRESS, t1, t2, odd_p1=1.20)

        self._add_bettor()

        def coupon(size: int) -> list[dict]:
            return [{"match_id": m_id, "outcome": "p1"} for m_id in match_ids[:size]]

        # 1 событие — ординар.
        ok, bet_id = database.place_user_bet(BETTOR_ID, 100, coupon(1))
        self.assertTrue(ok, f"Ординар должен приниматься, получено: {bet_id}")
        self.assertEqual(self._bet_status(bet_id)["bet_type"], "single")

        # Границы экспресса — 2 и 15 событий. Все длины подряд не перебираем:
        # 14 купонов упёрлись бы в лимит открытых купонов (12).
        for size in (database.MIN_EXPRESS_EVENTS, database.MAX_EXPRESS_EVENTS):
            ok, bet_id = database.place_user_bet(BETTOR_ID, 100, coupon(size))
            self.assertTrue(ok, f"Экспресс из {size} событий должен приниматься, получено: {bet_id}")
            bet = self._bet_status(bet_id)
            self.assertEqual(bet["bet_type"], "express", f"Купон из {size} событий — это экспресс")
            self.assertEqual(len(self._bet_items(bet_id)), size)

        # 16 событий — отказ валидации, монеты не списываются.
        balance_before = database.get_wallet_balance(BETTOR_ID)
        ok, err = database.place_user_bet(BETTOR_ID, 100, coupon(database.MAX_EXPRESS_EVENTS + 1))
        self.assertFalse(ok, "Шестнадцатое событие в экспрессе принимать нельзя")
        self.assertIsInstance(err, dict)
        self.assertEqual(err["error"], "MAX_EXPRESS_EVENTS_EXCEEDED")
        self.assertEqual(err["max_events"], database.MAX_EXPRESS_EVENTS)
        self.assertEqual(database.get_wallet_balance(BETTOR_ID), balance_before)

    # ─── 3. Парный цикл линий «два через два» ────────────────────────────

    def test_two_by_two_round_lifecycle(self):
        """Линия стоит на двух турах и после их открытия уезжает на следующие два."""
        from handlers.admin import _open_preseason_line

        match_id = MATCH_ID_MIN + 1
        for rnd in LIFECYCLE_ROUNDS:
            self._add_round(rnd)
            for i in range(2):
                t1, t2 = f"Логово Цикл {rnd}-{2 * i + 1}", f"Логово Цикл {rnd}-{2 * i + 2}"
                self._add_match(match_id, rnd, t1, t2)
                match_id += 1

        def state(rnd: int) -> tuple[int, int]:
            info = database.get_round_info(rnd, division_id=DIV_ID, season_id=SEASON_ID)
            self.assertIsNotNone(info, f"Тур {rnd} должен существовать")
            return info["is_open"], info["bets_open"]

        # Шаг 1: расписание сгенерировано — линия автоматически встаёт на Туры 1 и 2.
        opened = asyncio.run(_open_preseason_line(DIV_ID, SEASON_ID))
        self.assertEqual(opened, [1, 2])
        for rnd in (1, 2):
            self.assertEqual(state(rnd), (0, 1), f"Тур {rnd}: линия открыта, для игры тур закрыт")
        for rnd in (3, 4, 5, 6):
            self.assertEqual(state(rnd), (0, 0), f"Тур {rnd} пока вне линии")

        # Шаг 2: Туры 1-2 открыты для игры — их линия закрывается, линия уходит на 3-4.
        report = database.open_rounds_batch(1, 2, FUTURE_DEADLINE, division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(report["opened"], [1, 2])
        for rnd in (1, 2):
            self.assertEqual(state(rnd), (1, 0), f"Тур {rnd}: открыт для игры, приём прогнозов закрыт")
        for rnd in (3, 4):
            self.assertEqual(state(rnd), (0, 1), f"Тур {rnd}: линия открыта автоматически")
        for rnd in (5, 6):
            self.assertEqual(state(rnd), (0, 0), f"Тур {rnd} ещё вне линии")

        # Шаг 3: цикл повторяется — Туры 3-4 в игре, линия на 5-6. Но сначала
        # должен истечь дедлайн Туров 1-2: одновременно активных туров в
        # дивизионе не больше MAX_OPEN_ROUNDS_PER_DIVISION, и слот освобождает
        # именно дедлайн, а не ручное закрытие (см. test_max_active_rounds_limit).
        with database.transaction() as conn:
            conn.execute(
                "UPDATE rounds SET deadline = ? WHERE division_id = ? AND season_id = ? "
                "AND round_number IN (1, 2)",
                (PAST_DEADLINE, DIV_ID, SEASON_ID)
            )

        report = database.open_rounds_batch(3, 4, FUTURE_DEADLINE, division_id=DIV_ID, season_id=SEASON_ID)
        self.assertEqual(report["opened"], [3, 4])
        for rnd in (3, 4):
            self.assertEqual(state(rnd), (1, 0))
        for rnd in (5, 6):
            self.assertEqual(state(rnd), (0, 1))

        # Инвариант: состояние is_open = 1 AND bets_open = 1 недостижимо.
        with database.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM rounds WHERE is_open = 1 AND COALESCE(bets_open, 0) = 1 "
                "AND division_id = ? AND season_id = ?",
                (DIV_ID, SEASON_ID)
            ).fetchone()
        self.assertEqual(row["n"], 0, "Открытый для игры тур не может принимать прогнозы")

        # Открытый для игры тур ставок не принимает даже принудительно.
        self.assertFalse(database.set_round_bets_open(1, True, division_id=DIV_ID, season_id=SEASON_ID))
        self.assertEqual(state(1), (1, 0))

    # ─── 4. Технический результат (ТП / ТН) ──────────────────────────────

    def test_technical_result_voids_bets(self):
        """ТП 1:0 — 100% возврат по ординару и коэффициент 1.00 по ноге экспресса."""
        self._add_round(ROUND_TECH, bets_open=1, deadline=FUTURE_DEADLINE)
        tech_id, partner_id = MATCH_ID_MIN + 51, MATCH_ID_MIN + 52
        self._add_match(tech_id, ROUND_TECH, "Логово ТП Хозяева", "Логово ТП Гости")
        self._add_match(partner_id, ROUND_TECH, "Логово Нога Один", "Логово Нога Два")
        self._add_market(tech_id, ROUND_TECH, "Логово ТП Хозяева", "Логово ТП Гости", odd_p1=2.00)
        self._add_market(partner_id, ROUND_TECH, "Логово Нога Один", "Логово Нога Два", odd_p1=1.50)

        self._add_bettor()

        ok, single_id = database.place_user_bet(BETTOR_ID, 100, [{"match_id": tech_id, "outcome": "p1"}])
        self.assertTrue(ok, f"Ординар не принят: {single_id}")

        ok, express_id = database.place_user_bet(BETTOR_ID, 200, [
            {"match_id": tech_id, "outcome": "p1"},
            {"match_id": partner_id, "outcome": "p1"},
        ])
        self.assertTrue(ok, f"Экспресс не принят: {express_id}")
        self.assertEqual(self._bet_status(express_id)["total_odd"], 3.00)

        balance_after_bets = database.get_wallet_balance(BETTOR_ID)

        # Админ ставит ТП в пользу хозяев.
        database.set_technical_result(tech_id, 1, 0, "tp_home")

        with database.transaction() as conn:
            match_row = dict(conn.execute("SELECT * FROM matches WHERE id = ?", (tech_id,)).fetchone())
        self.assertEqual(match_row["is_technical"], 1)
        self.assertEqual(match_row["technical_type"], "tp_home")
        self.assertEqual(match_row["status"], "confirmed")

        # Ординар: спортивной выплаты нет, ставка возвращается целиком.
        single = self._bet_status(single_id)
        self.assertEqual(single["status"], "refunded")
        self.assertEqual(single["actual_payout"], 100)
        self.assertEqual(self._bet_items(single_id)[tech_id]["status"], "refunded")

        with database.transaction() as conn:
            refund = conn.execute(
                "SELECT amount FROM coin_transactions WHERE user_id = ? AND transaction_type = 'refund' "
                "AND reference_id = ?",
                (BETTOR_ID, single_id)
            ).fetchone()
        self.assertIsNotNone(refund, "Возврат по ординару должен быть проведён по кошельку")
        self.assertEqual(refund["amount"], 100, "Возврат — ровно 100% ставки")
        self.assertGreaterEqual(database.get_wallet_balance(BETTOR_ID), balance_after_bets + 100)

        # Экспресс: нога погашена, но купон жив и ждёт второй матч.
        express = self._bet_status(express_id)
        self.assertEqual(express["status"], "pending", "Технический результат не должен гасить экспресс")
        self.assertEqual(self._bet_items(express_id)[tech_id]["status"], "refunded")
        self.assertEqual(self._bet_items(express_id)[partner_id]["status"], "pending")

        # Вторая нога заходит — экспресс считается по коэффициенту 1.00 за ТП.
        database.settle_match_bets(partner_id, 2, 0)

        express = self._bet_status(express_id)
        self.assertEqual(express["status"], "won")
        self.assertEqual(
            express["actual_payout"], 300,
            "Нога с ТП идёт по 1.00: 200 × 1.50 = 300, а не 200 × 3.00 = 600"
        )

    # ─── 5. Долг: ставки замораживаются, а не рассчитываются ─────────────

    def test_debt_holds_bets(self):
        """Матч в долге не рассчитывается досрочно: ставки остаются pending."""
        self._add_round(ROUND_DEBT, bets_open=1, deadline=FUTURE_DEADLINE)
        debt_id = MATCH_ID_MIN + 61
        self._add_match(debt_id, ROUND_DEBT, "Логово Должник", "Логово Кредитор")
        self._add_market(debt_id, ROUND_DEBT, "Логово Должник", "Логово Кредитор")

        self._add_bettor()
        ok, bet_id = database.place_user_bet(BETTOR_ID, 100, [{"match_id": debt_id, "outcome": "p1"}])
        self.assertTrue(ok, f"Ставка не принята: {bet_id}")
        balance_after_bet = database.get_wallet_balance(BETTOR_ID)

        # Дедлайн прошёл, матч не сыгран — это и есть долг.
        with database.transaction() as conn:
            conn.execute(
                "UPDATE rounds SET deadline = ? WHERE round_number = ? AND division_id = ? AND season_id = ?",
                (PAST_DEADLINE, ROUND_DEBT, DIV_ID, SEASON_ID)
            )

        overdue = {m["id"] for m in database.get_detailed_overdue_matches(division_id=DIV_ID, season_id=SEASON_ID)}
        self.assertIn(debt_id, overdue, "Просроченный матч должен попасть в долги")

        # Самолечащий расчёт не трогает долги: матч не сыгран, счёта нет.
        payouts = database.settle_all_pending_finished_matches()
        self.assertEqual([p for p in payouts if p.get("bet_id") == bet_id], [])

        self.assertEqual(self._bet_status(bet_id)["status"], "pending")
        self.assertEqual(self._bet_items(bet_id)[debt_id]["status"], "pending")
        self.assertEqual(database.get_wallet_balance(BETTOR_ID), balance_after_bet)

        # Продление дедлайна тоже ничего не рассчитывает — ставки просто ждут.
        self.assertEqual(database.extend_match_deadline(debt_id), 1)
        database.settle_all_pending_finished_matches()
        self.assertEqual(self._bet_status(bet_id)["status"], "pending")

        # Приём новых прогнозов при этом закрыт: дедлайн истёк.
        ok, err = database.place_user_bet(BETTOR_ID, 100, [{"match_id": debt_id, "outcome": "p2"}])
        self.assertFalse(ok, "После дедлайна приём прогнозов должен быть закрыт")

        # Матч доигран в срок продления — обычный спортивный расчёт.
        database.settle_match_bets(debt_id, 2, 0)
        settled = self._bet_status(bet_id)
        self.assertEqual(settled["status"], "won")
        self.assertEqual(settled["actual_payout"], int(100 * settled["total_odd"]))


if __name__ == "__main__":
    unittest.main()
