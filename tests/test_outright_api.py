"""
tests/test_outright_api.py

Долгосрочные ставки через HTTP: витрина Mini App (`api/routes_outrights.py`) —
рынки с замками тренера, история цен, приём ставки и коды отказов, «мои
ставки»; панель Logovo.bet (`api/routes_admin_panel.py`) — остановка рынка и
исхода, ручная цена, расчёт с подтверждением, аннулирование с причиной, аудит;
и личные уведомления о расчёте (`obet_<id>`).

База — модульная из conftest: обработчики ходят в неё из пула потоков, поэтому
каждый тест начинает с того, что заново строит рынки (`_reset_markets`).
"""

import hashlib
import hmac
import json
import time
import urllib.parse
import uuid
from itertools import combinations

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from services import outright_service

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

BETTOR = 972001
ADMIN = 972900
COACH_BASE = 972100


def _init_data(user_id: int) -> str:
    user = {"id": user_id, "first_name": "Outright", "username": f"o{user_id}"}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def _seed() -> dict:
    """Дивизион 2: четыре клуба с тренерами, круг матчей в трёх турах. Идемпотентно."""
    database.ensure_canonical_divisions()
    div = next(d for d in database.get_divisions() if d["code"] == "DIV_2")["id"]
    clubs = list(config.DIVISION_CLUBS["DIV_2"][:4])
    coaches = {club: COACH_BASE + i for i, club in enumerate(clubs)}
    with database.transaction() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT id FROM seasons WHERE name = 'Outright API Season'").fetchone()
        if row:
            season = row["id"]
        else:
            cur.execute("UPDATE seasons SET status = 'finished'")
            cur.execute("INSERT INTO seasons (name, status) VALUES ('Outright API Season', 'active')")
            season = cur.lastrowid
            for uid, name in ((BETTOR, "o_bettor"), (ADMIN, "o_admin")):
                cur.execute("INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                            (uid, name))
            for club, uid in coaches.items():
                cur.execute("""
                    INSERT OR IGNORE INTO users (telegram_id, username, role, team_name, division_id)
                    VALUES (?, ?, 'player', ?, ?)
                """, (uid, f"ocoach{uid}", club, div))
            for rnd in (1, 2, 3):
                cur.execute("""
                    INSERT INTO rounds (season_id, division_id, round_number, is_open, status)
                    VALUES (?, ?, ?, 1, 'open')
                """, (season, div, rnd))
            for n, (a, b) in enumerate(combinations(clubs, 2)):
                cur.execute("""
                    INSERT INTO matches (division_id, season_id, round_number, tournament_type,
                                         player1_id, player2_id, player1_team, player2_team, status)
                    VALUES (?, ?, ?, 'league', ?, ?, ?, ?, 'pending')
                """, (div, season, n // 2 + 1, coaches[a], coaches[b], a, b))
    for uid in (BETTOR, *coaches.values()):
        database.get_or_create_wallet(uid)
    return {"div": div, "season": season, "clubs": clubs, "coaches": coaches}


def _reset_markets() -> None:
    """Каждому тесту — свежие рынки, пустые ставки и полные кошельки."""
    with database.transaction() as conn:
        cur = conn.cursor()
        for table in ("outright_bets", "outright_odds_history", "outright_selections", "outright_markets"):
            cur.execute(f"DELETE FROM {table}")  # имена таблиц — литералы теста
        cur.execute("DELETE FROM notification_events WHERE source_event_id LIKE 'obet_%'")
        cur.execute("DELETE FROM betting_bans")
        cur.execute("DELETE FROM system_config WHERE key = 'betting_pause'")
        cur.execute("UPDATE user_wallets SET balance = 100000")
    summary = outright_service.refresh_outrights(True)
    assert "skipped" not in summary, summary


class OutrightApiCase(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_token = config.TOKEN
        config.TOKEN = TEST_BOT_TOKEN
        config.ADMIN_IDS.append(ADMIN)
        await super().asyncSetUp()
        self.ctx = _seed()
        _reset_markets()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        config.TOKEN = self._orig_token
        while ADMIN in config.ADMIN_IDS:
            config.ADMIN_IDS.remove(ADMIN)

    async def _call(self, method: str, path: str, user_id: int | None = BETTOR, body=None):
        headers = {"X-Telegram-Init-Data": _init_data(user_id)} if user_id else {}
        resp = await self.client.request(method, path, headers=headers, json=body)
        return resp.status, await resp.json()

    def _winner_market(self) -> dict:
        return next(m for m in database.get_outright_markets(self.ctx["season"])
                    if m["market_type"] == "division_winner" and m["scope_key"] == f"D{self.ctx['div']}")

    async def _board_market(self, user_id: int = BETTOR) -> dict:
        status, body = await self._call("GET", "/api/outrights", user_id)
        self.assertEqual(status, 200, body)
        market_id = self._winner_market()["id"]
        return next(m for m in body["markets"] if m["id"] == market_id)

    async def _bet(self, selection: dict, amount: int = 100, user_id: int = BETTOR, **extra):
        body = {"selection_id": selection["id"], "amount": amount, "odd": selection["odds_value"],
                "idempotency_key": uuid.uuid4().hex, **extra}
        return await self._call("POST", "/api/outrights/bet", user_id, body)


class TestBoard(OutrightApiCase):
    async def test_board_requires_init_data(self):
        for path in ("/api/outrights", "/api/outrights/my", f"/api/outrights/{self._winner_market()['id']}/history"):
            status, body = await self._call("GET", path, None)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"], "unauthorized")

    async def test_board_lists_the_market_with_prices(self):
        status, body = await self._call("GET", "/api/outrights")
        self.assertEqual(status, 200)
        self.assertEqual(body["max_open_bets"], database.MAX_OPEN_OUTRIGHT_BETS)
        self.assertEqual(body["min_bet"], database.OUTRIGHT_MIN_BET)
        self.assertIsNone(body["coach"])
        self.assertIn(self.ctx["div"], [d["id"] for d in body["divisions"]])
        market = await self._board_market()
        self.assertEqual(market["type"], "division_winner")
        self.assertEqual(market["status"], "open")
        self.assertIsNone(market["locked"])
        self.assertEqual(len(market["selections"]), 4)
        for sel in market["selections"]:
            self.assertGreater(sel["odds"], 1.0)
            self.assertIsNone(sel["locked"])
        self.assertAlmostEqual(sum(s["probability"] for s in market["selections"]), 1.0, places=2)

    async def test_coach_sees_their_own_division_locked(self):
        coach = self.ctx["coaches"][self.ctx["clubs"][0]]
        market = await self._board_market(coach)
        self.assertIsNotNone(market["locked"])
        self.assertTrue(all(s["locked"] for s in market["selections"]))

    async def test_history_returns_the_leaders(self):
        market_id = self._winner_market()["id"]
        status, body = await self._call("GET", f"/api/outrights/{market_id}/history?top=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["market_id"], market_id)
        self.assertEqual(len(body["series"]), 2)
        probs = [s["points"][-1]["prob"] for s in body["series"]]
        self.assertGreaterEqual(probs[0], probs[1])
        for series in body["series"]:
            self.assertTrue(series["points"])
            self.assertIn("odds", series["points"][-1])

    async def test_history_of_an_unknown_market_is_404_and_bad_ids_are_400(self):
        status, _ = await self._call("GET", "/api/outrights/999999/history")
        self.assertEqual(status, 404)
        status, _ = await self._call("GET", "/api/outrights/abc/history")
        self.assertEqual(status, 400)
        status, _ = await self._call("GET", f"/api/outrights/{self._winner_market()['id']}/history?top=0")
        self.assertEqual(status, 400)


class TestPlacement(OutrightApiCase):
    async def test_bet_is_accepted_and_listed(self):
        sel = self._winner_market()["selections"][0]
        before = database.get_wallet_balance(BETTOR)
        status, body = await self._bet(sel, 150)
        self.assertEqual(status, 200, body)
        self.assertEqual(database.get_wallet_balance(BETTOR), before - 150)

        status, mine = await self._call("GET", "/api/outrights/my")
        self.assertEqual(status, 200)
        self.assertEqual(mine["open_bets"], 1)
        bet = mine["bets"][0]
        self.assertEqual(bet["selection_id"], sel["id"])
        self.assertEqual(bet["amount"], 150)
        self.assertEqual(bet["status"], "pending")
        self.assertEqual(bet["market_type"], "division_winner")

    async def test_coach_is_refused_with_403(self):
        coach = self.ctx["coaches"][self.ctx["clubs"][1]]
        status, body = await self._bet(self._winner_market()["selections"][0], user_id=coach)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"], database.OUTRIGHT_OWN_SCOPE_ERROR)

    async def test_changed_odd_is_a_conflict(self):
        sel = self._winner_market()["selections"][0]
        status, body = await self._bet(sel, odd=round(sel["odds_value"] + 0.5, 2))
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"], "ODDS_CHANGED")

    async def test_suspended_market_is_a_conflict(self):
        market = self._winner_market()
        database.set_outright_market_status(market["id"], "suspended")
        status, body = await self._bet(market["selections"][0])
        self.assertEqual(status, 409, body)

    async def test_malformed_bodies_are_400(self):
        resp = await self.client.request("POST", "/api/outrights/bet", data="{not json",
                                         headers={"X-Telegram-Init-Data": _init_data(BETTOR),
                                                  "Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)
        status, _ = await self._call("POST", "/api/outrights/bet", body=[1, 2])
        self.assertEqual(status, 400)
        sel = self._winner_market()["selections"][0]
        status, _ = await self._bet(sel, idempotency_key="x" * 129)
        self.assertEqual(status, 400)
        status, _ = await self._bet(sel, amount=1)
        self.assertEqual(status, 400)


class TestPanel(OutrightApiCase):
    async def test_panel_is_super_admin_only(self):
        for method, path in (("GET", "/api/admin/panel/outrights"),
                             ("POST", "/api/admin/panel/outrights/refresh")):
            status, _ = await self._call(method, path, BETTOR, {} if method == "POST" else None)
            self.assertEqual(status, 403, path)

    async def test_board_carries_the_exposure(self):
        market = self._winner_market()
        sel = market["selections"][0]
        await self._bet(sel, 200)
        status, body = await self._call("GET", "/api/admin/panel/outrights", ADMIN)
        self.assertEqual(status, 200)
        row = next(m for m in body["markets"] if m["id"] == market["id"])
        self.assertEqual(row["exposure"]["bets"], 1)
        self.assertEqual(row["exposure"]["stake"], 200)
        loaded = next(s for s in row["selections"] if s["id"] == sel["id"])
        self.assertEqual(loaded["exposure"]["stake"], 200)
        self.assertEqual(row["exposure"]["worst_case"], loaded["exposure"]["liability"] - 200)

    async def test_suspend_and_resume_are_audited(self):
        market = self._winner_market()
        path = f"/api/admin/panel/outrights/{market['id']}/action"
        status, _ = await self._call("POST", path, ADMIN, {"action": "suspend"})
        self.assertEqual(status, 200)
        self.assertEqual(database.get_outright_market(market["id"])["status"], "suspended")
        status, _ = await self._call("POST", path, ADMIN, {"action": "resume"})
        self.assertEqual(status, 200)
        self.assertEqual(database.get_outright_market(market["id"])["status"], "open")
        actions = [r["action"] for r in database.get_betting_audit_log(entity_type="outright_market")
                   if r["entity_id"] == market["id"]]
        self.assertIn("outright_suspend", actions)
        self.assertIn("outright_resume", actions)

    async def test_unknown_action_and_market(self):
        status, body = await self._call("POST", f"/api/admin/panel/outrights/{self._winner_market()['id']}/action",
                                        ADMIN, {"action": "explode"})
        self.assertEqual((status, body["error"]), (400, "invalid_action"))
        status, _ = await self._call("POST", "/api/admin/panel/outrights/999999/action", ADMIN, {"action": "suspend"})
        self.assertEqual(status, 404)

    async def test_selection_override_and_reset(self):
        sel = self._winner_market()["selections"][0]
        path = f"/api/admin/panel/outright-selections/{sel['id']}"
        status, body = await self._call("POST", path, ADMIN, {"odds": 7.5})
        self.assertEqual(status, 200, body)
        moved = next(s for s in database.get_outright_market(self._winner_market()["id"])["selections"]
                     if s["id"] == sel["id"])
        self.assertEqual(moved["odds_value"], 7.5)
        status, _ = await self._call("POST", path, ADMIN, {"odds": None})
        self.assertEqual(status, 200)
        audited = [r["action"] for r in database.get_betting_audit_log(entity_type="outright_selection")
                   if r["entity_id"] == sel["id"]]
        self.assertIn("outright_odds_override", audited)

    async def test_selection_suspend_blocks_the_bet(self):
        sel = self._winner_market()["selections"][0]
        path = f"/api/admin/panel/outright-selections/{sel['id']}"
        status, _ = await self._call("POST", path, ADMIN, {"status": "suspended"})
        self.assertEqual(status, 200)
        status, _ = await self._bet(sel)
        self.assertNotEqual(status, 200)
        status, _ = await self._call("POST", path, ADMIN, {"status": "active"})
        self.assertEqual(status, 200)

    async def test_selection_rejects_bad_input(self):
        path = f"/api/admin/panel/outright-selections/{self._winner_market()['selections'][0]['id']}"
        for body, code in (({}, "nothing_to_change"), ({"status": "won"}, "invalid_status"),
                           ({"odds": True}, "invalid_odds"), ({"odds": 0.5}, "invalid_odds")):
            with self.subTest(body=body):
                status, resp = await self._call("POST", path, ADMIN, body)
                self.assertEqual((status, resp["error"]), (400, code))

    async def test_settle_needs_confirmation_and_valid_winners(self):
        market = self._winner_market()
        path = f"/api/admin/panel/outrights/{market['id']}/action"
        winner = market["selections"][0]["id"]
        status, body = await self._call("POST", path, ADMIN, {"action": "settle", "winners": [winner]})
        self.assertEqual((status, body["error"]), (400, "confirmation_required"))
        status, body = await self._call("POST", path, ADMIN, {"action": "settle", "winners": [999999],
                                                              "confirm": True})
        self.assertEqual((status, body["error"]), (400, "invalid_winners"))
        status, body = await self._call("POST", path, ADMIN, {"action": "settle", "winners": [],
                                                              "confirm": True})
        self.assertEqual((status, body["error"]), (400, "invalid_winners"))

    async def test_settle_pays_the_winner_and_notifies(self):
        market = self._winner_market()
        win_sel, lose_sel = market["selections"][0], market["selections"][1]
        _, won = await self._bet(win_sel, 100)
        _, lost = await self._bet(lose_sel, 100)
        before = database.get_wallet_balance(BETTOR)

        path = f"/api/admin/panel/outrights/{market['id']}/action"
        status, body = await self._call("POST", path, ADMIN, {"action": "settle", "winners": [win_sel["id"]],
                                                              "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(database.get_outright_market(market["id"])["status"], "settled")
        self.assertEqual(database.get_wallet_balance(BETTOR), before + round(100 * win_sel["odds_value"]))

        notices = self._notices()
        self.assertIn(f"obet_{won['bet_id']}", notices)
        self.assertIn(f"obet_{lost['bet_id']}", notices)
        self.assertIn("выиграла", notices[f"obet_{won['bet_id']}"])
        self.assertIn("не сыграла", notices[f"obet_{lost['bet_id']}"])

        status, body = await self._call("POST", path, ADMIN, {"action": "settle", "winners": [win_sel["id"]],
                                                              "confirm": True})
        self.assertEqual((status, body["error"]), (409, "invalid_transition"))

    async def test_two_winners_split_the_payout(self):
        market = self._winner_market()
        a, b = market["selections"][0], market["selections"][1]
        _, bet = await self._bet(a, 100)
        before = database.get_wallet_balance(BETTOR)
        status, _ = await self._call("POST", f"/api/admin/panel/outrights/{market['id']}/action", ADMIN,
                                     {"action": "settle", "winners": [a["id"], b["id"]], "confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(database.get_wallet_balance(BETTOR), before + round(100 * a["odds_value"] * 0.5))
        self.assertIn("доле 50%", self._notice_bodies()[f"obet_{bet['bet_id']}"])

    async def test_void_needs_a_reason_and_refunds(self):
        market = self._winner_market()
        _, bet = await self._bet(market["selections"][0], 100)
        before = database.get_wallet_balance(BETTOR)
        path = f"/api/admin/panel/outrights/{market['id']}/action"
        status, body = await self._call("POST", path, ADMIN, {"action": "void", "confirm": True})
        self.assertEqual((status, body["error"]), (400, "reason_required"))
        status, body = await self._call("POST", path, ADMIN, {"action": "void", "reason": "дивизион расформирован"})
        self.assertEqual((status, body["error"]), (400, "confirmation_required"))
        status, _ = await self._call("POST", path, ADMIN, {"action": "void", "reason": "дивизион расформирован",
                                                           "confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(database.get_wallet_balance(BETTOR), before + 100)
        self.assertIn("возвращена", self._notices()[f"obet_{bet['bet_id']}"])
        audit = next(r for r in database.get_betting_audit_log(entity_type="outright_market")
                     if r["entity_id"] == market["id"] and r["action"] == "outright_void")
        self.assertIn("дивизион расформирован", audit["new_value"])

    async def test_refresh_is_audited(self):
        status, body = await self._call("POST", "/api/admin/panel/outrights/refresh", ADMIN, {})
        self.assertEqual(status, 200, body)
        self.assertIn("result", body)
        self.assertTrue(any(r["action"] == "outright_refresh"
                            for r in database.get_betting_audit_log(entity_type="outright_market")))

    # ─── helpers ───

    def _notice_rows(self) -> list:
        with database.transaction() as conn:
            return conn.execute(
                "SELECT source_event_id, title, body FROM notification_events "
                "WHERE user_id = ? AND source_event_id LIKE 'obet_%'", (BETTOR,),
            ).fetchall()

    def _notices(self) -> dict:
        return {r["source_event_id"]: r["title"] for r in self._notice_rows()}

    def _notice_bodies(self) -> dict:
        return {r["source_event_id"]: f"{r['title']}\n{r['body']}" for r in self._notice_rows()}
