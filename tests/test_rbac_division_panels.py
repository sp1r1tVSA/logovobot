import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers.admin import (
    show_admin_panel,
    show_super_admin_panel,
    show_division_admin_panel,
    admin_div_admins_hub,
    admin_div_admins_view,
    admin_div_manage_matches,
    admin_div_broadcast_debts,
    admin_div_manage_players,
    admin_rosters_for_division,
    admin_squad_clear,
    admin_squads_view_cb,
    admin_view_squad,
)


class TestRbacDivisionPanels(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.club_a = f"RBAC FC {uid}"
        self.div_a =database.create_division(name=f"RBAC Альфа {uid}", code=f"RBACA_{uid}")
        self.div_b = database.create_division(name=f"RBAC Бета {uid}", code=f"RBACB_{uid}")
        self.super_id = 970001
        self.div_admin_id = 970002
        self.multi_admin_id = 970003
        self.nobody_id = 970004
        self.player_id = 970005

        # division_admins.user_id ссылается на users.telegram_id — создаём профили
        database.register_user(self.super_id, "rbac_super")
        database.register_user(self.div_admin_id, "rbac_div_admin")
        database.register_user(self.multi_admin_id, "rbac_multi_admin")
        database.register_user(self.nobody_id, "rbac_nobody")
        database.register_user(self.player_id, "rbac_player", team_name=self.club_a)
        database.assign_user_division(self.player_id, self.div_a)

        database.add_division_admin(self.div_a, self.div_admin_id)
        database.add_division_admin(self.div_a, self.multi_admin_id)
        database.add_division_admin(self.div_b, self.multi_admin_id)

    async def asyncTearDown(self):
        for div_id in (self.div_a, self.div_b):
            for uid in (self.div_admin_id, self.multi_admin_id):
                database.remove_division_admin(div_id, uid)

    def _build_update(self, user_id: int, callback_data: str | None = None):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.message = None

        query = MagicMock()
        query.from_user.id = user_id
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        return update

    @staticmethod
    def _callbacks(markup) -> list[str]:
        return [btn.callback_data for row in markup.inline_keyboard for btn in row]

    def _patches(self, is_global: bool):
        return (
            patch("handlers.base.is_admin", return_value=True),
            patch("handlers.admin.is_admin", return_value=True),
            patch("handlers.admin.is_global_admin", return_value=is_global),
            patch("handlers.admin.safe_edit_or_reply", new=AsyncMock()),
        )

    # --- database getter ---

    async def test_get_admin_divisions_returns_bound_divisions(self):
        divs = database.get_admin_divisions(self.multi_admin_id)
        self.assertEqual({d["id"] for d in divs}, {self.div_a, self.div_b})
        self.assertEqual(database.get_admin_divisions(self.nobody_id), [])

    # --- routing ---

    async def test_router_sends_super_panel_for_global_admin(self):
        update = self._build_update(self.super_id, "admin_main_menu")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(True)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await show_admin_panel(update, context)

            self.assertTrue(edit_mock.called)
            text = edit_mock.call_args[0][2]
            markup = edit_mock.call_args[1]["reply_markup"]
            self.assertIn("Админ-панель", text)
            self.assertIn("admin_div_admins_hub", self._callbacks(markup))

    async def test_router_sends_division_panel_for_single_division_admin(self):
        update = self._build_update(self.div_admin_id, "admin_main_menu")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await show_admin_panel(update, context)

            text = edit_mock.call_args[0][2]
            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])
            self.assertIn("Админ-панель дивизиона", text)
            self.assertIn(f"admin_div_manage_matches:{self.div_a}", callbacks)
            self.assertIn(f"admin_div_debts_menu:{self.div_a}", callbacks)
            self.assertIn(f"admin_div_manage_players:{self.div_a}", callbacks)
            self.assertIn(f"admin_roster_div:{self.div_a}", callbacks)
            # Урезанная панель: глобальных разделов быть не должно
            self.assertNotIn("admin_manage_squads", callbacks)
            self.assertNotIn("admin_divs_hub", callbacks)

    async def test_router_offers_choice_for_multi_division_admin(self):
        update = self._build_update(self.multi_admin_id, "admin_main_menu")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await show_admin_panel(update, context)

            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])
            self.assertIn(f"admin_div_panel:{self.div_a}", callbacks)
            self.assertIn(f"admin_div_panel:{self.div_b}", callbacks)

    async def test_router_denies_user_without_divisions(self):
        update = self._build_update(self.nobody_id, "admin_main_menu")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await show_admin_panel(update, context)

            self.assertFalse(edit_mock.called)
            answers = [c.args[0] for c in update.callback_query.answer.call_args_list if c.args]
            self.assertTrue(any("нет прав" in a for a in answers), answers)

    async def test_division_panel_resolves_div_id_from_callback(self):
        update = self._build_update(self.multi_admin_id, f"admin_div_panel:{self.div_b}")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await show_division_admin_panel(update, context)

            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])
            self.assertIn(f"admin_div_manage_matches:{self.div_b}", callbacks)
            # У админа двух дивизионов есть переключатель
            self.assertIn("admin_main_menu", callbacks)

    # --- isolation / callback forgery ---

    async def test_interceptors_reject_foreign_division(self):
        forged = [
            (admin_div_manage_matches, f"admin_div_manage_matches:{self.div_b}"),
            (admin_div_broadcast_debts, f"admin_div_broadcast_debts:{self.div_b}"),
            (admin_div_manage_players, f"admin_div_manage_players:{self.div_b}"),
        ]
        for handler, data in forged:
            with self.subTest(callback=data):
                update = self._build_update(self.div_admin_id, data)
                context = MagicMock()
                p_base, p_adm, p_glob, p_edit = self._patches(False)
                with p_base, p_adm, p_glob, p_edit as edit_mock:
                    await handler(update, context)

                    self.assertFalse(edit_mock.called)
                    self.assertFalse(update.callback_query.edit_message_text.called)
                    answers = [c.args[0] for c in update.callback_query.answer.call_args_list if c.args]
                    self.assertTrue(any("нет прав на этот дивизион" in a for a in answers), answers)

    async def test_matches_interceptor_allows_own_division(self):
        update = self._build_update(self.div_admin_id, f"admin_div_manage_matches:{self.div_a}")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await admin_div_manage_matches(update, context)

            self.assertTrue(edit_mock.called)
            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])
            self.assertIn(f"admin_div_panel:{self.div_a}", callbacks)

    async def test_players_interceptor_allows_own_division(self):
        update = self._build_update(self.div_admin_id, f"admin_div_manage_players:{self.div_a}")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit:
            await admin_div_manage_players(update, context)

            # Список участников рендерится напрямую через edit_message_text
            self.assertTrue(update.callback_query.edit_message_text.called)
            markup = update.callback_query.edit_message_text.call_args[1]["reply_markup"]
            self.assertIn(f"admin_div_panel:{self.div_a}", self._callbacks(markup))

    # --- squads (rosters) ---

    async def test_rosters_open_for_own_division_and_return_to_panel(self):
        update = self._build_update(self.div_admin_id, f"admin_roster_div:{self.div_a}")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit:
            await admin_rosters_for_division(update, context)

        markup = update.callback_query.edit_message_text.call_args[1]["reply_markup"]
        callbacks = self._callbacks(markup)
        self.assertIn(f"admin_squad_view_{self.club_a}", callbacks)
        # Карточка дивизиона закрыта супер-админу — «Назад» ведёт в панель дивизиона
        self.assertIn(f"admin_div_panel:{self.div_a}", callbacks)
        self.assertNotIn(f"admin_div_view_{self.div_a}", callbacks)
        # Общелиговая загрузка фото админу дивизиона не показывается
        self.assertNotIn("admin_fetch_photos_cb", callbacks)

    async def test_rosters_reject_foreign_division(self):
        update = self._build_update(self.div_admin_id, f"admin_roster_div:{self.div_b}")
        context = MagicMock()
        context.user_data = {}
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit:
            await admin_rosters_for_division(update, context)

        self.assertFalse(update.callback_query.edit_message_text.called)
        self.assertNotIn("admin_roster_div_id", context.user_data)
        answers = [c.args[0] for c in update.callback_query.answer.call_args_list if c.args]
        self.assertTrue(any("нет прав на этот дивизион" in a for a in answers), answers)

    async def test_squad_actions_limited_to_own_division_clubs(self):
        foreign_club = f"RBAC Чужой {uuid.uuid4().hex[:6]}"
        for handler, data in (
            (admin_view_squad, f"admin_squad_view_{foreign_club}"),
            (admin_squad_clear, f"admin_squad_clear_{foreign_club}"),
        ):
            with self.subTest(callback=data):
                update = self._build_update(self.div_admin_id, data)
                context = MagicMock()
                context.user_data = {}
                p_base, p_adm, p_glob, p_edit = self._patches(False)
                with p_base, p_adm, p_glob, p_edit as edit_mock, \
                        patch("handlers.admin.database.clear_squad") as clear_mock:
                    await handler(update, context)

                    self.assertFalse(edit_mock.called)
                    self.assertFalse(update.callback_query.edit_message_text.called)
                    self.assertFalse(clear_mock.called)

        update = self._build_update(self.div_admin_id, f"admin_squad_view_{self.club_a}")
        context = MagicMock()
        context.user_data = {"admin_roster_div_id": self.div_a}
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await admin_view_squad(update, context)

            self.assertTrue(edit_mock.called)
            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])
            self.assertIn(f"admin_roster_div:{self.div_a}", callbacks)

    async def test_squads_status_keeps_division_admin_in_scope(self):
        update = self._build_update(self.div_admin_id, f"admin_squads_view:{self.div_a}")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await admin_squads_view_cb(update, context)

            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])
            self.assertIn(f"admin_div_panel:{self.div_a}", callbacks)
            self.assertNotIn("admin_squads_all", callbacks)
            self.assertNotIn(f"admin_squads_view:{self.div_b}", callbacks)

    # --- super-admin division admins UI ---

    async def test_div_admins_hub_is_super_admin_only(self):
        update = self._build_update(self.div_admin_id, "admin_div_admins_hub")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(False)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await admin_div_admins_hub(update, context)

            self.assertFalse(edit_mock.called)
            answers = [c.args[0] for c in update.callback_query.answer.call_args_list if c.args]
            self.assertTrue(any("супер-админу" in a for a in answers), answers)

    async def test_div_admins_view_lists_assigned_admins(self):
        update = self._build_update(self.super_id, f"admin_div_admins_view_{self.div_a}")
        context = MagicMock()
        p_base, p_adm, p_glob, p_edit = self._patches(True)
        with p_base, p_adm, p_glob, p_edit as edit_mock:
            await admin_div_admins_view(update, context)

            text = edit_mock.call_args[0][2]
            callbacks = self._callbacks(edit_mock.call_args[1]["reply_markup"])
            self.assertIn(str(self.div_admin_id), text)
            self.assertIn(f"admin_div_admin_add_{self.div_a}", callbacks)
            self.assertIn(f"admin_div_admin_del_{self.div_a}_{self.div_admin_id}", callbacks)


if __name__ == "__main__":
    unittest.main()
