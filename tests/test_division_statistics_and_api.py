import io
import json
import time
import urllib.parse
import hmac
import hashlib
import uuid
import database
import config
from aiohttp.test_utils import AioHTTPTestCase
from api.server import create_app
from services.graphics.table_generator import generate_league_table_image
from services.graphics.top_stats_generator import generate_top_stats_image


class TestDivisionStatisticsAndApi(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    def setUp(self):
        super().setUp()
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.div_a_id = database.create_division(name=f"Лига А {self.uid}", code=f"LIGA_A_{self.uid}")
        self.div_b_id = database.create_division(name=f"Лига Б {self.uid}", code=f"LIGA_B_{self.uid}")

        # Unique user ids
        self.p1_id = int(f"810{int(time.time()) % 100000}")
        self.p2_id = self.p1_id + 1
        self.p3_id = self.p1_id + 2
        self.p4_id = self.p1_id + 3

        database.register_user(self.p1_id, f"user_a1_{self.uid}", team_name=f"Команда А1 {self.uid}")
        database.register_user(self.p2_id, f"user_a2_{self.uid}", team_name=f"Команда А2 {self.uid}")
        database.register_user(self.p3_id, f"user_b1_{self.uid}", team_name=f"Команда Б1 {self.uid}")
        database.register_user(self.p4_id, f"user_b2_{self.uid}", team_name=f"Команда Б2 {self.uid}")

        database.assign_user_division(self.p1_id, self.div_a_id)
        database.assign_user_division(self.p2_id, self.div_a_id)
        database.assign_user_division(self.p3_id, self.div_b_id)
        database.assign_user_division(self.p4_id, self.div_b_id)

    def _generate_mock_init_data(self, user_id=12345678, username="test_bettor"):
        token = config.TOKEN or "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        user_dict = {
            "id": user_id,
            "first_name": "Tester",
            "username": username
        }
        data = {
            "auth_date": str(int(time.time())),
            "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "user": json.dumps(user_dict, separators=(",", ":"))
        }
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
        hash_val = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        data["hash"] = hash_val
        return urllib.parse.urlencode(data)

    def test_division_standings_isolation(self):
        """Test that get_standings properly isolates points, goals, and clubs per division."""
        # Create confirmed matches in Div A and Div B
        with database.transaction() as conn:
            cursor = conn.cursor()
            # Match Div A: P1 beats P2 3-1
            cursor.execute("""
                INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, player1_score, player2_score, status, division_id)
                VALUES (1, ?, ?, ?, ?, 3, 1, 'confirmed', ?)
            """, (self.p1_id, self.p2_id, f"Команда А1 {self.uid}", f"Команда А2 {self.uid}", self.div_a_id))
            m_a_id = cursor.lastrowid

            # Match Div B: P4 beats P3 2-0 (P3 home, P4 away)
            cursor.execute("""
                INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, player1_score, player2_score, status, division_id)
                VALUES (1, ?, ?, ?, ?, 0, 2, 'confirmed', ?)
            """, (self.p3_id, self.p4_id, f"Команда Б1 {self.uid}", f"Команда Б2 {self.uid}", self.div_b_id))
            m_b_id = cursor.lastrowid

        # Standings Div A
        standings_a = database.get_standings(division_id=self.div_a_id)
        teams_a = [s["team_name"] for s in standings_a]
        self.assertIn(f"Команда А1 {self.uid}", teams_a)
        self.assertIn(f"Команда А2 {self.uid}", teams_a)
        self.assertNotIn(f"Команда Б1 {self.uid}", teams_a)
        self.assertNotIn(f"Команда Б2 {self.uid}", teams_a)

        winner_a = standings_a[0]
        self.assertEqual(winner_a["team_name"], f"Команда А1 {self.uid}")
        self.assertEqual(winner_a["points"], 3)
        self.assertEqual(winner_a["goals_scored"], 3)
        self.assertEqual(winner_a["goals_conceded"], 1)

        # Standings Div B
        standings_b = database.get_standings(division_id=self.div_b_id)
        teams_b = [s["team_name"] for s in standings_b]
        self.assertIn(f"Команда Б1 {self.uid}", teams_b)
        self.assertIn(f"Команда Б2 {self.uid}", teams_b)
        self.assertNotIn(f"Команда А1 {self.uid}", teams_b)

        winner_b = standings_b[0]
        self.assertEqual(winner_b["team_name"], f"Команда Б2 {self.uid}")
        self.assertEqual(winner_b["points"], 3)
        self.assertEqual(winner_b["goals_scored"], 2)
        self.assertEqual(winner_b["goals_conceded"], 0)

        # Global legacy standings call should not crash
        global_standings = database.get_standings()
        self.assertIsInstance(global_standings, list)

    def test_division_top_stats_isolation(self):
        """Test that get_top_scorers and get_top_assists filter by division_id."""
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO matches (round_number, player1_id, player2_id, status, division_id)
                VALUES (1, ?, ?, 'confirmed', ?)
            """, (self.p1_id, self.p2_id, self.div_a_id))
            m_a = cursor.lastrowid

            cursor.execute("""
                INSERT INTO matches (round_number, player1_id, player2_id, status, division_id)
                VALUES (1, ?, ?, 'confirmed', ?)
            """, (self.p3_id, self.p4_id, self.div_b_id))
            m_b = cursor.lastrowid

            # Goals & assists for Div A
            cursor.execute("""
                INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                VALUES (?, 'TeamA', 'StrikerA', 'goal', 4)
            """, (m_a,))
            cursor.execute("""
                INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                VALUES (?, 'TeamA', 'PasserA', 'assist', 3)
            """, (m_a,))

            # Goals & assists for Div B
            cursor.execute("""
                INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                VALUES (?, 'TeamB', 'StrikerB', 'goal', 5)
            """, (m_b,))
            cursor.execute("""
                INSERT INTO match_events (match_id, team_name, player_name, event_type, count)
                VALUES (?, 'TeamB', 'PasserB', 'assist', 2)
            """, (m_b,))

        # Test Top Scorers Div A vs Div B
        scorers_a = database.get_top_scorers(division_id=self.div_a_id)
        players_a = [s["player_name"] for s in scorers_a]
        self.assertIn("StrikerA", players_a)
        self.assertNotIn("StrikerB", players_a)

        scorers_b = database.get_top_scorers(division_id=self.div_b_id)
        players_b = [s["player_name"] for s in scorers_b]
        self.assertIn("StrikerB", players_b)
        self.assertNotIn("StrikerA", players_b)

        # Test Top Assists Div A vs Div B
        assists_a = database.get_top_assists(division_id=self.div_a_id)
        passers_a = [a["player_name"] for a in assists_a]
        self.assertIn("PasserA", passers_a)
        self.assertNotIn("PasserB", passers_a)

    def test_graphic_generation_with_division(self):
        """Test that graphics generators render with division_name without errors."""
        standings = database.get_standings(division_id=self.div_a_id)
        buf_table = generate_league_table_image(standings=standings, division_name="Премьер-Лига")
        self.assertIsInstance(buf_table, io.BytesIO)
        self.assertGreater(len(buf_table.getvalue()), 1000)

        buf_stats = generate_top_stats_image(mode="goals", division_id=self.div_a_id, division_name="Премьер-Лига")
        self.assertIsInstance(buf_stats, io.BytesIO)
        self.assertGreater(len(buf_stats.getvalue()), 1000)

    async def test_miniapp_endpoints_with_divisions(self):
        """Test Mini App REST API endpoints for divisions and division filtering."""
        headers = {"X-Telegram-Init-Data": self._generate_mock_init_data()}

        # 1. GET /api/divisions
        resp = await self.client.request("GET", "/api/divisions", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        div_ids = [d["id"] for d in data["divisions"]]
        self.assertIn(self.div_a_id, div_ids)
        self.assertIn(self.div_b_id, div_ids)

        # 2. GET /api/tournaments/1/standings?division_id=X
        resp = await self.client.request("GET", f"/api/tournaments/1/standings?division_id={self.div_a_id}", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIsInstance(data["standings"], list)

        # 3. GET /api/matches?division_id=X
        resp = await self.client.request("GET", f"/api/matches?division_id={self.div_a_id}", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        for m in data["matches"]:
            self.assertEqual(m["division_id"], self.div_a_id)

        # 4. GET /api/tournaments/1/top-scorers?division_id=X
        resp = await self.client.request("GET", f"/api/tournaments/1/top-scorers?division_id={self.div_a_id}", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("top_scorers", data)
        self.assertIn("top_assists", data)
        self.assertIn("top_combined", data)
        self.assertFalse(data["is_overall"])

        # 5. GET /api/top-scorers?division_id=all (overall across all divisions)
        resp_all = await self.client.request("GET", "/api/top-scorers?division_id=all", headers=headers)
        self.assertEqual(resp_all.status, 200)
        data_all = await resp_all.json()
        self.assertEqual(data_all["status"], "ok")
        self.assertTrue(data_all["is_overall"])
        self.assertIn("top_scorers", data_all)
        self.assertIn("top_assists", data_all)
        self.assertIn("top_combined", data_all)
        self.assertIn("top_mvps", data_all)
