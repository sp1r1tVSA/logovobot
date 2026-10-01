"""«Темшик долги Ренн» — долги одного клуба.

Название клуба пишут люди: с падежом («Ренна», «Валенсии»), без «Аль-»,
с опечатками. `club_registry.resolve_club_query` должна узнать клуб, а при
неоднозначности — переспросить с подсказками, а не показать чужой список.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from club_registry import ClubQuery, resolve_club_query
from handlers.text_commands import _clean_club_query, handle_temshik_command


def build_update(user_id: int, text: str):
    update = MagicMock()
    update.message.text = text
    update.message.message_thread_id = None
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    update.effective_user.id = user_id
    update.effective_user.username = "tester"
    update.effective_chat.id = user_id
    update.effective_chat.type = "private"
    return update


class TestResolveClubQuery(unittest.TestCase):

    def assertClub(self, text, club):
        self.assertEqual(resolve_club_query(text).canonical, club, text)

    def test_exact_and_alias(self):
        self.assertClub("Ренн", "Ренн")
        self.assertClub("Аль-Кадисия", "Аль-Кадисия")
        self.assertClub("аль кадисия", "Аль-Кадисия")
        self.assertClub("псж", "ПСЖ")

    def test_declension(self):
        for text, club in [
            ("Ренна", "Ренн"), ("Ренну", "Ренн"), ("Валенсии", "Валенсия"),
            ("Бенфики", "Бенфика"), ("Спортинга", "Спортинг"), ("Ниццы", "Ницца"),
            ("Ривер Плейта", "Ривер Плейт"), ("Лос Анджелеса", "Лос Анджелес"), ("Лилля", "Лилль"),
        ]:
            self.assertClub(text, club)

    def test_without_article_and_typos(self):
        self.assertClub("Кадисия", "Аль-Кадисия")
        self.assertClub("кадисии", "Аль-Кадисия")
        self.assertClub("Кадисиия", "Аль-Кадисия")
        self.assertClub("Рэнн", "Ренн")
        self.assertClub("Хофенхайм", "Хоффенхайм")
        self.assertClub("Волверхэмптон", "Вулверхэмптон")
        self.assertClub("Тоттенхема", "Тоттенхэм")

    def test_ambiguous_names_ask_back(self):
        for text, expected in [
            ("Реал", {"Реал Мадрид", "Реал Сосьедад"}),
            ("Реала", {"Реал Мадрид", "Реал Сосьедад"}),
            ("Интер", {"Интер Милан", "Интер Майами"}),
            ("Манчестер", {"Манчестер Сити", "Манчестер Юнайтед"}),
        ]:
            result = resolve_club_query(text)
            self.assertIsNone(result.canonical, text)
            self.assertEqual(set(result.suggestions), expected, text)

    def test_empty_input(self):
        self.assertEqual(resolve_club_query(""), ClubQuery(None))
        self.assertEqual(resolve_club_query(None), ClubQuery(None))

    def test_clean_club_query_drops_fillers(self):
        self.assertEqual(_clean_club_query("у Ренна"), "Ренна")
        self.assertEqual(_clean_club_query("клуба «Аль-Кадисия»!"), "Аль-Кадисия")
        self.assertEqual(_clean_club_query("  "), "")


def _debt(rnd, t1, t2, u1=None, u2=None):
    return {"round_number": rnd, "player1_team": t1, "player2_team": t2,
            "p1_username": u1, "p2_username": u2}


DEBTS = [
    _debt(3, "Ренн", "Лидс", "renn_coach", "leeds_coach"),
    _debt(4, "Ницца", "Ренн", "nice_coach", "renn_coach"),
    _debt(4, "Аль-Кадисия", "Ланс", "qad_coach", "lens_coach"),
    _debt(5, "Порту", "Лацио"),
]


class TestClubDebtsCommand(unittest.IsolatedAsyncioTestCase):

    async def _run(self, text, debts=DEBTS, user_team=None):
        update = build_update(990_000_123, text)
        with patch("handlers.text_commands.is_admin", return_value=False), \
             patch("handlers.text_commands.resolve_division_id", return_value=None), \
             patch("database.get_active_divisions", return_value=[]), \
             patch("database.get_user_team", return_value=user_team), \
             patch("database.get_all_unplayed_league_matches", return_value=debts) as unplayed:
            handled = await handle_temshik_command(update, MagicMock())
        self.assertTrue(handled)
        return update.message.reply_text.call_args[0][0], unplayed

    async def test_only_the_named_club_is_listed(self):
        text, unplayed = await self._run("Темшик долги Ренна")
        unplayed.assert_called_once_with()  # по всем дивизионам
        self.assertIn("ДОЛГИ КЛУБА РЕНН", text)
        self.assertIn("2 матча", text)
        self.assertIn("Лидс", text)
        self.assertIn("Ницца", text)
        self.assertNotIn("Кадисия", text)
        self.assertNotIn("Порту", text)

    async def test_example_from_the_chat(self):
        text, _ = await self._run("Темшик долги Аль-Кадисия")
        self.assertIn("ДОЛГИ КЛУБА АЛЬ-КАДИСИЯ", text)
        self.assertIn("1 матч", text)
        self.assertNotIn("Ренн", text)

    async def test_club_without_debts(self):
        text, _ = await self._run("Темшик долги Порту", debts=DEBTS[:3])
        self.assertIn("У клуба Порту нет долгов", text)

    async def test_ambiguous_club_suggests(self):
        text, unplayed = await self._run("Темшик долги Реал")
        unplayed.assert_not_called()
        self.assertIn("Не нашёл клуб", text)
        self.assertIn("Темшик долги Реал Мадрид", text)
        self.assertIn("Темшик долги Реал Сосьедад", text)

    async def test_own_club(self):
        text, _ = await self._run("Темшик долги мои", user_team="Ренн")
        self.assertIn("ДОЛГИ КЛУБА РЕНН", text)

    async def test_own_club_without_a_club(self):
        text, unplayed = await self._run("Темшик долги мои", user_team=None)
        unplayed.assert_not_called()
        self.assertIn("не закреплён клуб", text)


if __name__ == "__main__":
    unittest.main()
