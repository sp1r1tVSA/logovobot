"""
Выбор источника фотографии в `services/graphics/player_photos.py`.

Провайдеры перебираются по качеству, но порядок не должен перебивать главное:
карточки рисуют игрока поверх фона, поэтому прозрачная вырезка у четвёртого
источника лучше плоского портрета у первого. Плоская картинка пишется только
когда вырезки нет ни у кого.

Эта цепочка — путь без клуба. С клубом игрок опознаётся в ростере
(`player_identity`), и глобальный поиск по имени не используется даже как
запасной: см. `TestFetchWithClub`.

Сеть не используется: провайдеры и загрузка замоканы, кэш уводится в tmp.
"""

import io
import os
import random
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from services.graphics import player_identity as pi
from services.graphics import player_photos as pp


def _png_bytes(size=(500, 500), mode="RGBA") -> bytes:
    """
    Шумная картинка: однотонная сжалась бы в пару сотен байт и не прошла бы
    порог `MIN_PHOTO_BYTES`, который отсекает заглушки провайдеров.
    """
    rng = random.Random(size[0] * 31 + len(mode))
    im = Image.new(mode, size)
    bands = len(im.getbands())
    im.putdata([tuple(rng.randrange(256) for _ in range(bands)) for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


CUTOUT = _png_bytes()
FLAT = _png_bytes(mode="RGB")
TINY = _png_bytes(size=(200, 200))
JUNK = b"x" * 100


class TestPhotoQuality(unittest.TestCase):
    def test_detects_cutout(self):
        self.assertEqual(pp._photo_quality(CUTOUT), "cutout")

    def test_detects_flat(self):
        self.assertEqual(pp._photo_quality(FLAT), "flat")

    def test_detects_palette_transparency(self):
        """У палитровых PNG альфа живёт в `info`, а не в `getbands()`."""
        with Image.open(io.BytesIO(FLAT)) as src:
            palette = src.convert("P")
        buf = io.BytesIO()
        palette.save(buf, format="PNG", transparency=0)
        data = buf.getvalue()
        with Image.open(io.BytesIO(data)) as check:
            self.assertNotIn("A", check.getbands())  # альфы в bands нет
        self.assertEqual(pp._photo_quality(data), "cutout")

    def test_rejects_too_small(self):
        self.assertIsNone(pp._photo_quality(TINY))

    def test_rejects_junk(self):
        self.assertIsNone(pp._photo_quality(JUNK))
        self.assertIsNone(pp._photo_quality(b"\x89PNG" + b"0" * 5000))


class TestFetchPrefersCutout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patchers = [
            patch.object(pp, "PHOTOS_DIR", self._tmp.name),
            # Имя уже латинское — Википедию для разрешения не трогаем.
            patch.object(pp, "_resolve_latin_name", side_effect=lambda name, team=None: name),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def _run(self, payloads: dict[str, bytes | None]):
        """
        Прогоняет `fetch_and_cache` с заданными ответами провайдеров.

        `payloads` — байты по имени провайдера; `None` означает, что провайдер
        вообще не нашёл URL. Возвращает `(путь, порядок скачиваний)`.
        """
        downloaded = []

        def fake_url(provider):
            return lambda name: f"https://{provider}.test/{name}.png" if payloads.get(provider) else None

        def fake_fetch(url, headers=None):
            provider = url.split("//")[1].split(".")[0]
            downloaded.append(provider)
            return payloads.get(provider)

        # Цепочка провайдеров собирается внутри `fetch_and_cache`, поэтому
        # подменяем сами геттеры URL.
        with patch.object(pp, "_fetch_photo_bytes", side_effect=fake_fetch), \
                patch.object(pp, "_get_thesportsdb_url", fake_url("TheSportsDB")), \
                patch.object(pp, "_get_sofifa_url", fake_url("SoFIFA")), \
                patch.object(pp, "_get_wikipedia_url", fake_url("Wikipedia")), \
                patch.object(pp, "_get_fotmob_url", fake_url("FotMob")):
            path = pp.fetch_and_cache("Rodrygo", None, force_refresh=True)
        return path, downloaded

    def _mode(self, path: str) -> str:
        with Image.open(path) as im:
            return "cutout" if ("A" in im.getbands() or "transparency" in im.info) else "flat"

    def test_cutout_from_later_provider_beats_flat_from_first(self):
        path, _ = self._run({"TheSportsDB": FLAT, "SoFIFA": CUTOUT})
        self.assertIsNotNone(path)
        self.assertEqual(self._mode(path), "cutout")

    def test_first_cutout_wins_and_stops_the_chain(self):
        path, downloaded = self._run({"TheSportsDB": CUTOUT, "SoFIFA": CUTOUT})
        self.assertEqual(self._mode(path), "cutout")
        self.assertEqual(downloaded, ["TheSportsDB"])

    def test_flat_is_used_when_no_cutout_exists(self):
        path, _ = self._run({"TheSportsDB": FLAT, "FotMob": FLAT})
        self.assertIsNotNone(path)
        self.assertEqual(self._mode(path), "flat")

    def test_earliest_flat_wins_as_fallback(self):
        """Порядок провайдеров по качеству сохраняется внутри плоского варианта."""
        big_flat = _png_bytes(size=(700, 700), mode="RGB")
        path, _ = self._run({"TheSportsDB": big_flat, "FotMob": FLAT})
        with Image.open(path) as im:
            self.assertEqual(im.size, (700, 700))

    def test_junk_is_never_cached(self):
        path, downloaded = self._run({"TheSportsDB": JUNK, "SoFIFA": TINY})
        self.assertIsNone(path)
        self.assertEqual(downloaded, ["TheSportsDB", "SoFIFA"])
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_no_provider_has_url(self):
        path, downloaded = self._run({})
        self.assertIsNone(path)
        self.assertEqual(downloaded, [])


class TestFetchWithClub(unittest.TestCase):
    """
    Клуб известен → опознание в ростере. Не опознан → фото нет, и к глобальному
    поиску по имени (он и приводил чужие лица) бот не откатывается.
    """

    IDENTITY = {"id": 1, "name": "Conor Bradley", "dob": "2003-07-09",
                "positions": ["RB"], "club_en": "Liverpool"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patchers = [
            patch.object(pp, "PHOTOS_DIR", self._tmp.name),
            patch.object(pp, "_resolve_latin_name", side_effect=lambda name, team=None: name),
        ]
        # Любое обращение к старой цепочке — провал теста.
        for getter in ("_get_thesportsdb_url", "_get_sofifa_url", "_get_wikipedia_url", "_get_fotmob_url"):
            self._patchers.append(patch.object(
                pp, getter, side_effect=AssertionError(f"{getter} вызван при известном клубе")))
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def test_identified_player_is_downloaded_under_club_path(self):
        photo = {"source": "TheSportsDB/cutout", "url": "u", "width": 500, "height": 500,
                 "alpha": True, "data": CUTOUT}
        with patch.object(pi, "identify_player", return_value=(self.IDENTITY, "tier=30", True)) as ident, \
                patch.object(pi, "download_photo", return_value=photo) as dl:
            path = pp.fetch_and_cache("BRADLEY", "Ливерпуль", force_refresh=True, position="RB")

        ident.assert_called_once_with("BRADLEY", "Ливерпуль", "RB")
        dl.assert_called_once_with(self.IDENTITY)
        self.assertEqual(path, pp.get_cached_photo_path("BRADLEY", "Ливерпуль"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), CUTOUT)

    def test_unidentified_player_gets_no_photo_and_no_global_search(self):
        with patch.object(pi, "identify_player", return_value=(None, "однофамильцы: A / B", True)), \
                patch.object(pi, "download_photo") as dl:
            path = pp.fetch_and_cache("MARTÍNEZ", "Интер Милан", force_refresh=True)
        self.assertIsNone(path)
        dl.assert_not_called()
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_missing_roster_gets_no_photo_either(self):
        with patch.object(pi, "identify_player", return_value=(None, "ростер клуба не получен", False)):
            self.assertIsNone(pp.fetch_and_cache("MENDY", "Аль-Ахли", force_refresh=True))
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_identified_but_no_photo_anywhere(self):
        with patch.object(pi, "identify_player", return_value=(self.IDENTITY, "tier=30", True)), \
                patch.object(pi, "download_photo", return_value=None):
            self.assertIsNone(pp.fetch_and_cache("BRADLEY", "Ливерпуль", force_refresh=True))
        self.assertEqual(os.listdir(self._tmp.name), [])

    def test_existing_file_is_kept_without_network(self):
        """Фото, уже положенное скриптом, не перезаписывается без force_refresh."""
        path = pp.get_cached_photo_path("BRADLEY", "Ливерпуль")
        with open(path, "wb") as f:
            f.write(CUTOUT)
        with patch.object(pi, "identify_player", side_effect=AssertionError("сеть не нужна")):
            self.assertEqual(pp.fetch_and_cache("BRADLEY", "Ливерпуль"), path)

    def test_fetch_all_players_passes_position(self):
        with patch.object(pp, "fetch_and_cache", return_value=None) as fetch:
            pp.fetch_all_players([("BRADLEY", "Ливерпуль", "RB"), ("SALAH", "Ливерпуль"), "Messi"])
        self.assertEqual(
            [c.args + (c.kwargs.get("position"),) for c in fetch.call_args_list],
            [("BRADLEY", "Ливерпуль", "RB"), ("SALAH", "Ливерпуль", None), ("Messi", None, None)],
        )


if __name__ == "__main__":
    unittest.main()
