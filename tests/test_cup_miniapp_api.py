"""
tests/test_cup_miniapp_api.py

Кубок в Mini App: роуты `/api/cup*`, полная роспись кубкового матча и то, как
кубковые ноги выглядят в истории купонов.

Главное, что здесь закреплено:
- линия закрытого этапа отдаётся без коэффициентов, даже если рынки ещё открыты —
  Mini App не должна показывать цену, по которой ставку всё равно не примут;
- `/api/matches/{id}/markets` берёт имена заголовка серии из `cup_series` и НЕ
  заводит кубку рынки лиговой моделью (в ней есть ничья, в кубке её нет);
- у заголовка серии в `matches` нет имён клубов, поэтому история и уведомления
  подставляют пару из `cup_series`, а не «Хозяева — Гости».
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from services import betting_engine, odds_engine

# Заведомо недействительный тестовый токен из документации Telegram.
TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

STAGE = "1/64"


class TestCupMiniAppApi(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._original_token = config.TOKEN
        config.TOKEN = TEST_BOT_TOKEN
        await super().asyncSetUp()

        if TestCupMiniAppApi._seeded is None:
            TestCupMiniAppApi._seeded = self._seed_cup()
        (self.season, self.stage_id, self.bettor, self.club_a, self.club_b,
         self.header_1, self.game_1) = TestCupMiniAppApi._seeded

    # `cup_series` уникальна по (stage, series_num) без сезона, поэтому сетка
    # заводится один раз на модуль, а не на каждый тест.
    _seeded = None

    def _seed_cup(self):
        uid = uuid.uuid4().hex[:6].upper()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO seasons (name, status) VALUES (?, 'active')",
                (f"Cup API Season {uid}",)
            )
            season = cursor.lastrowid

        club_a, club_b = f"Клуб А {uid}", f"Клуб Б {uid}"
        club_c, club_d = f"Клуб В {uid}", f"Клуб Г {uid}"
        bettor = 96401
        database.register_user(bettor, f"cup_api_{uid}", team_name=f"Клуб Х {uid}")
        database.add_coins(bettor, 50_000)

        database.create_cup_series(STAGE, [(club_a, club_b), (club_c, club_d)], season_id=season)
        stage_id = database.get_cup_stage(STAGE, season_id=season)["id"]
        database.provision_cup_stage_line(STAGE, season_id=season)
        ok, message = database.open_cup_stage_bets(stage_id)
        self.assertTrue(ok, message)
        betting_engine.generate_stage_markets(STAGE, season_id=season)

        rows = database.get_cup_stage_matches(STAGE, season_id=season)
        header_1 = next(r for r in rows if r["is_series_header"] and r["series_num"] == 1)["match_id"]
        game_1 = next(
            r for r in rows
            if not r["is_series_header"] and r["series_num"] == 1 and r["game_num_in_series"] == 1
        )["match_id"]
        return season, stage_id, bettor, club_a, club_b, header_1, game_1

    async def asyncTearDown(self):
        await super().asyncTearDown()
        config.TOKEN = self._original_token

    def _headers(self, user_id: int) -> dict:
        user_dict = {"id": user_id, "first_name": "Bettor", "username": f"cup_{user_id}"}
        data = {
            "auth_date": str(int(time.time())),
            "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "user": json.dumps(user_dict, separators=(",", ":")),
        }
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        data["hash"] = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {"X-Telegram-Init-Data": urllib.parse.urlencode(data)}

    async def _get(self, path: str, authed: bool = True):
        headers = self._headers(self.bettor) if authed else {}
        resp = await self.client.request("GET", path, headers=headers)
        return resp.status, await resp.json()

    # --- 1. доступ ---------------------------------------------------------

    async def test_01_cup_routes_require_init_data(self):
        for path in (
            "/api/cup",
            f"/api/cup/stages/{self.stage_id}/line",
            f"/api/cup/stages/{self.stage_id}/bracket",
        ):
            status, body = await self._get(path, authed=False)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "unauthorized")

    async def test_02_unknown_and_malformed_stage(self):
        status, _ = await self._get("/api/cup/stages/999999/line")
        self.assertEqual(status, 404)
        status, _ = await self._get("/api/cup/stages/999999/bracket")
        self.assertEqual(status, 404)
        status, _ = await self._get("/api/cup/stages/abc/line")
        self.assertEqual(status, 400)

    # --- 2. обзор, линия, сетка ------------------------------------------

    async def test_03_overview_opens_the_stage_with_open_bets(self):
        status, body = await self._get("/api/cup")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["current_stage_id"], self.stage_id)
        stage = next(s for s in body["stages"] if s["id"] == self.stage_id)
        self.assertEqual(stage["stage"], STAGE)
        self.assertTrue(stage["bets_open"])
        self.assertEqual(stage["series_total"], 2)
        self.assertEqual(stage["series_completed"], 0)

    async def test_04_open_line_carries_priced_tiles_without_a_draw(self):
        status, body = await self._get(f"/api/cup/stages/{self.stage_id}/line")
        self.assertEqual(status, 200)
        self.assertTrue(body["stage"]["bets_open"])
        self.assertEqual(len(body["series"]), 2)

        first = next(s for s in body["series"] if s["series_num"] == 1)
        self.assertEqual((first["team1_name"], first["team2_name"]), (self.club_a, self.club_b))
        self.assertIsNotNone(first["header"])
        self.assertEqual(first["header"]["match_id"], self.header_1)
        self.assertEqual(len(first["games"]), 3, "все три игры серии заводятся сразу")

        game = next(g for g in first["games"] if g["match_id"] == self.game_1)
        self.assertTrue(game["is_line"])
        self.assertGreater(game["odds"]["p1"], 1.0)
        self.assertGreater(game["odds"]["p2"], 1.0)
        self.assertNotIn("x", game["odds"], "ничьей в кубке нет")

    async def test_05_closed_stage_line_hides_odds(self):
        with database.transaction() as conn:
            conn.execute("UPDATE cup_stages SET bets_open = 0 WHERE id = ?", (self.stage_id,))
        try:
            status, body = await self._get(f"/api/cup/stages/{self.stage_id}/line")
        finally:
            with database.transaction() as conn:
                conn.execute("UPDATE cup_stages SET bets_open = 1 WHERE id = ?", (self.stage_id,))
        self.assertEqual(status, 200)
        self.assertFalse(body["stage"]["bets_open"])
        tiles = [t for s in body["series"] for t in [s["header"], *s["games"]] if t]
        self.assertTrue(tiles)
        for tile in tiles:
            self.assertEqual(tile["odds"], {})
            self.assertFalse(tile["is_line"])

    async def test_06_bracket_lists_series_with_games(self):
        status, body = await self._get(f"/api/cup/stages/{self.stage_id}/bracket")
        self.assertEqual(status, 200)
        self.assertEqual(body["stage"]["id"], self.stage_id)
        self.assertEqual(len(body["series"]), 2)
        first = next(s for s in body["series"] if s["series_num"] == 1)
        self.assertEqual((first["team1_wins"], first["team2_wins"]), (0, 0))
        self.assertIsNone(first["winner_name"])
        self.assertEqual([g["game_num"] for g in first["games"]], [1, 2, 3])
        self.assertNotIn(self.header_1, [g["match_id"] for g in first["games"]],
                         "заголовок серии — не игра")

    # --- 3. полная роспись кубкового матча -------------------------------

    async def test_07_header_markets_use_series_names_and_are_not_generated(self):
        called = []
        original = odds_engine.generate_match_markets
        odds_engine.generate_match_markets = lambda *a, **kw: called.append(a) or []
        try:
            status, body = await self._get(f"/api/matches/{self.header_1}/markets")
        finally:
            odds_engine.generate_match_markets = original
        self.assertEqual(status, 200)
        self.assertEqual((body["team1_name"], body["team2_name"]), (self.club_a, self.club_b))
        self.assertTrue(body["markets"], "рынки серии уже выставлены панелью этапа")
        self.assertEqual(called, [], "кубку рынки на лету не заводятся")

    def test_08_generate_match_markets_refuses_cup(self):
        before = {m["market_key"]: m for m in odds_engine.get_match_markets(self.game_1)}
        result = odds_engine.generate_match_markets(self.game_1, self.club_a, self.club_b)
        after = {m["market_key"]: m for m in result}
        self.assertEqual(set(before), set(after))
        keys = {s["selection_key"] for s in after["1x2"]["selections"]}
        self.assertNotIn("x", keys, "лиговая модель не дописала кубку ничью")

    # --- 4. история и уведомления ----------------------------------------

    def test_09_header_leg_in_history_shows_series_pair(self):
        ok, bet_id = database.place_user_bet(
            self.bettor, 100, [{"match_id": self.header_1, "outcome": "p1"}]
        )
        self.assertTrue(ok, bet_id)

        bet = next(b for b in database.get_user_bets(self.bettor) if b["id"] == bet_id)
        leg = bet["items"][0]
        self.assertEqual((leg["team1_name"], leg["team2_name"]), (self.club_a, self.club_b))
        self.assertEqual(leg["tournament_type"], "cup")
        self.assertEqual(leg["cup_stage"], STAGE)
        self.assertEqual(leg["is_series_header"], 1)

        single = database.get_user_bet_by_id(user_id=self.bettor, bet_id=bet_id)
        self.assertEqual(
            (single["items"][0]["team1_name"], single["items"][0]["team2_name"]),
            (self.club_a, self.club_b),
        )

        with database.transaction() as conn:
            legs = database.get_bet_legs_for_notice(conn.cursor(), bet_id)
        self.assertEqual((legs[0]["team1_name"], legs[0]["team2_name"]), (self.club_a, self.club_b))

    def test_10_game_leg_keeps_its_own_names(self):
        ok, bet_id = database.place_user_bet(
            self.bettor, 100, [{"match_id": self.game_1, "outcome": "p2"}]
        )
        self.assertTrue(ok, bet_id)
        bet = next(b for b in database.get_user_bets(self.bettor) if b["id"] == bet_id)
        leg = bet["items"][0]
        self.assertEqual((leg["team1_name"], leg["team2_name"]), (self.club_a, self.club_b))
        self.assertEqual(leg["game_num_in_series"], 1)
        self.assertEqual(leg["is_series_header"], 0)
