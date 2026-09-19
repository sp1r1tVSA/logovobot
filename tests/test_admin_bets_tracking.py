"""
tests/test_admin_bets_tracking.py

Тесты команды мониторинга и отслеживания ставок для супер-администратора:
1. Ограничение чата: строго ЛС (private chat only).
2. Ограничение прав: строго супер-админ (is_global_admin).
3. Функции базы данных: get_all_bets, get_bets_summary_stats, get_bet_by_id.
4. Фильтрация и пагинация.
5. Аннулирование ставки (Void / возврат средств).
6. Live-оповещения в ЛС о новых ставках.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers.admin_bets import (
    cmd_admin_bets,
    cb_admin_bets_navigate,
    cb_admin_bets_toggle_alerts,
    cb_admin_bet_detail,
    cb_admin_bet_void_ask,
    cb_admin_bet_void_execute,
    notify_super_admins_new_bet,
)
from handlers.base import is_global_admin


class TestAdminBetsTracking(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_admin_bets.db")
        database.DB_PATH = self.db_path
        database.close_thread_connection()
        database.init_db()

        self.super_id = 991001
        self.div_admin_id = 991002
        self.player1_id = 991003
        self.player2_id = 991004
        self.bettor1_id = 991005
        self.bettor2_id = 991006

        # Создаем дивизион
        self.div_id = database.create_division(name="Премьер-Лига", code="PL")

        # Создаем пользователей
        database.register_user(self.super_id, "super_boss", role="admin")
        database.register_user(self.div_admin_id, "div_chief", role="division_admin")
        database.assign_user_division(self.div_admin_id, self.div_id)
        database.add_division_admin(self.div_id, self.div_admin_id)

        # Игроки матча
        database.register_user(self.player1_id, "player_one", role="player", team_name="Реал Мадрид")
        database.assign_user_division(self.player1_id, self.div_id)
        database.register_user(self.player2_id, "player_two", role="player", team_name="Барселона")
        database.assign_user_division(self.player2_id, self.div_id)

        # Сторонние бетторы (не играют в этом матче, 322-защита их пропускает)
        database.register_user(self.bettor1_id, "bettor_one", role="player", team_name="Манчестер Сити")
        database.assign_user_division(self.bettor1_id, self.div_id)
        database.get_or_create_wallet(self.bettor1_id)
        database.add_coins(self.bettor1_id, 10000)
        self.b1_initial_bal = database.get_wallet_balance(self.bettor1_id)

        database.register_user(self.bettor2_id, "bettor_two", role="player", team_name="Арсенал")
        database.assign_user_division(self.bettor2_id, self.div_id)
        database.get_or_create_wallet(self.bettor2_id)
        database.add_coins(self.bettor2_id, 10000)
        self.b2_initial_bal = database.get_wallet_balance(self.bettor2_id)

        # Создаем тур и матчи
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO rounds (round_number, division_id, season_id, is_open, bets_open)
                VALUES (1, ?, 1, 0, 1)
            """, (self.div_id,))
            cur.execute("""
                INSERT INTO matches (tournament_id, round_number, division_id, player1_id, player2_id, player1_team, player2_team, status)
                VALUES (1, 1, ?, ?, ?, 'Реал Мадрид', 'Барселона', 'scheduled')
            """, (self.div_id, self.player1_id, self.player2_id))
            self.match_id = cur.lastrowid

            # Создаем рынок для матча
            cur.execute("""
                INSERT INTO markets (match_id, market_key, market_name, status)
                VALUES (?, '1x2', 'Основной исход', 'open')
            """, (self.match_id,))
            self.market_id = cur.lastrowid

            cur.execute("""
                INSERT INTO market_selections (market_id, selection_key, selection_name, odds_value, status)
                VALUES (?, 'p1', 'П1', 2.10, 'active'),
                       (?, 'draw', 'Ничья', 3.40, 'active'),
                       (?, 'p2', 'П2', 3.10, 'active')
            """, (self.market_id, self.market_id, self.market_id))

            # Также создаем bet_markets для совместимости
            cur.execute("""
                INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, is_active)
                VALUES (?, 1, 'Реал Мадрид', 'Барселона', 2.10, 3.40, 3.10, 1)
            """, (self.match_id,))

    async def asyncTearDown(self):
        database.close_thread_connection()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _build_update(self, user_id: int, chat_type: str = "private", callback_data: str | None = None, text: str = "/admin_bets"):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id

        update.effective_chat = MagicMock()
        update.effective_chat.type = chat_type
        update.effective_chat.id = user_id if chat_type == "private" else -1001234567

        update.effective_message = MagicMock()
        update.effective_message.text = text
        update.effective_message.reply_text = AsyncMock()

        if callback_data:
            query = MagicMock()
            query.from_user.id = user_id
            query.data = callback_data
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            update.callback_query = query
        else:
            update.callback_query = None

        return update

    # ──────────────────────────────────────────────────────────────────────────
    # 1. ТЕСТЫ ИЗОЛЯЦИИ И RBAC
    # ──────────────────────────────────────────────────────────────────────────

    async def test_cmd_admin_bets_rejected_in_group_chat(self):
        """Команда /admin_bets в групповом чате блокируется и предлагает перейти в ЛС."""
        update = self._build_update(self.super_id, chat_type="group")
        context = MagicMock()
        context.args = []
        context.bot.username = "test_bot"

        await cmd_admin_bets(update, context)

        update.effective_message.reply_text.assert_called_once()
        args, kwargs = update.effective_message.reply_text.call_args
        self.assertIn("только в личных сообщениях", args[0])

    async def test_cmd_admin_bets_rejected_for_regular_player(self):
        """Обычный игрок не может вызвать /admin_bets даже в ЛС."""
        update = self._build_update(self.bettor1_id, chat_type="private")
        context = MagicMock()
        context.args = []

        await cmd_admin_bets(update, context)

        update.effective_message.reply_text.assert_called_once()
        args, _ = update.effective_message.reply_text.call_args
        self.assertIn("Доступ запрещён", args[0])

    async def test_cmd_admin_bets_rejected_for_division_admin(self):
        """Админ дивизиона не имеет доступа к глобальному мониторингу ставок."""
        update = self._build_update(self.div_admin_id, chat_type="private")
        context = MagicMock()
        context.args = []

        await cmd_admin_bets(update, context)

        update.effective_message.reply_text.assert_called_once()
        args, _ = update.effective_message.reply_text.call_args
        self.assertIn("Доступ запрещён", args[0])

    async def test_cmd_admin_bets_allowed_for_super_admin(self):
        """Супер-админ в ЛС получает сводку мониторинга ставок."""
        update = self._build_update(self.super_id, chat_type="private")
        context = MagicMock()
        context.args = []

        await cmd_admin_bets(update, context)

        update.effective_message.reply_text.assert_called_once()
        args, kwargs = update.effective_message.reply_text.call_args
        self.assertIn("МОНИТОРИНГ СТАВОК", args[0])
        self.assertIn("Сводка по ставкам", args[0])
        self.assertIsNotNone(kwargs.get("reply_markup"))

    # ──────────────────────────────────────────────────────────────────────────
    # 2. ТЕСТЫ БАЗЫ ДАННЫХ И СВОДКИ СТАВОК
    # ──────────────────────────────────────────────────────────────────────────

    async def test_get_all_bets_and_summary_stats(self):
        """Проверка выборки всех ставок и подсчета финансовых KPI."""
        # Размещаем ставку 1 (беттор 1, 500 монет на П1)
        slip1 = [{"match_id": self.match_id, "outcome": "p1"}]
        ok1, bet_id_1 = database.place_user_bet(self.bettor1_id, 500, slip1)
        self.assertTrue(ok1, f"Bet 1 failed: {bet_id_1}")

        # Размещаем ставку 2 (беттор 2, 1000 монет на Ничью)
        slip2 = [{"match_id": self.match_id, "outcome": "draw"}]
        ok2, bet_id_2 = database.place_user_bet(self.bettor2_id, 1000, slip2)
        self.assertTrue(ok2, f"Bet 2 failed: {bet_id_2}")

        # Проверяем сводку
        stats = database.get_bets_summary_stats()
        self.assertEqual(stats["total_bets"], 2)
        self.assertEqual(stats["count_pending"], 2)
        self.assertEqual(stats["total_wagered"], 1500)
        self.assertEqual(stats["pending_exposure"], 1500)
        self.assertEqual(stats["total_paid_out"], 0)
        self.assertEqual(stats["bookmaker_profit"], 1500)

        # Проверяем get_all_bets
        bets, count = database.get_all_bets(status="all", limit=10, offset=0)
        self.assertEqual(count, 2)
        self.assertEqual(len(bets), 2)
        self.assertEqual(bets[0]["id"], bet_id_2)  # Newest first
        self.assertEqual(bets[0]["username"], "bettor_two")
        self.assertEqual(bets[0]["user_team"], "Арсенал")
        self.assertEqual(len(bets[0]["items"]), 1)
        self.assertEqual(bets[0]["items"][0]["team1_name"], "Реал Мадрид")

        # Фильтр по пользователю
        b1_bets, b1_count = database.get_all_bets(user_id=self.bettor1_id)
        self.assertEqual(b1_count, 1)
        self.assertEqual(b1_bets[0]["id"], bet_id_1)

    async def test_get_bet_by_id(self):
        """Проверка детальной выборки конкретной ставки."""
        slip = [{"match_id": self.match_id, "outcome": "p1"}]
        ok, bet_id = database.place_user_bet(self.bettor1_id, 750, slip)
        self.assertTrue(ok)

        bet = database.get_bet_by_id(bet_id)
        self.assertIsNotNone(bet)
        self.assertEqual(bet["amount"], 750)
        self.assertEqual(bet["username"], "bettor_one")
        self.assertEqual(bet["user_team"], "Манчестер Сити")
        self.assertEqual(bet["user_wallet_balance"], self.b1_initial_bal - 750)
        self.assertEqual(len(bet["items"]), 1)
        self.assertEqual(bet["items"][0]["selection_name"], "П1")

    # ──────────────────────────────────────────────────────────────────────────
    # 3. ТЕСТЫ ДЕТАЛЬНОЙ КАРТОЧКИ И АННУЛИРОВАНИЯ (VOID)
    # ──────────────────────────────────────────────────────────────────────────

    async def test_cb_admin_bet_detail_renders_card_and_void_button(self):
        """Просмотр детальной карточки ставки с кнопкой аннулирования."""
        slip = [{"match_id": self.match_id, "outcome": "p1"}]
        ok, bet_id = database.place_user_bet(self.bettor1_id, 600, slip)
        self.assertTrue(ok)

        update = self._build_update(self.super_id, chat_type="private", callback_data=f"admin_bet_view:{bet_id}")
        context = MagicMock()

        await cb_admin_bet_detail(update, context)

        update.callback_query.edit_message_text.assert_called_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        markup = update.callback_query.edit_message_text.call_args[1]["reply_markup"]

        self.assertIn(f"КАРТОЧКА СТАВКИ #{bet_id}", text)
        self.assertIn("bettor_one", text)
        self.assertIn("600 🪙", text)

        # Должна быть кнопка аннулирования
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn(f"admin_bet_void_ask:{bet_id}", callbacks)

    async def test_cb_admin_bet_void_flow(self):
        """Полный флоу аннулирования ставки: подтверждение, возврат средств и аудит-лог."""
        slip = [{"match_id": self.match_id, "outcome": "p1"}]
        ok, bet_id = database.place_user_bet(self.bettor1_id, 1200, slip)
        self.assertTrue(ok)

        # 1. Запрос подтверждения
        update_ask = self._build_update(self.super_id, chat_type="private", callback_data=f"admin_bet_void_ask:{bet_id}")
        await cb_admin_bet_void_ask(update_ask, MagicMock())
        update_ask.callback_query.edit_message_text.assert_called_once()
        text_ask = update_ask.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("Подтверждение аннулирования", text_ask)

        # 2. Выполнение аннулирования
        update_do = self._build_update(self.super_id, chat_type="private", callback_data=f"admin_bet_void_do:{bet_id}")
        await cb_admin_bet_void_execute(update_do, MagicMock())

        update_do.callback_query.edit_message_text.assert_called_once()
        text_do = update_do.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("успешно аннулирована", text_do)

        # Проверяем, что ставка в статусе refunded и монеты возвращены
        bet = database.get_bet_by_id(bet_id)
        self.assertEqual(bet["status"], "refunded")

        bal = database.get_wallet_balance(self.bettor1_id)
        self.assertEqual(bal, self.b1_initial_bal)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. ТЕСТЫ LIVE-ОПОВЕЩЕНИЙ
    # ──────────────────────────────────────────────────────────────────────────

    async def test_toggle_live_bet_alerts(self):
        """Включение и выключение Live-оповещений в ЛС."""
        self.assertFalse(database.is_live_bet_alerts_enabled(self.super_id))

        # Включаем
        database.set_live_bet_alerts_enabled(self.super_id, True)
        self.assertTrue(database.is_live_bet_alerts_enabled(self.super_id))
        self.assertIn(self.super_id, database.get_live_bet_alert_subscribers())

        # Выключаем
        database.set_live_bet_alerts_enabled(self.super_id, False)
        self.assertFalse(database.is_live_bet_alerts_enabled(self.super_id))
        self.assertNotIn(self.super_id, database.get_live_bet_alert_subscribers())

    async def test_notify_super_admins_new_bet(self):
        """Отправка нотификации подписанному супер-админу."""
        database.set_live_bet_alerts_enabled(self.super_id, True)

        slip = [{"match_id": self.match_id, "outcome": "p1"}]
        ok, bet_id = database.place_user_bet(self.bettor1_id, 800, slip)
        self.assertTrue(ok)

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        await notify_super_admins_new_bet(bot=mock_bot, bet_id=bet_id)
        # Ждем выполнения асинхронной таски отправки
        await asyncio.sleep(0.05)

        mock_bot.send_message.assert_called_once()
        args, kwargs = mock_bot.send_message.call_args
        self.assertEqual(kwargs.get("chat_id"), self.super_id)
        self.assertIn(f"Новая ставка #{bet_id}!", kwargs.get("text"))
        self.assertIn("bettor_one", kwargs.get("text"))

        # Время ставки — по Москве, а не сырое UTC из CURRENT_TIMESTAMP
        from handlers.admin_bets import _fmt_dt
        created_at = database.get_bet_by_id(bet_id)["created_at"]
        self.assertIn(f"Поставлена:</b> {_fmt_dt(created_at)} МСК", kwargs.get("text"))


if __name__ == "__main__":
    unittest.main()
