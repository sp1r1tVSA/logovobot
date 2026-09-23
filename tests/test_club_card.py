import os
import sys
import tempfile
import sqlite3
import datetime
import unittest

# Ensure logovobot root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
import database


class TestClubCard(unittest.TestCase):
    def setUp(self):
        """Create a temporary isolated SQLite database for each test."""
        self.tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.tf.name
        self.tf.close()

        self.orig_config_path = config.DB_PATH
        self.orig_database_path = database.DB_PATH

        config.DB_PATH = self.temp_db_path
        database.DB_PATH = self.temp_db_path
        database.init_db()

    def tearDown(self):
        """Restore original paths and cleanup temp db."""
        config.DB_PATH = self.orig_config_path
        database.DB_PATH = self.orig_database_path
        try:
            os.remove(self.temp_db_path)
        except Exception:
            pass

    def test_club_card_manager_and_stats(self):
        """Test that club card returns manager, standings, form, and retains history across manager changes."""
        # 1. Register manager for Porto
        database.register_user(1001, "porto_boss", "manager", "Порту")
        database.register_user(1002, "benfica_boss", "manager", "Бенфика")

        # 2. Add squad for Porto
        database.save_squad_players("Порту", ["Francisco Moura", "David Neres", "Galeno"])

        # 3. Create and confirm matches for Porto
        # Match 1: Porto 3 : 1 Benfica
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, status, tournament_type) "
                "VALUES (1, 'Порту', 'Бенфика', 3, 1, 'confirmed', 'league')"
            )
            m1_id = c.lastrowid
            # Events for Match 1
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'David Neres', 'Порту', 'goal', 2)", (m1_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Francisco Moura', 'Порту', 'goal', 1)", (m1_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Galeno', 'Порту', 'assist', 2)", (m1_id,))

        # Fetch club card
        card = database.get_club_card_data("Порту")
        self.assertEqual(card["team_name"], "Порту")
        self.assertIsNotNone(card["manager"])
        self.assertEqual(card["manager"]["username"], "porto_boss")
        self.assertEqual(card["league_stats"]["played"], 1)
        self.assertEqual(card["league_stats"]["wins"], 1)
        self.assertEqual(card["league_stats"]["points"], 3)
        self.assertEqual(card["league_stats"]["goals_scored"], 3)
        self.assertEqual(card["recent_form"], ["W"])

        # Check top scorers
        self.assertEqual(len(card["top_scorers"]), 2)
        self.assertEqual(card["top_scorers"][0]["player_name"], "David Neres")
        self.assertEqual(card["top_scorers"][0]["goals"], 2)

        # Check top assists
        self.assertEqual(len(card["top_assists"]), 1)
        self.assertEqual(card["top_assists"][0]["player_name"], "Galeno")
        self.assertEqual(card["top_assists"][0]["assists"], 2)

        # Check squad stats
        squad_stats = database.get_club_squad_stats("Порту")
        self.assertEqual(len(squad_stats), 3)
        neres = next(p for p in squad_stats if p["player_name"] == "David Neres")
        self.assertEqual(neres["goals"], 2)

        # 4. Change manager: Replace porto_boss with new_boss
        with database.transaction() as conn:
            conn.execute("UPDATE users SET team_name = NULL WHERE telegram_id = 1001")
        database.register_user(1003, "new_porto_boss", "manager", "Порту")

        # Club card should still have all 3 goals, 1 win, and squad intact, but with new manager!
        card_after = database.get_club_card_data("Порту")
        self.assertEqual(card_after["manager"]["username"], "new_porto_boss")
        self.assertEqual(card_after["league_stats"]["points"], 3)
        self.assertEqual(card_after["top_scorers"][0]["player_name"], "David Neres")

    def test_club_card_ranks_inside_its_own_division(self):
        """Клуб 5-го дивизиона считается по своей таблице, а не по всей лиге."""
        database.register_user(3001, "chelsea_boss", "manager", "Челси")
        database.register_user(3002, "liverpool_boss", "manager", "Ливерпуль")
        database.register_user(3003, "porto_boss", "manager", "Порту")
        season_id = database.get_active_season()["id"]
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET division_id = 5 WHERE telegram_id IN (3001, 3002)")
            c.execute("UPDATE users SET division_id = 1 WHERE telegram_id = 3003")
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, "
                "status, tournament_type, division_id, season_id) "
                "VALUES (1, 'Ливерпуль', 'Челси', 4, 2, 'confirmed', 'league', 5, ?)", (season_id,)
            )
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, "
                "status, tournament_type, division_id, season_id) "
                "VALUES (2, 'Челси', 'Ливерпуль', 3, 0, 'confirmed', 'league', 5, ?)", (season_id,)
            )

        card = database.get_club_card_data("Челси")
        self.assertEqual(card["division_id"], 5)
        stats = card["league_stats"]
        self.assertEqual((stats["played"], stats["wins"], stats["losses"]), (2, 1, 1))
        self.assertEqual((stats["goals_scored"], stats["goals_conceded"], stats["points"]), (5, 4, 3))
        # Ливерпуль тоже 3 очка, но разница у Челси лучше; Порту из 1-го дивизиона не в счёт.
        self.assertEqual(stats["rank"], 1)
        self.assertEqual(card["recent_form"], ["L", "W"])

    def test_club_card_folds_player_spellings_onto_the_squad(self):
        """'Emegha' в событиях и 'EMEGA' в заявке — один игрок, а не два бомбардира."""
        database.register_user(3101, "chelsea_boss", "manager", "Челси")
        database.save_squad_players("Челси", ["EMEGA", "ROGERS"])
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, status, tournament_type) "
                "VALUES (1, 'Челси', 'Аль-Наср', 4, 1, 'confirmed', 'league')"
            )
            m_id = c.lastrowid
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'EMEGA', 'Челси', 'goal', 1)", (m_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Emegha', 'Челси', 'goal', 3)", (m_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Emegha', 'Челси', 'assist', 1)", (m_id,))

        card = database.get_club_card_data("Челси")
        self.assertEqual(card["top_scorers"], [{"player_name": "EMEGA", "goals": 4, "assists": 1}])
        self.assertEqual(card["top_assists"][0]["player_name"], "EMEGA")

        squad = {p["player_name"]: p for p in database.get_club_squad_stats("Челси")}
        self.assertEqual(set(squad), {"EMEGA", "ROGERS"})
        self.assertEqual(squad["EMEGA"]["goals"], 4)

    def test_mini_app_squad_folds_player_spellings_onto_the_squad(self):
        """Вкладка «Состав клуба» в Mini App считает так же, как карточка: без дублей."""
        database.register_user(3102, "chelsea_boss", "manager", "Челси")
        database.save_squad_players("Челси", ["EMEGA", "ROGERS"])
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, "
                "status, tournament_type, mvp_player) "
                "VALUES (1, 'Челси', 'Аль-Наср', 4, 1, 'confirmed', 'league', 'Emegha')"
            )
            m_id = c.lastrowid
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'EMEGA', 'Челси', 'goal', 1)", (m_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Emegha', 'Челси', 'goal', 3)", (m_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Emegha', 'Челси', 'assist', 1)", (m_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'ROGERS', 'Челси', 'assist', 2)", (m_id,))

        stats = database.get_cabinet_squad_stats("Челси")
        by_name = {p["player_name"]: p for p in stats["players"]}
        self.assertEqual(set(by_name), {"EMEGA", "ROGERS"})
        self.assertEqual((by_name["EMEGA"]["goals"], by_name["EMEGA"]["assists"]), (4, 1))
        self.assertEqual(by_name["EMEGA"]["mvp_count"], 1)
        self.assertEqual(stats["top_scorer"]["player_name"], "EMEGA")
        self.assertEqual(stats["top_assistant"]["player_name"], "ROGERS")
        self.assertEqual(stats["top_mvp"], {"player_name": "EMEGA", "mvp_count": 1})

    def _seed_split_spellings(self):
        """EMEGA в составе Челси, а голы и короны записаны и как «EMEGA», и как «Emegha»."""
        database.register_user(3103, "chelsea_boss", "manager", "Челси")
        database.save_squad_players("Челси", ["EMEGA", "ROGERS"])
        with database.transaction() as conn:
            c = conn.cursor()
            ids = []
            for mvp in ("EMEGA", "Emegha"):
                c.execute(
                    "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, "
                    "status, tournament_type, mvp_player) "
                    "VALUES (1, 'Челси', 'Аль-Наср', 3, 0, 'confirmed', 'league', ?)", (mvp,)
                )
                ids.append(c.lastrowid)
            events = [
                (ids[0], "EMEGA", "goal", 1), (ids[0], "ROGERS", "assist", 1),
                (ids[1], "Emegha", "goal", 2), (ids[1], "Emegha", "assist", 1),
                (ids[1], "ROGERS", "goal", 2),
            ]
            for m_id, name, ev, cnt in events:
                c.execute(
                    "INSERT INTO match_events (match_id, player_name, team_name, event_type, count) "
                    "VALUES (?, ?, 'Челси', ?, ?)", (m_id, name, ev, cnt)
                )

    def test_league_tops_fold_player_spellings(self):
        """Топы бомбардиров, ассистентов, MVP и игрок тура не делят игрока на два написания."""
        self._seed_split_spellings()

        scorers = database.get_top_scorers(limit=1)
        self.assertEqual([(r["player_name"], r["total_goals"]) for r in scorers], [("EMEGA", 3)])

        assists = {r["player_name"]: r["total_assists"] for r in database.get_top_assists()}
        self.assertEqual(assists, {"EMEGA": 1, "ROGERS": 1})

        mvps = database.get_top_mvps()
        self.assertEqual([(r["player_name"], r["team_name"], r["mvp_count"]) for r in mvps],
                         [("EMEGA", "Челси", 2)])

        round_stats = database.get_round_player_stats(1)
        self.assertEqual([(r["player_name"], r["goals"], r["assists"]) for r in round_stats],
                         [("EMEGA", 3, 1), ("ROGERS", 2, 1)])

    def test_club_views_fold_player_spellings(self):
        """Топы клуба, карточка игрока и голы в расписании считают все написания вместе."""
        self._seed_split_spellings()

        self.assertEqual(database.get_club_top_scorers("Челси"),
                         [{"player_name": "EMEGA", "total": 3}, {"player_name": "ROGERS", "total": 2}])
        self.assertEqual(database.get_club_top_assisters("Челси"),
                         [{"player_name": "EMEGA", "total": 1}, {"player_name": "ROGERS", "total": 1}])

        card = database.get_player_card_stats("EMEGA", "Челси")
        self.assertEqual((card["total_goals"], card["total_assists"]), (3, 1))
        self.assertEqual(card["rounds"][1], {"goals": 3, "assists": 1})

    def test_ocr_spelling_is_not_a_missing_squad_player(self):
        """«Emegha» — это EMEGA из состава: его нельзя добавить в состав вторым игроком."""
        self._seed_split_spellings()

        self.assertEqual(database.get_missing_squad_players("Челси"), [])
        self.assertEqual(database.add_missing_squad_players("Челси"), 0)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("SELECT player_name FROM squad_players WHERE team_name = 'Челси' ORDER BY id")
            self.assertEqual([r["player_name"] for r in c.fetchall()], ["EMEGA", "ROGERS"])

    def test_spelling_merge_plan_and_apply(self):
        """scripts/merge_player_spellings.py: план находит «Emegha», --apply переписывает, повтор пуст."""
        self._seed_split_spellings()

        plan = database.plan_player_spelling_merges()
        self.assertEqual([(i["team_name"], i["old_name"], i["new_name"], i["method"], i["total"])
                          for i in plan["events"]],
                         [("Челси", "Emegha", "EMEGA", "fuzzy", 3)])
        self.assertEqual([(i["old_name"], i["new_name"]) for i in plan["mvp"]], [("Emegha", "EMEGA")])
        self.assertEqual(database.plan_player_spelling_merges(include_fuzzy=False),
                         {"events": [], "mvp": []})

        self.assertEqual(database.apply_player_spelling_merges(plan), {"events": 2, "mvp": 1})
        self.assertEqual(database.plan_player_spelling_merges(), {"events": [], "mvp": []})
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("SELECT DISTINCT player_name FROM match_events WHERE team_name = 'Челси' ORDER BY 1")
            self.assertEqual([r["player_name"] for r in c.fetchall()], ["EMEGA", "ROGERS"])
            c.execute("SELECT mvp_player FROM matches ORDER BY id")
            self.assertEqual([r["mvp_player"] for r in c.fetchall()], ["EMEGA", "EMEGA"])
        self.assertEqual(database.get_player_card_stats("EMEGA", "Челси")["total_goals"], 3)

    def test_club_match_history_and_summary(self):
        """Test get_club_match_history and get_all_clubs_summary."""
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, status, tournament_type) "
                "VALUES (1, 'Аякс', 'ПСВ', 2, 2, 'confirmed', 'league')"
            )
            m_id = c.lastrowid
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Brobbey', 'Аякс', 'goal', 2)", (m_id,))

        history = database.get_club_match_history("Аякс")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["opponent_team"], "ПСВ")
        self.assertEqual(history[0]["outcome"], "D")
        self.assertIn("Brobbey (2)", history[0]["scorers"])

        summary = database.get_all_clubs_summary()
        self.assertGreaterEqual(len(summary), 15)
        ajax = next(c for c in summary if c["team_name"] == "Аякс")
        self.assertEqual(ajax["points"], 1)
        self.assertEqual(ajax["draws"], 1)

    def test_club_card_image_generator(self):
        """Test that club_card_generator.generate_club_card produces valid PNG bytes without error."""
        from services.graphics import club_card_generator
        card_data = {
            "team_name": "Фейеноорд",
            "manager": {"username": "georgiy", "warn_count": 0, "telegram_id": 12345},
            "league_stats": {
                "rank": 1, "played": 22, "wins": 18, "draws": 3, "losses": 1,
                "goals_scored": 58, "goals_conceded": 24, "goal_diff": 34, "points": 57
            },
            "recent_form": ["W", "W", "D", "W", "W"],
            "cup_stats": {
                "stage": "1/4", "opponent": "Бенфика", "club_wins": 2, "opp_wins": 1, "status": "active"
            },
            "top_scorers": [{"player_name": "Serhou Guirassy", "goals": 18}, {"player_name": "Sem Steijn", "goals": 14}],
            "top_assists": [{"player_name": "Raheem Sterling", "assists": 12}, {"player_name": "Jordan Lotomba", "assists": 8}],
            "squad_count": 18,
            "debts_count": 0
        }
        buf = club_card_generator.generate_club_card(card_data)
        self.assertIsNotNone(buf)
        buf_bytes = buf.getvalue()
        self.assertGreater(len(buf_bytes), 1000)
        # PNG signature check
        self.assertTrue(buf_bytes.startswith(b'\x89PNG\r\n\x1a\n'))

    def test_club_schedule_and_results_image_generator(self):
        """Test database.get_club_schedule_and_results and club_schedule_generator."""
        from services.graphics import club_schedule_generator
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, status, tournament_type) "
                "VALUES (22, 'Фейеноорд', 'Бенфика', 5, 4, 'confirmed', 'league')"
            )
            m_id = c.lastrowid
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Guirassy', 'Фейеноорд', 'goal', 2)", (m_id,))
            c.execute("INSERT INTO match_events (match_id, player_name, team_name, event_type, count) VALUES (?, 'Steijn', 'Фейеноорд', 'goal', 2)", (m_id,))

        sched_data = database.get_club_schedule_and_results("Фейеноорд")
        self.assertEqual(sched_data["played_count"], 1)
        self.assertEqual(len(sched_data["matches"]), 1)
        self.assertEqual(sched_data["matches"][0]["outcome"], "W")
        self.assertIn("Guirassy (2)", sched_data["matches"][0]["scorers"])

        buf = club_schedule_generator.generate_club_schedule(sched_data)
        self.assertIsNotNone(buf)
        buf_bytes = buf.getvalue()
        self.assertGreater(len(buf_bytes), 1000)
        self.assertTrue(buf_bytes.startswith(b'\x89PNG\r\n\x1a\n'))

    def test_logo_lookup(self):
        """Test that get_team_logo_filename finds benfica.png for 'Бенфика' case-insensitively."""
        from services.graphics.table_generator import get_team_logo_filename
        from services.graphics import club_card_generator
        logo = get_team_logo_filename("Бенфика")
        self.assertEqual(logo, "benfica.png")
        logo_lower = get_team_logo_filename("бенфика")
        self.assertEqual(logo_lower, "benfica.png")

        # Клуб вне лиги в карте не значится: пустой бейдж, а не падение.
        self.assertIsNone(get_team_logo_filename("Расинг"))

        card_data = {
            "team_name": "Бенфика",
            "manager": {"username": "ch1lyx", "warn_count": 1, "telegram_id": 99999},
            "league_stats": {
                "rank": 1, "played": 23, "wins": 18, "draws": 1, "losses": 4,
                "goals_scored": 80, "goals_conceded": 32, "goal_diff": 48, "points": 55
            },
            "recent_form": ["L", "W", "L", "W", "W"],
            "cup_stats": {
                "stage": "FINAL", "opponent": "Брага", "club_wins": 3, "opp_wins": 2, "status": "completed"
            },
            "top_scorers": [{"player_name": "Giacomo Raspadori", "goals": 36}],
            "top_assists": [{"player_name": "Jamie Bynoe-Gittens", "assists": 23}],
            "squad_count": 11,
            "debts_count": 7
        }
        buf = club_card_generator.generate_club_card(card_data)
        self.assertIsNotNone(buf)
        self.assertTrue(buf.getvalue().startswith(b'\x89PNG\r\n\x1a\n'))

    def test_club_card_debts_excludes_future_and_open_rounds(self):
        """Test that get_club_card_data does not count future unopened rounds or open tours as debts."""
        database.register_user(2001, "racing_mgr", "manager", "Расинг")
        database.register_user(2002, "braga_mgr", "manager", "Брага")
        database.register_user(2003, "porto_mgr", "manager", "Порту")

        now = datetime.datetime.now()
        future_dl = (now + datetime.timedelta(days=3)).strftime("%d.%m.%Y %H:%M")
        past_dl = (now - datetime.timedelta(days=2)).strftime("%d.%m.%Y %H:%M")

        with database.transaction() as conn:
            c = conn.cursor()
            # Rounds 1..24 played, 25-26 open with future deadline, 27-30 future unopened
            c.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (25, 1, ?)", (future_dl,))
            c.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (26, 1, ?)", (future_dl,))
            c.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (27, 0, NULL)")
            c.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (28, 0, NULL)")
            c.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (29, 0, NULL)")
            c.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (30, 0, NULL)")

            # Pending matches for Racing in rounds 25..30
            c.execute("INSERT INTO matches (round_number, player1_team, player2_team, status) VALUES (25, 'Расинг', 'Брага', 'pending')")
            c.execute("INSERT INTO matches (round_number, player1_team, player2_team, status) VALUES (26, 'Порту', 'Расинг', 'pending')")
            c.execute("INSERT INTO matches (round_number, player1_team, player2_team, status) VALUES (27, 'Расинг', 'Брага', 'pending')")
            c.execute("INSERT INTO matches (round_number, player1_team, player2_team, status) VALUES (28, 'Расинг', 'Порту', 'pending')")
            c.execute("INSERT INTO matches (round_number, player1_team, player2_team, status) VALUES (29, 'Брага', 'Расинг', 'pending')")
            c.execute("INSERT INTO matches (round_number, player1_team, player2_team, status) VALUES (30, 'Порту', 'Расинг', 'pending')")

        card = database.get_club_card_data("Расинг")
        # Racing has 6 pending matches, but 0 debts!
        self.assertEqual(card["debts_count"], 0)
        self.assertEqual(len(card["pending_matches"]), 6)
        self.assertTrue(all(not m["is_overdue"] for m in card["pending_matches"]))

        # Now add a past closed round with expired deadline and unplayed match
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO rounds (round_number, is_open, deadline) VALUES (20, 0, ?)", (past_dl,))
            c.execute("INSERT INTO matches (round_number, player1_team, player2_team, status) VALUES (20, 'Расинг', 'Брага', 'pending')")

        card_with_debt = database.get_club_card_data("Расинг")
        self.assertEqual(card_with_debt["debts_count"], 1)

    def test_club_card_avatar_cropping_and_rendering(self):
        """Test generating club card with non-square custom avatar."""
        from PIL import Image
        from services.graphics import club_card_generator

        # Create temporary non-square avatar (200x120)
        tf_av = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        av_path = tf_av.name
        tf_av.close()

        try:
            test_img = Image.new("RGB", (200, 120), color=(100, 150, 200))
            test_img.save(av_path)

            card_data = {
                "team_name": "Расинг",
                "manager": {"username": "ch1lyx", "warn_count": 0, "telegram_id": 99999},
                "league_stats": {
                    "rank": 1, "played": 26, "wins": 20, "draws": 2, "losses": 4,
                    "goals_scored": 90, "goals_conceded": 36, "goal_diff": 54, "points": 62
                },
                "recent_form": ["L", "W", "W", "W", "D"],
                "cup_stats": {
                    "stage": "FINAL", "opponent": "Брага", "club_wins": 3, "opp_wins": 2, "status": "completed"
                },
                "top_scorers": [{"player_name": "Giacomo Raspadori", "goals": 39}],
                "top_assists": [{"player_name": "Noa Lang", "assists": 24}],
                "squad_count": 11,
                "debts_count": 0
            }

            buf = club_card_generator.generate_club_card(card_data, avatar_path=av_path)
            self.assertIsNotNone(buf)
            buf_bytes = buf.getvalue()
            self.assertTrue(buf_bytes.startswith(b'\x89PNG\r\n\x1a\n'))
        finally:
            try:
                os.remove(av_path)
            except Exception:
                pass

    def test_avatar_fetch_and_update_lifecycle(self):
        """Test get_cached_or_fetch_user_avatar lifecycle with fresh downloads and deletion."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        from handlers import cabinet

        mock_bot = MagicMock()
        mock_file_obj = AsyncMock()
        async def fake_download(buf):
            buf.write(b"fake_image_bytes_here")
        mock_file_obj.download_to_memory = fake_download
        mock_bot.get_file = AsyncMock(return_value=mock_file_obj)

        photo_item1 = MagicMock()
        photo_item1.file_id = "photo_v1"
        photos_resp1 = MagicMock()
        photos_resp1.total_count = 1
        photos_resp1.photos = [[photo_item1]]
        mock_bot.get_user_profile_photos = AsyncMock(return_value=photos_resp1)

        user_id = 888777
        cabinet._user_avatar_file_ids.clear()

        # 1. Fetch avatar for user_id -> downloads photo_v1
        path1 = asyncio.run(cabinet.get_cached_or_fetch_user_avatar(mock_bot, user_id))
        self.assertIsNotNone(path1)
        self.assertTrue(os.path.exists(path1))
        self.assertEqual(cabinet._user_avatar_file_ids.get(user_id), "photo_v1")

        # 2. Call again with unchanged file_id -> reuses local cache without calling get_file
        mock_bot.get_file.reset_mock()
        path2 = asyncio.run(cabinet.get_cached_or_fetch_user_avatar(mock_bot, user_id))
        self.assertEqual(path1, path2)
        mock_bot.get_file.assert_not_called()

        # 3. User updates avatar in Telegram -> file_id changes to photo_v2
        photo_item2 = MagicMock()
        photo_item2.file_id = "photo_v2"
        photos_resp2 = MagicMock()
        photos_resp2.total_count = 1
        photos_resp2.photos = [[photo_item2]]
        mock_bot.get_user_profile_photos = AsyncMock(return_value=photos_resp2)

        path3 = asyncio.run(cabinet.get_cached_or_fetch_user_avatar(mock_bot, user_id))
        self.assertEqual(cabinet._user_avatar_file_ids.get(user_id), "photo_v2")
        mock_bot.get_file.assert_called_once_with("photo_v2")

        # 4. User removes avatar in Telegram -> total_count = 0
        photos_resp_empty = MagicMock()
        photos_resp_empty.total_count = 0
        photos_resp_empty.photos = []
        mock_bot.get_user_profile_photos = AsyncMock(return_value=photos_resp_empty)

        path4 = asyncio.run(cabinet.get_cached_or_fetch_user_avatar(mock_bot, user_id))
        self.assertIsNone(path4)
        self.assertNotIn(user_id, cabinet._user_avatar_file_ids)
        self.assertFalse(os.path.exists(path1))


class TestLogoMapCoversTheRoster(unittest.TestCase):
    """Карта логотипов обязана идти нога в ногу с составом дивизионов.

    Клуб без записи в TEAM_LOGO_MAP молча рисуется пустым бейджем — ошибки не будет,
    просто у одного клуба в таблице не окажется герба, и заметят это уже в чате.
    Дешевле поймать расхождение здесь.
    """

    def setUp(self):
        from services.graphics.table_generator import TEAM_LOGO_MAP
        self.logo_map = TEAM_LOGO_MAP
        self.roster = [club for clubs in config.DIVISION_CLUBS.values() for club in clubs]

    def test_every_club_of_every_division_has_a_logo_filename(self):
        missing = [club for club in self.roster if club not in self.logo_map]
        self.assertEqual(missing, [], f"Клубы без логотипа: {missing}")

    def test_filenames_are_unique_per_club(self):
        """Один PNG на два клуба — это чужой герб в таблице, а не экономия."""
        files = [self.logo_map[club] for club in self.roster]
        duplicates = {f for f in files if files.count(f) > 1}
        self.assertEqual(duplicates, set(), f"Один файл на несколько клубов: {duplicates}")

    def test_map_holds_nothing_outside_the_roster(self):
        """Клуб, выбывший из лиги, обязан уходить и отсюда — иначе карта копит мусор."""
        roster_lower = {club.lower() for club in self.roster}
        extra = [k for k in self.logo_map if k.lower() not in roster_lower]
        self.assertEqual(extra, [], f"Лишние клубы в карте: {extra}")

    def test_lookup_resolves_every_club_to_its_own_file(self):
        from services.graphics.table_generator import get_team_logo_filename
        for club in self.roster:
            with self.subTest(club=club):
                self.assertEqual(get_team_logo_filename(club), self.logo_map[club])
                self.assertEqual(get_team_logo_filename(club.lower()), self.logo_map[club])


if __name__ == "__main__":
    unittest.main()
