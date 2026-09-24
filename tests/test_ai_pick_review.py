"""
tests/test_ai_pick_review.py

Сверка «ИИ-прогноза» с сыгранными матчами: журнал `ai_pick_log`
(database.log_ai_picks / get_ai_pick_log), чистая сводка
services/ai/pick_review.summarize и маршрут /api/admin/panel/picks/review.
"""

import unittest
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from services.ai import bet_picks, pick_review
from tests.test_ai_picks import TEST_BOT_TOKEN, make_init_data

REVIEW_ADMIN = 973900
REVIEW_PLAYER = 973001


def _row(market_key, selection_key, s1, s2, probability, line_probability, odds, **extra):
    row = {
        "selection_id": extra.pop("selection_id", 1), "match_id": extra.pop("match_id", 1),
        "market_key": market_key, "selection_key": selection_key,
        "player1_score": s1, "player2_score": s2, "ht_score1": None, "ht_score2": None,
        "tournament_type": "league", "cup_winner_team": None,
        "player1_team": "Лилль", "player2_team": "Вест Хэм", "team1": "Лилль", "team2": "Вест Хэм",
        "odds": odds, "probability": probability, "line_probability": line_probability,
        "model": "first/model:free",
    }
    row.update(extra)
    return row


def _pick(selection_id, match_id, key="p1", probability=60.0, odds=1.8, **extra):
    pick = {
        "selection_id": selection_id, "match_id": match_id, "division_id": 1, "round_number": 3,
        "team1": "Лилль", "team2": "Вест Хэм", "market_key": "1x2", "market_group": "result",
        "market_name": "Исход матча", "selection_key": key, "selection_name": f"П1 ({key})",
        "odds": odds, "probability": probability, "line_probability": 52.0,
    }
    pick.update(extra)
    return pick


class TestSummarize(unittest.TestCase):
    def test_outcomes_brier_and_roi(self):
        rows = [
            _row("1x2", "p1", 2, 0, 80, 60, 1.5),        # зашёл
            _row("1x2", "p2", 2, 0, 40, 30, 3.0),        # не зашёл
            _row("total_goals", "over_2.5", 2, 1, 70, 50, 1.9),  # зашёл
        ]
        res = pick_review.summarize(rows)
        t = res["total"]
        self.assertEqual((t["count"], t["won"]), (3, 2))
        self.assertEqual(t["hit_rate"], 66.7)
        self.assertEqual(t["brier_ai"], round((0.2 ** 2 + 0.4 ** 2 + 0.3 ** 2) / 3, 4))
        self.assertEqual(t["brier_line"], round((0.4 ** 2 + 0.3 ** 2 + 0.5 ** 2) / 3, 4))
        self.assertEqual(t["roi"], round((0.5 - 1 + 0.9) / 3 * 100, 1))
        self.assertEqual(res["verdict"], "few")
        self.assertEqual([p["won"] for p in res["recent"]], [True, False, True])
        self.assertEqual(res["recent"][0]["score"], "2:0")

    def test_value_subset(self):
        rows = [
            _row("1x2", "p1", 1, 0, 60, 50, 2.0),   # 0.6 × 2.0 > 1 — ценный
            _row("1x2", "p1", 0, 1, 40, 45, 2.0),   # 0.4 × 2.0 < 1
        ]
        value = pick_review.summarize(rows)["value"]
        self.assertEqual((value["count"], value["won"], value["roi"]), (1, 1, 100.0))

    def test_refunds_and_unknown_markets_are_left_out(self):
        rows = [
            _row("1x2", "p1", 1, 0, 60, 50, 2.0),
            _row("poker", "royal", 1, 0, 60, 50, 2.0),
        ]
        res = pick_review.summarize(rows)
        self.assertEqual(res["total"]["count"], 1)
        self.assertEqual(res["voided"], 1)

    def test_buckets_and_models(self):
        rows = [
            _row("1x2", "p1", 1, 0, 45, 40, 2.2, model="a"),
            _row("1x2", "p1", 1, 0, 85, 80, 1.2, model="b"),
            _row("1x2", "p1", 0, 1, 82, 80, 1.2, model="b"),
        ]
        res = pick_review.summarize(rows)
        self.assertEqual([(b["label"], b["count"]) for b in res["buckets"]],
                         [("до 50%", 1), ("80% и выше", 2)])
        self.assertEqual([(m["model"], m["count"]) for m in res["models"]], [("b", 2), ("a", 1)])

    def test_cup_game_is_settled_by_the_winner_field(self):
        # 2:2 в основное время, игру взял клуб 2 — «П1» не зашёл.
        row = _row("1x2", "p1", 2, 2, 60, 50, 1.9, tournament_type="cup", cup_winner_team="Вест Хэм")
        res = pick_review.summarize([row])
        self.assertEqual((res["total"]["count"], res["total"]["won"]), (1, 0))

    def test_verdict(self):
        n = pick_review.MIN_SAMPLE
        sharp = [_row("1x2", "p1", 1, 0, 90, 60, 1.6)] * n
        self.assertEqual(pick_review.summarize(sharp)["verdict"], "ai")
        dull = [_row("1x2", "p1", 1, 0, 40, 70, 1.6)] * n
        self.assertEqual(pick_review.summarize(dull)["verdict"], "line")
        same = [_row("1x2", "p1", 1, 0, 70, 70, 1.6)] * n
        self.assertEqual(pick_review.summarize(same)["verdict"], "even")
        self.assertEqual(pick_review.summarize(sharp[:n - 1])["verdict"], "few")

    def test_empty(self):
        res = pick_review.summarize([])
        self.assertEqual(res["total"]["count"], 0)
        self.assertIsNone(res["total"]["brier_ai"])
        self.assertEqual(res["verdict"], "few")


# match_id: (division_id, status, score1, score2, is_technical)
MATCHES = {
    973101: (1, "pending", None, None, 0),
    973102: (1, "confirmed", 2, 1, 0),
    973103: (2, "confirmed", 0, 3, 0),
    973104: (1, "confirmed", 3, 0, 1),   # техническое — не в сверку
    973105: (1, "cancelled", None, None, 0),
}


def _seed_matches():
    database.ensure_canonical_divisions()
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ai_pick_log WHERE match_id BETWEEN 973000 AND 973999")
        cursor.execute("DELETE FROM matches WHERE id BETWEEN 973000 AND 973999")
        for match_id, (div, status, s1, s2, tech) in MATCHES.items():
            cursor.execute("""
                INSERT INTO matches (id, division_id, round_number, player1_team, player2_team,
                                     status, player1_score, player2_score, is_technical)
                VALUES (?, ?, 3, 'Лилль', 'Вест Хэм', ?, ?, ?, ?)
            """, (match_id, div, status, s1, s2, tech))


def _set_played(match_id, s1, s2):
    with database.transaction() as conn:
        conn.execute("UPDATE matches SET status = 'confirmed', player1_score = ?, player2_score = ? WHERE id = ?",
                     (s1, s2, match_id))


class TestPickLog(unittest.TestCase):
    def setUp(self):
        _seed_matches()

    def test_played_matches_are_not_logged(self):
        written = database.log_ai_picks([_pick(1, 973101), _pick(2, 973102)], "m")
        self.assertEqual(written, 1)
        played, pending = database.get_ai_pick_log()
        self.assertEqual([r for r in played if 973000 <= r["match_id"] < 974000], [])
        self.assertEqual(pending, 1)

    def test_upsert_keeps_the_last_estimate(self):
        database.log_ai_picks([_pick(1, 973101, probability=55)], "old")
        database.log_ai_picks([_pick(1, 973101, probability=71, odds=1.7)], "new")
        _set_played(973101, 1, 0)
        played, _ = database.get_ai_pick_log()
        row = next(r for r in played if r["selection_id"] == 1)
        self.assertEqual((row["probability"], row["odds"], row["model"]), (71.0, 1.7, "new"))
        self.assertEqual((row["player1_score"], row["player2_score"]), (1, 0))
        self.assertTrue(row["predicted_at"])

    def test_technical_cancelled_and_scope(self):
        database.log_ai_picks([_pick(1, 973101), _pick(3, 973105), _pick(4, 973101, key="p2")], "m")
        with database.transaction() as conn:
            # Журнал писался до результата: подменяем матчи уже после записи.
            conn.execute("UPDATE ai_pick_log SET match_id = 973104 WHERE selection_id = 4")
            conn.execute("UPDATE ai_pick_log SET match_id = 973103, division_id = 2 WHERE selection_id = 1")
        played, pending = database.get_ai_pick_log()
        self.assertEqual({r["selection_id"] for r in played}, {1})
        self.assertEqual(pending, 0)   # отменённый не «ждёт», технический сыгран
        self.assertEqual(database.get_ai_pick_log([1])[0], [])
        self.assertEqual([r["selection_id"] for r in database.get_ai_pick_log([2])[0]], [1])

    def test_review_counts_pending(self):
        database.log_ai_picks([_pick(1, 973101), _pick(2, 973101, key="x")], "m")
        review = pick_review.get_review(None)
        self.assertEqual(review["pending"], 2)
        self.assertEqual(review["total"]["count"], 0)


class TestBuildPicksLogs(unittest.TestCase):
    def setUp(self):
        from tests.test_ai_picks import _seed, _selection_id
        _seed()
        self._sel = _selection_id
        with database.transaction() as conn:
            conn.execute("DELETE FROM ai_pick_log")
        self._orig = config.OPENROUTER_API_KEY

    def tearDown(self):
        config.OPENROUTER_API_KEY = self._orig
        bet_picks.clear_cache()

    def _count(self):
        with database.transaction() as conn:
            return conn.execute("SELECT COUNT(*) FROM ai_pick_log").fetchone()[0]

    def test_line_fallback_is_not_logged(self):
        config.OPENROUTER_API_KEY = ""
        bet_picks.build_picks([1, 2])
        self.assertEqual(self._count(), 0)

    def test_ai_picks_are_logged(self):
        config.OPENROUTER_API_KEY = "sk-or-v1-test-not-a-real-key-7f3a"
        p2 = self._sel(972103, "p2")
        with patch.object(bet_picks, "_enrich"), \
             patch.object(bet_picks, "_call_openrouter",
                          return_value=({"picks": [{"id": p2, "probability": 58}]}, "first/model:free")):
            bet_picks.build_picks([1, 2])
        with database.transaction() as conn:
            row = dict(conn.execute("SELECT * FROM ai_pick_log WHERE selection_id = ?", (p2,)).fetchone())
        self.assertEqual((row["market_key"], row["selection_key"], row["probability"], row["model"]),
                         ("1x2", "p2", 58.0, "first/model:free"))

    def test_log_failure_does_not_break_the_picks(self):
        config.OPENROUTER_API_KEY = "sk-or-v1-test-not-a-real-key-7f3a"
        p2 = self._sel(972103, "p2")
        with patch.object(bet_picks, "_enrich"), \
             patch.object(bet_picks, "_call_openrouter",
                          return_value=({"picks": [{"id": p2, "probability": 58}]}, "m")), \
             patch.object(database, "log_ai_picks", side_effect=RuntimeError("disk")):
            res = bet_picks.build_picks([1, 2])
        self.assertEqual(res["source"], "ai")


class TestReviewRoute(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_token = config.TOKEN
        config.TOKEN = TEST_BOT_TOKEN
        if REVIEW_ADMIN not in config.ADMIN_IDS:
            config.ADMIN_IDS.append(REVIEW_ADMIN)
        await super().asyncSetUp()
        _seed_matches()
        with database.transaction() as conn:
            for uid in (REVIEW_ADMIN, REVIEW_PLAYER):
                conn.execute("INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                             (uid, f"u{uid}"))
        database.log_ai_picks([_pick(1, 973101, probability=70, odds=1.8)], "m")
        _set_played(973101, 2, 0)

    async def asyncTearDown(self):
        await super().asyncTearDown()
        config.TOKEN = self._orig_token

    async def _get(self, query, user_id):
        resp = await self.client.get(f"/api/admin/panel/picks/review{query}",
                                     headers={"X-Telegram-Init-Data": make_init_data(user_id)})
        return resp.status, await resp.json()

    async def test_player_is_forbidden(self):
        status, _ = await self._get("", REVIEW_PLAYER)
        self.assertEqual(status, 403)

    async def test_admin_gets_the_review(self):
        status, body = await self._get("?division_id=1", REVIEW_ADMIN)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "ok")
        self.assertEqual((body["total"]["count"], body["total"]["won"]), (1, 1))
        self.assertEqual(body["verdict"], "few")
        self.assertEqual(body["recent"][0]["score"], "2:0")
        status, body = await self._get("?division_id=2", REVIEW_ADMIN)
        self.assertEqual(body["total"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
