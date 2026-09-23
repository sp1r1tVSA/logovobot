"""
tests/test_fetch_squad_photos.py

Тесты сопоставления игроков и обработки исключений (перешедшие игроки, прозвища,
транслитерация и OCR-опечатки) для scripts/fetch_squad_photos.py.
"""

import unittest
from scripts.fetch_squad_photos import (
    _norm,
    match_in_roster,
    PLAYER_NAME_OVERRIDES,
    PINNED_PLAYERS,
    process_club,
)


class TestFetchSquadPhotosOverrides(unittest.TestCase):
    def test_player_name_overrides_exist(self):
        """Все 14 игроков с альтернативными написаниями присутствуют в PLAYER_NAME_OVERRIDES."""
        expected_keys = [
            "nacho fernandez",
            "abner vinicius",
            "pedro goncalves",
            "desmet",
            "gorrotxa",
            "al juwair",
            "thekri",
            "al shanqeeti",
            "batagov",
            "doechi",
            "boushal",
            "balobaid",
            "gamer",
            "urion",
        ]
        for key in expected_keys:
            self.assertIn(key, PLAYER_NAME_OVERRIDES, f"Отсутствует override для {key}")

    def test_pinned_players_exist(self):
        """Все 14 перешедших игроков зафиксированы в PINNED_PLAYERS с валидными ID."""
        expected_pinned = [
            ("Аталанта", "bakker"),
            ("Атлетик Бильбао", "boiro"),
            ("Атлетик Бильбао", "gorosabel"),
            ("Бешикташ", "hadziahmetovic"),
            ("Бурирам", "toku"),
            ("Бурирам", "ko myeong seok"),
            ("Вулверхэмптон", "arias"),
            ("Вулверхэмптон", "joao gomes"),
            ("Интер Майами", "allen"),
            ("Майнц", "hong hyeon seok"),
            ("Ренн", "seidu"),
            ("Трабзонспор", "lundstram"),
            ("Хоффенхайм", "akpoguma"),
            ("Эвертон", "patterson"),
        ]
        for club, norm_name in expected_pinned:
            self.assertIn((club, norm_name), PINNED_PLAYERS, f"Отсутствует PINNED_PLAYERS для {club}, {norm_name}")
            info = PINNED_PLAYERS[(club, norm_name)]
            self.assertTrue(isinstance(info.get("id"), int) and info["id"] > 0)
            self.assertTrue(bool(info.get("name")))

    def test_match_in_roster_with_overrides(self):
        """Игроки с прозвищами и опечатками находят правильных игроков в ростере клуба."""
        # 1. NACHO FERNÁNDEZ -> Nacho
        roster_nacho = [{"id": 213499, "name": "Nacho", "positions": ["CB"], "rating": 6.8, "value": 500000}]
        identity, why = match_in_roster("NACHO FERNÁNDEZ", "CB", roster_nacho)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["id"], 213499)

        # 2. ABNER VINÍCIUS -> Abner
        roster_abner = [{"id": 1060604, "name": "Abner", "positions": ["LB"], "rating": 6.9, "value": 6000000}]
        identity, why = match_in_roster("ABNER VINÍCIUS", "LB", roster_abner)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["id"], 1060604)

        # 3. PEDRO GONÇALVES -> Pote
        roster_pote = [{"id": 875133, "name": "Pote", "positions": ["CAM"], "rating": 7.3, "value": 22000000}]
        identity, why = match_in_roster("PEDRO GONÇALVES", "CAM", roster_pote)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["id"], 875133)

        # 4. GAMER -> James Garner (OCR typo)
        roster_garner = [{"id": 950474, "name": "James Garner", "positions": ["CDM"], "rating": 7.1, "value": 30000000}]
        identity, why = match_in_roster("GAMER", "CDM", roster_garner)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["id"], 950474)

        # 5. Urión -> Ezequiel Centurión (OCR truncation)
        roster_centurion = [{"id": 971901, "name": "Ezequiel Centurión", "positions": ["GK"], "rating": 6.5, "value": 900000}]
        identity, why = match_in_roster("Urión", "ST", roster_centurion)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["id"], 971901)


if __name__ == "__main__":
    unittest.main()
