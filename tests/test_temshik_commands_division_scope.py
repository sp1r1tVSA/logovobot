"""Текстовые команды «Темшик ...» обязаны быть скоуплены по дивизиону.

Сезон 2 разнёс лигу на пять дивизионов, а справка и часть команд остались от единой
лиги: `бомбардиры`, `ассистенты` и `долги` ходили в базу вообще без `division_id`, а
админские `открыть/закрыть тур`, `дедлайн` и `линия` меняли статус тура сразу везде.
Здесь проверяется, что дивизион либо берётся из аргументов, либо резолвится из
контекста, а при полной неопределённости команда честно переспрашивает вместо того,
чтобы показать (или изменить) чужие данные.

Про «Темшик таблица» есть отдельный файл — tests/test_temshik_table_division_scope.py.
"""
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
from handlers.text_commands import (
    _match_division_in_args,
    handle_temshik_command,
)


def build_update(user_id: int, text: str):
    """Личка с ботом: дивизион резолвится из users.division_id либо из аргументов."""
    update = MagicMock()
    update.message.text = text
    update.message.message_thread_id = None
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    update.effective_message = update.message
    update.effective_user.id = user_id
    update.effective_user.username = "tester"
    update.effective_chat.id = user_id
    update.effective_chat.type = "private"
    return update


class TestDivisionArgumentParsing(unittest.TestCase):
    """Чистый парсер: дивизион из аргументов, не трогая базу."""

    DIVS = [
        {"id": 1, "name": "Дивизион 1", "code": "DIV_1"},
        {"id": 2, "name": "Дивизион 2", "code": "DIV_2"},
        {"id": 3, "name": "Дивизион 3", "code": "DIV_3"},
    ]

    def test_full_name_is_extracted_and_cut_out(self):
        self.assertEqual(_match_division_in_args("10 Дивизион 2", self.DIVS), (2, "10"))

    def test_code_in_both_spellings(self):
        self.assertEqual(_match_division_in_args("DIV_3", self.DIVS)[0], 3)
        self.assertEqual(_match_division_in_args("div3", self.DIVS)[0], 3)

    def test_keyword_with_number(self):
        self.assertEqual(_match_division_in_args("див 2", self.DIVS), (2, ""))
        self.assertEqual(_match_division_in_args("дивизион: 1", self.DIVS), (1, ""))

    def test_bare_number_is_not_a_division(self):
        """«бомбардиры 10» — это лимит, а не дивизион; угадывать тут нельзя."""
        self.assertEqual(_match_division_in_args("10", self.DIVS), (None, "10"))
        self.assertEqual(_match_division_in_args("тур 18", self.DIVS), (None, "тур 18"))

    def test_name_match_respects_word_boundaries(self):
        """«Дивизион 12» не должен схлопнуться до «Дивизион 1»."""
        self.assertEqual(_match_division_in_args("Дивизион 12", self.DIVS), (None, "Дивизион 12"))

    def test_round_number_survives_division_extraction(self):
        self.assertEqual(
            _match_division_in_args("тур 18 Дивизион 2", self.DIVS), (2, "тур 18")
        )
        self.assertEqual(
            _match_division_in_args("18 18.08 23:59 див 2", self.DIVS), (2, "18 18.08 23:59")
        )


class TestTemshikCommandsAreDivisionScoped(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()
        self.uid = uid

        self.div_a_id = database.create_division(name=f"CMD Альфа {uid}", code=f"CMDA_{uid}")
        self.div_b_id = database.create_division(name=f"CMD Бета {uid}", code=f"CMDB_{uid}")

        self.user_a, self.orphan = 97701, 97799
        database.register_user(self.user_a, f"cmd_a_{uid}", team_name=f"CMD Alpha {uid}")
        database.assign_user_division(self.user_a, self.div_a_id)

        database.register_user(self.orphan, f"cmd_orphan_{uid}", team_name=f"CMD Orphan {uid}")
        database.assign_user_division(self.orphan, None)

    async def asyncTearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "DELETE FROM users WHERE telegram_id IN (?, ?)", (self.user_a, self.orphan)
            )
            c.execute("DELETE FROM divisions WHERE id IN (?, ?)", (self.div_a_id, self.div_b_id))

    # ------------------------------------------------------------------ помощь

    async def test_help_explains_divisions_to_players(self):
        update = build_update(self.user_a, "Темшик помощь")
        with patch("handlers.text_commands.is_admin", return_value=False):
            handled = await handle_temshik_command(update, MagicMock())

        self.assertTrue(handled)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("дивизион", text.lower())
        self.assertIn("Темшик таблица [дивизион]", text)
        self.assertIn("Темшик бомбардиры [число] [дивизион]", text)
        self.assertIn("Темшик долги [дивизион]", text)
        self.assertIn("Темшик дивизионы", text)
        # Админского блока обычному участнику показывать не надо.
        self.assertNotIn("открыть линию", text)

    async def test_help_for_admin_lists_division_scoped_admin_commands(self):
        update = build_update(self.user_a, "Темшик команды")
        with patch("handlers.text_commands.is_admin", return_value=True):
            await handle_temshik_command(update, MagicMock())

        text = update.message.reply_text.call_args[0][0]
        self.assertIn("Темшик закрыть тур [номер] [дивизион]", text)
        self.assertNotIn("Темшик открыть тур", text)
        self.assertNotIn("Темшик дедлайн", text)
        self.assertNotIn("Темшик открыть линию", text)
        self.assertIn("Темшик топики [дивизион]", text)
        self.assertIn("/naznachit_topik", text)
        self.assertIn("/diviziony", text)

    async def test_divisions_command_lists_active_divisions(self):
        update = build_update(self.user_a, "Темшик дивизионы")
        with patch("handlers.text_commands.is_admin", return_value=False):
            handled = await handle_temshik_command(update, MagicMock())

        self.assertTrue(handled)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn(f"CMD Альфа {self.uid}", text)
        self.assertIn(f"CMD Бета {self.uid}", text)

    # -------------------------------------------------------- публичная статистика

    async def test_scorers_list_is_scoped_to_the_speakers_division(self):
        update = build_update(self.user_a, "Темшик бомбардиры 10")
        with patch("handlers.text_commands.is_admin", return_value=False), \
             patch("database.get_top_scorers", return_value=[]) as scorers:
            handled = await handle_temshik_command(update, MagicMock())

        self.assertTrue(handled)
        self.assertEqual(scorers.call_args.kwargs.get("division_id"), self.div_a_id)
        # Число рядом с командой — это лимит, а не номер дивизиона.
        self.assertEqual(scorers.call_args.args[0], 10)

    async def test_explicit_division_argument_beats_the_speakers_own_division(self):
        update = build_update(self.user_a, f"Темшик бомбардиры 5 CMD Бета {self.uid}")
        with patch("handlers.text_commands.is_admin", return_value=False), \
             patch("database.get_top_scorers", return_value=[]) as scorers:
            await handle_temshik_command(update, MagicMock())

        self.assertEqual(scorers.call_args.kwargs.get("division_id"), self.div_b_id)
        self.assertEqual(scorers.call_args.args[0], 5)

    async def test_scorers_graphic_card_also_carries_the_division(self):
        update = build_update(self.user_a, "Темшик бомбардиры")
        gen = MagicMock(return_value=b"png")
        with patch("handlers.text_commands.is_admin", return_value=False), \
             patch("services.graphics.top_stats_generator.generate_top_stats_image", gen):
            await handle_temshik_command(update, MagicMock())

        self.assertTrue(gen.called, "генератор карточки не был вызван")
        self.assertIn(self.div_a_id, gen.call_args.args)
        caption = update.message.reply_photo.call_args.kwargs["caption"]
        self.assertIn(f"CMD АЛЬФА {self.uid}", caption)

    async def test_assists_are_scoped_too(self):
        update = build_update(self.user_a, "Темшик ассистенты 7")
        with patch("handlers.text_commands.is_admin", return_value=False), \
             patch("database.get_top_assists", return_value=[]) as assists:
            await handle_temshik_command(update, MagicMock())

        self.assertEqual(assists.call_args.kwargs.get("division_id"), self.div_a_id)

    async def test_debts_are_scoped_and_name_the_division_when_empty(self):
        update = build_update(self.user_a, "Темшик долги")
        with patch("handlers.text_commands.is_admin", return_value=False), \
             patch("database.get_all_unplayed_league_matches", return_value=[]) as debts:
            handled = await handle_temshik_command(update, MagicMock())

        self.assertTrue(handled)
        self.assertEqual(debts.call_args.kwargs.get("division_id"), self.div_a_id)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn(f"CMD Альфа {self.uid}", text)
        self.assertIn("нет долгов", text)

    async def test_undetermined_division_asks_instead_of_showing_cross_division_data(self):
        update = build_update(self.orphan, "Темшик долги")
        with patch("handlers.text_commands.is_admin", return_value=False), \
             patch("database.get_all_unplayed_league_matches", return_value=[]) as debts:
            handled = await handle_temshik_command(update, MagicMock())

        self.assertTrue(handled)
        debts.assert_not_called()
        text = update.message.reply_text.call_args[0][0]
        self.assertIn("дивизион", text.lower())
        self.assertIn(f"CMD Альфа {self.uid}", text)  # подсказка перечисляет дивизионы

    # ------------------------------------------------------------- админские туры

    async def test_close_round_uses_the_explicit_division(self):
        """Закрытие переводит матчи в долг — команда показывает подтверждение, а не закрывает."""
        update = build_update(self.user_a, f"Темшик закрыть тур 18 CMD Бета {self.uid}")
        with patch("handlers.text_commands.is_admin", return_value=True), \
             patch("database.update_round_status") as upd, \
             patch("database.close_round") as close, \
             patch("database.preview_close_round", wraps=database.preview_close_round) as prev:
            handled = await handle_temshik_command(update, MagicMock())

        self.assertTrue(handled)
        upd.assert_not_called()
        close.assert_not_called()
        self.assertEqual(prev.call_args.args[:2], (18, self.div_b_id))
        kwargs = update.message.reply_text.call_args.kwargs
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, f"admin_div_round_close_ok:{self.div_b_id}:18")
        self.assertIn(f"CMD Бета {self.uid}", update.message.reply_text.call_args[0][0])

    async def test_close_round_is_scoped(self):
        update = build_update(self.user_a, "Темшик закрыть тур 4")
        with patch("handlers.text_commands.is_admin", return_value=True), \
             patch("database.preview_close_round", wraps=database.preview_close_round) as prev:
            await handle_temshik_command(update, MagicMock())

        self.assertEqual(prev.call_args.args[:2], (4, self.div_a_id))

    async def test_close_round_without_any_division_refuses(self):
        """Без дивизиона тур не закрываем нигде — это запись, а не чтение."""
        update = build_update(self.orphan, "Темшик закрыть тур 4")
        with patch("handlers.text_commands.is_admin", return_value=True), \
             patch("database.update_round_status") as upd:
            await handle_temshik_command(update, MagicMock())

        upd.assert_not_called()
        self.assertIn("дивизион", update.message.reply_text.call_args[0][0].lower())

    # ------------------------------------------------------------------- топики

    async def test_topics_command_reports_division_topic_setup(self):
        update = build_update(self.user_a, "Темшик топики")
        with patch("handlers.text_commands.is_admin", return_value=True), \
             patch("services.topic_cache.topic_cache.get_division_topics_summary",
                   return_value={"draft": {"group_chat_id": -100, "message_thread_id": 77}}):
            handled = await handle_temshik_command(update, MagicMock())

        self.assertTrue(handled)
        text = update.message.reply_text.call_args[0][0]
        self.assertIn(f"CMD АЛЬФА {self.uid}", text)
        self.assertIn("77", text)
        self.assertIn("❌", text)  # остальные топики не привязаны
        self.assertIn(f"/naznachit_topik {self.div_a_id}", text)

    async def test_topics_command_is_admin_only(self):
        update = build_update(self.user_a, "Темшик топики")
        with patch("handlers.text_commands.is_admin", return_value=False):
            await handle_temshik_command(update, MagicMock())

        self.assertIn("администратор", update.message.reply_text.call_args[0][0].lower())


if __name__ == "__main__":
    unittest.main()
