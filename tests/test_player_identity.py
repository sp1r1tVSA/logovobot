"""
Опознание игрока внутри ростера клуба (`services/graphics/player_identity.py`).

Главный инвариант тот же, что у `club_registry`: неразводимая ничья — отказ,
а не догадка. Пустая карточка исправима, чужое лицо — нет.

Сеть не используется: ростер и ответы провайдеров замоканы.
"""

import unittest
from unittest.mock import patch

from services.graphics import player_identity as pi


def _player(pid, name, positions=(), rating=0, value=0, dob=""):
    return {"id": pid, "name": name, "dob": dob, "positions": list(positions),
            "number": None, "rating": rating, "value": value, "club_en": "Test FC"}


LIVERPOOL = [
    _player(1, "Conor Bradley", ["RB"], rating=7.0, value=30_000_000),
    _player(2, "Mohamed Salah", ["RW"], rating=7.8, value=60_000_000),
    _player(3, "Bradley Smith", ["CM"]),  # имя совпадает с фамилией запроса
]

INTER = [
    _player(10, "Lautaro Martínez", ["ST"], rating=7.4, value=90_000_000),
    _player(11, "Josep Martínez", ["GK"], rating=6.8, value=12_000_000),
]


class TestNameMatching(unittest.TestCase):
    def test_norm_strips_diacritics_and_punctuation(self):
        self.assertEqual(pi._norm("ÉDER MILITÃO"), "eder militao")
        self.assertEqual(pi._norm("C. RONALDO"), "c ronaldo")

    def test_surname_beats_first_name(self):
        """«BRADLEY» в Ливерпуле — Conor Bradley, а не тот, кого зовут Bradley."""
        identity, _ = pi.match_in_roster("BRADLEY", None, LIVERPOOL)
        self.assertEqual(identity["id"], 1)

    def test_position_splits_namesakes(self):
        identity, _ = pi.match_in_roster("MARTÍNEZ", "ST", INTER)
        self.assertEqual(identity["id"], 10)
        identity, _ = pi.match_in_roster("MARTÍNEZ", "GK", INTER)
        self.assertEqual(identity["id"], 11)

    def test_unresolvable_tie_is_refused(self):
        twins = [_player(20, "Kevin Mendy", ["CB"], rating=7.0, value=5_000_000),
                 _player(21, "Ferland Mendy", ["CB"], rating=7.1, value=6_000_000)]
        identity, why = pi.match_in_roster("MENDY", "CB", twins)
        self.assertIsNone(identity)
        self.assertIn("однофамильцы", why)

    def test_clear_prominence_breaks_tie(self):
        twins = [_player(20, "Kevin Mendy", ["CB"], rating=7.0, value=30_000_000),
                 _player(21, "Ferland Mendy", ["CB"], value=1_000_000)]
        identity, _ = pi.match_in_roster("MENDY", "CB", twins)
        self.assertEqual(identity["id"], 20)

    def test_not_in_roster(self):
        identity, why = pi.match_in_roster("HAALAND", "ST", LIVERPOOL)
        self.assertIsNone(identity)
        self.assertEqual(why, "не найден в ростере")

    def test_nickname_override(self):
        roster = [_player(30, "Vinicius Junior", ["LW"], rating=7.5)]
        identity, _ = pi.match_in_roster("VINI JR.", "LW", roster)
        self.assertEqual(identity["id"], 30)


class TestClubsMatch(unittest.TestCase):
    def test_same_club_different_spelling(self):
        self.assertTrue(pi._clubs_match("Bayern München", "bayern munchen"))
        self.assertTrue(pi._clubs_match("VfB Stuttgart", "Stuttgart"))

    def test_clubs_sharing_a_word_are_different(self):
        self.assertFalse(pi._clubs_match("Real Madrid", "Real Sociedad"))
        self.assertFalse(pi._clubs_match("Manchester City", "Manchester United"))
        self.assertFalse(pi._clubs_match("Inter", "Inter Miami CF"))

    def test_empty_never_matches(self):
        self.assertFalse(pi._clubs_match("", "Arsenal"))


class TestIdentifyInRoster(unittest.TestCase):
    def test_pinned_wins_over_roster(self):
        roster = [_player(99, "Someone Bakker", ["LB"], rating=7.0)]
        with patch.object(pi, "search_player_globally", side_effect=AssertionError("не нужен")):
            identity, why = pi.identify_in_roster("BAKKER", "LB", "Аталанта", "Atalanta", roster)
        self.assertEqual(identity["id"], pi.PINNED_PLAYERS[("Аталанта", "bakker")]["id"])
        self.assertIn("зафиксирован", why)

    def test_global_search_only_with_matching_club(self):
        def fake_json(url, headers, timeout=15):
            return {"squadMemberSuggest": [{"options": [
                {"text": "Bradley Barcola|1", "payload": {"id": 500, "teamName": "Paris Saint-Germain"}},
                {"text": "Conor Bradley|2", "payload": {"id": 1, "teamName": "Liverpool"}},
            ]}]}

        with patch.object(pi, "_request_json", side_effect=fake_json), \
                patch.object(pi.time, "sleep"):
            identity, why = pi.identify_in_roster("BRADLEY", "RB", "Ливерпуль", "Liverpool", [])
        self.assertEqual(identity["id"], 1)
        self.assertIn("клуб совпал", why)

    def test_global_search_rejects_other_clubs(self):
        def fake_json(url, headers, timeout=15):
            return {"squadMemberSuggest": [{"options": [
                {"text": "Bradley Barcola|1", "payload": {"id": 500, "teamName": "Paris Saint-Germain"}},
            ]}]}

        with patch.object(pi, "_request_json", side_effect=fake_json), \
                patch.object(pi.time, "sleep"):
            identity, _ = pi.identify_in_roster("BRADLEY", "RB", "Ливерпуль", "Liverpool", [])
        self.assertIsNone(identity)


class TestIdentifyPlayerCaching(unittest.TestCase):
    def setUp(self):
        pi.clear_roster_cache()
        self.addCleanup(pi.clear_roster_cache)

    def test_roster_is_fetched_once_per_club(self):
        with patch.object(pi, "fetch_club_roster", return_value=(8650, "Liverpool", LIVERPOOL)) as fetch:
            first = pi.identify_player("BRADLEY", "Ливерпуль", "RB")
            second = pi.identify_player("SALAH", "Ливерпуль", "RW")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first[0]["id"], 1)
        self.assertTrue(first[2])
        self.assertEqual(second[0]["id"], 2)

    def test_unidentified_answer_is_cached(self):
        """Иначе карточка неопознанного игрока гоняла бы глобальный поиск на каждой отрисовке."""
        with patch.object(pi, "fetch_club_roster", return_value=(8650, "Liverpool", LIVERPOOL)), \
                patch.object(pi, "search_player_globally", return_value=(None, "нет")) as search:
            self.assertIsNone(pi.identify_player("HAALAND", "Ливерпуль", "ST")[0])
            self.assertIsNone(pi.identify_player("HAALAND", "Ливерпуль", "ST")[0])
        self.assertEqual(search.call_count, 1)

    def test_roster_failure_is_reported_and_short_lived(self):
        with patch.object(pi, "fetch_club_roster", return_value=(8650, "", [])):
            identity, why, roster_ok = pi.identify_player("BRADLEY", "Ливерпуль")
        self.assertIsNone(identity)
        self.assertFalse(roster_ok)
        self.assertIn("ростер", why)
        expires, _ = pi._roster_cache["Ливерпуль"]
        self.assertLessEqual(expires - pi.time.monotonic(), pi.ROSTER_FAILURE_TTL)

    def test_pinned_player_survives_roster_failure(self):
        with patch.object(pi, "fetch_club_roster", return_value=(None, "", [])):
            identity, _, roster_ok = pi.identify_player("BAKKER", "Аталанта", "LB")
        self.assertFalse(roster_ok)
        self.assertEqual(identity["id"], pi.PINNED_PLAYERS[("Аталанта", "bakker")]["id"])


class TestPhotoSourcesVerifyIdentity(unittest.TestCase):
    IDENTITY = {"id": 1, "name": "Conor Bradley", "dob": "2003-07-09", "club_en": "Liverpool"}

    def test_thesportsdb_rejects_namesake_from_other_club(self):
        data = {"player": [{"strPlayer": "Conor Bradley", "strTeam": "Bolton",
                            "dateBorn": "1990-01-01", "strCutout": "https://x/wrong.png"}]}
        with patch.object(pi, "_request_json", return_value=data):
            self.assertEqual(pi._tsdb_candidates(self.IDENTITY), [])

    def test_thesportsdb_accepts_by_birth_date(self):
        data = {"player": [{"strPlayer": "Conor Bradley", "strTeam": "Northern Ireland",
                            "dateBorn": "2003-07-09", "strCutout": "https://x/right.png"}]}
        with patch.object(pi, "_request_json", return_value=data):
            self.assertEqual(pi._tsdb_candidates(self.IDENTITY),
                             [("TheSportsDB/cutout", "https://x/right.png")])

    def test_fotmob_needs_an_id(self):
        self.assertEqual(pi._fotmob_candidates({"name": "X"}), [])
        self.assertEqual(len(pi._fotmob_candidates(self.IDENTITY)), 1)


class TestRegistry(unittest.TestCase):
    def test_every_roster_club_has_a_fotmob_id(self):
        from config import DIVISION_CLUBS
        clubs = {club for roster in DIVISION_CLUBS.values() for club in roster}
        self.assertEqual(clubs - set(pi.CLUB_FOTMOB_IDS), set())


if __name__ == "__main__":
    unittest.main()
