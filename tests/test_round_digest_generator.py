"""
Рендер картинки «Итоги тура» для топика АНАЛИТИКА.

Проверяем, что генератор переживает любой payload из
services.round_preview.build_digest_payload — в том числе вырожденные
(тур без матчей, без игрока тура, без движения по таблице).
"""

import io
import unittest

from PIL import Image

from services.graphics.round_digest_generator import generate_round_digest_image


def _full_payload() -> dict:
    return {
        "kind": "digest",
        "division_id": 1,
        "division_name": "Логово Фифарей (Основная Лига)",
        "round_number": 7,
        "results": [
            {"match_id": 101, "team1": "Порту", "team2": "Бенфика", "score1": 4, "score2": 0, "margin": 4, "total_goals": 4},
            {"match_id": 102, "team1": "Аякс", "team2": "ПСВ", "score1": 2, "score2": 2, "margin": 0, "total_goals": 4},
            {"match_id": 103, "team1": "Селтик", "team2": "Рейнджерс", "score1": 1, "score2": 0, "margin": 1, "total_goals": 1},
        ],
        "matches_total": 3,
        "matches_played": 3,
        "goals_total": 9,
        "rout": {"match_id": 101, "team1": "Порту", "team2": "Бенфика", "score1": 4, "score2": 0, "margin": 4, "total_goals": 4},
        "player_of_the_round": {"player_name": "David Neres", "team_name": "Порту", "goals": 3, "assists": 1},
        "table": [
            {"position": 1, "team": "Порту", "played": 7, "points": 17, "goals_scored": 20, "goals_conceded": 6, "previous_position": 3, "movement": 2},
            {"position": 2, "team": "Аякс", "played": 7, "points": 15, "goals_scored": 14, "goals_conceded": 9, "previous_position": 1, "movement": -1},
        ],
        "movers": [
            {"position": 1, "team": "Порту", "points": 17, "previous_position": 3, "movement": 2},
            {"position": 2, "team": "Аякс", "points": 15, "previous_position": 1, "movement": -1},
        ],
        "leader": {"position": 1, "team": "Порту", "points": 17},
    }


class TestRoundDigestGenerator(unittest.TestCase):
    def _render(self, payload):
        buf = generate_round_digest_image(payload)
        self.assertIsInstance(buf, io.BytesIO)
        data = buf.getvalue()
        self.assertGreater(len(data), 1000, "Картинка подозрительно маленькая")
        img = Image.open(io.BytesIO(data))
        img.load()
        self.assertEqual(img.format, "PNG")
        return img

    def test_full_payload_renders(self):
        img = self._render(_full_payload())
        self.assertEqual(img.width, 1000)
        self.assertGreater(img.height, 200)

    def test_taller_when_there_are_more_results(self):
        small = self._render(_full_payload())

        payload = _full_payload()
        payload["results"] = payload["results"] * 3
        big = self._render(payload)

        self.assertGreater(big.height, small.height)

    def test_round_without_highlights_renders(self):
        payload = _full_payload()
        payload["rout"] = None
        payload["player_of_the_round"] = None
        payload["movers"] = []
        self._render(payload)

    def test_empty_round_renders_a_placeholder(self):
        payload = _full_payload()
        payload.update({
            "results": [],
            "matches_played": 0,
            "goals_total": 0,
            "rout": None,
            "player_of_the_round": None,
            "table": [],
            "movers": [],
            "leader": None,
        })
        self._render(payload)

    def test_crowned_matches_render(self):
        """👑 в строке матча: имя любой длины и матч без короны рядом."""
        payload = _full_payload()
        payload["results"][0]["mvp_player"] = "David Neres"
        payload["results"][1]["mvp_player"] = "Имя Которое Никуда Не Влезает По Ширине Строки"
        payload["results"][2]["mvp_player"] = None
        payload["mvp_of_the_round"] = {"player_name": "David Neres", "mvp_count": 1}
        crowned = self._render(payload)

        # Короны живут внутри существующей строки — картинка не растёт.
        self.assertEqual(crowned.height, self._render(_full_payload()).height)

    def test_unknown_clubs_do_not_break_the_render(self):
        """Эмблемы нет — бейдж рисуется пустым, но картинка всё равно выходит."""
        payload = _full_payload()
        payload["results"] = [
            {"match_id": 1, "team1": "Неизвестный Клуб", "team2": "Ещё Один", "score1": 0, "score2": 3, "margin": 3, "total_goals": 3}
        ]
        payload["rout"] = payload["results"][0]
        self._render(payload)


if __name__ == "__main__":
    unittest.main()
