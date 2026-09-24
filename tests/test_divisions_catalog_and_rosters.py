import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import config
import database
from handlers.cabinet import (
    show_clubs_catalog_divisions,
    show_clubs_catalog_for_division,
)
from handlers.admin import (
    admin_bind_club_card,
    admin_bind_division,
    admin_bind_execute,
    admin_bind_free_execute,
    admin_bind_hub,
    _division_code_from_name,
    admin_edit_club_execute,
    admin_edit_club_select,
    admin_rosters_for_division,
    _build_debts_summary,
    _club_owner_labels,
    _post_or_update_debts_for_division,
)


class TestDivisionsCatalogAndRosters(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        self.admin_id = 999123

        # Create two distinct test divisions
        uid = uuid.uuid4().hex[:6].upper()
        self.div_a_code = f"DIV_A_{uid}"
        self.div_b_code = f"DIV_B_{uid}"
        self.div_a_id = database.create_division(name="Первый Дивизион", code=self.div_a_code)
        self.div_b_id = database.create_division(name="Второй Дивизион", code=self.div_b_code)

        # Create test users in these divisions
        self.user_a1_id = 99901
        self.user_a2_id = 99902
        self.user_b1_id = 99903

        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id)
                VALUES (?, 'user_a1', 'Реал Мадрид', 'player', ?)
            """, (self.user_a1_id, self.div_a_id))
            c.execute("""
                INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id)
                VALUES (?, 'user_a2', 'Барселона', 'player', ?)
            """, (self.user_a2_id, self.div_a_id))
            c.execute("""
                INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id)
                VALUES (?, 'user_b1', 'Арсенал', 'player', ?)
            """, (self.user_b1_id, self.div_b_id))

    async def asyncTearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM matches WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM rounds WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM division_topics WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?, ?)", (self.user_a1_id, self.user_a2_id, self.user_b1_id))
            c.execute("DELETE FROM divisions WHERE id IN (?, ?)", (self.div_a_id, self.div_b_id))

    def _build_mock_update(self, callback_data: str):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = self.admin_id
        update.effective_chat = MagicMock()
        update.effective_chat.id = self.admin_id

        query = MagicMock()
        query.from_user.id = self.admin_id
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock()
        query.message.photo = None
        query.message.delete = AsyncMock()
        query.message.is_topic_message = False
        query.message.chat_id = self.admin_id
        query.message.message_thread_id = None

        update.callback_query = query
        update.message = None
        return update

    async def test_get_division_teams_isolation(self):
        """Test database.get_division_teams returns only teams belonging to the specified division."""
        teams_a = database.get_division_teams(self.div_a_id)
        teams_b = database.get_division_teams(self.div_b_id)

        self.assertIn("Реал Мадрид", teams_a)
        self.assertIn("Барселона", teams_a)
        self.assertNotIn("Арсенал", teams_a)

        self.assertIn("Арсенал", teams_b)
        self.assertNotIn("Реал Мадрид", teams_b)
        self.assertNotIn("Барселона", teams_b)

    async def test_get_clubs_summary_for_division(self):
        """Test database.get_clubs_summary_for_division returns clubs summary only for that division."""
        summary_a = database.get_clubs_summary_for_division(self.div_a_id)
        summary_b = database.get_clubs_summary_for_division(self.div_b_id)

        names_a = [c["team_name"] for c in summary_a]
        names_b = [c["team_name"] for c in summary_b]

        self.assertIn("Реал Мадрид", names_a)
        self.assertIn("Барселона", names_a)
        self.assertNotIn("Арсенал", names_a)

        self.assertIn("Арсенал", names_b)
        self.assertNotIn("Реал Мадрид", names_b)

    async def test_show_clubs_catalog_divisions(self):
        """Test Step 1 of Clubs Catalog: lists active divisions."""
        update = self._build_mock_update(callback_data="cb_clubs_catalog")
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await show_clubs_catalog_divisions(update, context)

        update.callback_query.edit_message_text.assert_called_once()
        args, kwargs = update.callback_query.edit_message_text.call_args
        text = args[0]
        reply_markup = kwargs.get("reply_markup")

        self.assertIn("КАТАЛОГ КЛУБОВ ПО ДИВИЗИОНАМ", text)
        buttons_cb = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
        self.assertIn(f"clubs_catalog_div:{self.div_a_id}", buttons_cb)
        self.assertIn(f"clubs_catalog_div:{self.div_b_id}", buttons_cb)
        self.assertIn("main_menu", buttons_cb)

    async def test_show_clubs_catalog_for_division(self):
        """Test Step 2 of Clubs Catalog: lists clubs for chosen division with back button."""
        update = self._build_mock_update(callback_data=f"clubs_catalog_div:{self.div_a_id}")
        context = MagicMock()
        context.bot.send_message = AsyncMock()

        await show_clubs_catalog_for_division(update, context)

        update.callback_query.edit_message_text.assert_called_once()
        args, kwargs = update.callback_query.edit_message_text.call_args
        text = args[0]
        reply_markup = kwargs.get("reply_markup")

        self.assertIn("ПЕРВЫЙ ДИВИЗИОН", text.upper())
        buttons_cb = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
        self.assertIn("view_club_Реал Мадрид", buttons_cb)
        self.assertIn("view_club_Барселона", buttons_cb)
        self.assertNotIn("view_club_Арсенал", buttons_cb)
        self.assertIn("cb_clubs_catalog", buttons_cb)

    async def test_admin_rosters_for_division(self):
        """Составы дивизиона — единственная точка входа, глобального списка больше нет."""
        update = self._build_mock_update(callback_data=f"admin_roster_div:{self.div_a_id}")
        context = MagicMock()
        context.user_data = {}

        with patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.admin.is_global_admin", return_value=True):
            await admin_rosters_for_division(update, context)

        self.assertEqual(context.user_data.get("admin_roster_div_id"), self.div_a_id)

        update.callback_query.edit_message_text.assert_called_once()
        args, kwargs = update.callback_query.edit_message_text.call_args
        text = args[0]
        reply_markup = kwargs.get("reply_markup")

        self.assertIn("Первый Дивизион", text)
        buttons_cb = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
        self.assertIn("admin_squad_view_Реал Мадрид", buttons_cb)
        self.assertIn("admin_squad_view_Барселона", buttons_cb)
        self.assertNotIn("admin_squad_view_Арсенал", buttons_cb)
        # Возврат — в карточку своего дивизиона, а не в глобальный экран составов
        self.assertIn(f"admin_div_view_{self.div_a_id}", buttons_cb)
        self.assertNotIn("admin_manage_squads", buttons_cb)
        # Утилита «добавить из матчей» работает в скоупе дивизиона
        self.assertIn(f"admin_squad_add_missing_div:{self.div_a_id}", buttons_cb)
        self.assertNotIn("admin_squad_add_missing_all", buttons_cb)

    async def test_build_debts_summary_with_division_filter(self):
        """Test _build_debts_summary correctly isolates unplayed matches by division."""
        past_dl = "01.01.2025 00:00"
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (1, 0, ?, ?)",
                (past_dl, self.div_a_id)
            )
            conn.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id) "
                "VALUES (1, ?, ?, 'Реал Мадрид', 'Барселона', 'pending', ?)",
                (self.user_a1_id, self.user_a2_id, self.div_a_id)
            )

        summary_a, count_a = await _build_debts_summary(division_id=self.div_a_id, division_name="Первый Дивизион")
        summary_b, count_b = await _build_debts_summary(division_id=self.div_b_id, division_name="Второй Дивизион")

        self.assertIsNotNone(summary_a)
        self.assertIn("ПЕРВЫЙ ДИВИЗИОН", summary_a)
        self.assertIn("Реал Мадрид", summary_a)
        self.assertIn("Барселона", summary_a)
        self.assertGreater(count_a, 0)

        # Division B has no unplayed matches
        self.assertIsNone(summary_b)
        self.assertEqual(count_b, 0)

    async def test_post_or_update_debts_for_division(self):
        """Test _post_or_update_debts_for_division posts to the division's bound topic."""
        # Create unplayed match in Division A
        past_dl = "01.01.2025 00:00"
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (1, 0, ?, ?)",
                (past_dl, self.div_a_id)
            )
            conn.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id) "
                "VALUES (1, ?, ?, 'Реал Мадрид', 'Барселона', 'pending', ?)",
                (self.user_a1_id, self.user_a2_id, self.div_a_id)
            )

        # Bind division A to topic 555
        database.set_division_topic(self.div_a_id, "previews", 555)

        context = MagicMock()
        context.bot.send_message = AsyncMock()
        sent_msg = MagicMock()
        sent_msg.message_id = 777
        context.bot.send_message.return_value = sent_msg

        with patch("database.get_group_id", return_value=-100123):
            success, count = await _post_or_update_debts_for_division(
                context, division_id=self.div_a_id, division_name="Первый Дивизион"
            )

        self.assertTrue(success)
        self.assertGreater(count, 0)
        context.bot.send_message.assert_called_once()
        _, call_kwargs = context.bot.send_message.call_args
        self.assertEqual(call_kwargs.get("chat_id"), -100123)
        self.assertEqual(call_kwargs.get("message_thread_id"), 555)
        self.assertIn("ПЕРВЫЙ ДИВИЗИОН", call_kwargs.get("text"))


class TestSeededDivisionRoster(unittest.IsolatedAsyncioTestCase):
    """Сид-состав дивизиона виден до того, как в нём кто-то зарегистрировался.

    Иначе получается замкнутый круг: клуб появляется в списке, только когда его
    уже кто-то занял, а занять его админу не из чего — пустой сезон невозможно
    расписать по тренерам.
    """

    async def asyncSetUp(self):
        database.init_db()
        database.ensure_canonical_divisions()
        self.admin_id = 999124
        self.coach_id = 99911
        div_five = database.get_division_by_code("DIV_5")
        self.div_five_id = div_five["id"]

        with database.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'coach_no_club', NULL, 'player', ?)",
                (self.coach_id, self.div_five_id)
            )

    async def asyncTearDown(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM users WHERE telegram_id = ?", (self.coach_id,))

    async def test_division_teams_come_from_the_seeded_roster(self):
        teams = database.get_division_teams(self.div_five_id)

        for club in config.DIVISION_CLUBS["DIV_5"]:
            self.assertIn(club, teams)

    async def test_seeded_roster_is_scoped_to_its_own_division(self):
        teams = database.get_division_teams(self.div_five_id)

        for club in config.DIVISION_CLUBS["DIV_1"]:
            self.assertNotIn(club, teams)

    async def test_admin_club_picker_offers_the_seeded_roster(self):
        """«Изменить клуб» для тренера без клуба предлагает все 16 клубов его дивизиона."""
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = self.admin_id
        query = MagicMock()
        query.from_user.id = self.admin_id
        query.data = f"admin_edit_club_select_{self.coach_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {}

        with patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin.is_admin", return_value=True):
            await admin_edit_club_select(update, context)

        query.edit_message_text.assert_called_once()
        _, kwargs = query.edit_message_text.call_args
        labels = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]

        offered = context.user_data[f"admin_edit_clubs_{self.coach_id}"]
        for club in config.DIVISION_CLUBS["DIV_5"]:
            self.assertIn(club, offered)
        # Клубов никто не занял — ни один не должен быть помечен красным.
        self.assertTrue(any("Реал Мадрид (свободен)" in label for label in labels))
        self.assertFalse(any(label.startswith("🔴") for label in labels))

    async def test_club_owner_without_username_still_marks_the_club_occupied(self):
        """Владелец без @username не превращает свой клуб в «свободен».

        set_player_club снимает прежнего владельца молча, поэтому неверная
        подпись стоила бы тренеру клуба.
        """
        with database.transaction() as conn:
            conn.execute(
                "UPDATE users SET username = NULL, team_name = 'Челси' WHERE telegram_id = ?",
                (self.coach_id,)
            )

        labels = _club_owner_labels(
            [dict(u) for u in database.list_users()],
            division_id=self.div_five_id,
        )

        self.assertEqual(labels.get("челси"), f"ID {self.coach_id}")


class TestBindingACoachToAClub(unittest.IsolatedAsyncioTestCase):
    """Нажатие на клуб в «Изменить клуб» и его последствия для прежнего владельца."""

    async def asyncSetUp(self):
        database.init_db()
        database.ensure_canonical_divisions()
        self.admin_id = 999125
        self.coach_id = 99921
        self.rival_id = 99922
        self.club = config.DIVISION_CLUBS["DIV_5"][0]
        self.div_five_id = database.get_division_by_code("DIV_5")["id"]

        with database.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'coach_to_bind', NULL, 'player', ?)",
                (self.coach_id, self.div_five_id)
            )
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id, warn_count) "
                "VALUES (?, 'rival_owner', ?, 'player', ?, 2)",
                (self.rival_id, self.club, self.div_five_id)
            )
            conn.execute(
                "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) "
                "VALUES (?, ?, 'Неявка', 'WARN_ADD', '2026-09-01 12:00:00')",
                (self.rival_id, self.admin_id)
            )

    async def asyncTearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM user_warns WHERE user_id IN (?, ?)", (self.coach_id, self.rival_id))
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?)", (self.coach_id, self.rival_id))

    async def _press_club(self, player_id: int, club: str):
        """Нажать кнопку клуба с пустым `user_data`.

        Это не упрощение, а рабочий случай: список клубов живёт в памяти процесса,
        и после перезапуска бота нажатие на уже нарисованную кнопку приходит
        именно так — обработчик обязан пересобрать тот же список сам.
        """
        clubs = await asyncio.to_thread(database.get_division_teams, self.div_five_id)
        query = MagicMock()
        query.data = f"admin_eclub_{player_id}_{clubs.index(club)}"
        query.from_user.id = self.admin_id
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = self.admin_id
        context = MagicMock()
        context.user_data = {}

        with patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.admin._post_or_update_debts_in_warns", new=AsyncMock()), \
             patch("handlers.admin.admin_view_player", new=AsyncMock()):
            await admin_edit_club_execute(update, context)

        return query.answer.call_args[0][0]

    async def test_pressing_a_free_club_binds_the_coach(self):
        free_club = config.DIVISION_CLUBS["DIV_5"][1]

        alert = await self._press_club(self.coach_id, free_club)

        self.assertEqual(database.get_user(self.coach_id)["team_name"], free_club)
        self.assertIn(free_club, alert)
        self.assertIn("@coach_to_bind", alert)
        # Алерт Telegram — обычный текст: разметка в нём показалась бы звёздочками.
        self.assertNotIn("**", alert)

    async def test_taking_an_occupied_club_wipes_the_previous_owner_completely(self):
        """Прежний владелец теряет клуб, счётчик варнов и их историю.

        Историю раньше чистил подзапрос по `team_name`, который выполнялся уже
        после обнуления этого поля и не находил никого: счётчик показывал 0, а
        варны оставались висеть на бывшем владельце.
        """
        alert = await self._press_club(self.coach_id, self.club)

        rival = database.get_user(self.rival_id)
        self.assertIsNone(rival["team_name"])
        self.assertEqual(rival["warn_count"], 0)
        self.assertEqual(database.get_user_warns(self.rival_id), [])
        self.assertEqual(database.get_user(self.coach_id)["team_name"], self.club)
        # Отъём клуба не должен быть молчаливым.
        self.assertIn("@rival_owner", alert)


class TestClubBindingScreen(unittest.IsolatedAsyncioTestCase):
    """Экран «🔗 Привязка клубов»: взгляд от клуба, а не от игрока."""

    async def asyncSetUp(self):
        database.init_db()
        database.ensure_canonical_divisions()
        self.admin_id = 999126
        self.free_coach_id = 99931
        self.owner_id = 99932
        self.homeless_id = 99933
        self.div_five_id = database.get_division_by_code("DIV_5")["id"]
        self.div_four_id = database.get_division_by_code("DIV_4")["id"]
        self.teams = database.get_division_teams(self.div_five_id)
        self.taken_club = config.DIVISION_CLUBS["DIV_5"][0]
        self.free_club = config.DIVISION_CLUBS["DIV_5"][1]

        with database.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'free_coach', NULL, 'player', ?)",
                (self.free_coach_id, self.div_five_id)
            )
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id, warn_count) "
                "VALUES (?, 'club_owner', ?, 'player', ?, 2)",
                (self.owner_id, self.taken_club, self.div_five_id)
            )
            conn.execute(
                "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) "
                "VALUES (?, ?, 'Неявка', 'WARN_ADD', '2026-09-01 12:00:00')",
                (self.owner_id, self.admin_id)
            )
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id) "
                "VALUES (?, 'homeless_coach', NULL, 'player', NULL)",
                (self.homeless_id,)
            )

    async def asyncTearDown(self):
        ids = (self.free_coach_id, self.owner_id, self.homeless_id)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM user_warns WHERE user_id IN (?, ?, ?)", ids)
            c.execute("DELETE FROM users WHERE telegram_id IN (?, ?, ?)", ids)

    def _update(self, data: str, user_id: int | None = None):
        query = MagicMock()
        query.data = data
        query.from_user.id = user_id or self.admin_id
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = user_id or self.admin_id
        return update, query

    async def _run(self, handler, data: str, **patches):
        """Прогнать хендлер привязки. `user_data` намеренно пуст — индекс клуба
        обязан пересобираться из БД, а не доставаться из памяти процесса."""
        update, query = self._update(data)
        context = MagicMock()
        context.user_data = {}
        with patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.admin.is_global_admin", return_value=True), \
             patch("handlers.admin._post_or_update_debts_in_warns", new=AsyncMock()):
            await handler(update, context)
        return query

    def _screen(self, query) -> tuple[str, list[str]]:
        text = query.edit_message_text.call_args[0][0]
        markup = query.edit_message_text.call_args[1]["reply_markup"]
        buttons = [b.text for row in markup.inline_keyboard for b in row]
        return text, buttons

    def _callbacks(self, query) -> list[str]:
        markup = query.edit_message_text.call_args[1]["reply_markup"]
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def _back_cb(self, query) -> str:
        markup = query.edit_message_text.call_args[1]["reply_markup"]
        return markup.inline_keyboard[-1][0].callback_data

    async def test_division_screen_shows_every_club_with_its_status(self):
        query = await self._run(admin_bind_division, f"admin_bind_div:{self.div_five_id}")

        text, buttons = self._screen(query)
        self.assertIn(f"Занято: <b>1/{len(self.teams)}</b>", text)
        self.assertIn(f"🔴 {self.taken_club} (@club_owner)", buttons)
        self.assertIn(f"🟢 {self.free_club} (свободен)", buttons)
        # Каждый клуб дивизиона должен быть на экране, иначе свободный не найти.
        self.assertEqual(sum(1 for b in buttons if b.startswith(("🔴 ", "🟢 "))), len(self.teams))

    async def test_pressing_a_candidate_binds_them_to_the_club(self):
        idx = self.teams.index(self.free_club)

        query = await self._run(
            admin_bind_execute, f"admin_bind_set:{self.div_five_id}:{idx}:{self.free_coach_id}"
        )

        self.assertEqual(database.get_user(self.free_coach_id)["team_name"], self.free_club)
        alert = query.answer.call_args[0][0]
        self.assertIn(self.free_club, alert)
        # Алерт Telegram — обычный текст: разметка в нём показалась бы звёздочками.
        self.assertNotIn("**", alert)
        # После привязки админ возвращается на экран клубов с обновлённым статусом.
        _, buttons = self._screen(query)
        self.assertIn(f"🔴 {self.free_club} (@free_coach)", buttons)

    async def test_binding_a_coach_without_a_division_moves_them_into_it(self):
        """Клуб принадлежит дивизиону, значит и его владелец обязан в нём числиться —
        иначе тренер выпадет из таблицы и долгов, которые считаются по division_id."""
        idx = self.teams.index(self.free_club)

        query = await self._run(
            admin_bind_execute, f"admin_bind_set:{self.div_five_id}:{idx}:{self.homeless_id}"
        )

        bound = database.get_user(self.homeless_id)
        self.assertEqual(bound["team_name"], self.free_club)
        self.assertEqual(bound["division_id"], self.div_five_id)
        self.assertIn("дивизион", query.answer.call_args[0][0])

    async def test_taking_an_occupied_club_wipes_the_previous_owners_warns(self):
        """Регрессия: счётчик и история варнов обязаны сниматься вместе с клубом."""
        idx = self.teams.index(self.taken_club)

        await self._run(
            admin_bind_execute, f"admin_bind_set:{self.div_five_id}:{idx}:{self.free_coach_id}"
        )

        previous = database.get_user(self.owner_id)
        self.assertIsNone(previous["team_name"])
        self.assertEqual(previous["warn_count"], 0)
        self.assertEqual(database.get_user_warns(self.owner_id), [])
        self.assertEqual(database.get_user(self.free_coach_id)["team_name"], self.taken_club)

    async def test_releasing_a_club_keeps_its_owner_in_the_league(self):
        idx = self.teams.index(self.taken_club)

        query = await self._run(
            admin_bind_free_execute, f"admin_bind_free_ok:{self.div_five_id}:{idx}"
        )

        released = database.get_user(self.owner_id)
        self.assertIsNotNone(released)
        self.assertIsNone(released["team_name"])
        self.assertEqual(released["division_id"], self.div_five_id)
        self.assertEqual(released["warn_count"], 0)
        self.assertEqual(database.get_user_warns(self.owner_id), [])
        _, buttons = self._screen(query)
        self.assertIn(f"🟢 {self.taken_club} (свободен)", buttons)

    # --- «Назад» ведёт туда, откуда пришли ---

    async def test_back_returns_to_the_hub_when_entered_from_it(self):
        """Экран открывается из трёх мест; раньше «Назад» выбирал цель по роли
        и супер-админа из хаба выбрасывало в карточку дивизиона."""
        query = await self._run(admin_bind_division, f"admin_bind_div:{self.div_five_id}:h")

        self.assertEqual(self._back_cb(query), "admin_bind_hub")

    async def test_back_returns_to_the_division_card_when_entered_from_it(self):
        query = await self._run(admin_bind_division, f"admin_bind_div:{self.div_five_id}")

        self.assertEqual(self._back_cb(query), f"admin_div_view_{self.div_five_id}")

    async def test_the_hub_origin_survives_a_trip_into_a_club_card(self):
        """Метка обязана ехать через всю цепочку, иначе «Назад» теряет её на
        первом же клике по клубу."""
        idx = self.teams.index(self.free_club)

        clubs = await self._run(admin_bind_division, f"admin_bind_div:{self.div_five_id}:h")
        self.assertIn(f"admin_bind_club:{self.div_five_id}:{idx}:0:h", self._callbacks(clubs))

        card = await self._run(admin_bind_club_card, f"admin_bind_club:{self.div_five_id}:{idx}:0:h")
        callbacks = self._callbacks(card)
        self.assertIn(f"admin_bind_div:{self.div_five_id}:h", callbacks)
        self.assertIn(f"admin_bind_set:{self.div_five_id}:{idx}:{self.free_coach_id}:h", callbacks)

    async def test_the_hub_origin_survives_the_binding_itself(self):
        idx = self.teams.index(self.free_club)

        query = await self._run(
            admin_bind_execute, f"admin_bind_set:{self.div_five_id}:{idx}:{self.free_coach_id}:h"
        )

        self.assertEqual(database.get_user(self.free_coach_id)["team_name"], self.free_club)
        self.assertEqual(self._back_cb(query), "admin_bind_hub")

    async def test_division_admin_cannot_open_a_foreign_division(self):
        """callback_data подделывается руками, поэтому права проверяются на каждом шаге."""
        update, query = self._update(f"admin_bind_div:{self.div_four_id}")
        context = MagicMock()
        context.user_data = {}
        with patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.admin.is_global_admin", return_value=False), \
             patch("handlers.admin.database.get_admin_divisions",
                   return_value=[{"id": self.div_five_id}]):
            await admin_bind_division(update, context)

        query.edit_message_text.assert_not_called()


class TestDivisionCodeDrivesTheRoster(unittest.IsolatedAsyncioTestCase):
    """Код дивизиона — ключ к сезонному составу клубов, а не косметика.

    Промах по коду обнулял экран привязки: 16 клубов превращались в «(0/0)»,
    и понять, что сломалось, было нельзя.
    """

    async def asyncSetUp(self):
        database.init_db()
        database.ensure_canonical_divisions()
        self.admin_id = 999127
        self.orphan_code = f"DIV_{uuid.uuid4().hex[:4].upper()}"
        self.orphan_id = database.create_division(
            name=f"Сирота {uuid.uuid4().hex[:4]}", code=self.orphan_code
        )

    async def asyncTearDown(self):
        with database.transaction() as conn:
            conn.execute("DELETE FROM divisions WHERE id = ?", (self.orphan_id,))

    # --- генератор кода ---

    def test_code_transliterates_cyrillic_instead_of_dropping_it(self):
        """Выбрасывание кириллицы оставляло от названия пустоту и случайный код."""
        self.assertEqual(_division_code_from_name("Дивизион 6"), "DIVIZION6")
        self.assertEqual(_division_code_from_name("Премьер-Лига"), "PREMERLIGA")
        self.assertEqual(_division_code_from_name("Кубок Надежды"), "KUBOKNADEZHDY")

    def test_code_is_stable_and_bounded(self):
        """Один и тот же ввод даёт один и тот же код, длиной не больше колонки."""
        name = "Первый Дивизион Логова Фифарей"
        self.assertEqual(_division_code_from_name(name), _division_code_from_name(name))
        self.assertLessEqual(len(_division_code_from_name(name)), 16)
        self.assertEqual(_division_code_from_name("Division 1"), "DIVISION1")

    def test_a_nameless_code_still_falls_back_to_random(self):
        """Из «⚽⚽» транслитерировать нечего — код обязан остаться уникальным."""
        code = _division_code_from_name("⚽⚽")
        self.assertTrue(code.startswith("DIV_"))
        self.assertNotEqual(code, _division_code_from_name("⚽⚽"))

    # --- экран привязки без состава ---

    async def test_empty_binding_screen_names_the_code_that_missed(self):
        query = MagicMock()
        query.data = f"admin_bind_div:{self.orphan_id}"
        query.from_user.id = self.admin_id
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = self.admin_id
        context = MagicMock()
        context.user_data = {}

        with patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.admin.is_global_admin", return_value=True):
            await admin_bind_division(update, context)

        text = query.edit_message_text.call_args[0][0]
        # Админ должен увидеть, по какому именно коду состав не нашёлся.
        self.assertIn(self.orphan_code, text)
        self.assertIn("не привязан состав клубов", text)

    async def test_hub_flags_a_division_without_clubs(self):
        query = MagicMock()
        query.data = "admin_bind_hub"
        query.from_user.id = self.admin_id
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = self.admin_id
        context = MagicMock()
        context.user_data = {}

        with patch("handlers.base.is_admin", return_value=True), \
             patch("handlers.admin.is_admin", return_value=True), \
             patch("handlers.admin.is_global_admin", return_value=True):
            await admin_bind_hub(update, context)

        markup = query.edit_message_text.call_args[1]["reply_markup"]
        labels = {
            b.callback_data: b.text for row in markup.inline_keyboard for b in row
        }
        # «(0/0)» читалось как «клубы ещё не разобрали», а не как поломка.
        self.assertIn("⚠️ нет клубов", labels[f"admin_bind_div:{self.orphan_id}:h"])
        div_one_id = database.get_division_by_code("DIV_1")["id"]
        self.assertIn(f"/{len(config.DIVISION_CLUBS['DIV_1'])})", labels[f"admin_bind_div:{div_one_id}:h"])


if __name__ == "__main__":
    unittest.main()
