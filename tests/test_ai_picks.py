"""
tests/test_ai_picks.py

Вкладка «ИИ-прогноз» панели Logovo.bet (services/ai/bet_picks.py): кандидаты
из открытой линии, разбор ответа модели, фолбэк на вероятности линии, кэш
и маршрут /api/admin/panel/picks. Сеть не трогается — OpenRouter подменён.
"""

import hashlib
import hmac
import io
import json
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase

import config
import database
from api.server import create_app
from services.ai import bet_picks

TEST_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
FAKE_KEY = "sk-or-v1-test-not-a-real-key-7f3a"

PICKS_ADMIN = 972900
PICKS_PLAYER = 972001

# match_id: (division_id, round, team1, team2, status)
MATCHES = {
    972101: (1, 3, "Лилль", "Вест Хэм", "pending"),
    972102: (1, 4, "Порту", "Бенфика", "pending"),
    972103: (2, 3, "Севилья", "Бетис", "pending"),
    972104: (2, 5, "Лион", "Ницца", "confirmed"),   # сыгран — не кандидат
}


def _seed() -> None:
    database.ensure_canonical_divisions()
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM market_selections WHERE market_id BETWEEN 972000 AND 972999")
        cursor.execute("DELETE FROM markets WHERE id BETWEEN 972000 AND 972999")
        cursor.execute("DELETE FROM matches WHERE id BETWEEN 972000 AND 972999")
        for uid, name in ((PICKS_ADMIN, "picks_admin"), (PICKS_PLAYER, "picks_player")):
            cursor.execute("INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                           (uid, name))
        sel_id = 972500
        for match_id, (div, rnd, t1, t2, status) in MATCHES.items():
            cursor.execute("""
                INSERT INTO matches (id, division_id, round_number, player1_team, player2_team, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (match_id, div, rnd, t1, t2, status))
            base = (match_id - 972100) * 10
            markets = [
                # 1X2 с маржой 1.076, как в живой линии
                (972200 + base, "1x2", "Исход матча", "open",
                 [("p1", f"П1 ({t1})", 1.81), ("x", "Ничья (X)", 4.48), ("p2", f"П2 ({t2})", 3.33)]),
                (972201 + base, "total_goals", "Тотал голов", "open",
                 [("over_1.5", "Тотал больше (1.5)", 1.05), ("over_2.5", "Тотал больше (2.5)", 1.30),
                  ("under_2.5", "Тотал меньше (2.5)", 3.26)]),
                (972202 + base, "btts", "Обе забьют", "closed",
                 [("btts_yes", "Обе забьют: Да", 1.34)]),
            ]
            for market_id, key, name, mstatus, sels in markets:
                cursor.execute("""
                    INSERT INTO markets (id, match_id, market_key, market_name, status)
                    VALUES (?, ?, ?, ?, ?)
                """, (market_id, match_id, key, name, mstatus))
                for skey, sname, odds in sels:
                    sel_id += 1
                    cursor.execute("""
                        INSERT INTO market_selections (id, market_id, selection_key, selection_name,
                                                       odds_value, model_odds, status, odds_version)
                        VALUES (?, ?, ?, ?, ?, ?, 'active', 1)
                    """, (sel_id, market_id, skey, sname, odds, odds))
    bet_picks.clear_cache()


def _selection_id(match_id: int, key: str) -> int:
    for m in bet_picks.collect_candidates([1, 2], max_matches=50):
        if m["match_id"] == match_id:
            for o in m["options"]:
                if o["selection_key"] == key:
                    return o["selection_id"]
    raise AssertionError(f"no selection {key} in match {match_id}")


class PicksCase(unittest.TestCase):
    def setUp(self):
        _seed()
        self._orig = (config.OPENROUTER_API_KEY, config.OPENROUTER_MODEL)
        config.OPENROUTER_API_KEY = ""
        config.OPENROUTER_MODEL = "first/model:free,second/model:free"

    def tearDown(self):
        config.OPENROUTER_API_KEY, config.OPENROUTER_MODEL = self._orig
        bet_picks.clear_cache()


class TestCandidates(PicksCase):
    def test_only_open_markets_of_unplayed_matches(self):
        matches = bet_picks.collect_candidates([1, 2], max_matches=50)
        self.assertEqual({m["match_id"] for m in matches}, {972101, 972102, 972103})
        for m in matches:
            keys = {o["selection_key"] for o in m["options"]}
            self.assertNotIn("btts_yes", keys)     # рынок закрыт
            self.assertNotIn("over_1.5", keys)     # кэф ниже MIN_ODDS
            self.assertIn("over_2.5", keys)

    def test_line_probability_has_the_margin_removed(self):
        m = next(x for x in bet_picks.collect_candidates([1], max_matches=50) if x["match_id"] == 972101)
        probs = {o["selection_key"]: o["line_probability"] for o in m["options"]}
        self.assertAlmostEqual(probs["p1"] + probs["x"] + probs["p2"], 100.0, delta=0.3)
        self.assertAlmostEqual(probs["over_2.5"] + probs["under_2.5"], 100.0, delta=0.3)

    def test_division_scope(self):
        matches = bet_picks.collect_candidates([2], max_matches=50)
        self.assertEqual([m["match_id"] for m in matches], [972103])

    def test_matches_are_taken_round_robin_across_divisions(self):
        matches = bet_picks.collect_candidates([1, 2], max_matches=2)
        self.assertEqual({m["division_id"] for m in matches}, {1, 2})
        self.assertIn(972101, [m["match_id"] for m in matches])  # ближайший тур дивизиона 1

    def test_options_budget_limits_the_matches(self):
        # 5 исходов на матч: бюджет 7 пускает только первый, 10 — два.
        self.assertEqual(len(bet_picks.collect_candidates([1, 2], max_options=7)), 1)
        self.assertEqual(len(bet_picks.collect_candidates([1, 2], max_options=10)), 2)
        # Первый матч берётся, даже если он один больше бюджета.
        self.assertEqual(len(bet_picks.collect_candidates([1, 2], max_options=1)), 1)


class TestFilters(PicksCase):
    def test_market_group_filter(self):
        f = bet_picks.normalize_filters("total")
        for m in bet_picks.collect_candidates([1, 2], filters=f):
            self.assertEqual({o["market_group"] for o in m["options"]}, {"total"})

    def test_odds_range_filter(self):
        f = bet_picks.normalize_filters(None, "1.5", "3,5")
        odds = [o["odds"] for m in bet_picks.collect_candidates([1, 2], filters=f) for o in m["options"]]
        self.assertTrue(odds)
        self.assertTrue(all(1.5 <= x <= 3.5 for x in odds))
        self.assertIn(1.81, odds)
        self.assertNotIn(4.48, odds)

    def test_line_probability_ignores_the_market_filter(self):
        # Маржа снимается по 1X2, даже если 1X2 отфильтрован.
        full = {o["selection_id"]: o["line_probability"]
                for m in bet_picks.collect_candidates([1]) for o in m["options"]}
        for m in bet_picks.collect_candidates([1], filters=bet_picks.normalize_filters("total")):
            for o in m["options"]:
                self.assertEqual(o["line_probability"], full[o["selection_id"]])

    def test_normalize_rejects_garbage(self):
        for args in (("casino",), (None, "abc"), (None, "0.5"), (None, "nan"), (None, "3", "2")):
            with self.assertRaises(ValueError, msg=args):
                bet_picks.normalize_filters(*args)

    def test_all_groups_equal_no_filter(self):
        f = bet_picks.normalize_filters(",".join(bet_picks.MARKET_GROUPS))
        self.assertEqual(f, bet_picks.normalize_filters())

    def test_filters_are_cached_separately_and_echoed(self):
        with patch.object(bet_picks, "build_picks", wraps=bet_picks.build_picks) as build:
            plain = bet_picks.get_picks([1, 2])
            totals = bet_picks.get_picks([1, 2], filters=bet_picks.normalize_filters("total"))
            again = bet_picks.get_picks([1, 2], filters=bet_picks.normalize_filters(["total"]))
        self.assertEqual(build.call_count, 2)
        self.assertTrue(again["cached"])
        self.assertEqual(plain["filters"]["markets"], [])
        self.assertEqual(totals["filters"]["markets"], ["total"])
        self.assertEqual({p["market_group"] for p in totals["picks"]}, {"total"})
        self.assertEqual([g["id"] for g in totals["market_groups"]], list(bet_picks.MARKET_GROUPS))


class TestRanking(PicksCase):
    def setUp(self):
        super().setUp()
        self.matches = bet_picks.collect_candidates([1, 2], max_matches=50)

    def test_ai_answer_is_validated_and_sorted(self):
        p1 = _selection_id(972101, "p1")
        over = _selection_id(972102, "over_2.5")
        x = _selection_id(972103, "x")
        data = {"picks": [
            {"id": p1, "probability": 61, "reason": "лидер против аутсайдера"},
            {"id": over, "probability": 140, "reason": "много голов"},   # обрезается до 99
            {"id": 999999, "probability": 95},                           # чужой id
            {"id": p1, "probability": 90},                               # дубль
            {"id": x, "probability": "abc"},                             # мусор
            "not a dict",
        ]}
        rows = bet_picks.rank_ai_picks(self.matches, data)
        self.assertEqual([r["selection_id"] for r in rows], [over, p1])
        self.assertEqual(rows[0]["probability"], 99.0)
        self.assertEqual(rows[1]["reason"], "лидер против аутсайдера")
        self.assertEqual(rows[1]["team1"], "Лилль")

    def test_at_most_two_picks_per_match(self):
        data = {"picks": [{"id": o["selection_id"], "probability": 50 + i}
                          for i, o in enumerate(self.matches[0]["options"])]}
        rows = bet_picks.rank_ai_picks(self.matches, data)
        self.assertEqual(len(rows), bet_picks.MAX_PER_MATCH)

    def test_pick_carries_what_the_coupon_needs(self):
        # Сборщик купона ставит исход по market_id + selection_id + selection_key.
        rows = bet_picks.rank_line_picks(self.matches)
        with database.transaction() as conn:
            owner = {r["id"]: (r["market_id"], r["selection_key"]) for r in conn.execute(
                "SELECT id, market_id, selection_key FROM market_selections "
                "WHERE market_id BETWEEN 972000 AND 972999")}
        for r in rows:
            self.assertEqual((r["market_id"], r["selection_key"]), owner[r["selection_id"]])
            self.assertIn("cup_series_id", r)

    def test_line_ranking_goes_from_most_to_least_likely(self):
        rows = bet_picks.rank_line_picks(self.matches)
        probs = [r["probability"] for r in rows]
        self.assertEqual(probs, sorted(probs, reverse=True))
        self.assertTrue(rows)

    def test_extract_json_tolerates_fences_and_prose(self):
        text = 'Вот ответ:\n```json\n{"picks": [{"id": 1, "probability": 70}]}\n```\nУдачи!'
        self.assertEqual(bet_picks._extract_json(text)["picks"][0]["id"], 1)
        self.assertIsNone(bet_picks._extract_json("no json here"))
        self.assertIsNone(bet_picks._extract_json("{broken"))

    def test_extract_json_skips_inline_reasoning(self):
        text = '<think>Берём {что-то} из данных…</think>\n{"picks": [{"id": 2, "probability": 61}]}'
        self.assertEqual(bet_picks._extract_json(text)["picks"][0]["id"], 2)

    def test_extract_json_tolerates_trailing_commas_and_single_quotes(self):
        text_commas = '{"picks": [{"id": 3, "probability": 80, "reason": "win",},],}'
        self.assertEqual(bet_picks._extract_json(text_commas)["picks"][0]["id"], 3)
        text_quotes = "{'picks': [{'id': 4, 'probability': 75, 'reason': 'form'}]}"
        self.assertEqual(bet_picks._extract_json(text_quotes)["picks"][0]["id"], 4)

    def test_extract_json_handles_unclosed_reasoning_and_prose(self):
        text = '<think>Рассуждения о матче...\n{"picks": [{"id": 5, "probability": 66}]}'
        self.assertEqual(bet_picks._extract_json(text)["picks"][0]["id"], 5)

    def test_extract_json_salvages_truncated_picks(self):
        # Оборванный на середине ответ модели спасает уже сгенерированные исходы
        text = '{"picks": [{"id": 6, "probability": 70, "reason": "ok"}, {"id": 7, "prob'
        res = bet_picks._extract_json(text)
        self.assertIsNotNone(res)
        self.assertEqual(len(res["picks"]), 1)
        self.assertEqual(res["picks"][0]["id"], 6)


class TestBuildPicks(PicksCase):
    def test_without_key_the_line_is_used(self):
        with patch.object(bet_picks, "_call_openrouter") as call:
            res = bet_picks.build_picks([1, 2])
        call.assert_not_called()
        self.assertEqual(res["source"], "line")
        self.assertEqual(res["error"], "no_key")
        self.assertFalse(res["ai_configured"])
        self.assertTrue(res["picks"])

    def test_ai_answer_is_used_when_valid(self):
        config.OPENROUTER_API_KEY = FAKE_KEY
        p2 = _selection_id(972103, "p2")
        with patch.object(bet_picks, "_enrich"), \
             patch.object(bet_picks, "_call_openrouter",
                          return_value=({"picks": [{"id": p2, "probability": 58, "reason": "форма"}]},
                                        "first/model:free")):
            res = bet_picks.build_picks([1, 2])
        self.assertEqual(res["source"], "ai")
        self.assertEqual(res["model"], "first/model:free")
        self.assertEqual([p["selection_id"] for p in res["picks"]], [p2])

    def test_useless_ai_answer_falls_back_to_the_line(self):
        config.OPENROUTER_API_KEY = FAKE_KEY
        with patch.object(bet_picks, "_enrich"), \
             patch.object(bet_picks, "_call_openrouter", return_value=({"picks": [{"id": 1, "probability": 90}]}, "m")):
            res = bet_picks.build_picks([1, 2])
        self.assertEqual(res["source"], "line")
        self.assertEqual(res["error"], "ai_unavailable")
        self.assertTrue(res["picks"])

    def test_enrich_survives_a_fresh_database(self):
        matches = bet_picks.collect_candidates([1], max_matches=50)
        bet_picks._enrich(matches)  # таблица/ансамбль могут не посчитаться — но без исключений
        self.assertIn("options", matches[0])

    def test_no_open_markets(self):
        res = bet_picks.build_picks([5])
        self.assertEqual(res["picks"], [])
        self.assertEqual(res["matches_considered"], 0)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestOpenRouterCall(PicksCase):
    def test_falls_through_to_the_next_model_and_never_logs_the_key(self):
        config.OPENROUTER_API_KEY = FAKE_KEY
        matches = bet_picks.collect_candidates([1], max_matches=50)
        answer = {"choices": [{"message": {"content": '```json\n{"picks": []}\n```'}}]}
        requests = []

        def fake_urlopen(req, timeout):
            requests.append(req)
            if len(requests) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
            return _FakeResponse(json.dumps(answer).encode("utf-8"))

        with patch.object(bet_picks.urllib.request, "urlopen", side_effect=fake_urlopen), \
             self.assertLogs("services.ai.bet_picks", level="WARNING") as logs:
            data, model = bet_picks._call_openrouter(matches)

        self.assertEqual(data, {"picks": []})
        self.assertEqual(model, "second/model:free")
        self.assertTrue(requests[0].full_url.endswith("/chat/completions"))
        self.assertEqual(requests[0].get_header("Authorization"), f"Bearer {FAKE_KEY}")
        sent = json.loads(requests[1].data.decode("utf-8"))
        self.assertEqual(sent["model"], "second/model:free")
        # Рассуждения моделей вроде Qwen3 — коротко и вне content, с запасом токенов.
        self.assertEqual(sent["reasoning"], {"effort": "low", "exclude": True})
        self.assertGreaterEqual(sent["max_tokens"], 8000)
        self.assertNotIn(FAKE_KEY, "\n".join(logs.output))

    def test_no_key_means_no_request(self):
        with patch.object(bet_picks.urllib.request, "urlopen") as urlopen:
            self.assertEqual(bet_picks._call_openrouter([]), (None, None))
        urlopen.assert_not_called()


class TestCache(PicksCase):
    def test_second_request_is_served_from_cache(self):
        with patch.object(bet_picks, "build_picks", wraps=bet_picks.build_picks) as build:
            first = bet_picks.get_picks([1, 2])
            second = bet_picks.get_picks([1, 2])
            # Ручное обновление сразу после расчёта упирается в REFRESH_MIN_SECONDS.
            third = bet_picks.get_picks([1, 2], refresh=True)
        self.assertEqual(build.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertTrue(third["cached"])

    def test_refresh_after_the_interval_recomputes(self):
        with patch.object(bet_picks, "build_picks", wraps=bet_picks.build_picks) as build:
            bet_picks.get_picks([1, 2])
            key = bet_picks._cache_key([1, 2])
            ts, data = bet_picks._cache[key]
            bet_picks._cache[key] = (ts - bet_picks.REFRESH_MIN_SECONDS - 1, data)
            res = bet_picks.get_picks([1, 2], refresh=True)
        self.assertEqual(build.call_count, 2)
        self.assertFalse(res["cached"])

    def test_scopes_are_cached_separately(self):
        with patch.object(bet_picks, "build_picks", wraps=bet_picks.build_picks) as build:
            bet_picks.get_picks([1])
            bet_picks.get_picks([2])
        self.assertEqual(build.call_count, 2)


def make_init_data(user_id: int) -> str:
    user = {"id": user_id, "first_name": "Picks", "username": f"u{user_id}"}
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class TestPicksRoute(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    async def asyncSetUp(self):
        self._orig_token = config.TOKEN
        self._orig_key = config.OPENROUTER_API_KEY
        config.TOKEN = TEST_BOT_TOKEN
        config.OPENROUTER_API_KEY = ""
        if PICKS_ADMIN not in config.ADMIN_IDS:
            config.ADMIN_IDS.append(PICKS_ADMIN)
        await super().asyncSetUp()
        _seed()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        config.TOKEN = self._orig_token
        config.OPENROUTER_API_KEY = self._orig_key
        bet_picks.clear_cache()

    async def _get(self, query: str, user_id: int):
        resp = await self.client.get(f"/api/admin/panel/picks{query}",
                                     headers={"X-Telegram-Init-Data": make_init_data(user_id)})
        return resp.status, await resp.json()

    async def test_player_is_forbidden(self):
        status, _ = await self._get("", PICKS_PLAYER)
        self.assertEqual(status, 403)

    async def test_admin_gets_ranked_picks(self):
        status, body = await self._get("?division_id=1", PICKS_ADMIN)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"], "line")
        self.assertTrue(body["picks"])
        self.assertEqual({p["division_id"] for p in body["picks"]}, {1})
        probs = [p["probability"] for p in body["picks"]]
        self.assertEqual(probs, sorted(probs, reverse=True))

    async def test_filters_reach_the_ranking(self):
        status, body = await self._get("?markets=result&odds_min=2&odds_max=5", PICKS_ADMIN)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["picks"])
        for p in body["picks"]:
            self.assertEqual(p["market_group"], "result")
            self.assertTrue(2 <= p["odds"] <= 5)

    async def test_bad_filters_are_rejected(self):
        for query in ("?markets=poker", "?odds_min=abc", "?odds_min=4&odds_max=2"):
            status, body = await self._get(query, PICKS_ADMIN)
            self.assertEqual(status, 400, query)
            self.assertEqual(body["error"], "bad_filters")


if __name__ == "__main__":
    unittest.main()
