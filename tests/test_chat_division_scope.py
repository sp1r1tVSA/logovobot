"""Изоляция контекста Темшика по дивизиону.

Промты переписаны под дивизионную структуру, поэтому контекст обязан быть скоуплен:
в него попадают таблица, форма, расписание, бомбардиры и составы ТОЛЬКО того дивизиона,
в котором идёт разговор. Данные соседнего дивизиона не должны утекать ни при каких условиях.
"""
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers.chat import handle_ai_chat


class TestChatDivisionScope(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.uid = uid

        self.div_a_id = database.create_division(name=f"CHT Альфа {uid}", code=f"CHTA_{uid}")
        self.div_b_id = database.create_division(name=f"CHT Бета {uid}", code=f"CHTB_{uid}")

        self.user_a1, self.user_a2, self.user_b1, self.user_b2 = 97401, 97402, 97411, 97412
        self.team_a1 = f"CHT Alpha One {uid}"
        self.team_a2 = f"CHT Alpha Two {uid}"
        self.team_b1 = f"CHT Beta One {uid}"
        self.team_b2 = f"CHT Beta Two {uid}"

        for tg_id, nick, team, div in (
            (self.user_a1, f"cht_a1_{uid}", self.team_a1, self.div_a_id),
            (self.user_a2, f"cht_a2_{uid}", self.team_a2, self.div_a_id),
            (self.user_b1, f"cht_b1_{uid}", self.team_b1, self.div_b_id),
            (self.user_b2, f"cht_b2_{uid}", self.team_b2, self.div_b_id),
        ):
            database.register_user(tg_id, nick, team_name=team)
            database.assign_user_division(tg_id, div)

        season = database.get_active_season()
        self.season_id = season["id"] if season else 1

        with database.transaction() as conn:
            c = conn.cursor()
            for div_id in (self.div_a_id, self.div_b_id):
                c.execute(
                    "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (1, 1, ?, ?)",
                    ("01.01.2030 00:00", div_id),
                )
            # Сыгранный матч в каждом дивизионе.
            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, "
                "player1_score, player2_score, status, division_id, season_id, tournament_type) "
                "VALUES (1, ?, ?, ?, ?, 3, 1, 'confirmed', ?, ?, 'league')",
                (self.user_a1, self.user_a2, self.team_a1, self.team_a2, self.div_a_id, self.season_id),
            )
            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, "
                "player1_score, player2_score, status, division_id, season_id, tournament_type) "
                "VALUES (1, ?, ?, ?, ?, 5, 0, 'confirmed', ?, ?, 'league')",
                (self.user_b1, self.user_b2, self.team_b1, self.team_b2, self.div_b_id, self.season_id),
            )

    async def asyncTearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM matches WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute("DELETE FROM rounds WHERE division_id IN (?, ?)", (self.div_a_id, self.div_b_id))
            c.execute(
                "DELETE FROM users WHERE telegram_id IN (?, ?, ?, ?)",
                (self.user_a1, self.user_a2, self.user_b1, self.user_b2),
            )
            c.execute("DELETE FROM divisions WHERE id IN (?, ?)", (self.div_a_id, self.div_b_id))

    def _build_update(self, user_id: int):
        """Личка с ботом: дивизион резолвится из users.division_id."""
        update = MagicMock()
        update.message.text = "Темшик какие у меня шансы"
        update.message.voice = None
        update.message.reply_to_message = None
        update.message.message_thread_id = None
        update.message.reply_text = AsyncMock()
        update.effective_message = update.message
        update.effective_user.id = user_id
        update.effective_user.username = "tester"
        update.effective_chat.id = user_id
        update.effective_chat.type = "private"
        return update

    async def _capture_context(self, user_id: int) -> str:
        """Прогнать хендлер и вернуть context_data, ушедший в модель."""
        update = self._build_update(user_id)
        ctx = MagicMock()
        ctx.bot.id = 999
        ctx.bot.send_chat_action = AsyncMock()

        with patch("handlers.chat.handle_temshik_command", new=AsyncMock(return_value=False)), \
             patch("handlers.chat.ai_chat.generate_chat_reply", return_value="ok") as gen:
            await handle_ai_chat(update, ctx)

        self.assertTrue(gen.called, "generate_chat_reply не был вызван")
        return gen.call_args[0][3]

    async def test_context_contains_only_own_division(self):
        """Тренер дивизиона А не видит клубов дивизиона Б."""
        ctx_a = await self._capture_context(self.user_a1)

        self.assertIn(self.team_a1, ctx_a)
        self.assertIn(self.team_a2, ctx_a)
        self.assertNotIn(self.team_b1, ctx_a)
        self.assertNotIn(self.team_b2, ctx_a)

    async def test_context_is_symmetric_for_other_division(self):
        """Зеркальная проверка: тренер дивизиона Б не видит клубов дивизиона А."""
        ctx_b = await self._capture_context(self.user_b1)

        self.assertIn(self.team_b1, ctx_b)
        self.assertIn(self.team_b2, ctx_b)
        self.assertNotIn(self.team_a1, ctx_b)
        self.assertNotIn(self.team_a2, ctx_b)

    async def test_context_names_division_and_promotion_rules(self):
        """В контекст попадают имя дивизиона и правила повышения/вылета."""
        ctx_a = await self._capture_context(self.user_a1)

        self.assertIn(f"CHT Альфа {self.uid}", ctx_a)
        self.assertIn("СТРУКТУРА ТУРНИРА", ctx_a)
        self.assertTrue(
            "повышени" in ctx_a.lower() or "вылет" in ctx_a.lower(),
            "В контексте нет упоминания повышения/вылета",
        )

    async def test_archive_is_marked_as_history(self):
        """Старая единая лига подаётся как архив, а не как текущая таблица."""
        ctx_a = await self._capture_context(self.user_a1)

        self.assertIn("АРХИВ", ctx_a)
        self.assertIn("ДО ДИВИЗИОНОВ", ctx_a.upper())

    async def test_user_without_division_gets_no_tournament_data(self):
        """Без дивизиона турнирных данных не отдаём вовсе — вместо каши из всех дивизионов."""
        orphan_id = 97499
        database.register_user(orphan_id, f"cht_orphan_{self.uid}", team_name=f"CHT Orphan {self.uid}")
        database.assign_user_division(orphan_id, None)
        try:
            ctx = await self._capture_context(orphan_id)

            self.assertIn("НЕ приписан", ctx)
            for team in (self.team_a1, self.team_a2, self.team_b1, self.team_b2):
                self.assertNotIn(team, ctx)
        finally:
            with database.transaction() as conn:
                conn.cursor().execute("DELETE FROM users WHERE telegram_id = ?", (orphan_id,))

    async def test_recent_matches_are_division_scoped(self):
        """Счёт чужого дивизиона не утекает в блок последних матчей."""
        ctx_a = await self._capture_context(self.user_a1)

        self.assertIn("3 : 1", ctx_a)
        self.assertNotIn("5 : 0", ctx_a)

    async def test_structure_reports_division_size(self):
        """Модель должна знать точный размер дивизиона, а не гадать про «16 клубов»."""
        ctx_a = await self._capture_context(self.user_a1)

        self.assertIn("Клубов в этом дивизионе: 2", ctx_a)

    async def test_club_names_are_not_swapped_for_registry_names(self):
        """Клуб вне реестра обязан приехать в контекст под своим именем.

        resolve_team_name фуззи-матчит против config.CLUB_REGISTRY, поэтому клуб,
        которого там нет, рискует подмениться похожим каноном — и тренер увидит
        в ответе бота чужое название.
        """
        import config

        ctx_a = await self._capture_context(self.user_a1)

        self.assertIn(f". {self.team_a1} (@", ctx_a)
        self.assertIn(f". {self.team_a2} (@", ctx_a)
        for canon in config.CLUB_REGISTRY:
            self.assertNotIn(
                f". {canon} (@",
                ctx_a,
                f"Клуб дивизиона подменён каноническим именем реестра: {canon}",
            )

    async def test_form_rides_on_the_standings_line(self):
        """Форма стоит в строке таблицы, а не отдельным списком всех клубов ещё раз."""
        ctx_a = await self._capture_context(self.user_a1)

        line_a1 = next(l for l in ctx_a.splitlines() if f". {self.team_a1} (@" in l)
        self.assertTrue(line_a1.rstrip(" 🚀🔻").endswith("W"), line_a1)
        self.assertEqual(ctx_a.count(f"{self.team_a1} (@"), 1)

    async def test_squads_are_limited_to_the_relevant_clubs(self):
        """Составов в промте — клуб собеседника и упомянутые клубы, а не весь дивизион."""
        squads = {
            self.team_a1: ["Own Striker"],
            self.team_a2: ["Rival Keeper"],
        }
        with patch("handlers.chat.database.get_all_squads", return_value=squads):
            ctx_plain = await self._capture_context(self.user_a1)
            self.assertIn("Own Striker", ctx_plain)
            self.assertNotIn("Rival Keeper", ctx_plain)

            update = self._build_update(self.user_a1)
            update.message.text = f"Темшик кто лучший в {self.team_a2}"
            ctx = MagicMock()
            ctx.bot.id = 999
            ctx.bot.send_chat_action = AsyncMock()
            with patch("handlers.chat.handle_temshik_command", new=AsyncMock(return_value=False)),                  patch("handlers.chat.ai_chat.generate_chat_reply", return_value="ok") as gen:
                await handle_ai_chat(update, ctx)
            self.assertIn("Rival Keeper", gen.call_args[0][3])

    async def test_schedule_keeps_own_matches_and_the_nearest_round(self):
        """Расписание: ближайший открытый тур целиком плюс матчи собеседника из дальних туров."""
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (2, 1, ?, ?)",
                ("08.01.2030 00:00", self.div_a_id),
            )
            c.execute(
                "INSERT INTO rounds (round_number, is_open, deadline, division_id) VALUES (3, 1, ?, ?)",
                ("15.01.2030 00:00", self.div_a_id),
            )
            for rnd in (2, 3):
                c.execute(
                    "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, "
                    "status, division_id, season_id, tournament_type) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 'league')",
                    (rnd, self.user_a1, self.user_a2, self.team_a1, self.team_a2, self.div_a_id, self.season_id),
                )
        ctx_own = await self._capture_context(self.user_a1)
        self.assertIn("Тур 2:", ctx_own)
        self.assertIn("Тур 3:", ctx_own)

        # Тренер, у которого нет матчей в дальних турах, видит только ближайший.
        outsider = 97403
        database.register_user(outsider, f"cht_a3_{self.uid}", team_name=f"CHT Alpha Three {self.uid}")
        database.assign_user_division(outsider, self.div_a_id)
        try:
            ctx_other = await self._capture_context(outsider)
            self.assertIn("Тур 2:", ctx_other)
            self.assertNotIn("Тур 3:", ctx_other)
        finally:
            with database.transaction() as conn:
                conn.cursor().execute("DELETE FROM users WHERE telegram_id = ?", (outsider,))


class TestTrimToLastSentence(unittest.TestCase):
    def test_cut_tail_is_dropped(self):
        from services.ai.ai_chat import _trim_to_last_sentence

        self.assertEqual(
            _trim_to_last_sentence("Шансы есть. Бери баньку и вперёд! А потом ещё надо бы"),
            "Шансы есть. Бери баньку и вперёд!",
        )

    def test_run_on_sentence_gets_an_ellipsis(self):
        from services.ai.ai_chat import _trim_to_last_sentence

        self.assertEqual(_trim_to_last_sentence("Братан, тут такое дело, что"), "Братан, тут такое дело, что…")


if __name__ == "__main__":
    unittest.main()
