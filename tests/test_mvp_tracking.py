"""Отслеживание «Игрока матча» (MVP): OCR → SQLite → REST API.

Золотая корона со скриншота EA FC Mobile проходит четыре слоя: нормализацию имени
в `ai_recognizer`, колонку `matches.mvp_player`, агрегаты `get_top_mvps` /
`get_cabinet_squad_stats` и два эндпоинта Mini App. Проверяется весь путь, а
отдельно — изоляция по дивизионам: корона из чужого дивизиона не должна попадать
в таблицу лидеров.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest
import urllib.parse
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from handlers import cabinet
from services.ai.ai_recognizer import clean_mvp_name

# Заведомо недействительный токен из документации Telegram: настоящий
# TELEGRAM_BOT_TOKEN в тестах не используется и в репозиторий не попадает.
TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _init_data(user_id: int, username: str = "mvp_tester") -> str:
    """Собрать подписанный initData так же, как это делает Telegram."""
    user_dict = {"id": user_id, "first_name": "Coach", "username": username}
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
    return urllib.parse.urlencode(data)


def _headers(user_id: int) -> dict:
    return {"X-Telegram-Init-Data": _init_data(user_id)}


class TestMvpNameCleaning(unittest.TestCase):
    """Постобработка поля `mvp_player`, пришедшего от Gemini."""

    def test_strips_badges_and_keeps_clean_name(self):
        self.assertEqual(clean_mvp_name("Ricardo Horta"), "Ricardo Horta")
        self.assertEqual(clean_mvp_name("  Ricardo Horta  "), "Ricardo Horta")

    def test_missing_mvp_is_none_not_empty_string(self):
        """Все способы, которыми модель пишет «короны нет», сводятся к None."""
        for raw in (None, "", "   ", "null", "NULL", "None", "nan", "-", "—", "нет"):
            self.assertIsNone(clean_mvp_name(raw), f"raw={raw!r}")


class TestMvpReportPayload(unittest.TestCase):
    """Корона внутри личного кабинета: из user_data в отчёт и на карточку."""

    def _context(self, **user_data):
        return types.SimpleNamespace(user_data=dict(user_data))

    @property
    def _match(self):
        return {"id": 1, "player1_team": "Бавария", "player2_team": "Реал"}

    def test_payload_carries_recognized_mvp(self):
        ctx = self._context(
            report_home_goals=2, report_away_goals=1,
            home_goals_count={"Kane": 2}, away_goals_count={"Mbappe": 1},
            report_mvp_player="Harry Kane",
        )
        payload = cabinet.collect_report_payload(ctx, self._match)
        self.assertEqual(payload["mvp_player"], "Harry Kane")

    def test_payload_without_mvp_is_none(self):
        """Ручной ввод короны не даёт — в базу должен уйти NULL, а не пустая строка."""
        ctx = self._context(report_home_goals=0, report_away_goals=0)
        self.assertIsNone(cabinet.collect_report_payload(ctx, self._match)["mvp_player"])

    def test_card_line_shown_only_with_mvp(self):
        line = cabinet._mvp_card_line({"mvp_player": "Harry Kane"})
        self.assertIn("Harry Kane", line)
        self.assertIn("👑", line)
        for empty in ({}, {"mvp_player": None}, {"mvp_player": "   "}):
            self.assertEqual(cabinet._mvp_card_line(empty), "")

    def test_card_line_escapes_html(self):
        self.assertNotIn("<b>Kane", cabinet._mvp_card_line({"mvp_player": "<b>Kane"}))


class TestMvpDatabase(unittest.TestCase):
    """Хранение короны в `matches.mvp_player` и агрегаты поверх неё."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()

        self.div_a = database.create_division(name=f"MVP Дивизион A {self.uid}", code=f"MVPA_{self.uid}")
        self.div_b = database.create_division(name=f"MVP Дивизион B {self.uid}", code=f"MVPB_{self.uid}")
        self.divisions = (self.div_a, self.div_b)

        season = database.get_active_season()
        self.season_id = season["id"] if season else 1

        self.team_a1 = f"MVP Alpha {self.uid}"
        self.team_a2 = f"MVP Beta {self.uid}"
        self.team_b1 = f"MVP Gamma {self.uid}"
        self.team_b2 = f"MVP Delta {self.uid}"
        self.teams = (self.team_a1, self.team_a2, self.team_b1, self.team_b2)

        self.coach_a1 = 97401
        self.coach_a2 = 97402
        self.coach_b1 = 97403
        self.coach_b2 = 97404
        self.user_ids = (self.coach_a1, self.coach_a2, self.coach_b1, self.coach_b2)

        for tg_id, team, div in (
            (self.coach_a1, self.team_a1, self.div_a),
            (self.coach_a2, self.team_a2, self.div_a),
            (self.coach_b1, self.team_b1, self.div_b),
            (self.coach_b2, self.team_b2, self.div_b),
        ):
            database.register_user(tg_id, f"mvp_coach_{tg_id}_{self.uid}", team_name=team)
            database.assign_user_division(tg_id, div)

        self.star = f"MVP Star {self.uid}"
        self.rookie = f"MVP Rookie {self.uid}"
        self.outsider = f"MVP Outsider {self.uid}"
        database.add_squad(self.team_a1, [self.star, self.rookie])
        database.add_squad(self.team_b1, [self.outsider])

        self.match_ids = []

    def tearDown(self):
        with database.transaction() as conn:
            c = conn.cursor()
            if self.match_ids:
                c.execute(
                    f"DELETE FROM match_events WHERE match_id IN ({','.join('?' * len(self.match_ids))})",
                    self.match_ids,
                )
            c.execute(
                f"DELETE FROM matches WHERE division_id IN ({','.join('?' * len(self.divisions))})",
                self.divisions,
            )
            c.execute(
                f"DELETE FROM squad_players WHERE team_name IN ({','.join('?' * len(self.teams))})",
                self.teams,
            )
            c.execute(
                f"DELETE FROM users WHERE telegram_id IN ({','.join('?' * len(self.user_ids))})",
                self.user_ids,
            )
            c.execute(
                f"DELETE FROM divisions WHERE id IN ({','.join('?' * len(self.divisions))})",
                self.divisions,
            )

    # ------------------------------------------------------------------ utils
    def _create_match(self, division_id: int, home_team: str, away_team: str,
                      home_id: int, away_id: int) -> int:
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, "
                "status, division_id, season_id, tournament_type) "
                "VALUES (1, ?, ?, ?, ?, 'pending', ?, ?, 'league')",
                (home_id, away_id, home_team, away_team, division_id, self.season_id),
            )
            match_id = c.lastrowid
        self.match_ids.append(match_id)
        return match_id

    def _read_mvp(self, match_id: int):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("SELECT mvp_player FROM matches WHERE id = ?", (match_id,))
            return c.fetchone()["mvp_player"]

    # ------------------------------------------------------------------ tests
    def test_confirm_stores_and_clears_mvp_player(self):
        """1. Корона сохраняется в матче; пустое значение ложится в NULL."""
        match_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                      self.coach_a1, self.coach_a2)
        database.confirm_and_finalize_match(
            match_id, 2, 1,
            [(self.team_a1, self.star, "goal", 2)],
            reporter_id=self.coach_a1,
            mvp_player=self.star,
        )
        self.assertEqual(self._read_mvp(match_id), self.star)

        # Матч без золотой короны: пустая строка не должна стать «безымянным» MVP.
        blank_id = self._create_match(self.div_a, self.team_a2, self.team_a1,
                                      self.coach_a2, self.coach_a1)
        database.confirm_and_finalize_match(
            blank_id, 0, 0, [], reporter_id=self.coach_a2, mvp_player="   "
        )
        self.assertIsNone(self._read_mvp(blank_id))

        # Сброс матча снимает награду вместе с результатом.
        database.reset_match(match_id)
        self.assertIsNone(self._read_mvp(match_id))

    def test_get_top_mvps_counts_and_isolates_divisions(self):
        """2. Лидеры MVP считаются по дивизиону и не смешиваются между ними."""
        for _ in range(2):
            m_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                      self.coach_a1, self.coach_a2)
            database.confirm_and_finalize_match(
                m_id, 1, 0, [(self.team_a1, self.star, "goal", 1)],
                reporter_id=self.coach_a1, mvp_player=self.star,
            )
        m_id = self._create_match(self.div_a, self.team_a2, self.team_a1,
                                  self.coach_a2, self.coach_a1)
        database.confirm_and_finalize_match(
            m_id, 0, 1, [(self.team_a1, self.rookie, "goal", 1)],
            reporter_id=self.coach_a2, mvp_player=self.rookie,
        )

        # Чужой дивизион: своя корона, которая не должна протечь в дивизион A.
        m_id = self._create_match(self.div_b, self.team_b1, self.team_b2,
                                  self.coach_b1, self.coach_b2)
        database.confirm_and_finalize_match(
            m_id, 3, 0, [(self.team_b1, self.outsider, "goal", 3)],
            reporter_id=self.coach_b1, mvp_player=self.outsider,
        )

        top_a = database.get_top_mvps(division_id=self.div_a)
        names_a = [row["player_name"] for row in top_a]
        self.assertIn(self.star, names_a)
        self.assertIn(self.rookie, names_a)
        self.assertNotIn(self.outsider, names_a)

        # Лидер идёт первым, счёт наград совпадает с числом матчей.
        self.assertEqual(top_a[0]["player_name"], self.star)
        self.assertEqual(top_a[0]["mvp_count"], 2)
        self.assertEqual(top_a[0]["team_name"], self.team_a1)

        top_b = database.get_top_mvps(division_id=self.div_b)
        self.assertEqual([row["player_name"] for row in top_b], [self.outsider])
        self.assertEqual(top_b[0]["mvp_count"], 1)

        # Незавершённый матч наград не даёт.
        pending_id = self._create_match(self.div_b, self.team_b2, self.team_b1,
                                        self.coach_b2, self.coach_b1)
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET mvp_player = ? WHERE id = ?", (self.outsider, pending_id)
            )
        self.assertEqual(database.get_top_mvps(division_id=self.div_b)[0]["mvp_count"], 1)

    def test_get_match_exposes_mvp_player(self):
        """Строка матча отдаёт корону: посты после подтверждения берут её оттуда."""
        match_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                      self.coach_a1, self.coach_a2)
        database.confirm_and_finalize_match(
            match_id, 1, 0, [(self.team_a1, self.star, "goal", 1)],
            reporter_id=self.coach_a1, mvp_player=self.star,
        )
        self.assertEqual(database.get_match(match_id).get("mvp_player"), self.star)

    def test_pending_report_round_trip_keeps_mvp(self):
        """Отложенный отчёт переживает корону: соперник/админ подтверждает с ней."""
        match_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                      self.coach_a1, self.coach_a2)
        payload = {
            "h_score": 1, "a_score": 0,
            "scorers": [{"player_name": self.star, "team_name": self.team_a1, "count": 1}],
            "assists": [], "photo_id": None, "mvp_player": self.star,
        }
        database.save_pending_report(match_id, self.coach_a1, payload)
        self.assertEqual(database.get_pending_report(match_id).get("mvp_player"), self.star)
        database.delete_pending_report(match_id)

    def test_cabinet_squad_stats_reports_mvp(self):
        """3. Состав клуба: mvp_count у игрока и лидер top_mvp у клуба."""
        m_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                  self.coach_a1, self.coach_a2)
        database.confirm_and_finalize_match(
            m_id, 2, 0, [(self.team_a1, self.star, "goal", 2)],
            reporter_id=self.coach_a1, mvp_player=self.star,
        )

        squad = database.get_cabinet_squad_stats(self.team_a1)
        by_name = {p["player_name"]: p for p in squad["players"]}
        self.assertEqual(by_name[self.star]["mvp_count"], 1)
        self.assertEqual(by_name[self.rookie]["mvp_count"], 0)
        self.assertEqual(squad["top_mvp"], {"player_name": self.star, "mvp_count": 1})

        # Корона соперника в общем матче не засчитывается чужому клубу.
        self.assertIsNone(database.get_cabinet_squad_stats(self.team_a2)["top_mvp"])

    def test_cabinet_squad_stats_shows_mvp_without_goals(self):
        """Корона вратарю: ни гола, ни ассиста — строка игрока всё равно нужна."""
        keeper = f"MVP Keeper {self.uid}"
        database.add_squad(self.team_a1, [keeper])
        m_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                  self.coach_a1, self.coach_a2)
        database.confirm_and_finalize_match(
            m_id, 0, 0, [], reporter_id=self.coach_a1, mvp_player=keeper,
        )

        squad = database.get_cabinet_squad_stats(self.team_a1)
        by_name = {p["player_name"]: p for p in squad["players"]}
        self.assertEqual(by_name[keeper]["mvp_count"], 1)
        self.assertEqual(squad["top_mvp"], {"player_name": keeper, "mvp_count": 1})

    def test_cabinet_squad_stats_shows_mvp_outside_the_roster(self):
        """Игрок забил и получил корону раньше, чем состав попал в squad_players."""
        newcomer = f"MVP Newcomer {self.uid}"
        m_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                  self.coach_a1, self.coach_a2)
        database.confirm_and_finalize_match(
            m_id, 1, 0, [(self.team_a1, newcomer, "goal", 1)],
            reporter_id=self.coach_a1, mvp_player=newcomer,
        )

        squad = database.get_cabinet_squad_stats(self.team_a1)
        by_name = {p["player_name"]: p for p in squad["players"]}
        self.assertEqual(by_name[newcomer]["mvp_count"], 1)
        self.assertEqual(by_name[newcomer]["goals"], 1)

    def test_admin_score_correction_clears_the_crown(self):
        """Админ переписал счёт: события снесены, значит и корона недействительна."""
        m_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                  self.coach_a1, self.coach_a2)
        database.confirm_and_finalize_match(
            m_id, 2, 0, [(self.team_a1, self.star, "goal", 2)],
            reporter_id=self.coach_a1, mvp_player=self.star,
        )
        self.assertEqual(self._read_mvp(m_id), self.star)

        database.admin_set_match_score(m_id, 0, 3)
        self.assertIsNone(self._read_mvp(m_id))
        self.assertIsNone(database.get_cabinet_squad_stats(self.team_a1)["top_mvp"])

    def test_technical_result_clears_the_crown(self):
        """Техническое поражение отменяет разбор матча вместе с наградой."""
        m_id = self._create_match(self.div_a, self.team_a1, self.team_a2,
                                  self.coach_a1, self.coach_a2)
        database.confirm_and_finalize_match(
            m_id, 2, 0, [(self.team_a1, self.star, "goal", 2)],
            reporter_id=self.coach_a1, mvp_player=self.star,
        )
        database.set_technical_result(m_id, 0, 3, technical_type="tp_away")
        self.assertIsNone(self._read_mvp(m_id))


class TestMvpNameResolution(unittest.TestCase):
    """Имя с короны приводится к написанию состава — как голы и ассисты."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.home = f"MVP Res Home {self.uid}"
        self.away = f"MVP Res Away {self.uid}"
        database.add_squad(self.home, ["Harry Kane", "Joshua Kimmich"])
        database.add_squad(self.away, ["Vinicius Junior"])

    def tearDown(self):
        with database.transaction() as conn:
            conn.cursor().execute(
                "DELETE FROM squad_players WHERE team_name IN (?, ?)", (self.home, self.away)
            )

    def test_ocr_form_maps_to_the_declared_spelling(self):
        resolved = cabinet.resolve_mvp_player_name("H. Kane", self.home, self.away)
        self.assertEqual(resolved, "Harry Kane")

    def test_crown_of_the_away_side_is_resolved_too(self):
        """Корона достаётся сопернику не реже, чем хозяину."""
        self.assertEqual(
            cabinet.resolve_mvp_player_name("Vinicius", self.home, self.away),
            "Vinicius Junior",
        )

    def test_unknown_name_is_kept_as_read(self):
        """Игрока нет ни в одной заявке — сырое имя лучше потерянной короны."""
        self.assertEqual(
            cabinet.resolve_mvp_player_name("Lamine Yamal", self.home, self.away),
            "Lamine Yamal",
        )

    def test_ambiguous_name_is_not_guessed(self):
        """Тёзки в обоих составах: угадывать клуб опаснее, чем оставить как есть."""
        twin_home = f"MVP Twin Home {self.uid}"
        twin_away = f"MVP Twin Away {self.uid}"
        database.add_squad(twin_home, ["Rodrigo Silva"])
        database.add_squad(twin_away, ["Rodrigo Costa"])
        try:
            self.assertEqual(
                cabinet.resolve_mvp_player_name("Rodrigo", twin_home, twin_away), "Rodrigo"
            )
        finally:
            with database.transaction() as conn:
                conn.cursor().execute(
                    "DELETE FROM squad_players WHERE team_name IN (?, ?)", (twin_home, twin_away)
                )

    def test_missing_crown_stays_none(self):
        for raw in (None, "", "   "):
            self.assertIsNone(cabinet.resolve_mvp_player_name(raw, self.home, self.away))


class TestMvpApi(AioHTTPTestCase):
    """Оба эндпоинта Mini App, которым нужны короны."""

    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._original_token = config.TOKEN
        config.TOKEN = TEST_BOT_TOKEN
        await super().asyncSetUp()

        uid = uuid.uuid4().hex[:6].upper()
        self.uid = uid
        self.division_id = database.create_division(name=f"MVP API Дивизион {uid}", code=f"MVPAPI_{uid}")

        self.owner_id = 97501
        self.rival_id = 97502
        self.user_ids = (self.owner_id, self.rival_id)
        self.owner_team = f"MVP API Owner {uid}"
        self.rival_team = f"MVP API Rival {uid}"
        self.teams = (self.owner_team, self.rival_team)

        for tg_id, team in ((self.owner_id, self.owner_team), (self.rival_id, self.rival_team)):
            database.register_user(tg_id, f"mvp_api_{tg_id}_{uid}", team_name=team)
            database.assign_user_division(tg_id, self.division_id)

        season = database.get_active_season()
        self.season_id = season["id"] if season else 1

        self.star = f"MVP API Star {uid}"
        database.add_squad(self.owner_team, [self.star])

        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, "
                "status, division_id, season_id, tournament_type) "
                "VALUES (1, ?, ?, ?, ?, 'pending', ?, ?, 'league')",
                (self.owner_id, self.rival_id, self.owner_team, self.rival_team,
                 self.division_id, self.season_id),
            )
            self.match_id = c.lastrowid

        database.confirm_and_finalize_match(
            self.match_id, 3, 1,
            [(self.owner_team, self.star, "goal", 3)],
            reporter_id=self.owner_id,
            mvp_player=self.star,
        )

    async def asyncTearDown(self):
        try:
            with database.transaction() as conn:
                c = conn.cursor()
                c.execute("DELETE FROM match_events WHERE match_id = ?", (self.match_id,))
                c.execute("DELETE FROM matches WHERE division_id = ?", (self.division_id,))
                c.execute("DELETE FROM squad_players WHERE team_name IN (?, ?)", self.teams)
                c.execute("DELETE FROM users WHERE telegram_id IN (?, ?)", self.user_ids)
                c.execute("DELETE FROM divisions WHERE id = ?", (self.division_id,))
        finally:
            await super().asyncTearDown()
            config.TOKEN = self._original_token

    async def test_top_scorers_endpoint_returns_top_mvps(self):
        """4. /api/tournaments/{id}/top-scorers отдаёт top_mvps рядом с бомбардирами."""
        resp = await self.client.request(
            "GET",
            f"/api/tournaments/{self.division_id}/top-scorers?division_id={self.division_id}",
            headers=_headers(self.owner_id),
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("top_mvps", data)
        self.assertIn("top_scorers", data)
        self.assertIn("top_assists", data)

        rows = {row["player_name"]: row for row in data["top_mvps"]}
        self.assertIn(self.star, rows)
        self.assertEqual(rows[self.star]["mvp_count"], 1)
        self.assertEqual(rows[self.star]["team_name"], self.owner_team)

    async def test_cabinet_squad_endpoint_returns_mvp_fields(self):
        """5. /api/cabinet/squad отдаёт top_mvp клуба и mvp_count по игрокам."""
        resp = await self.client.request(
            "GET", "/api/cabinet/squad", headers=_headers(self.owner_id)
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["registered"])

        self.assertEqual(data["top_mvp"], {"player_name": self.star, "mvp_count": 1})
        by_name = {p["player_name"]: p for p in data["players"]}
        self.assertIn("mvp_count", by_name[self.star])
        self.assertEqual(by_name[self.star]["mvp_count"], 1)


if __name__ == "__main__":
    unittest.main()
