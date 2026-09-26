"""Logovo Tracker: сквозной сценарий мобильного приложения.

Проверяется весь путь устройства — ПИН из бота, обмен на токен, список матчей,
старт трансляции, тики, события, финиш — и три границы, которые обязаны
держаться:

1. Без валидного Bearer-токена не отвечает ни один маршрут трекера.
2. Чужой матч недоступен по прямому id (IDOR).
3. Трансляция не трогает официальный протокол: `matches.status` и счёт
   остаются нетронутыми, их владелец — бот.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api import rate_limiter
from api.routes_tracker import PIN_CODE_LENGTH, issue_pin_code, reset_tracker_state
from api.server import create_app


class TestTrackerApi(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_rate_limit = config.API_RATE_LIMIT_ENABLED
        self._orig_ocr = config.TRACKER_OCR_ENABLED
        self._orig_dev_pin = config.TRACKER_DEV_PIN_ENABLED
        config.API_RATE_LIMIT_ENABLED = False
        # OCR ходит в сеть: в тестах кадр разбираться не должен.
        config.TRACKER_OCR_ENABLED = False
        # Бэкдор 7777/0000 выключен по умолчанию — как в проде. Тесты самого
        # бэкдора включают его точечно и возвращают обратно в tearDown.
        config.TRACKER_DEV_PIN_ENABLED = False

        await super().asyncSetUp()

        reset_tracker_state()
        rate_limiter.reset_all()

        uid = uuid.uuid4().hex[:6].upper()
        self.owner_id = 980101
        self.rival_id = 980102
        self.own_team = f"Tracker FC {uid}"
        self.rival_team = f"Rival FC {uid}"
        database.register_user(self.owner_id, f"tracker_owner_{uid}", team_name=self.own_team)
        database.register_user(self.rival_id, f"tracker_rival_{uid}", team_name=self.rival_team)

        self.match_id = self._seed_match(self.own_team, self.rival_team)
        # Матч, к которому владелец токена отношения не имеет.
        self.foreign_match_id = self._seed_match(f"Alien A {uid}", f"Alien B {uid}")

    async def asyncTearDown(self):
        config.API_RATE_LIMIT_ENABLED = self._orig_rate_limit
        config.TRACKER_OCR_ENABLED = self._orig_ocr
        config.TRACKER_DEV_PIN_ENABLED = self._orig_dev_pin
        reset_tracker_state()
        await super().asyncTearDown()

    def _seed_match(self, home: str, away: str) -> int:
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO matches
                       (tournament_id, round_number, player1_team, player2_team,
                        status, tournament_type, division_id, season_id)
                   VALUES (1, 1, ?, ?, 'pending', 'league', 1, 1)""",
                (home, away),
            )
            return cur.lastrowid

    def _match_row(self, match_id: int) -> dict:
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
            return dict(cur.fetchone())

    def _live_row(self, match_id: int) -> dict | None:
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM live_match_states WHERE match_id = ?", (match_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    async def _pair(self, user_id: int) -> str:
        pin, ttl = await self.loop.run_in_executor(None, issue_pin_code, user_id)
        self.assertEqual(PIN_CODE_LENGTH, len(pin))
        self.assertGreater(ttl, 0)
        resp = await self.client.post(
            "/api/tracker/auth/pair",
            json={"pin_code": pin, "device_info": "iPhone 15 Pro, iOS 18"},
        )
        self.assertEqual(200, resp.status)
        body = await resp.json()
        self.assertEqual("ok", body["status"])
        return body["token"]

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    async def test_pair_rejects_bad_pin(self):
        """Неверный, короткий и отсутствующий ПИН одинаково дают 401."""
        for payload in ({}, {"pin_code": ""}, {"pin_code": "12"}, {"pin_code": "abcd"},
                        {"pin_code": "0000000"}):
            resp = await self.client.post("/api/tracker/auth/pair", json=payload)
            self.assertEqual(401, resp.status, f"payload={payload}")

    async def test_pin_is_single_use(self):
        """Код сгорает при первом обмене: второй раз тем же ПИНом не войти."""
        pin, _ = issue_pin_code(self.owner_id)
        first = await self.client.post("/api/tracker/auth/pair", json={"pin_code": pin})
        self.assertEqual(200, first.status)
        second = await self.client.post("/api/tracker/auth/pair", json={"pin_code": pin})
        self.assertEqual(401, second.status)

    async def test_new_pin_invalidates_previous_one(self):
        """Повторный /tracker гасит прошлый код — он не остаётся рабочим в переписке."""
        old_pin, _ = issue_pin_code(self.owner_id)
        new_pin, _ = issue_pin_code(self.owner_id)
        self.assertNotEqual(old_pin, new_pin)

        stale = await self.client.post("/api/tracker/auth/pair", json={"pin_code": old_pin})
        self.assertEqual(401, stale.status)
        fresh = await self.client.post("/api/tracker/auth/pair", json={"pin_code": new_pin})
        self.assertEqual(200, fresh.status)

    async def test_routes_require_bearer_token(self):
        """Без токена и с мусорным токеном все маршруты трекера отдают 401."""
        calls = [
            ("GET", "/api/tracker/matches"),
            ("POST", "/api/tracker/session/start"),
            ("POST", "/api/tracker/session/tick"),
            ("POST", "/api/tracker/session/event"),
            ("POST", "/api/tracker/session/finish"),
        ]
        for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
            for method, path in calls:
                resp = await self.client.request(method, path, headers=headers, json={"match_id": self.match_id})
                self.assertEqual(401, resp.status, f"{method} {path} headers={headers}")

    async def test_matches_list_shows_own_open_match(self):
        """Список отдаёт несыгранный матч клуба с соперником и стороной поля."""
        token = await self._pair(self.owner_id)
        resp = await self.client.get("/api/tracker/matches", headers=self._auth(token))
        self.assertEqual(200, resp.status)
        body = await resp.json()

        found = [m for m in body["matches"] if m["match_id"] == self.match_id]
        self.assertEqual(1, len(found), "Свой открытый матч не попал в список")
        match = found[0]
        self.assertTrue(match["is_home"])
        self.assertEqual(self.rival_team, match["opponent_team"])
        self.assertEqual("league", match["tournament_type"])
        self.assertFalse(match["is_cup"])

        ids = [m["match_id"] for m in body["matches"]]
        self.assertNotIn(self.foreign_match_id, ids, "В списке оказался чужой матч")

    async def test_foreign_match_is_forbidden(self):
        """IDOR: чужой матч не транслируется по прямому id."""
        token = await self._pair(self.owner_id)
        for path in ("/api/tracker/session/start", "/api/tracker/session/finish"):
            resp = await self.client.post(
                path, headers=self._auth(token), json={"match_id": self.foreign_match_id})
            self.assertEqual(403, resp.status, path)

    async def test_missing_match_is_404(self):
        token = await self._pair(self.owner_id)
        resp = await self.client.post(
            "/api/tracker/session/start", headers=self._auth(token), json={"match_id": 999000111})
        self.assertEqual(404, resp.status)

    async def test_invalid_payloads_are_400(self):
        """Валидация тела: битый JSON и выходящие за диапазон значения — 4xx."""
        token = await self._pair(self.owner_id)
        headers = {**self._auth(token), "Content-Type": "application/json"}

        broken = await self.client.post("/api/tracker/session/tick", headers=headers, data="{not json")
        self.assertEqual(400, broken.status)

        bad_bodies = [
            {"match_id": self.match_id, "minute": 999, "score_home": 0, "score_away": 0},
            {"match_id": self.match_id, "minute": -1, "score_home": 0, "score_away": 0},
            {"match_id": self.match_id, "minute": 10, "score_home": -3, "score_away": 0},
            {"match_id": self.match_id, "minute": 10, "score_home": 0, "score_away": 0, "period": "ЧТО-ТО"},
            {"minute": 10, "score_home": 0, "score_away": 0},
        ]
        for body in bad_bodies:
            resp = await self.client.post("/api/tracker/session/tick", headers=self._auth(token), json=body)
            self.assertEqual(400, resp.status, f"body={body}")

    async def test_tick_before_start_is_rejected(self):
        """Тик без запущенной трансляции — конфликт, а не молчаливая запись."""
        token = await self._pair(self.owner_id)
        resp = await self.client.post(
            "/api/tracker/session/tick",
            headers=self._auth(token),
            json={"match_id": self.match_id, "minute": 5, "score_home": 0, "score_away": 0},
        )
        self.assertEqual(409, resp.status)

    async def test_full_broadcast_flow(self):
        """Старт → тик → гол → перерыв → финиш, и протокол остаётся нетронутым."""
        token = await self._pair(self.owner_id)
        headers = self._auth(token)
        before = self._match_row(self.match_id)

        start = await self.client.post(
            "/api/tracker/session/start", headers=headers, json={"match_id": self.match_id})
        self.assertEqual(200, start.status)
        self.assertEqual("LIVE", self._live_row(self.match_id)["status"])

        tick = await self.client.post(
            "/api/tracker/session/tick",
            headers=headers,
            json={"match_id": self.match_id, "minute": 34, "score_home": 1,
                  "score_away": 0, "period": "1H"},
        )
        self.assertEqual(200, tick.status)
        state = self._live_row(self.match_id)
        self.assertEqual(34, state["minute"])
        self.assertEqual(1, state["home_score"])
        self.assertEqual(0, state["away_score"])
        self.assertEqual("1h", state["period"])

        goal = await self.client.post(
            "/api/tracker/session/event",
            headers=headers,
            json={"match_id": self.match_id, "minute": 34, "event_type": "GOAL",
                  "player_name": "Haaland", "team_side": "home",
                  "client_event_id": "evt-1"},
        )
        self.assertEqual(200, goal.status)
        goal_body = await goal.json()
        self.assertFalse(goal_body["duplicate"])
        self.assertEqual("Haaland", goal_body["player_name"])

        # Повтор при обрыве связи не должен задваивать событие в ленте.
        repeat = await self.client.post(
            "/api/tracker/session/event",
            headers=headers,
            json={"match_id": self.match_id, "minute": 34, "event_type": "GOAL",
                  "player_name": "Haaland", "team_side": "home",
                  "client_event_id": "evt-1"},
        )
        self.assertEqual(200, repeat.status)
        self.assertTrue((await repeat.json())["duplicate"])

        # Счётом владеют тики: событие его не инкрементирует.
        self.assertEqual(1, self._live_row(self.match_id)["home_score"])

        halftime = await self.client.post(
            "/api/tracker/session/event",
            headers=headers,
            json={"match_id": self.match_id, "minute": 45, "event_type": "HALFTIME",
                  "team_side": "home"},
        )
        self.assertEqual(200, halftime.status)
        self.assertEqual("HALFTIME", self._live_row(self.match_id)["status"])

        finish = await self.client.post(
            "/api/tracker/session/finish", headers=headers, json={"match_id": self.match_id})
        self.assertEqual(200, finish.status)
        final_state = self._live_row(self.match_id)
        self.assertEqual("FINISHED", final_state["status"])
        self.assertEqual("ft", final_state["period"])

        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT event_type, player_name FROM live_events WHERE match_id = ? AND provider = 'tracker' ORDER BY id",
                (self.match_id,),
            )
            events = [dict(r) for r in cur.fetchall()]
        self.assertEqual(["goal", "halftime"], [e["event_type"] for e in events])

        after = self._match_row(self.match_id)
        self.assertEqual(before["status"], after["status"], "Трансляция изменила статус матча")
        self.assertEqual(before["player1_score"], after["player1_score"], "Трансляция изменила официальный счёт")
        self.assertEqual(before["player2_score"], after["player2_score"], "Трансляция изменила официальный счёт")
        self.assertEqual(45, after["live_minute"])

    async def test_finish_is_idempotent(self):
        """Повторный финиш не ломается и сообщает, что трансляция уже закрыта."""
        token = await self._pair(self.owner_id)
        headers = self._auth(token)
        await self.client.post("/api/tracker/session/start", headers=headers, json={"match_id": self.match_id})
        first = await self.client.post("/api/tracker/session/finish", headers=headers, json={"match_id": self.match_id})
        self.assertEqual(200, first.status)
        self.assertFalse((await first.json())["already_finished"])

        second = await self.client.post("/api/tracker/session/finish", headers=headers, json={"match_id": self.match_id})
        self.assertEqual(200, second.status)
        self.assertTrue((await second.json())["already_finished"])

    async def test_restart_after_finish_is_rejected(self):
        """Завершённую трансляцию нельзя перезапустить."""
        token = await self._pair(self.owner_id)
        headers = self._auth(token)
        await self.client.post("/api/tracker/session/start", headers=headers, json={"match_id": self.match_id})
        await self.client.post("/api/tracker/session/finish", headers=headers, json={"match_id": self.match_id})
        again = await self.client.post("/api/tracker/session/start", headers=headers, json={"match_id": self.match_id})
        self.assertEqual(409, again.status)

    async def test_dev_pin_backdoor_disabled_by_default(self):
        """Коды 7777/0000 без флага — обычный неверный ПИН, а не бэкдор."""
        self.assertFalse(config.TRACKER_DEV_PIN_ENABLED)
        for pin in ("7777", "0000"):
            resp = await self.client.post("/api/tracker/auth/pair", json={"pin_code": pin})
            self.assertEqual(401, resp.status, f"pin={pin}")

    async def test_dev_pin_backdoor_works_only_behind_flag(self):
        """Флаг включён — 7777/0000 мгновенно выдают токен на мок-профиль."""
        config.TRACKER_DEV_PIN_ENABLED = True
        for pin in ("7777", "0000"):
            resp = await self.client.post("/api/tracker/auth/pair", json={"pin_code": pin})
            self.assertEqual(200, resp.status, f"pin={pin}")
            body = await resp.json()
            self.assertEqual("ok", body["status"])
            self.assertTrue(body["token"])

    async def test_logout_revokes_token_immediately(self):
        """После /auth/logout тот же токен сразу получает 401, не дожидаясь TTL."""
        token = await self._pair(self.owner_id)
        headers = self._auth(token)

        before = await self.client.get("/api/tracker/matches", headers=headers)
        self.assertEqual(200, before.status)

        logout = await self.client.post("/api/tracker/auth/logout", headers=headers)
        self.assertEqual(200, logout.status)
        self.assertEqual("ok", (await logout.json())["status"])

        after = await self.client.get("/api/tracker/matches", headers=headers)
        self.assertEqual(401, after.status)

    async def test_logout_is_idempotent_without_token(self):
        """Logout не должен требовать валидную сессию: выйти можно и с мусорным/пустым токеном."""
        for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
            resp = await self.client.post("/api/tracker/auth/logout", headers=headers)
            self.assertEqual(200, resp.status, f"headers={headers}")
            self.assertEqual("ok", (await resp.json())["status"])
