"""Карточки Renderz: таблица OVR, подсказка форм заявки и запасной портрет."""
import json
import os

import pytest

import config
import database
from transfers import repo, requests as req_mod, suggest

CLUBS = ["Челси", "Арсенал", "Манчестер Сити"]


def _card(rid, name, club, ovr, tradable=True, selected=True, program="P"):
    return {"renderz_id": rid, "player_name": name, "club": club, "ovr": ovr, "position": "ST",
            "program": program, "tradable": tradable, "selected": selected}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in ("transfer_player_cards", "transfer_players", "squad_players", "users"):
            conn.execute(f"DELETE FROM {table}")
        for team, name in [("Арсенал", "SAKA"), ("Арсенал", "ØDEGAARD"), ("Арсенал", "Cole Campbell"),
                           ("Челси", "Cole Palmer"), ("Манчестер Сити", "Erling Haaland")]:
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, 'X')",
                         (team, name))
    monkeypatch.setattr(config, "CLUB_REGISTRY", CLUBS, raising=False)


class TestRepo:
    def test_replace_is_a_full_snapshot_and_empty_does_not_wipe(self):
        assert repo.replace_player_cards([_card(1, "Bukayo Saka", "Арсенал", 110)]) == 1
        assert repo.replace_player_cards([]) == 0
        assert [c["renderz_id"] for c in repo.list_player_cards()] == [1]
        repo.replace_player_cards([_card(2, "Martin Ødegaard", "Арсенал", 108)])
        assert [c["renderz_id"] for c in repo.list_player_cards()] == [2]

    def test_bad_rows_are_skipped_and_selected_filter(self):
        n = repo.replace_player_cards([
            {"renderz_id": 1, "player_name": "", "ovr": 100},
            {"renderz_id": None, "player_name": "X Y", "ovr": 100},
            {"renderz_id": 3, "player_name": "X Y", "ovr": None},
            _card(4, "Bukayo Saka", "Арсенал", 110, tradable=False, selected=False),
            _card(5, "Bukayo Saka", "Арсенал", 110),
        ])
        assert n == 2
        assert [c["renderz_id"] for c in repo.list_player_cards()] == [5]
        assert [c["renderz_id"] for c in repo.list_player_cards(selected_only=False)] == [4, 5]


class TestSuggestCards:
    def test_short_surname_finds_full_card_name_and_diacritics_fold(self):
        repo.replace_player_cards([_card(1, "Bukayo Saka", "Arsenal", 110),
                                   _card(2, "Martin Ødegaard", "Arsenal", 108),
                                   _card(3, "Martin Ødegaard", "Arsenal", 103, tradable=False, program="Q")])
        by_name = {i["name"]: i for i in suggest.players(999, "saka")}
        assert [c["ovr"] for c in by_name["SAKA"]["cards"]] == [110]
        item = suggest.players(999, "odeg")[0]
        assert [c["ovr"] for c in item["cards"]] == [108, 103]          # по убыванию OVR
        assert [c["tradable"] for c in item["cards"]] == [True, False]

    def test_ambiguous_surname_in_club_gets_no_cards(self):
        repo.replace_player_cards([_card(1, "Cole Palmer", "Chelsea", 100),
                                   _card(2, "Cole Campbell", "Arsenal", 95)])
        item = suggest.players(999, "cole campbell")[0]
        assert [c["ovr"] for c in item["cards"]] == [95]               # точное полное имя
        repo.replace_player_cards([_card(1, "Cole Campbell", "Arsenal", 95),
                                   _card(2, "Cole Campbell Jr", "Arsenal", 90)])
        with database.transaction() as conn:
            conn.execute("DELETE FROM squad_players WHERE player_name = 'Cole Campbell'")
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES ('Арсенал', 'CAMPBELL', 'X')")
        item = [i for i in suggest.players(999, "campbell") if i["name"] == "CAMPBELL"][0]
        assert item["cards"] == []

    def test_initial_and_surname_find_the_full_name(self):
        with database.transaction() as conn:
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES ('Челси', 'C. RONALDO', 'X')")
        repo.replace_player_cards([_card(1, "Cristiano Ronaldo", "Chelsea", 112)])
        item = [i for i in suggest.players(999, "ronaldo") if i["name"] == "C. RONALDO"][0]
        assert [c["ovr"] for c in item["cards"]] == [112]

    def test_no_table_data_means_empty_cards(self):
        assert suggest.players(999, "haaland")[0]["cards"] == []


class TestPortraitFallback:
    @pytest.fixture
    def folders(self, tmp_path, monkeypatch):
        rz = tmp_path / "rz"
        rz.mkdir()
        monkeypatch.setattr(req_mod, "RENDERZ_PORTRAITS_DIR", str(rz))
        monkeypatch.setattr(req_mod.player_photos, "PHOTOS_DIR", str(tmp_path / "players"), raising=False)
        os.makedirs(tmp_path / "players", exist_ok=True)
        return rz

    def test_falls_back_to_renderz_folder_only_when_allowed(self, folders):
        slug = req_mod.player_photos._slugify("Bukayo Saka")
        (folders / f"{slug}.png").write_bytes(b"png")
        assert req_mod.portrait_path("Bukayo Saka", "Арсенал") == str(folders / f"{slug}.png")
        assert req_mod.portrait_url("Bukayo Saka", "Арсенал") == f"/assets/renderz_portraits/{slug}.png"
        assert req_mod.portrait_path("Bukayo Saka", "Арсенал", fallback=False) is None
        assert req_mod.portrait_url("Bukayo Saka", "Арсенал", fallback=False) is None

    def test_empty_file_and_unknown_player_give_none(self, folders):
        slug = req_mod.player_photos._slugify("Bukayo Saka")
        (folders / f"{slug}.png").write_bytes(b"")
        assert req_mod.portrait_path("Bukayo Saka", "Арсенал") is None
        assert req_mod.portrait_path("Nobody Here", "Арсенал") is None
        assert req_mod.portrait_path(None) is None


    def test_short_squad_name_finds_the_full_name_file_through_the_club(self, folders):
        slug = req_mod.player_photos._slugify("Bukayo Saka")
        (folders / f"{slug}.png").write_bytes(b"png")
        repo.replace_player_cards([_card(1, "Bukayo Saka", "Арсенал", 110)])
        assert req_mod.portrait_path("SAKA", "Челси", "Арсенал") == str(folders / f"{slug}.png")
        assert req_mod.portrait_path("SAKA", "Челси") is None          # не тот клуб
        assert req_mod.portrait_path("SAKA", req_mod.URN_CLUB) is None

    def test_two_namesakes_in_the_club_give_no_portrait(self, folders):
        for name in ("Cole Palmer", "Cole Campbell"):
            (folders / (req_mod.player_photos._slugify(name) + ".png")).write_bytes(b"png")
        repo.replace_player_cards([_card(1, "Cole Palmer", "Челси", 100), _card(2, "Cole Campbell", "Челси", 95)])
        assert req_mod.portrait_path("COLE", "Челси") is None
        assert req_mod.portrait_path("PALMER", "Челси").endswith(req_mod.player_photos._slugify("Cole Palmer") + ".png")


class TestNameCovers:
    @pytest.mark.parametrize("short, full, ok", [
        ("SAKA", "Bukayo Saka", True),
        ("C. RONALDO", "Cristiano Ronaldo", True),
        ("R. RONALDO", "Cristiano Ronaldo", False),
        ("C.", "Cristiano Ronaldo", False),
        ("MILINKOVIC-SAVIC", "Sergej Milinković-Savić", True),
        ("ØDEGAARD", "Martin Ødegaard", True),
        ("SAKA SAKA", "Bukayo Saka", False),
        ("SAKA", "", False),
    ])
    def test_cases(self, short, full, ok):
        from transfers.engine import name_covers
        assert name_covers(short, full) is ok


class TestImporter:
    def test_load_cards_flattens_versions_and_pairs_portraits(self, tmp_path):
        from scripts import import_renderz_cards as imp
        from services.graphics.player_photos import _slugify
        (tmp_path / "portraits").mkdir()
        (tmp_path / "portraits" / (_slugify("Bukayo Saka") + ".png")).write_bytes(b"png")
        db = {"players": [
            {"player": "Bukayo Saka", "club": "Арсенал", "ea_id": 1, "versions": [
                {"renderz_id": 1, "ovr": 110, "tradable": True, "selected": True, "program": "A", "position": "RW"},
                {"renderz_id": 2, "ovr": 104, "tradable": False, "selected": False}]},
            {"player": "No Portrait", "club": "Челси", "ea_id": 2, "versions": [
                {"renderz_id": 3, "ovr": 101, "tradable": True, "selected": True}]},
        ]}
        (tmp_path / "ovr_db.json").write_text(json.dumps(db), encoding="utf-8")
        cards, portraits = imp.load_cards(str(tmp_path))
        assert [c["renderz_id"] for c in cards] == [1, 2, 3]
        assert [c["selected"] for c in cards] == [True, False, True]
        assert [name for _, name in portraits] == [_slugify("Bukayo Saka") + ".png"]
