"""`scripts/renderz_sync.py`: club table, name matching and card selection (no network)."""
import datetime as dt

import config
from scripts import renderz_sync as rs


def _card(id_, name, ea, ovr, tradable=True, program="P1", club_img=1, page=1):
    return {"id": id_, "slug": name.lower(), "name": name, "ovr": ovr, "position": "ST",
            "ea_id": ea, "program": program, "club_img": club_img, "tradable": tradable, "page": page}


def _tm(*clubs):
    return {club: {"tm_id": 1, "tm_name": club, "players": [{"name": n} for n in names]}
            for club, names in clubs}


def test_clubs_table_matches_division_clubs():
    roster = {name for names in config.DIVISION_CLUBS.values() for name in names}
    assert set(rs.CLUBS) == roster
    assert len(set(rs.CLUBS.values())) == len(rs.CLUBS)


def test_tokens_match():
    t = rs.norm_tokens
    assert t("Vinícius Júnior") == ("vinicius", "junior")
    assert t("Ødegaard") == ("odegaard",)
    assert rs.tokens_match(t("Vini Jr."), t("Vinícius Júnior"))
    assert rs.tokens_match(t("Cole"), t("Cole Palmer"))
    assert not rs.tokens_match(t("Cole Palmer"), t("Cole"))
    assert not rs.tokens_match(t("Silva"), t("Salah"))
    assert not rs.tokens_match((), t("Salah"))


def test_parse_joined_and_as_of():
    assert rs.parse_joined("06/08/2026") == dt.date(2026, 8, 6)
    assert rs.parse_joined("-") is None
    assert rs.parse_joined("18/09/2026") > rs.AS_OF >= rs.parse_joined("17/09/2026")


def test_tradable_copy_wins_and_lone_untradable_is_kept():
    tm = _tm(("Арсенал", ["Bukayo Saka", "Martin Odegaard"]))
    rows = [
        _card(1, "Saka", 100, 110, tradable=False),
        _card(2, "Saka", 100, 110, tradable=True),
        _card(3, "Ødegaard", 200, 108, tradable=False),
    ]
    res = rs.run_match(tm, rows)
    by = {p["player"]: p for p in res["players"]}
    saka = {v["renderz_id"]: v for v in by["Bukayo Saka"]["versions"]}
    assert saka[2]["selected"] and not saka[1]["selected"]
    assert [v["selected"] for v in by["Martin Odegaard"]["versions"]] == [True]
    assert len(saka) == 2          # the OVR base keeps every row


def test_several_versions_stay_in_the_base_and_each_is_selected():
    tm = _tm(("Арсенал", ["Bukayo Saka"]))
    rows = [_card(1, "Saka", 100, 110, program="A"), _card(2, "Saka", 100, 104, program="B")]
    res = rs.run_match(tm, rows)
    assert [v["ovr"] for v in res["players"][0]["versions"]] == [110, 104]
    assert all(v["selected"] for v in res["players"][0]["versions"])


def test_ambiguous_surname_is_dropped_without_club_evidence():
    tm = _tm(("Челси", ["Cole Palmer"]), ("Арсенал", ["Cole Campbell"]))
    res = rs.run_match(tm, [_card(1, "Cole", 300, 105, club_img=114154)])
    assert res["players"] == []
    assert res["ambiguous"][0]["ea_id"] == 300


def test_club_image_learned_from_unambiguous_cards_resolves_ambiguity():
    tm = _tm(("Челси", ["Cole Palmer", "Reece James", "Enzo Fernandez", "Levi Colwill"]),
             ("Арсенал", ["Cole Campbell"]))
    rows = [
        _card(10, "Reece James", 1, 100, club_img=777),
        _card(11, "Enzo Fernandez", 2, 100, club_img=777),
        _card(12, "Levi Colwill", 3, 100, club_img=777),
        _card(13, "Cole", 4, 105, club_img=777),
    ]
    res = rs.run_match(tm, rows)
    assert res["club_map"] == {"777": "Челси"}
    assert {p["player"] for p in res["players"]} >= {"Cole Palmer"}


def test_card_without_ea_id_still_matches_by_exact_name():
    tm = _tm(("Арсенал", ["Bukayo Saka"]))
    res = rs.run_match(tm, [_card(5, "Bukayo Saka", None, 109, program=None)])
    assert res["players"][0]["ea_id"] == 0
    assert res["players"][0]["versions"][0]["selected"]


def test_is_wanted_drops_cards_above_the_cap_and_icons_and_heroes():
    w = rs.is_wanted
    assert w(_card(1, "A", 1, 114, program="TWG26_LIVE"))
    assert not w(_card(1, "A", 1, 115, program="TWG26_LIVE"))
    assert not w(_card(1, "A", 1, 100, program="TOTS26_ICON"))
    assert not w(_card(1, "A", 1, 100, program="ANS26_HERO"))
    assert w(_card(1, "A", 1, 100, program=None))
    assert w(_card(1, "A", 1, 100, program="TWG26_ICON"), keep_icons=True)
    assert w(_card(1, "A", 1, 130, program="X"), ovr_max=0)
    assert not w(_card(1, "A", 1, 115, program="X"), keep_icons=True)


def test_read_rows_filters_cap_icons_and_duplicates(tmp_path):
    import json
    rows = [_card(1, "A", 1, 120), _card(2, "B", 2, 110, program="X_ICON"),
            _card(3, "C", 3, 110, program="X_LIVE"), _card(3, "C", 3, 110, program="X_LIVE")]
    (tmp_path / "renderz_rows.jsonl").write_text(chr(10).join(json.dumps(r) for r in rows), encoding="utf-8")
    assert [r["id"] for r in rs._read_rows(str(tmp_path), 114)] == [3]
    assert [r["id"] for r in rs._read_rows(str(tmp_path), 114, keep_icons=True)] == [2, 3]


def test_first_page_within_cap_bisects(monkeypatch):
    pages = {n: [_card(n, "A", n, 120 - n // 10)] for n in range(1, rs.LAST_PAGE + 1)}
    monkeypatch.setattr(rs.rz, "fetch", lambda url: url)
    monkeypatch.setattr(rs.rz, "page_url", lambda n, ovr_min: n)
    monkeypatch.setattr(rs, "parse_card_rows", lambda n: pages[n])
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    first = rs._first_page_within_cap(114, 0)
    assert pages[first][0]["ovr"] <= 114 and (first == 1 or pages[first - 1][0]["ovr"] > 114)


def test_is_wanted_reads_icon_and_hero_from_the_background():
    base = _card(1, "Rush", None, 114, program=None)
    assert rs.is_wanted({**base, "bg": "bg_23_backgrounds_twg26_TWG26_ICON_WALES_STATIC_L1"}) is False
    assert rs.is_wanted({**base, "bg": "bg_23_B_CHRONICLES_HIGH_ICON_STATIC"}) is False
    assert rs.is_wanted({**base, "bg": "bg_23_B_SOME_HERO_STATIC"}) is False
    assert rs.is_wanted({**base, "bg": "bg_23_B_UCL26_BASE_LIVE_STATIC"}) is True
    assert rs.is_wanted({**base, "bg": "bg_23_B_ICONIC_LIVE_STATIC"}) is True
    assert rs.is_wanted({**base, "bg": "bg_23_B_X_ICON_STATIC"}, keep_icons=True) is True


def test_parse_card_rows_reads_the_background_name():
    page = ('<a class="group flex min-h-[104px] items-stretch" aria-label="Rush" href="/player/9-rush">'
            '<img src="https://images-v2.renderz.app/bg_23_B_X_ICON_STATIC?verify=1%2B2" alt="Player Card Background"/>'
            '<img src="https://images-v2.renderz.app/player_25_247706?verify=1" alt="Action shot"/>'
            '<div class="rating"><span>114</span></div></a>')
    row = rs.parse_card_rows(page)[0]
    assert row["bg"] == "bg_23_B_X_ICON_STATIC" and row["program"] is None


def test_scan_pos_walks_each_position_until_the_floor(tmp_path, monkeypatch):
    import argparse
    import json
    import urllib.error

    # goalkeeper: 3 pages, OVR 110 / 90 / 60 (the third is below the floor); the others are empty (HTTP 500)
    lists = {"goalkeeper": {1: [110, 105], 2: [90, 72], 3: [60]}}
    cards = {}

    def fetch(url):
        pos, page = url.split("/position/")[1].split("?page=")
        if pos not in lists or int(page) not in lists[pos]:
            raise urllib.error.HTTPError(url, 500, "end", None, None)
        return f"{pos}:{page}"

    def parse(html):
        pos, page = html.split(":")
        return [{**_card(int(page) * 100 + i, f"P{page}{i}", None, ovr, page=0), "portrait_url": None}
                for i, ovr in enumerate(lists[pos][int(page)])]

    monkeypatch.setattr(rs, "_fetch_retry", fetch)
    monkeypatch.setattr(rs, "parse_card_rows", parse)
    monkeypatch.setattr(rs, "load_tm", lambda out: {})
    monkeypatch.setattr(rs, "_first_pos_page_within_cap", lambda pos, cap, delay: 1)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    args = argparse.Namespace(out=str(tmp_path), ovr_max=114, ovr_min=70, keep_icons=False, delay=0)
    assert rs.stage_scan_pos(args) == 0
    saved = [json.loads(line) for line in (tmp_path / "renderz_rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["ovr"] for r in saved] == [110, 105, 90, 72]
    assert {r["page"] for r in saved} == {100001, 100002}      # goalkeeper = position index 0 -> 100000 + k
    rs.stage_scan_pos(args)                                      # resumable: nothing is written twice
    assert len((tmp_path / "renderz_rows.jsonl").read_text(encoding="utf-8").splitlines()) == 4


def test_cards_without_ea_id_merge_into_one_player_entry():
    tm = _tm(("Арсенал", ["Bukayo Saka"]))
    rows = [_card(1, "Bukayo Saka", 100, 110, program="A"),
            _card(2, "Bukayo Saka", None, 95, program="B"),
            _card(3, "Bukayo Saka", None, 95, program="B", tradable=False),
            _card(4, "Bukayo Saka", None, 80, program=None)]
    res = rs.run_match(tm, rows)
    assert len(res["players"]) == 1
    p = res["players"][0]
    assert p["ea_id"] == 100 and len(p["versions"]) == 4
    assert [v["renderz_id"] for v in p["versions"] if v["selected"]] == [1, 2, 4]


def test_list_url_decodes_main_and_position_page_keys():
    assert rs._list_url(7) == rs.rz.page_url(7, None)
    assert rs._list_url(100003).endswith("/players/position/goalkeeper?page=3")
    assert rs._list_url(500177).endswith("/players/position/striker?page=177")


def test_untagged_cards_dedupe_by_background_not_by_id():
    """Most cards have no program tag: the copies of one card share the background, and only the
    tradable one is selected; another background with the same OVR is a different version."""
    tm = _tm(("Айнтрахт", ["Ansgar Knauff"]))
    rows = [dict(_card(1, "Knauff", None, 108, tradable=False, program=None), bg="bg_ERAS"),
            dict(_card(2, "Knauff", None, 108, tradable=True, program=None), bg="bg_ERAS"),
            dict(_card(3, "Knauff", None, 108, tradable=True, program=None), bg="bg_TOTW")]
    res = rs.run_match(tm, rows)
    assert [v["renderz_id"] for v in res["players"][0]["versions"] if v["selected"]] == [2, 3]


def test_parse_card_detail_from_json_ld_and_team():
    page = (
        '<html><head>'
        '<script type="application/ld+json">{"@context": "https://schema.org", "@graph": ['
        '{"@type": "Person", "name": "William Saliba"}]}</script>'
        '<title>William Saliba — 114 OVR · FC Mobile | RenderZ</title></head><body>'
        '<span class="uppercase">TEAM</span><span class="block">Arsenal</span>'
        '</body></html>'
    )
    res = rs.parse_card_detail(page)
    assert res == {"full_name": "William Saliba", "team": "Arsenal"}


def test_parse_card_detail_fallback_to_title():
    page = '<html><head><title>Cole Palmer — 105 OVR · FC Mobile</title></head><body></body></html>'
    res = rs.parse_card_detail(page)
    assert res["full_name"] == "Cole Palmer" and res["team"] is None


def test_run_match_with_names_resolves_ambiguous_group_and_does_not_shadow():
    tm = _tm(("Челси", ["Cole Palmer"]), ("Арсенал", ["Cole Campbell"]),
             ("Лацио", ["Albert Gudmundsson"]), ("Лидс", ["Gabriel Gudmundsson"]))
    rows = [
        _card(1, "Cole", 300, 105, club_img=114154),
        _card(2, "Gudmundsson", 400, 105, club_img=114155),
    ]
    # Card 1 has no detail (stays ambiguous); card 2 has detail and resolves.
    # Without fixing variable shadowing, card 1 would turn `names` into a list and crash on card 2.
    names = {2: {"full_name": "Albert Gudmundsson", "team": "Lazio"}}
    res = rs.run_match(tm, rows, names=names)
    assert [p["player"] for p in res["players"]] == ["Albert Gudmundsson"]
    assert len(res["ambiguous"]) == 1
    assert res["ambiguous"][0]["ea_id"] == 300


def test_stage_resolve_fetches_detail_and_writes_card_details(tmp_path, monkeypatch):
    import argparse
    import json

    tm = _tm(("Челси", ["Cole Palmer"]), ("Арсенал", ["Cole Campbell"]))
    (tmp_path / "tm_squads.json").write_text(json.dumps(tm), encoding="utf-8")
    rows = [_card(1, "Cole", 300, 105, club_img=114154)]
    (tmp_path / "renderz_rows.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")

    mock_html = (
        '<script type="application/ld+json">{"@graph": [{"@type": "Person", "name": "Cole Palmer"}]}</script>'
        '<div><span class="uppercase">TEAM</span><span>Chelsea</span></div>'
    )
    monkeypatch.setattr(rs, "_fetch_retry", lambda url: mock_html)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)

    args = argparse.Namespace(out=str(tmp_path), ovr_max=114, keep_icons=False, delay=0, limit=0)
    assert rs.stage_resolve(args) == 0

    details_file = tmp_path / "card_details.json"
    assert details_file.exists()
    details = json.loads(details_file.read_text(encoding="utf-8"))
    assert details["1"] == {"full_name": "Cole Palmer", "team": "Chelsea"}

    # Resumable: calling it again does not fetch again
    called = []
    monkeypatch.setattr(rs, "_fetch_retry", lambda url: called.append(url))
    assert rs.stage_resolve(args) == 0
    assert len(called) == 0


def test_stage_match_loads_card_details_json(tmp_path, monkeypatch):
    import argparse
    import json

    tm = _tm(("Челси", ["Cole Palmer"]), ("Арсенал", ["Cole Campbell"]))
    (tmp_path / "tm_squads.json").write_text(json.dumps(tm), encoding="utf-8")
    rows = [_card(1, "Cole", 300, 105, club_img=114154)]
    (tmp_path / "renderz_rows.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    (tmp_path / "card_details.json").write_text(
        json.dumps({"1": {"full_name": "Cole Palmer", "team": "Chelsea"}}), encoding="utf-8"
    )

    args = argparse.Namespace(out=str(tmp_path), ovr_max=114, keep_icons=False)
    assert rs.stage_match(args) == 0

    ovr = json.loads((tmp_path / "ovr_db.json").read_text(encoding="utf-8"))
    assert [p["player"] for p in ovr["players"]] == ["Cole Palmer"]
    assert ovr["ambiguous"] == []
