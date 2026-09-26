"""
tests/test_market_analysis.py

Вкладка «Анализ рынка» панели Logovo.bet (services/ai/market_analysis.py).

Проверяет сырьё из базы (дивизион каждой ноги: кубок — к владельцу этапа,
общий кубок — отдельно, лига без дивизиона — к дивизиону 1), разрезы
статистики, предложения правил и главное — что предложение модели проходит
ту же проверку, что ввод админа в POST /limits, и только ужесточает.
Модель не вызывается: `call_model_chain` подменяется.

База — модульная из conftest; таблицы ставок и лимитов чистятся перед каждым тестом.
"""

import datetime
import unittest
from unittest import mock

import config
import database
from services.ai import market_analysis as ma
from services.betting_limits import BettingLimitsService
from time_utils import now_msk

USER_A = 973001
USER_B = 973002
USER_C = 973003

SINCE = "2026-09-01 00:00:00"
RECENT = "2026-09-10 12:00:00"
OLD = "2026-08-01 12:00:00"


def _clean() -> None:
    database.ensure_canonical_divisions()
    with database.transaction() as conn:
        for table in ("bet_items", "user_bets", "coin_transactions", "risk_limits_config"):
            conn.execute(f"DELETE FROM {table}")  # имена таблиц — литералы теста


def _bet(bet_id, user, amount, status, payout=0, bet_type="single", created=RECENT,
         settled=RECENT, potential=None, username=None):
    return {"id": bet_id, "user_id": user, "bet_type": bet_type, "amount": amount,
            "potential_win": potential if potential is not None else amount * 2,
            "actual_payout": payout, "status": status, "created_at": created,
            "settled_at": settled if status != "pending" else None,
            "username": username, "team_name": None}


def _leg(bet_id, division_id, group="result"):
    return {"bet_id": bet_id, "division_id": division_id, "group": group}


def _data(bets, legs, flows=()):
    return {"bets": bets, "legs": legs, "coin_flows": list(flows),
            "wallets": {"wallets": 0, "coins_in_wallets": 0, "max_balance": 0, "top5_balance": 0},
            "divisions": [{"id": d, "name": f"Дивизион {d}"} for d in range(1, 6)]}


def _key(s):
    return s["scope_type"], s["scope_id"], s["limit_key"], s["value"]


class TestMarketAnalysisData(unittest.TestCase):
    """database.get_market_analysis_data: какие купоны берутся и чей дивизион у ноги."""

    def setUp(self):
        _clean()
        now = now_msk()
        self.recent = (now - datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        self.old = (now - datetime.timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
        self.since = (now - datetime.timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM cup_stages WHERE stage LIKE 'MA-%'")
            c.execute("INSERT INTO cup_stages (season_id, stage, stage_order, division_id) "
                      "VALUES (1, 'MA-1/8@D3', 1, 3)")
            div_stage = c.lastrowid
            c.execute("INSERT INTO cup_stages (season_id, stage, stage_order, division_id) "
                      "VALUES (1, 'MA-1/8', 1, NULL)")
            general_stage = c.lastrowid
            # (match_id, division_id, tournament_type, stage_id)
            self.matches = {
                "div2": (973101, 2, "league", None),
                "nodiv": (973102, None, "league", None),
                "cup_div3": (973103, 0, "cup", div_stage),
                "cup_general": (973104, 0, "cup", general_stage),
            }
            for key, (mid, div, ttype, stage) in self.matches.items():
                c.execute("DELETE FROM matches WHERE id = ?", (mid,))
                c.execute("INSERT INTO matches (id, division_id, tournament_type, stage_id, status, "
                          "player1_team, player2_team) VALUES (?, ?, ?, ?, 'open', ?, ?)",
                          (mid, div, ttype, stage, f"H {key}", f"A {key}"))
                c.execute("DELETE FROM markets WHERE id = ?", (mid + 100,))
                c.execute("INSERT INTO markets (id, match_id, market_key, market_name, status) "
                          "VALUES (?, ?, 'total_goals', 'Тотал', 'open')", (mid + 100, mid))

    def _insert_bet(self, bet_id, status, created, settled, legs, bet_type="single"):
        with database.transaction() as conn:
            conn.execute("INSERT INTO user_bets (id, user_id, bet_type, amount, total_odd, potential_win, "
                         "status, actual_payout, created_at, settled_at) VALUES (?, ?, ?, 100, 2.0, 200, ?, 0, ?, ?)",
                         (bet_id, USER_A, bet_type, status, created, settled))
            for match_key, market in legs:
                mid = self.matches[match_key][0]
                conn.execute("INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, status, market_id) "
                             "VALUES (?, ?, 'over_2.5', 2.0, 'pending', ?)",
                             (bet_id, mid, mid + 100 if market else None))

    def test_leg_division_and_group(self):
        self._insert_bet(1, "lost", self.recent, self.recent,
                         [("div2", True), ("nodiv", True), ("cup_div3", True), ("cup_general", True)],
                         bet_type="express")
        data = database.get_market_analysis_data(self.since)
        by_match = {leg["match_id"]: leg for leg in data["legs"]}
        self.assertEqual(by_match[973101]["division_id"], 2)
        self.assertEqual(by_match[973102]["division_id"], 1)
        self.assertEqual(by_match[973103]["division_id"], 3)
        self.assertIsNone(by_match[973104]["division_id"])
        self.assertTrue(all(leg["group"] == "total" for leg in data["legs"]))

    def test_group_falls_back_to_the_outcome_without_a_market(self):
        self._insert_bet(1, "pending", self.recent, None, [("div2", False)])
        leg = database.get_market_analysis_data(self.since)["legs"][0]
        self.assertEqual(leg["group"], "total")

    def test_period_and_open_coupons(self):
        self._insert_bet(1, "lost", self.recent, self.recent, [("div2", True)])
        self._insert_bet(2, "lost", self.old, self.old, [("div2", True)])
        self._insert_bet(3, "pending", self.old, None, [("div2", True)])
        self._insert_bet(4, "won", self.old, self.recent, [("div2", True)])
        data = database.get_market_analysis_data(self.since)
        self.assertEqual({b["id"] for b in data["bets"]}, {1, 3, 4})
        self.assertEqual({leg["bet_id"] for leg in data["legs"]}, {1, 3, 4})

    def test_coin_flows_are_grouped_within_the_period(self):
        with database.transaction() as conn:
            for amount, kind, ts in ((500, "achievement_reward", self.recent),
                                     (300, "achievement_reward", self.recent),
                                     (-200, "bet_placed", self.recent),
                                     (9_000, "achievement_reward", self.old)):
                conn.execute("INSERT INTO coin_transactions (user_id, amount, transaction_type, created_at) "
                             "VALUES (?, ?, ?, ?)", (USER_A, amount, kind, ts))
        flows = {f["transaction_type"]: f for f in database.get_market_analysis_data(self.since)["coin_flows"]}
        self.assertEqual((flows["achievement_reward"]["cnt"], flows["achievement_reward"]["credited"]), (2, 800))
        self.assertEqual((flows["bet_placed"]["credited"], flows["bet_placed"]["debited"]), (0, 200))


class TestComputeStats(unittest.TestCase):
    def test_express_counts_once_in_totals_and_in_every_division(self):
        bets = [_bet(1, USER_A, 1_000, "won", payout=4_000, bet_type="express")]
        legs = [_leg(1, 1), _leg(1, 2)]
        stats = ma.compute_stats(_data(bets, legs), 14, SINCE)
        self.assertEqual(stats["totals"]["settled_stake"], 1_000)
        self.assertEqual(stats["totals"]["ggr"], -3_000)
        self.assertEqual(stats["totals"]["margin_pct"], -300.0)
        self.assertEqual([g["group"] for g in stats["totals"]["groups"]], ["express"])
        scopes = {s["division_id"]: s for s in stats["scopes"]}
        self.assertEqual(set(scopes), {1, 2})
        for s in scopes.values():
            self.assertEqual((s["settled_stake"], s["paid_out"]), (1_000, 4_000))

    def test_scopes_are_ordered_with_the_general_cup_last(self):
        bets = [_bet(1, USER_A, 100, "lost"), _bet(2, USER_A, 100, "lost"), _bet(3, USER_A, 100, "lost")]
        legs = [_leg(1, None), _leg(2, 3), _leg(3, 1)]
        stats = ma.compute_stats(_data(bets, legs), 14, SINCE)
        self.assertEqual([s["division_id"] for s in stats["scopes"]], [1, 3, None])
        self.assertEqual(stats["scopes"][-1]["name"], ma.GENERAL_CUP_LABEL)

    def test_settled_pending_and_void(self):
        bets = [
            _bet(1, USER_A, 500, "lost"),
            _bet(2, USER_A, 300, "pending", potential=900),
            _bet(3, USER_A, 700, "refunded"),
            _bet(4, USER_A, 200, "won", payout=400, created=OLD),   # создан до периода, рассчитан в нём
            _bet(5, USER_A, 800, "lost", created=OLD, settled=OLD),  # целиком до периода
        ]
        legs = [_leg(i, 1) for i in range(1, 6)]
        t = ma.compute_stats(_data(bets, legs), 14, SINCE)["totals"]
        self.assertEqual(t["settled_stake"], 700)
        self.assertEqual(t["paid_out"], 400)
        self.assertEqual(t["settled_bets"], 2)
        self.assertEqual((t["pending_count"], t["pending_stake"], t["pending_liability"]), (1, 300, 900))
        self.assertEqual((t["turnover"], t["bets"]), (800, 2))

    def test_top_winners_and_share(self):
        bets = [
            _bet(1, USER_A, 1_000, "won", payout=4_000, username="alpha"),
            _bet(2, USER_B, 1_000, "won", payout=2_000),
            _bet(3, USER_C, 5_000, "lost"),
            _bet(4, USER_C, 2_000, "pending"),
        ]
        stats = ma.compute_stats(_data(bets, [_leg(i, 1) for i in range(1, 5)]), 14, SINCE)
        self.assertEqual(stats["players_profit"], 4_000)
        self.assertEqual([(w["user_id"], w["profit"], w["share_pct"]) for w in stats["top_winners"]],
                         [(USER_A, 3_000, 75.0), (USER_B, 1_000, 25.0)])
        self.assertEqual(stats["top_pending"]["user_id"], USER_C)
        self.assertEqual(stats["top_pending"]["pending_stake"], 2_000)

    def test_inflow_split(self):
        flows = [
            {"transaction_type": "achievement_reward", "cnt": 3, "credited": 1_000, "debited": 0},
            {"transaction_type": "level_up_reward", "cnt": 1, "credited": 500, "debited": 0},
            {"transaction_type": "bet_placed", "cnt": 4, "credited": 0, "debited": 800},
            {"transaction_type": "bet_won", "cnt": 1, "credited": 600, "debited": 0},
            {"transaction_type": "admin_credit", "cnt": 1, "credited": 200, "debited": 0},
            {"transaction_type": "balance_reset", "cnt": 1, "credited": 0, "debited": 100},
        ]
        stats = ma.compute_stats(_data([], [], flows), 14, SINCE)
        self.assertEqual(stats["inflow"], {"rewards": 1_500, "bets": -200, "admin": 200, "total": 1_400})
        self.assertEqual(stats["coin_flows"][0]["type"], "achievement_reward")
        self.assertEqual(stats["coin_flows"][0]["label"], "Достижения")


class TestRules(unittest.TestCase):
    def setUp(self):
        _clean()
        self.limits = ma.current_limits([1, 2, 3, 4, 5])

    def _rules(self, bets, legs):
        stats = ma.compute_stats(_data(bets, legs), 14, SINCE)
        return ma.rule_suggestions(stats, self.limits), stats

    def test_heavy_loss_in_a_division(self):
        # Дивизион 5: десять синглов на тотал по 1 000, выплачено 20 000 — маржа −100%.
        bets = [_bet(i, USER_A if i % 2 else USER_B, 1_000, "won", payout=2_000) for i in range(1, 11)]
        legs = [_leg(i, 5, "total") for i in range(1, 11)]
        rules, _ = self._rules(bets, legs)
        keys = {_key(s) for s in rules}
        self.assertIn(("division", 5, "ban_total", 1), keys)
        self.assertIn(("division", 5, "max_payout", 5_000), keys)
        self.assertIn(("division", 5, "max_bet", 1_000), keys)
        self.assertIn(("division", 5, "max_open_bets", 8), keys)
        self.assertTrue(all(s["severity"] == "high" for s in rules if s["scope_id"] == 5))

    def test_moderate_loss_only_lowers_the_payout_cap(self):
        bets = [_bet(1, USER_A, 10_000, "won", payout=11_500)]
        rules, _ = self._rules(bets, [_leg(1, 3)])
        self.assertEqual({_key(s) for s in rules}, {("division", 3, "max_payout", 7_500)})
        self.assertEqual(rules[0]["severity"], "medium")

    def test_result_group_is_never_banned(self):
        bets = [_bet(1, USER_A, 10_000, "won", payout=30_000)]
        rules, _ = self._rules(bets, [_leg(1, 4, "result")])
        self.assertFalse([s for s in rules if s["limit_key"].startswith("ban_")])

    def test_healthy_division_and_small_sample_get_nothing(self):
        bets = [_bet(1, USER_A, 10_000, "lost"), _bet(2, USER_B, 1_000, "won", payout=4_000)]
        rules, _ = self._rules(bets, [_leg(1, 1, "total"), _leg(2, 2, "total")])
        self.assertEqual(rules, [])

    def test_express_margin_is_raised_across_all_divisions(self):
        bets = [_bet(1, USER_A, 5_000, "won", payout=15_000, bet_type="express")]
        rules, _ = self._rules(bets, [_leg(1, 1), _leg(1, 2)])
        current = self.limits["global"]["express_margin_pct"]
        express = [s for s in rules if s["limit_key"] == "express_margin_pct"]
        self.assertEqual(len(express), 1)
        self.assertEqual(_key(express[0])[:3], ("global", 0, "express_margin_pct"))
        self.assertEqual(express[0]["value"], min(database.MAX_EXPRESS_MARGIN_PCT, current + 5))

    def test_express_margin_is_not_raised_past_the_maximum(self):
        self.limits["global"]["express_margin_pct"] = database.MAX_EXPRESS_MARGIN_PCT
        bets = [_bet(1, USER_A, 5_000, "won", payout=15_000, bet_type="express")]
        rules, _ = self._rules(bets, [_leg(1, 1), _leg(1, 2)])
        self.assertFalse([s for s in rules if s["limit_key"] == "express_margin_pct"])

    def test_general_cup_lowers_the_global_payout_cap(self):
        bets = [_bet(1, USER_A, 3_000, "won", payout=6_000), _bet(2, USER_B, 3_000, "won", payout=6_000)]
        rules, _ = self._rules(bets, [_leg(1, None), _leg(2, None)])
        self.assertIn(("global", 0, "max_payout", 7_500), {_key(s) for s in rules})
        self.assertFalse([s for s in rules if s["scope_type"] == "division"])

    def test_dominant_winner_gets_a_personal_cap(self):
        bets = [_bet(1, USER_A, 2_000, "won", payout=9_000, username="alpha"),
                _bet(2, USER_B, 5_000, "lost")]
        rules, _ = self._rules(bets, [_leg(1, 1), _leg(2, 1)])
        personal = [s for s in rules if s["scope_type"] == "user"]
        self.assertEqual([_key(s) for s in personal], [("user", USER_A, "max_payout", 5_000)])
        self.assertIn("@alpha", personal[0]["reason"])

    def test_open_exposure_when_losing_and_one_player_holds_most_of_it(self):
        bets = [_bet(1, USER_A, 10_000, "won", payout=20_000),
                _bet(2, USER_B, 30_000, "pending")]
        rules, _ = self._rules(bets, [_leg(1, 1), _leg(2, 1)])
        self.assertIn(("global", 0, "max_open_exposure", 26_000), {_key(s) for s in rules})

    def test_suggestions_are_capped_and_unique(self):
        bets, legs = [], []
        for div in range(1, 6):
            for n in range(10):
                bid = div * 100 + n
                bets.append(_bet(bid, USER_A, 1_000, "won", payout=2_500))
                legs.append(_leg(bid, div, ("total", "btts", "handicap")[n % 3]))
        rules, _ = self._rules(bets, legs)
        self.assertLessEqual(len(rules), ma.MAX_SUGGESTIONS)
        self.assertEqual(len({s[:3] for s in map(_key, rules)}), len(rules))

    def test_findings(self):
        bets = [_bet(1, USER_A, 10_000, "won", payout=20_000, username="alpha"),
                _bet(2, USER_B, 10_000, "lost")]
        flows = [{"transaction_type": "achievement_reward", "cnt": 1, "credited": 50_000, "debited": 0}]
        stats = ma.compute_stats(_data(bets, [_leg(1, 5, "total"), _leg(2, 1)], flows), 14, SINCE)
        findings = ma.rule_findings(stats)
        texts = " ".join(f["text"] for f in findings)
        self.assertIn("Дивизион 5", texts)
        self.assertIn("В плюсе у букмекера: Дивизион 1", texts)
        self.assertIn("@alpha", texts)
        reward = next(f for f in findings if "награды" in f["text"])
        self.assertEqual(reward["severity"], "high")


class TestValidateSuggestion(unittest.TestCase):
    def setUp(self):
        _clean()
        self.limits = ma.current_limits([1, 2])
        self.users = {USER_A}

    def _check(self, **raw):
        base = {"scope_type": "division", "scope_id": 1, "limit_key": "max_payout", "value": 5_000,
                "reason": "почему", "severity": "high"}
        base.update(raw)
        return ma.validate_suggestion(base, self.limits, self.users)

    def test_valid_suggestion_passes(self):
        s = self._check()
        self.assertEqual(_key(s), ("division", 1, "max_payout", 5_000))
        self.assertEqual((s["reason"], s["severity"]), ("почему", "high"))

    def test_rejections(self):
        cases = {
            "unknown key": {"limit_key": "nonsense"},
            "key not allowed at the division": {"limit_key": "max_daily_stake", "value": 1_000},
            "unknown scope": {"scope_type": "team"},
            "unknown division": {"scope_id": 99},
            "unknown user": {"scope_type": "user", "scope_id": USER_B},
            "below the bounds": {"limit_key": "max_open_bets", "value": 0},
            "loosening": {"value": 20_000},
            "same value": {"value": self.limits["divisions"][1]["max_payout"]},
            "bool value": {"value": True},
            "text value": {"value": "много"},
            "ban with value 2": {"limit_key": "ban_total", "value": 2},
            "ban for a user": {"scope_type": "user", "scope_id": USER_A, "limit_key": "ban_total", "value": 1},
            "unknown ban group": {"limit_key": "ban_nonsense", "value": 1},
            "min_bet above max_bet": {"scope_type": "global", "limit_key": "min_bet",
                                      "value": self.limits["global"]["max_bet"] + 1},
            "lower express margin": {"scope_type": "global", "limit_key": "express_margin_pct", "value": 0},
            "express margin above max": {"scope_type": "global", "limit_key": "express_margin_pct",
                                         "value": database.MAX_EXPRESS_MARGIN_PCT + 1},
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.assertIsNone(self._check(**raw))
        self.assertIsNone(ma.validate_suggestion("max_payout=5000", self.limits, self.users))

    def test_bans_and_stricter_settings(self):
        self.assertEqual(_key(self._check(limit_key="ban_express", value=1)), ("division", 1, "ban_express", 1))
        self.assertEqual(_key(self._check(scope_type="global", scope_id=42, limit_key="ban_btts", value=1)),
                         ("global", 0, "ban_btts", 1))
        higher = self.limits["global"]["express_margin_pct"] + 2
        self.assertIsNotNone(self._check(scope_type="global", limit_key="express_margin_pct", value=higher))
        self.assertIsNotNone(self._check(scope_type="user", scope_id=USER_A, value="4000.4"))

    def test_existing_ban_is_not_suggested_again(self):
        BettingLimitsService.set_limit("division", 1, "ban_total", 1)
        limits = ma.current_limits([1, 2])
        self.assertIsNone(ma.validate_suggestion(
            {"scope_type": "division", "scope_id": 1, "limit_key": "ban_total", "value": 1}, limits, set()))

    def test_reason_and_severity_are_sanitized(self):
        s = self._check(reason="x" * 1_000, severity="critical")
        self.assertEqual(len(s["reason"]), ma.REASON_MAX_CHARS)
        self.assertEqual(s["severity"], "medium")
        self.assertEqual(self._check(reason=None)["reason"], "")

    def test_annotate(self):
        stats = {"scopes": [{"division_id": 1, "name": "Дивизион 1"}], "top_winners": [
            {"user_id": USER_A, "username": "alpha", "team_name": None}], "top_pending": None}
        current = self.limits["divisions"][1]["max_payout"]
        rows = ma.annotate([
            {"scope_type": "division", "scope_id": 1, "limit_key": "max_payout", "value": current},
            {"scope_type": "division", "scope_id": 2, "limit_key": "ban_total", "value": 1},
            {"scope_type": "global", "scope_id": 0, "limit_key": "max_bet", "value": 1_000},
            {"scope_type": "user", "scope_id": USER_A, "limit_key": "max_payout", "value": 1_000},
        ], self.limits, stats)
        self.assertEqual([r["applied"] for r in rows], [True, False, False, False])
        self.assertEqual([r["scope_label"] for r in rows],
                         ["Дивизион 1", "Дивизион 2", ma.ALL_DIVISIONS_LABEL, "Игрок @alpha"])
        self.assertTrue(rows[1]["is_ban"])
        self.assertTrue(rows[1]["label"].startswith("Запрет: "))
        self.assertEqual((rows[1]["current"], rows[2]["label"]), (0, "Максимальная ставка"))


class TestBuildAnalysis(unittest.TestCase):
    def setUp(self):
        _clean()
        ma.clear_cache()
        patches = [mock.patch.object(config, "OPENROUTER_API_KEY", "test-key"),
                   mock.patch.object(config, "OPENROUTER_MODEL", "test/model:free")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(ma.clear_cache)

    def _with_model(self, answer, model="test/model:free"):
        return mock.patch.object(ma.bet_picks, "call_model_chain", return_value=(answer, model))

    def test_ai_report_with_valid_suggestions(self):
        answer = {
            "summary": "  Дивизион 5 в минусе из-за экспрессов.  ",
            "risks": ["Экспрессы", "", 42],
            "suggestions": [
                {"scope_type": "division", "scope_id": 5, "limit_key": "max_payout", "value": 5_000,
                 "severity": "high", "reason": "Потолок ниже."},
                {"scope_type": "division", "scope_id": 5, "limit_key": "max_payout", "value": 4_000},
                {"scope_type": "division", "scope_id": 5, "limit_key": "ban_express", "value": 1},
                {"scope_type": "division", "scope_id": 77, "limit_key": "max_payout", "value": 5_000},
                {"scope_type": "global", "scope_id": 0, "limit_key": "max_payout", "value": 90_000},
                {"scope_type": "user", "scope_id": 555, "limit_key": "max_payout", "value": 1_000},
            ],
        }
        with self._with_model(answer) as call:
            result = ma.build_analysis(14)
        call.assert_called_once()
        system, user = call.call_args.args[:2]
        self.assertIn("все дивизионы", system)
        self.assertIn("ДАННЫЕ", user)
        self.assertEqual((result["source"], result["model"], result["error"]), ("ai", "test/model:free", None))
        self.assertEqual(result["summary"], "Дивизион 5 в минусе из-за экспрессов.")
        self.assertEqual(result["risks"], ["Экспрессы"])
        self.assertEqual(result["suggestions_source"], "ai")
        self.assertEqual([_key(s) for s in result["suggestions"]],
                         [("division", 5, "max_payout", 5_000), ("division", 5, "ban_express", 1)])

    def test_empty_ai_suggestions_fall_back_to_rules(self):
        with mock.patch.object(ma, "rule_suggestions", return_value=[
                {"scope_type": "global", "scope_id": 0, "limit_key": "max_bet", "value": 1_000,
                 "reason": "правило", "severity": "medium"}]):
            with self._with_model({"summary": "Всё спокойно.", "suggestions": [{"limit_key": "nonsense"}]}):
                result = ma.build_analysis(14)
        self.assertEqual((result["source"], result["summary"]), ("ai", "Всё спокойно."))
        self.assertEqual(result["suggestions_source"], "rules")
        self.assertEqual([s["reason"] for s in result["suggestions"]], ["правило"])

    def test_no_summary_means_ai_unavailable(self):
        for answer in (None, {"summary": "   ", "suggestions": []}, ["not", "a", "dict"]):
            with self.subTest(answer=answer), self._with_model(answer, model=None):
                result = ma.build_analysis(14)
            self.assertEqual((result["source"], result["error"]), ("rules", "ai_unavailable"))
            self.assertEqual(result["suggestions_source"], "rules")

    def test_no_key_skips_the_model(self):
        with mock.patch.object(config, "OPENROUTER_API_KEY", ""), self._with_model({"summary": "x"}) as call:
            result = ma.build_analysis(7)
        call.assert_not_called()
        self.assertEqual((result["source"], result["error"], result["ai_configured"]), ("rules", "no_key", False))
        self.assertEqual((result["period_days"], result["periods"]), (7, [7, 14, 30]))
        for key in ("findings", "stats", "suggestions", "generated_at"):
            self.assertIn(key, result)


class TestCacheAndPeriod(unittest.TestCase):
    def setUp(self):
        _clean()
        ma.clear_cache()
        self.addCleanup(ma.clear_cache)
        self.built = {"source": "rules", "stats": {"scopes": [], "top_winners": [], "top_pending": None},
                      "suggestions": [{"scope_type": "division", "scope_id": 2, "limit_key": "max_payout",
                                       "value": 5_000, "reason": "", "severity": "medium"}]}

    def test_normalize_period(self):
        self.assertEqual(ma.normalize_period(None), ma.DEFAULT_PERIOD)
        self.assertEqual(ma.normalize_period(""), ma.DEFAULT_PERIOD)
        self.assertEqual(ma.normalize_period(" 30 "), 30)
        for bad in ("5", "abc", 0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ma.normalize_period(bad)

    def test_cache_per_period_and_refresh(self):
        with mock.patch.object(ma, "build_analysis", side_effect=lambda days: dict(self.built)) as build:
            first = ma.get_analysis(14)
            second = ma.get_analysis(14)
            early_refresh = ma.get_analysis(14, refresh=True)
            other = ma.get_analysis(30)
            self.assertEqual(build.call_count, 2)
            self.assertEqual((first["cached"], second["cached"], early_refresh["cached"], other["cached"]),
                             (False, True, True, False))

            ts, data = ma._cache[14]
            ma._cache[14] = (ts - ma.REFRESH_MIN_SECONDS - 1, data)
            refreshed = ma.get_analysis(14, refresh=True)
            self.assertFalse(refreshed["cached"])
            self.assertEqual(build.call_count, 3)

    def test_cached_suggestions_show_the_current_value(self):
        with mock.patch.object(ma, "build_analysis", side_effect=lambda days: dict(self.built)):
            self.assertFalse(ma.get_analysis(14)["suggestions"][0]["applied"])
            BettingLimitsService.set_limit("division", 2, "max_payout", 5_000)
            cached = ma.get_analysis(14)
        self.assertTrue(cached["cached"])
        self.assertEqual((cached["suggestions"][0]["current"], cached["suggestions"][0]["applied"]), (5_000, True))
        self.assertEqual(cached["suggestions"][0]["scope_label"], "Дивизион 2")


if __name__ == "__main__":
    unittest.main()
