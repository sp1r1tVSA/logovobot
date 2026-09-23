"""Картинка символической сборной: размер, пустой блок, работа без фото и без сети."""

import io
import unittest
from unittest.mock import patch

from PIL import Image

from services.graphics import totw_generator
from services.graphics.totw_generator import generate_totw_image
from services.totw_service import build_totw_lineup


def _cand(name, team, position, **stats):
    base = {
        "player_name": name, "team_name": team, "position": position, "is_starter": True,
        "goals": 0, "assists": 0, "mvp": 0, "braces": 0,
        "matches": 5, "wins": 2, "clean_sheets": 1, "goals_conceded": 5,
    }
    base.update(stats)
    return base


def _payload(candidates):
    return {
        "division_id": 1, "division_name": "Дивизион 1", "division_code": "DIV_1",
        "season_id": 1, "season_name": "Сезон 26/27",
        "start_round": 1, "end_round": 5, "candidates_count": len(candidates),
        **build_totw_lineup(candidates),
    }


def _full_payload():
    positions = ["GK", "LB", "CB", "CB", "RB", "CDM", "CM", "CAM", "LW", "ST", "RW",
                 "GK", "CB", "CM", "ST"]
    return _payload([
        _cand(f"Игрок Очень-Длинная-Фамилия {i}", f"Клуб {i // 2}", pos, goals=i % 4, assists=i % 3, mvp=i % 2)
        for i, pos in enumerate(positions)
    ])


class TestTotwImage(unittest.TestCase):
    def _open(self, buf):
        self.assertIsInstance(buf, io.BytesIO)
        img = Image.open(buf)
        img.load()
        return img

    def test_retina_size(self):
        img = self._open(generate_totw_image(_full_payload(), 1, 1, 5))
        self.assertEqual(img.size, (2400, 3300))
        self.assertEqual(img.format, "PNG")

    def test_empty_block_still_renders(self):
        img = self._open(generate_totw_image(_payload([]), 1, 1, 5))
        self.assertEqual(img.size, (2400, 3300))

    def test_no_photos_and_no_network_by_default(self):
        with patch.object(totw_generator, "_load_photo", return_value=None) as load, \
                patch("services.graphics.player_photos.get_player_photo") as fetch:
            img = self._open(generate_totw_image(_full_payload(), 1, 1, 5))
        self.assertEqual(img.size, (2400, 3300))
        self.assertTrue(load.called)
        fetch.assert_not_called()

    def test_partial_xi_and_single_round(self):
        payload = _payload([_cand("Один", "К", "ST", goals=2), _cand("Кипер", "Л", "GK")])
        payload["start_round"] = payload["end_round"] = 7
        img = self._open(generate_totw_image(payload, 1, 7, 7))
        self.assertEqual(img.size, (2400, 3300))

    def test_prefetch_skips_cached_players(self):
        payload = _full_payload()
        with patch("services.graphics.player_photos.get_photo_path", return_value="cached.png"), \
                patch("services.graphics.player_photos.get_player_photo") as fetch:
            fetched = totw_generator.prefetch_photos(payload)
        # Все уже в кэше: в сеть никто не ходит, а в ответе — все с фото.
        self.assertEqual(fetched, len(payload["xi"]) + len(payload["bench"]))
        fetch.assert_not_called()


class TestTotwFont(unittest.TestCase):
    def test_bundled_condensed_font_is_used(self):
        # Шрифт карточек едет в репозитории: без него сервер без системных шрифтов
        # откатывается на широкий DejaVu Sans Bold, и имена обрезаются.
        font = totw_generator._display_font(40)
        self.assertEqual(getattr(font, "path", None), totw_generator.DISPLAY_FONT_PATH)

    def test_bundled_font_covers_cyrillic_and_diacritics(self):
        # Глиф, которого в шрифте нет, рисуется «тофу» (.notdef) — сравниваем с ним.
        font = totw_generator._display_font(40)
        tofu = bytes(font.getmask("￿"))
        for ch in "РУСЛАНЁЖЩÖØÇÉÍ":
            self.assertNotEqual(bytes(font.getmask(ch)), tofu, ch)


if __name__ == "__main__":
    unittest.main()
