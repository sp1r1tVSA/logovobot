"""
Символическая сборная (TOTW): очки, сборка 4-3-3, разбор диапазона туров,
готовность блока в базе и цифры блока из матчей.

Gemini не вызывается: подпись проверяется на шаблонной ветке.
"""

import itertools
import unittest
import uuid
from unittest.mock import patch

import database
from services import totw_service
from services.totw_service import (
    block_bounds,
    build_totw_lineup,
    calculate_totw_player_score,
    generate_totw_caption,
    parse_round_range,
)

_ID_SEQ = itertools.count(871100)


def _cand(name, team, position, **stats):
    base = {
        "player_name": name, "team_name": team, "position": position, "is_starter": True,
        "goals": 0, "assists": 0, "mvp": 0, "braces": 0,
        "matches": 5, "wins": 0, "clean_sheets": 0, "goals_conceded": 10,
    }
    base.update(stats)
    return base


def _full_pool():
    """Одиннадцать игроков на свои позиции из шести разных клубов + запасные."""
    return [
        _cand("Вратарь", "Клуб1", "GK", clean_sheets=3, wins=4, goals_conceded=2),
        _cand("Левый", "Клуб1", "LB", clean_sheets=3, goals_conceded=2),
        _cand("Центр-1", "Клуб2", "CB", clean_sheets=2, goals=1),
        _cand("Центр-2", "Клуб2", "CB", clean_sheets=2),
        _cand("Правый", "Клуб3", "RB", clean_sheets=1, assists=1),
        _cand("Опорник", "Клуб3", "CDM", assists=2),
        _cand("Хав-1", "Клуб4", "CM", goals=2, assists=1),
        _cand("Хав-2", "Клуб4", "CAM", goals=1, assists=3),
        _cand("Вингер-Л", "Клуб5", "LW", goals=3),
        _cand("Форвард", "Клуб5", "ST", goals=6, braces=2, mvp=2),
        _cand("Вингер-П", "Клуб6", "RW", goals=2, assists=2),
        # Запасные — по одному на линию.
        _cand("Вратарь-2", "Клуб6", "GK", clean_sheets=1, wins=1),
        _cand("Защитник-3", "Клуб6", "CB", clean_sheets=1),
        _cand("Хав-3", "Клуб6", "CM", assists=1),
        _cand("Форвард-2", "Клуб6", "ST", goals=1),
    ]


class TestScoring(unittest.TestCase):
    def test_goalkeeper(self):
        gk = _cand("GK", "К", "GK", clean_sheets=2, wins=3, goals_conceded=4, mvp=1)
        # 2×15 сухарей + 10 (0.8 пропущенных за матч) + 12 MVP + 3×3 победы
        self.assertEqual(calculate_totw_player_score(gk), 30 + 10 + 12 + 9)

    def test_goalkeeper_without_matches_gets_no_low_conceded_bonus(self):
        gk = _cand("GK", "К", "GK", matches=0, goals_conceded=0)
        self.assertEqual(calculate_totw_player_score(gk), 0)

    def test_defender_reliability_needs_a_starter(self):
        cb = _cand("CB", "К", "CB", clean_sheets=2, goals=1, assists=1, goals_conceded=5)
        # 2×10 + 15 + 10 + 5 надёжность (1.0 за матч — ещё надёжно)
        self.assertEqual(calculate_totw_player_score(cb), 50)
        cb["is_starter"] = False
        self.assertEqual(calculate_totw_player_score(cb), 45)
        cb["is_starter"], cb["goals_conceded"] = True, 6
        self.assertEqual(calculate_totw_player_score(cb), 45)

    def test_midfield_clean_sheets_only_for_cdm_and_cm(self):
        cdm = _cand("CDM", "К", "CDM", clean_sheets=3, goals=1, assists=2)
        cam = dict(cdm, position="CAM")
        self.assertEqual(calculate_totw_player_score(cdm), 10 + 20 + 12)
        self.assertEqual(calculate_totw_player_score(cam), 10 + 20)

    def test_forward(self):
        st = _cand("ST", "К", "ST", goals=5, assists=1, braces=2, mvp=1)
        self.assertEqual(calculate_totw_player_score(st), 60 + 8 + 10 + 12)

    def test_unknown_position_counts_as_attack(self):
        p = _cand("X", "К", "", goals=1)
        self.assertEqual(calculate_totw_player_score(p), 12)


class TestLineup(unittest.TestCase):
    def test_full_pool_fills_4_3_3_in_position(self):
        totw = build_totw_lineup(_full_pool())
        self.assertEqual(totw["formation"], "4-3-3")
        slots = [p["slot"] for p in totw["xi"]]
        self.assertEqual(slots, ["GK", "LB", "LCB", "RCB", "RB", "CDM", "LCM", "RCM", "LW", "ST", "RW"])
        self.assertFalse(any(p["out_of_position"] for p in totw["xi"]))
        by_slot = {p["slot"]: p["player_name"] for p in totw["xi"]}
        self.assertEqual(by_slot["GK"], "Вратарь")
        self.assertEqual(by_slot["ST"], "Форвард")

    def test_captain_is_the_top_scorer(self):
        totw = build_totw_lineup(_full_pool())
        self.assertEqual(totw["captain"]["player_name"], "Форвард")
        captains = [p for p in totw["xi"] if p["is_captain"]]
        self.assertEqual(len(captains), 1)
        self.assertEqual(captains[0]["player_name"], "Форвард")

    def test_bench_has_one_player_per_line(self):
        totw = build_totw_lineup(_full_pool())
        bench = totw["bench"]
        self.assertEqual([p["line"] for p in bench], ["GK", "DEF", "MID", "FWD"])
        xi_names = {p["player_name"] for p in totw["xi"]}
        self.assertFalse(xi_names & {p["player_name"] for p in bench})
        self.assertFalse(any(p["is_captain"] for p in bench))

    def test_at_most_two_players_per_club(self):
        pool = _full_pool() + [
            # Вся атака одного клуба сильнее любого другого — а в XI только двое.
            _cand("Звезда-1", "Клуб7", "ST", goals=20),
            _cand("Звезда-2", "Клуб7", "LW", goals=19),
            _cand("Звезда-3", "Клуб7", "RW", goals=18),
        ]
        totw = build_totw_lineup(pool)
        clubs = [p["team_name"] for p in totw["xi"]]
        self.assertLessEqual(max(clubs.count(c) for c in set(clubs)), 2)
        self.assertEqual(clubs.count("Клуб7"), 2)
        self.assertNotIn("Звезда-3", {p["player_name"] for p in totw["xi"]})

    def test_winger_takes_the_mirrored_flank(self):
        pool = [
            _cand("П-1", "А", "RW", goals=5),
            _cand("П-2", "Б", "RW", goals=4),
        ]
        totw = build_totw_lineup(pool)
        by_slot = {p["slot"]: p for p in totw["xi"]}
        self.assertEqual(by_slot["RW"]["player_name"], "П-1")
        self.assertEqual(by_slot["LW"]["player_name"], "П-2")
        self.assertFalse(by_slot["LW"]["out_of_position"])

    def test_outfield_player_never_fills_the_goal(self):
        pool = [_cand(f"Ф-{i}", f"К{i}", "ST", goals=i) for i in range(1, 12)]
        totw = build_totw_lineup(pool)
        slots = {p["slot"] for p in totw["xi"]}
        self.assertNotIn("GK", slots)
        self.assertEqual(len(totw["xi"]), 10)
        self.assertTrue(any(p["out_of_position"] for p in totw["xi"]))

    def test_empty_pool(self):
        totw = build_totw_lineup([])
        self.assertEqual(totw["xi"], [])
        self.assertIsNone(totw["captain"])
        self.assertEqual(totw["bench"], [])


class TestRoundRangeParsing(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(parse_round_range("1-5"), (1, 5))
        self.assertEqual(parse_round_range("6–10"), (6, 10))
        self.assertEqual(parse_round_range("1 — 5"), (1, 5))
        self.assertEqual(parse_round_range("туры 1..5"), (1, 5))
        self.assertEqual(parse_round_range("5-1"), (1, 5))

    def test_single_round(self):
        self.assertEqual(parse_round_range("7"), (7, 7))
        self.assertEqual(parse_round_range("тур 12"), (12, 12))

    def test_nothing_to_parse(self):
        for text in (None, "", "сезон", "0", "0-0"):
            self.assertIsNone(parse_round_range(text), text)

    def test_block_bounds(self):
        self.assertEqual(block_bounds(1), (1, 5))
        self.assertEqual(block_bounds(2), (6, 10))
        self.assertEqual(block_bounds(0), (1, 5))


class TestCaption(unittest.TestCase):
    def test_fallback_caption(self):
        pool = _full_pool()
        pool[0]["team_name"] = "<Клуб&1>"
        totw = build_totw_lineup(pool)
        caption = generate_totw_caption(totw, "Дивизион <1>", 1, 5, use_ai=False)
        self.assertIn("СИМВОЛИЧЕСКАЯ СБОРНАЯ", caption)
        self.assertIn("ТУРЫ 1–5", caption)
        self.assertIn("Форвард", caption)
        self.assertIn("Дивизион &lt;1&gt;", caption)
        self.assertNotIn("<Клуб", caption)
        self.assertLessEqual(len(caption), 1000)

    def test_empty_block_caption(self):
        caption = generate_totw_caption(build_totw_lineup([]), "Д", 1, 5, use_ai=True)
        self.assertIn("пока нет", caption)

    def test_ai_failure_falls_back_to_template(self):
        totw = build_totw_lineup(_full_pool())
        with patch("services.round_preview._call_gemini", return_value=None) as gemini:
            caption = generate_totw_caption(totw, "Д", 6, 10, use_ai=True)
        gemini.assert_called_once()
        self.assertIn("ТУРЫ 6–10", caption)

    def test_ai_caption_is_used_when_present(self):
        totw = build_totw_lineup(_full_pool())
        with patch("services.round_preview._call_gemini", return_value="🌟 <b>Сборная от Темшика</b>"):
            caption = generate_totw_caption(totw, "Д", 1, 5, use_ai=True)
        self.assertIn("Сборная от Темшика", caption)


class TotwDbTestBase(unittest.TestCase):
    """Дивизион с двумя клубами и одиннадцатью игроками основы у каждого."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_id = database.create_division(name=f"Сборная {self.uid}", code=f"TOTW_{self.uid}")
        self.teams, self.user_ids = {}, {}
        for key, word in (("A", "Кварцит"), ("B", "Гранит")):
            uid = next(_ID_SEQ)
            team = f"{word} {self.uid}"
            database.register_user(uid, f"totw_{key.lower()}_{self.uid}", team_name=team)
            database.assign_user_division(uid, self.div_id)
            self.teams[key], self.user_ids[key] = team, uid

    def _add_match(self, round_number, s1=None, s2=None, status="confirmed", mvp=None, technical=0):
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO matches
                    (round_number, player1_id, player2_id, player1_team, player2_team,
                     player1_score, player2_score, status, division_id, tournament_type,
                     mvp_player, is_technical)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'league', ?, ?)
            """, (
                round_number, self.user_ids["A"], self.user_ids["B"], self.teams["A"], self.teams["B"],
                s1, s2, status, self.div_id, mvp, technical,
            ))
            return cursor.lastrowid

    def _add_event(self, match_id, team_key, player, event_type, count=1):
        with database.transaction() as conn:
            conn.cursor().execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) VALUES (?, ?, ?, ?, ?)",
                (match_id, self.teams[team_key], player, event_type, count),
            )

    def _add_squad(self, team_key, rows):
        team = self.teams[team_key]
        with database.transaction() as conn:
            cursor = conn.cursor()
            for name, pos in rows:
                cursor.execute(
                    "INSERT INTO squad_players (team_name, player_name, position, norm_name, norm_team_name) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (team, name, pos, database.normalize_player_name_key(name),
                     database.normalize_team_name(team)),
                )

    def _play_block(self, start=1, end=5):
        return [self._add_match(rn, 1, 0) for rn in range(start, end + 1)]


class TestBlockCompletion(TotwDbTestBase):
    def test_full_block_is_complete_and_pending(self):
        self._play_block()
        self.assertTrue(database.is_round_range_completed(1, 5, self.div_id))
        pending = database.get_completed_totw_blocks_pending_publication(division_id=self.div_id)
        self.assertEqual([(b["start_round"], b["end_round"]) for b in pending], [(1, 5)])
        self.assertEqual(database.get_completed_totw_blocks(self.div_id), [(1, 5)])
        self.assertEqual(database.get_last_completed_round(self.div_id), 5)

    def test_open_debt_blocks_the_block(self):
        self._play_block(1, 4)
        debt = self._add_match(5, status="pending")
        self.assertFalse(database.is_round_range_completed(1, 5, self.div_id))
        self.assertEqual(database.get_completed_totw_blocks_pending_publication(division_id=self.div_id), [])
        self.assertEqual(database.get_last_completed_round(self.div_id), 4)

        # Технический результат по долгу закрывает блок.
        database.set_technical_result(debt, 3, 0, "tp_home")
        self.assertTrue(database.is_round_range_completed(1, 5, self.div_id))

    def test_missing_round_is_not_a_played_block(self):
        for rn in (1, 2, 4, 5):
            self._add_match(rn, 1, 1)
        self.assertFalse(database.is_round_range_completed(1, 5, self.div_id))
        self.assertEqual(database.get_completed_totw_blocks(self.div_id), [])

    def test_cancelled_match_does_not_hold_the_block(self):
        self._play_block()
        self._add_match(3, status="cancelled")
        self.assertTrue(database.is_round_range_completed(1, 5, self.div_id))

    def test_posted_block_leaves_the_queue(self):
        self._play_block(1, 10)
        pending = database.get_completed_totw_blocks_pending_publication(division_id=self.div_id)
        self.assertEqual([(b["start_round"], b["end_round"]) for b in pending], [(1, 5), (6, 10)])

        database.record_round_content_post(self.div_id, 5, "totw", message_id=77)
        pending = database.get_completed_totw_blocks_pending_publication(division_id=self.div_id)
        self.assertEqual([(b["start_round"], b["end_round"]) for b in pending], [(6, 10)])
        # Список для меню не зависит от публикации.
        self.assertEqual(database.get_completed_totw_blocks(self.div_id), [(1, 5), (6, 10)])

    def test_partial_trailing_block_is_not_offered(self):
        self._play_block(1, 7)
        self.assertEqual(database.get_completed_totw_blocks(self.div_id), [(1, 5)])
        self.assertEqual(database.get_last_completed_round(self.div_id), 7)


class TestTotwStats(TotwDbTestBase):
    def setUp(self):
        super().setUp()
        positions = ["LB", "CB", "CB", "RB", "CDM", "CM", "CM", "LW", "ST", "RW", "GK"]
        self._add_squad("A", [(f"Кварц {pos} {i}", pos) for i, pos in enumerate(positions)]
                        + [("Кварц запасной", "CB")])
        self._add_squad("B", [(f"Гранит {pos} {i}", pos) for i, pos in enumerate(positions)])

    def test_block_numbers(self):
        # A выигрывает 1:0 все пять туров; форвард A забивает дубль в туре 1.
        ids = self._play_block()
        self._add_event(ids[0], "A", "Кварц ST 8", "goal", 2)
        self._add_event(ids[1], "A", "Кварц ST 8", "goal", 1)
        self._add_event(ids[1], "A", "Кварц CM 5", "assist", 1)
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE matches SET mvp_player = ? WHERE id = ?", ("Кварц ST 8", ids[0]))

        stats = {s["player_name"]: s for s in database.get_totw_stats(1, 5, self.div_id)}

        st = stats["Кварц ST 8"]
        self.assertEqual((st["goals"], st["braces"], st["mvp"]), (3, 1, 1))
        self.assertEqual(st["position"], "ST")
        self.assertEqual(stats["Кварц CM 5"]["assists"], 1)

        gk = stats["Кварц GK 10"]
        self.assertEqual(gk["position"], "GK")
        self.assertTrue(gk["is_starter"])
        self.assertEqual((gk["matches"], gk["wins"], gk["clean_sheets"], gk["goals_conceded"]), (5, 5, 5, 0))

        # Проигравший клуб без сухарей; запасной сухари не получает и в пул не попадает.
        self.assertEqual(stats["Гранит GK 10"]["clean_sheets"], 0)
        self.assertNotIn("Кварц запасной", stats)

    def test_technical_results_carry_no_numbers(self):
        self._add_match(1, 3, 0, mvp="Кварц ST 8", technical=1)
        self.assertEqual(database.get_totw_stats(1, 1, self.div_id), [])

    def test_payload_builds_the_team(self):
        ids = self._play_block()
        self._add_event(ids[0], "A", "Кварц ST 8", "goal", 1)
        payload = totw_service.build_totw_payload(self.div_id, 1, 5)
        self.assertEqual(payload["start_round"], 1)
        self.assertEqual(payload["end_round"], 5)
        self.assertEqual(payload["division_name"], f"Сборная {self.uid}")
        # Два клуба при лимите два на клуб — в XI ровно четверо, остальное пусто.
        clubs = [p["team_name"] for p in payload["xi"]]
        self.assertEqual(len(clubs), 4)
        self.assertEqual(clubs.count(self.teams["A"]), 2)
        self.assertEqual(clubs.count(self.teams["B"]), 2)
        # Пять сухарей и пять побед — вратарь победителя лучший и капитан.
        self.assertEqual(payload["captain"]["player_name"], "Кварц GK 10")


if __name__ == "__main__":
    unittest.main()
