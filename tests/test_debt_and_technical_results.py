"""48-часовой регламент долгов: ТП / ТН, продление и правило одного варна.

Покрывает связку `database.set_technical_result` →
`services.settlement_engine.settle_match_predictions(..., "voided")` →
`handlers.admin._process_technical_verdict`:

* ТП — победитель получает 3 очка и −1 варн, игнорщик 0 очков и +1 варн
  (раньше варн снимался с ОБОИХ — это и был баг);
* ТН — по 1 очку и +1 варн каждому;
* любой технический результат = 100% возврат ставок (кэф 1.00);
* продление матча ставки НЕ трогает — они висят `pending`;
* один долг стоит игроку максимум один варн, и он выдаётся только вердиктом.
"""

import asyncio
import datetime
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


class DebtTechnicalResultsBase(unittest.TestCase):
    """Отдельная временная БД на каждый тест — вердикты меняют варны и таблицу."""

    HOME_TEAM = "Debt Home FC"
    AWAY_TEAM = "Debt Away FC"

    P1_ID = 8801001
    P2_ID = 8801002
    BETTOR_ID = 8801003
    ADMIN_ID = 8809999

    MATCH_ID = 7701
    ROUND = 3
    STAKE = 100

    def setUp(self):
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.tf.name
        self.tf.close()

        self.orig_config_path = config.DB_PATH
        self.orig_database_path = database.DB_PATH

        config.DB_PATH = self.temp_db_path
        database.DB_PATH = self.temp_db_path
        database.init_db()

        self.division_id = database.create_division(name="Debt Div", code="DEBTDIV")
        season = database.get_active_season()
        self.season_id = season["id"] if season else 1

        self.start_balance = config.INITIAL_WALLET_BALANCE

    def tearDown(self):
        config.DB_PATH = self.orig_config_path
        database.DB_PATH = self.orig_database_path
        try:
            os.remove(self.temp_db_path)
        except Exception:
            pass

    # ------------------------------------------------------------------ setup

    def _seed_players(self, p1_warns: int = 0, p2_warns: int = 0):
        database.register_user(self.P1_ID, "debt_home", team_name=self.HOME_TEAM)
        database.register_user(self.P2_ID, "debt_away", team_name=self.AWAY_TEAM)
        database.assign_user_division(self.P1_ID, self.division_id)
        database.assign_user_division(self.P2_ID, self.division_id)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET warn_count = ? WHERE telegram_id = ?", (p1_warns, self.P1_ID))
            c.execute("UPDATE users SET warn_count = ? WHERE telegram_id = ?", (p2_warns, self.P2_ID))

    def _seed_match(self, deadline: datetime.datetime | None = None):
        """Тур с открытой линией (is_open=0, bets_open=1) и pending-матч в нём."""
        dl = deadline or (datetime.datetime.now() + datetime.timedelta(days=1))
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO rounds (round_number, division_id, season_id, is_open, bets_open, deadline) "
                "VALUES (?, ?, ?, 0, 1, ?)",
                (self.ROUND, self.division_id, self.season_id, _fmt(dl)),
            )
            c.execute(
                "INSERT INTO matches (id, tournament_id, round_number, player1_id, player2_id, "
                "player1_team, player2_team, status, division_id, season_id, tournament_type) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, 'pending', ?, ?, 'league')",
                (self.MATCH_ID, self.ROUND, self.P1_ID, self.P2_ID,
                 self.HOME_TEAM, self.AWAY_TEAM, self.division_id, self.season_id),
            )
        database.save_bet_market(
            self.MATCH_ID, self.ROUND, self.HOME_TEAM, self.AWAY_TEAM,
            2.00, 3.30, 3.10, 1.80, 1.90, 1.75, 2.00
        )

    def _place_bet(self, outcome: str = "p1", odd: float = 2.00) -> int:
        """Ставка третьего лица: участник матча ставить на свой матч не может."""
        database.get_or_create_wallet(self.BETTOR_ID)
        ok, bet_id = database.place_user_bet(
            self.BETTOR_ID, self.STAKE,
            [{"match_id": self.MATCH_ID, "outcome": outcome, "odd": odd}]
        )
        self.assertTrue(ok, f"Ставка не принята: {bet_id}")
        self.assertEqual(database.get_wallet_balance(self.BETTOR_ID), self.start_balance - self.STAKE)
        return bet_id

    def _close_round(self, hours_overdue: float = 60.0):
        """Тур сыгран/закрыт для ставок, дедлайн в прошлом — матч стал долгом."""
        past = datetime.datetime.now() - datetime.timedelta(hours=hours_overdue)
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE rounds SET is_open = 1, bets_open = 0, deadline = ? "
                "WHERE round_number = ? AND division_id = ?",
                (_fmt(past), self.ROUND, self.division_id),
            )

    def _make_context(self):
        ctx = MagicMock()
        ctx.bot = AsyncMock()
        ctx.bot_data = {}
        return ctx

    # --------------------------------------------------------------- asserts

    def _match_row(self) -> dict:
        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT * FROM matches WHERE id = ?", (self.MATCH_ID,)
            ).fetchone()
        return dict(row)

    def _bet_item_statuses(self) -> list[str]:
        with database.transaction() as conn:
            rows = conn.cursor().execute(
                "SELECT status FROM bet_items WHERE match_id = ?", (self.MATCH_ID,)
            ).fetchall()
        return [r["status"] for r in rows]

    def _refund_transactions(self) -> list[dict]:
        with database.transaction() as conn:
            rows = conn.cursor().execute(
                "SELECT * FROM coin_transactions WHERE user_id = ? AND transaction_type = 'refund'",
                (self.BETTOR_ID,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _assert_bet_fully_refunded(self):
        self.assertEqual(self._bet_item_statuses(), ["refunded"])
        bets = database.get_user_bets(self.BETTOR_ID)
        self.assertEqual(len(bets), 1)
        self.assertEqual(bets[0]["status"], "refunded")
        self.assertEqual(bets[0]["actual_payout"], self.STAKE)
        # 100% возврат: баланс вернулся к исходному, без выигрыша и без потерь.
        self.assertEqual(database.get_wallet_balance(self.BETTOR_ID), self.start_balance)
        refunds = self._refund_transactions()
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["amount"], self.STAKE)

    def _standing(self, team: str) -> dict:
        table = database.get_standings(division_id=self.division_id, season_id=self.season_id)
        for row in table:
            if row["team_name"] == team:
                return row
        self.fail(f"Команда {team} не найдена в таблице дивизиона")

    def _verdict(self, verdict: str):
        from handlers.admin import _process_technical_verdict
        ctx = self._make_context()
        asyncio.run(_process_technical_verdict(ctx, self.MATCH_ID, verdict, admin_id=self.ADMIN_ID))
        return ctx


class TestTechnicalWinHome(DebtTechnicalResultsBase):
    """ТП Хозяевам 1:0."""

    def test_tp_home_points_warns_and_refund(self):
        self._seed_players(p1_warns=1, p2_warns=0)
        self._seed_match()
        self._place_bet(outcome="p1", odd=2.00)
        self._close_round()

        database.set_technical_result(self.MATCH_ID, 1, 0, "tp_home")
        self._verdict("home")

        m = self._match_row()
        self.assertEqual(m["player1_score"], 1)
        self.assertEqual(m["player2_score"], 0)
        self.assertEqual(m["status"], "confirmed")
        self.assertEqual(m["is_technical"], 1)
        self.assertEqual(m["technical_type"], "tp_home")

        # Победитель: −1 варн за закрытый долг. Игнорщик: +1 варн.
        self.assertEqual(database.get_user_warn_count(self.P1_ID), 0)
        self.assertEqual(database.get_user_warn_count(self.P2_ID), 1)

        home = self._standing(self.HOME_TEAM)
        away = self._standing(self.AWAY_TEAM)
        self.assertEqual(home["points"], 3)
        self.assertEqual(home["wins"], 1)
        self.assertEqual(away["points"], 0)
        self.assertEqual(away["losses"], 1)

        # Ставка на выигравшего хозяина всё равно возвращается: матч не играли.
        self._assert_bet_fully_refunded()

    def test_technical_goals_never_reach_scorers(self):
        self._seed_players()
        self._seed_match()
        self._close_round()
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) "
                "VALUES (?, ?, 'Ghost Striker', 'goal', 1)",
                (self.MATCH_ID, self.HOME_TEAM),
            )

        database.set_technical_result(self.MATCH_ID, 1, 0, "tp_home")

        with database.transaction() as conn:
            left = conn.cursor().execute(
                "SELECT COUNT(*) AS c FROM match_events WHERE match_id = ?", (self.MATCH_ID,)
            ).fetchone()["c"]
        self.assertEqual(left, 0)


class TestTechnicalWinAway(DebtTechnicalResultsBase):
    """ТП Гостям 0:1 — зеркало домашнего сценария."""

    def test_tp_away_points_warns_and_refund(self):
        self._seed_players(p1_warns=0, p2_warns=2)
        self._seed_match()
        self._place_bet(outcome="p1", odd=2.00)
        self._close_round()

        database.set_technical_result(self.MATCH_ID, 0, 1, "tp_away")
        self._verdict("away")

        m = self._match_row()
        self.assertEqual((m["player1_score"], m["player2_score"]), (0, 1))
        self.assertEqual(m["is_technical"], 1)
        self.assertEqual(m["technical_type"], "tp_away")

        self.assertEqual(database.get_user_warn_count(self.P2_ID), 1)  # 2 − 1
        self.assertEqual(database.get_user_warn_count(self.P1_ID), 1)  # 0 + 1

        home = self._standing(self.HOME_TEAM)
        away = self._standing(self.AWAY_TEAM)
        self.assertEqual(away["points"], 3)
        self.assertEqual(away["wins"], 1)
        self.assertEqual(home["points"], 0)
        self.assertEqual(home["losses"], 1)

        # Ставка на проигравшего по ТП хозяина не сгорает — тоже полный возврат.
        self._assert_bet_fully_refunded()


class TestTechnicalDraw(DebtTechnicalResultsBase):
    """ТН 0:0 при обоюдном молчании."""

    def test_tech_draw_one_point_each_and_warns_for_both(self):
        self._seed_players(p1_warns=0, p2_warns=0)
        self._seed_match()
        self._place_bet(outcome="p1", odd=2.00)
        self._close_round()

        database.set_technical_result(self.MATCH_ID, 0, 0, "tech_draw")
        self._verdict("draw")

        m = self._match_row()
        self.assertEqual((m["player1_score"], m["player2_score"]), (0, 0))
        self.assertEqual(m["is_technical"], 1)
        self.assertEqual(m["technical_type"], "tech_draw")

        # Молчали оба — варн получают оба, снятия варнов нет ни у кого.
        self.assertEqual(database.get_user_warn_count(self.P1_ID), 1)
        self.assertEqual(database.get_user_warn_count(self.P2_ID), 1)

        for team in (self.HOME_TEAM, self.AWAY_TEAM):
            row = self._standing(team)
            self.assertEqual(row["points"], 1, f"{team}: ТН должен давать ровно 1 очко")
            self.assertEqual(row["draws"], 1)
            self.assertEqual(row["wins"], 0)
            self.assertEqual(row["losses"], 0)

        self._assert_bet_fully_refunded()


class TestMatchExtension(DebtTechnicalResultsBase):
    """Продление долга админом: ставки продолжают висеть."""

    def test_extension_freezes_clock_and_keeps_bets_pending(self):
        self._seed_players()
        self._seed_match()
        self._place_bet(outcome="p1", odd=2.00)
        self._close_round(hours_overdue=50.0)

        # Админа уже дёрнули по сроку долга.
        database.sync_match_debts()
        self.assertTrue(database.mark_debt_stage(self.MATCH_ID, "escalated"))
        self.assertEqual(database.get_match_debt(self.MATCH_ID)["state"], "escalated")

        until = database.extend_match_deadline_by_hours(self.MATCH_ID, 24)
        self.assertIsNotNone(until)

        m = self._match_row()
        self.assertEqual(m["is_extended"], 1)
        self.assertIsNotNone(m["extended_until"])
        self.assertEqual(m["status"], "pending")

        expiry = database.get_match_extension_expiry(self.MATCH_ID)
        self.assertIsNotNone(expiry)
        delta_h = (expiry - datetime.datetime.now()).total_seconds() / 3600.0
        self.assertGreater(delta_h, 23.0)
        self.assertLess(delta_h, 25.0)

        # Эскалация сброшена: после продления админа спросят заново.
        debt = database.get_match_debt(self.MATCH_ID)
        self.assertIsNone(debt["escalated_at"])
        self.assertEqual(debt["state"], "active")

        # ГЛАВНОЕ: ставки не возвращены, они висят до реального исхода.
        self.assertEqual(self._bet_item_statuses(), ["pending"])
        bets = database.get_user_bets(self.BETTOR_ID)
        self.assertEqual(bets[0]["status"], "pending")
        self.assertEqual(database.get_wallet_balance(self.BETTOR_ID), self.start_balance - self.STAKE)
        self.assertEqual(self._refund_transactions(), [])

        # Варнов за продление никто не получает.
        self.assertEqual(database.get_user_warn_count(self.P1_ID), 0)
        self.assertEqual(database.get_user_warn_count(self.P2_ID), 0)

    def test_extension_expiry_resumes_the_debt_clock(self):
        self._seed_players()
        self._seed_match()
        self._close_round(hours_overdue=50.0)

        database.extend_match_deadline_by_hours(self.MATCH_ID, 24)
        self.assertEqual(self._match_row()["is_extended"], 1)

        database.expire_match_extension(self.MATCH_ID)
        m = self._match_row()
        self.assertEqual(m["is_extended"], 0)
        self.assertIsNone(m["extended_until"])


class TestOneWarnPerDebtRule(DebtTechnicalResultsBase):
    """Один долг — максимум один варн, и только по вердикту."""

    def test_24h_reminder_issues_no_warn(self):
        from handlers import admin as admin_handlers

        self._seed_players()
        self._seed_match()
        self._close_round(hours_overdue=30.0)

        ctx = self._make_context()
        # Первый прогон — сообщение о долге, второй — мягкое предупреждение.
        asyncio.run(admin_handlers._run_debt_lifecycle_tracker(ctx))
        asyncio.run(admin_handlers._run_debt_lifecycle_tracker(ctx))

        # 30 часов просрочки — напоминание ушло, варнов нет.
        debt = database.get_match_debt(self.MATCH_ID)
        self.assertIsNotNone(debt["last_reminder_at"])
        self.assertIsNotNone(debt["soft_warned_at"])
        self.assertEqual(database.get_user_warn_count(self.P1_ID), 0)
        self.assertEqual(database.get_user_warn_count(self.P2_ID), 0)

        # И до срока долга админа не дёргают.
        self.assertIsNone(debt["escalated_at"])
        self.assertEqual(debt["state"], "active")

        self.assertTrue(ctx.bot.send_message.await_count >= 1)

    def test_tracker_never_warns_twice_for_the_same_debt(self):
        from handlers import admin as admin_handlers

        self._seed_players()
        self._seed_match()
        self._close_round(hours_overdue=30.0)

        ctx = self._make_context()
        for _ in range(3):
            asyncio.run(admin_handlers._run_debt_lifecycle_tracker(ctx))

        self.assertEqual(database.get_user_warn_count(self.P1_ID), 0)
        self.assertEqual(database.get_user_warn_count(self.P2_ID), 0)

    def test_verdict_grants_exactly_one_warn_and_is_idempotent(self):
        self._seed_players()
        self._seed_match()
        self._close_round()

        database.set_technical_result(self.MATCH_ID, 0, 0, "tech_draw")
        self._verdict("draw")

        self.assertEqual(database.get_user_warn_count(self.P1_ID), 1)
        self.assertEqual(database.get_user_warn_count(self.P2_ID), 1)
        self.assertTrue(database.has_debt_stage(self.MATCH_ID, "verdict_processed"))

        # Повторный клик админа по той же карточке не выдаёт второй варн.
        self._verdict("draw")
        self.assertEqual(database.get_user_warn_count(self.P1_ID), 1)
        self.assertEqual(database.get_user_warn_count(self.P2_ID), 1)

    def test_tp_loser_gets_one_warn_not_two(self):
        self._seed_players()
        self._seed_match()
        self._close_round()

        database.set_technical_result(self.MATCH_ID, 1, 0, "tp_home")
        self._verdict("home")
        self._verdict("home")

        self.assertEqual(database.get_user_warn_count(self.P2_ID), 1)
        with database.transaction() as conn:
            warns = conn.cursor().execute(
                "SELECT COUNT(*) AS c FROM user_warns WHERE user_id = ? AND type = 'WARN_ADD'",
                (self.P2_ID,),
            ).fetchone()["c"]
        self.assertEqual(warns, 1)


if __name__ == "__main__":
    unittest.main()
