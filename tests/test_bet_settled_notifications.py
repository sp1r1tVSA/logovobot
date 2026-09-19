"""
tests/test_bet_settled_notifications.py

Telegram notices for won/refunded bets. Settlement enqueues a BET_SETTLED row in
`notification_events` inside the payout transaction, and
`process_notification_queue_job` delivers it even with SMART_NOTIFICATIONS_ENABLED
off (the other smart notifications stay behind the flag).
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import database
import services.settlement_engine as settlement_engine
from services.background_sync import process_notification_queue_job

USER_ID = 997701
MATCH_ID = 997801
MATCH_ID_2 = 997802


def _events(user_id=USER_ID):
    with database.transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM notification_events WHERE user_id = ? AND event_type = 'BET_SETTLED' ORDER BY id",
            (user_id,),
        )
        return [dict(r) for r in cur.fetchall()]


class TestBetSettledNotifications(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self._orig_flag = getattr(config, "SMART_NOTIFICATIONS_ENABLED", False)
        config.SMART_NOTIFICATIONS_ENABLED = False
        self._cleanup()
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO users (telegram_id, username, team_name) VALUES (?, 'notice_user', NULL)",
                (USER_ID,),
            )
            for mid, t1, t2 in ((MATCH_ID, "Арсенал", "Челси"), (MATCH_ID_2, "Ливерпуль", "Эвертон")):
                conn.execute(
                    "INSERT INTO matches (id, tournament_id, round_number, player1_team, player2_team, status) "
                    "VALUES (?, 1, 1, ?, ?, 'scheduled')",
                    (mid, t1, t2),
                )
        database.get_or_create_wallet(USER_ID)

    def tearDown(self):
        self._cleanup()
        config.SMART_NOTIFICATIONS_ENABLED = self._orig_flag

    def _cleanup(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM notification_events WHERE user_id = ?", (USER_ID,))
            conn.execute("DELETE FROM user_notification_settings WHERE user_id = ?", (USER_ID,))
            conn.execute("DELETE FROM bet_items WHERE match_id IN (?, ?)", (MATCH_ID, MATCH_ID_2))
            conn.execute("DELETE FROM user_bets WHERE user_id = ?", (USER_ID,))
            conn.execute("DELETE FROM coin_transactions WHERE user_id = ?", (USER_ID,))
            conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (USER_ID,))
            conn.execute("DELETE FROM matches WHERE id IN (?, ?)", (MATCH_ID, MATCH_ID_2))
            conn.execute("DELETE FROM users WHERE telegram_id = ?", (USER_ID,))

    def _place(self, legs, bet_type="single", amount=200):
        total = 1.0
        for _, _, odd in legs:
            total *= odd
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO user_bets (user_id, bet_type, amount, total_odd, potential_win, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (USER_ID, bet_type, amount, round(total, 2), int(amount * total)),
            )
            bet_id = cur.lastrowid
            for match_id, outcome, odd in legs:
                cur.execute(
                    "INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status) VALUES (?, ?, ?, ?, 'pending')",
                    (bet_id, match_id, outcome, odd),
                )
        return bet_id

    def _run_job(self):
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        asyncio.run(process_notification_queue_job(ctx))
        return ctx.bot.send_message

    def test_won_bet_enqueues_one_notice_and_resettle_is_idempotent(self):
        bet_id = self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")

        events = _events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source_event_id"], f"bet_{bet_id}")
        self.assertEqual(events[0]["status"], "pending")
        self.assertIn(f"#{bet_id}", events[0]["title"])
        self.assertIn("Ординар", events[0]["title"])
        self.assertIn("+500", events[0]["body"])

    def test_won_notice_details_pick_score_stake_profit_and_balance(self):
        self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")
        body = _events()[0]["body"]
        balance = database.get_or_create_wallet(USER_ID)["balance"]

        self.assertIn("<b>Арсенал 2:0 Челси</b>", body)
        self.assertIn("✅ Победит Арсенал (П1) · @2.50", body)
        self.assertIn("Ставка: <b>200 🪙</b> × 2.50", body)
        self.assertIn("Выигрыш: <b>+500 🪙</b>", body)
        self.assertIn("Чистая прибыль: <b>+300 🪙</b>", body)
        self.assertIn(f"Баланс: <b>{settlement_engine._coins(balance)} 🪙</b>", body)

    def test_express_notice_lists_every_leg(self):
        self._place([(MATCH_ID, "p1", 2.0), (MATCH_ID_2, "p2", 1.5)], bet_type="express", amount=100)
        settlement_engine.settle_match_predictions(MATCH_ID, 1, 0, "finished")
        settlement_engine.settle_match_predictions(MATCH_ID_2, 0, 3, "finished")
        body = _events()[0]["body"]
        self.assertIn("Арсенал 1:0 Челси", body)
        self.assertIn("Ливерпуль 0:3 Эвертон", body)
        self.assertIn("Победит Эвертон (П2) · @1.50", body)
        self.assertIn("Чистая прибыль: <b>+200 🪙</b>", body)

    def test_team_names_are_html_escaped(self):
        with database.transaction() as conn:
            conn.execute("UPDATE matches SET player1_team = 'A<b>&' WHERE id = ?", (MATCH_ID,))
        self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")
        body = _events()[0]["body"]
        self.assertIn("A&lt;b&gt;&amp; 2:0 Челси", body)
        self.assertNotIn("A<b>&", body)

    def test_refund_notice_shows_balance(self):
        self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 3, 0, "voided")
        body = _events()[0]["body"]
        balance = database.get_or_create_wallet(USER_ID)["balance"]
        self.assertIn("Арсенал", body)
        self.assertIn(f"Баланс: <b>{settlement_engine._coins(balance)} 🪙</b>", body)

    def test_lost_bet_is_silent(self):
        self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 0, 1, "finished")
        self.assertEqual(_events(), [])

    def test_express_notifies_only_when_the_last_leg_settles(self):
        bet_id = self._place([(MATCH_ID, "p1", 2.0), (MATCH_ID_2, "p2", 1.5)], bet_type="express", amount=100)
        settlement_engine.settle_match_predictions(MATCH_ID, 1, 0, "finished")
        self.assertEqual(_events(), [])
        settlement_engine.settle_match_predictions(MATCH_ID_2, 0, 3, "finished")
        events = _events()
        self.assertEqual(len(events), 1)
        self.assertIn("Экспресс", events[0]["title"])
        self.assertIn("+300", events[0]["body"])
        self.assertIn(f"#{bet_id}", events[0]["title"])

    def test_voided_match_enqueues_refund_notice(self):
        bet_id = self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 3, 0, "voided")
        events = _events()
        self.assertEqual(len(events), 1)
        self.assertIn("Возврат", events[0]["title"])
        self.assertIn(f"#{bet_id}", events[0]["title"])

    def test_score_correction_notifies_about_the_new_outcome(self):
        self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")
        settlement_engine.resettle_match_predictions(MATCH_ID, 0, 2, "finished")
        # Same corrected score again: nothing changed, nothing to tell.
        settlement_engine.resettle_match_predictions(MATCH_ID, 0, 2, "finished")
        events = _events()
        self.assertEqual(len(events), 2)
        self.assertIn("проигрыш", events[1]["title"])
        self.assertIn("500", events[1]["body"])

    def test_opted_out_user_gets_no_notice_but_still_gets_paid(self):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO user_notification_settings (user_id, notification_type, is_enabled) "
                "VALUES (?, 'BET_SETTLED', 0)",
                (USER_ID,),
            )
        self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")
        self.assertEqual(_events(), [])
        self.assertEqual(database.get_or_create_wallet(USER_ID)["total_won"], 500)

    def test_bettor_without_users_row_does_not_break_settlement(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM users WHERE telegram_id = ?", (USER_ID,))
        bet_id = self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")
        with database.transaction() as conn:
            row = conn.execute("SELECT status FROM user_bets WHERE id = ?", (bet_id,)).fetchone()
        self.assertEqual(row["status"], "won")
        self.assertEqual(_events(), [])

    def test_queue_job_delivers_bet_notices_with_smart_flag_off(self):
        self._place([(MATCH_ID, "p1", 2.5)])
        settlement_engine.settle_match_predictions(MATCH_ID, 2, 0, "finished")
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO notification_events (user_id, event_type, source_event_id, title, body) "
                "VALUES (?, 'GOAL', 'goal_notice_test', 'Гол!', 'x')",
                (USER_ID,),
            )

        send = self._run_job()

        self.assertEqual(send.await_count, 1)
        kwargs = send.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], USER_ID)
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("сыграла", kwargs["text"])
        self.assertEqual(_events()[0]["status"], "sent")
        with database.transaction() as conn:
            goal = conn.execute(
                "SELECT status FROM notification_events WHERE source_event_id = 'goal_notice_test'"
            ).fetchone()
        self.assertEqual(goal["status"], "pending")

        # Already sent: a second run sends nothing.
        self.assertEqual(self._run_job().await_count, 0)


if __name__ == "__main__":
    unittest.main()
