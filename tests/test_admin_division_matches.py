"""Управление матчами в скоупе дивизиона (модуль div-matches).

Глобального экрана матчей больше нет: сетка туров, карточка тура, линия ставок
и просроченные открываются только из карточки конкретного дивизиона и видят
данные только этого дивизиона.
"""
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers.admin import (
    admin_div_manage_matches,
    admin_div_round,
    admin_div_round_matches,
    admin_list_overdue,
    _round_back_cb,
    admin_remind_round,
    admin_send_selected_reminders,
    send_round_reminders,
)


class TestAdminDivisionMatches(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.div_a_id = database.create_division(name=f"MTX Альфа {uid}", code=f"MTXA_{uid}")
        self.div_b_id = database.create_division(name=f"MTX Бета {uid}", code=f"MTXB_{uid}")
        self.admin_id = 972001

        self.user_a1 = 97201
        self.user_a2 = 97202
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'mtx_a1', 'Real Madrid', 'player', ?)",
                (self.user_a1, self.div_a_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'mtx_a2', 'Barcelona', 'player', ?)",
                (self.user_a2, self.div_a_id),
            )
            # Тур 1 открыт только в дивизионе A, тур 2 закрыт — карточки должны отличаться.
            c.execute(
                "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (1, 1, ?, ?)",
                ("01.01.2025 00:00", self.div_a_id),
            )
            c.execute(
                "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (2, 0, NULL, ?)",
                (self.div_a_id,),
            )
            c.execute(
                "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (7, 1, ?, ?)",
                ("01.01.2025 00:00", self.div_b_id),
            )
            # Сетка туров строится из matches, а не из rounds — каждому туру нужен матч.
            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id) "
                "VALUES (1, ?, ?, 'Real Madrid', 'Barcelona', 'pending', ?)",
                (self.user_a1, self.user_a2, self.div_a_id),
            )
            self.match_a1 = c.lastrowid
            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id) "
                "VALUES (2, ?, ?, 'Real Madrid', 'Barcelona', 'pending', ?)",
                (self.user_a1, self.user_a2, self.div_a_id),
            )
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, status, division_id) "
                "VALUES (1, 'Arsenal', 'Chelsea', 'pending', ?)",
                (self.div_b_id,),
            )
            self.match_b1 = c.lastrowid
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, status, division_id) "
                "VALUES (7, 'Arsenal', 'Chelsea', 'pending', ?)",
                (self.div_b_id,),
            )

    async def asyncTearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM matches WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM rounds WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?)", (self.user_a1, self.user_a2))
            c.execute("DELETE FROM divisions WHERE id IN (?, ?)", (self.div_a_id, self.div_b_id))

    def _build_update(self, callback_data: str):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = self.admin_id
        update.message = None

        query = MagicMock()
        query.from_user.id = self.admin_id
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock()
        query.message.photo = None
        query.message.chat_id = self.admin_id
        query.message.message_thread_id = None
        query.message.is_topic_message = False
        update.callback_query = query
        return update

    @staticmethod
    def _callbacks(markup) -> list[str]:
        return [btn.callback_data for row in markup.inline_keyboard for btn in row]

    def _patches(self):
        return (
            patch("handlers.base.is_admin", return_value=True),
            patch("handlers.admin.is_admin", return_value=True),
            patch("handlers.admin.is_global_admin", return_value=True),
            patch("handlers.admin.safe_edit_or_reply", new=AsyncMock()),
        )

    # --- сетка туров дивизиона ---

    async def test_manage_matches_shows_action_block_and_own_rounds(self):
        update = self._build_update(f"admin_div_manage_matches:{self.div_a_id}")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await admin_div_manage_matches(update, context)

            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])

        # Действия дивизиона стоят над сеткой туров
        self.assertEqual(callbacks[0], f"admin_gen_div_{self.div_a_id}")
        self.assertEqual(callbacks[1], f"admin_batch_open_div:{self.div_a_id}")
        self.assertEqual(callbacks[2], f"admin_div_overdue:{self.div_a_id}")
        # Туры — только свои
        self.assertIn(f"admin_div_round:{self.div_a_id}:1", callbacks)
        self.assertIn(f"admin_div_round:{self.div_a_id}:2", callbacks)
        self.assertNotIn(f"admin_div_round:{self.div_a_id}:7", callbacks)
        self.assertNotIn(f"admin_div_round:{self.div_b_id}:7", callbacks)
        # Возврат супер-админа — в карточку дивизиона
        self.assertEqual(callbacks[-1], f"admin_div_view_{self.div_a_id}")

    # --- карточка тура ---

    async def test_round_card_open_round_offers_close_and_reminders(self):
        update = self._build_update(f"admin_div_round:{self.div_a_id}:1")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit:
            await admin_div_round(update, context)

            args, kwargs = update.callback_query.edit_message_text.call_args
            callbacks = self._callbacks(kwargs["reply_markup"])

        self.assertIn("MTX Альфа", args[0])
        self.assertIn(f"admin_div_round_close:{self.div_a_id}:1", callbacks)
        self.assertIn("admin_remind_round_1", callbacks)
        self.assertIn(f"admin_div_round_matches:{self.div_a_id}:1", callbacks)
        self.assertEqual(callbacks[-1], f"admin_div_manage_matches:{self.div_a_id}")
        # Дивизион запомнен для экранов, ключуемых одним номером тура
        self.assertEqual(context.user_data.get("admin_round_div_id"), self.div_a_id)

    async def test_round_card_closed_round_offers_open_and_bets(self):
        update = self._build_update(f"admin_div_round:{self.div_a_id}:2")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit:
            await admin_div_round(update, context)

            callbacks = self._callbacks(update.callback_query.edit_message_text.call_args[1]["reply_markup"])

        self.assertIn(f"admin_div_round_open:{self.div_a_id}:2", callbacks)
        self.assertIn(f"admin_div_bets_open:{self.div_a_id}:2", callbacks)
        self.assertNotIn(f"admin_div_round_close:{self.div_a_id}:2", callbacks)

    async def test_round_card_rejects_round_from_other_division(self):
        """Тур 7 существует, но в другом дивизионе — подмена div_id ничего не открывает."""
        update = self._build_update(f"admin_div_round:{self.div_a_id}:7")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit:
            await admin_div_round(update, context)

            args, kwargs = update.callback_query.edit_message_text.call_args

        self.assertIn("не найден", args[0])
        self.assertEqual(
            self._callbacks(kwargs["reply_markup"]),
            [f"admin_div_manage_matches:{self.div_a_id}"],
        )

    # --- матчи тура ---

    async def test_round_matches_lists_only_own_division(self):
        update = self._build_update(f"admin_div_round_matches:{self.div_a_id}:1")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await admin_div_round_matches(update, context)

            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])

        # Тур 1 есть в обоих дивизионах — видим только свой матч
        self.assertIn(f"admin_view_match_{self.match_a1}", callbacks)
        self.assertNotIn(f"admin_view_match_{self.match_b1}", callbacks)
        self.assertEqual(callbacks[-1], f"admin_div_round:{self.div_a_id}:1")
        self.assertEqual(context.user_data.get("admin_round_div_id"), self.div_a_id)

    # --- просроченные ---

    async def test_overdue_is_division_scoped(self):
        update = self._build_update(f"admin_div_overdue:{self.div_a_id}")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit:
            await admin_list_overdue(update, context)

            args, kwargs = update.callback_query.edit_message_text.call_args
            callbacks = self._callbacks(kwargs["reply_markup"])

        self.assertIn("MTX Альфа", args[0])
        self.assertNotIn("Arsenal", args[0])
        self.assertEqual(callbacks[-1], f"admin_div_manage_matches:{self.div_a_id}")

    async def test_overdue_requires_division_id(self):
        update = self._build_update("admin_div_overdue:")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit:
            await admin_list_overdue(update, context)

            self.assertFalse(update.callback_query.edit_message_text.called)

    # --- возврат с экранов, ключуемых одним номером тура ---

    def test_round_back_cb_uses_session_division(self):
        context = MagicMock()
        context.user_data = {"admin_round_div_id": self.div_a_id}
        self.assertEqual(_round_back_cb(context, 3), f"admin_div_round:{self.div_a_id}:3")

    def test_round_back_cb_falls_back_to_admin_panel(self):
        """Без дивизиона в сессии уводим в админку: она роутит и супер-админа, и админа дивизиона."""
        context = MagicMock()
        context.user_data = {}
        self.assertEqual(_round_back_cb(context, 3), "admin_main_menu")

    # --- напоминания должникам тура (изоляция по дивизиону) ---

    async def test_admin_remind_round_scopes_matches_to_division(self):
        """Экран выбора матчей для напоминаний должен видеть только матчи своего дивизиона."""
        update = self._build_update("admin_remind_round_1")
        context = MagicMock()
        context.user_data = {"admin_round_div_id": self.div_a_id}
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit:
            await admin_remind_round(update, context, round_number=1)

            markup = update.callback_query.edit_message_text.call_args[1]["reply_markup"]
            callbacks = self._callbacks(markup)

        # В туре 1 не сыграны и match_a1 (Div A), и match_b1 (Div B).
        # В экране Div A должен присутствовать только match_a1!
        self.assertIn(f"admin_toggle_remind_match_1_{self.match_a1}", callbacks)
        self.assertNotIn(f"admin_toggle_remind_match_1_{self.match_b1}", callbacks)
        self.assertIn(f"admin_send_selected_reminders_1", callbacks)

    async def test_admin_send_selected_reminders_passes_division_id(self):
        """Отправка напоминаний должна передавать division_id в send_round_reminders."""
        update = self._build_update("admin_send_selected_reminders_1")
        context = MagicMock()
        context.user_data = {
            "admin_round_div_id": self.div_a_id,
            f"remind_selected_{self.div_a_id}_1": {self.match_a1},
        }
        p_base, p_adm, p_glob, p_edit = self._patches()
        with p_base, p_adm, p_glob, p_edit:
            with patch("handlers.admin.send_round_reminders", new_callable=AsyncMock) as mock_send:
                mock_send.return_value = (2, 1)
                await admin_send_selected_reminders(update, context)

                mock_send.assert_awaited_once_with(
                    context, 1, target_match_ids={self.match_a1}, division_id=self.div_a_id
                )

    async def test_send_round_reminders_scopes_pms_and_topic_to_division(self):
        """send_round_reminders рассылает ЛС только участникам указанного дивизиона."""
        context = MagicMock()
        context.bot = MagicMock()
        context.bot.send_message = AsyncMock()

        with patch("handlers.admin.safe_send_notification", new_callable=AsyncMock) as mock_notify, \
             patch("handlers.admin.resolve_division_target", new_callable=AsyncMock) as mock_target:
            mock_notify.return_value = True
            mock_target.return_value = (-100111, 42)

            pm_sent, count_matches = await send_round_reminders(
                context, round_number=1, division_id=self.div_a_id
            )

        # Должен быть обработан только матч match_a1 (1 матч, 2 игрока: user_a1, user_a2)
        self.assertEqual(count_matches, 1)
        self.assertEqual(pm_sent, 2)
        notified_users = {call.args[1] for call in mock_notify.call_args_list}
        self.assertEqual(notified_users, {self.user_a1, self.user_a2})

        # Сводка должна уходить строго в топик дивизиона A (-100111, thread 42)
        mock_target.assert_awaited_with(self.div_a_id, "reports", "previews", legacy_topic_keys=("reports_topic_id",))
        context.bot.send_message.assert_awaited_once()
        self.assertEqual(context.bot.send_message.call_args[1]["chat_id"], -100111)
        self.assertEqual(context.bot.send_message.call_args[1]["message_thread_id"], 42)


if __name__ == "__main__":
    unittest.main()
