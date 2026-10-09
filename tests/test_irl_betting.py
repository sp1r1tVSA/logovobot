"""
tests/test_irl_betting.py

IRL-ставки: чистая логика выбора и расчёта (services/irl_betting.py) и деньги
(database.place_irl_bet / settle_irl_match / void_irl_match).
"""

import threading
from datetime import datetime, timedelta

import pytest

import config
import database
from services import irl_betting as irl
from services.irl_betting import Candidate
from time_utils import now_msk

NOW = datetime(2026, 10, 3, 9, 0, 0)
PRIORITY = [2, 39, 140, 10]
TOP = ["Real Madrid", "Barcelona", "Arsenal", "Brazil"]


def cand(fid, league, home, away, hours=5, odds=(2.0, 3.3, 3.5), **kw):
    return Candidate(fixture_id=fid, league_id=league, home=home, away=away,
                     kickoff=NOW + timedelta(hours=hours),
                     odd_home=odds[0], odd_draw=odds[1], odd_away=odds[2], **kw)


def pick(cands, **kw):
    kw.setdefault("priority", PRIORITY)
    kw.setdefault("top_teams", TOP)
    kw.setdefault("limit", 2)
    return irl.pick_top_matches(cands, NOW, **kw)


# ─── Выбор матча ─────────────────────────────────────────────────────────────

class TestPickTopMatches:
    def test_first_priority_league_with_matches_wins(self):
        cands = [cand(1, 39, "Arsenal", "Chelsea"), cand(2, 2, "Porto", "Benfica")]
        assert [c.fixture_id for c in pick(cands)] == [2]

    def test_falls_through_to_next_league_when_empty(self):
        cands = [cand(1, 140, "Real Madrid", "Sevilla")]
        assert [c.fixture_id for c in pick(cands)] == [1]

    def test_national_team_day_reaches_national_competition(self):
        cands = [cand(1, 10, "Brazil", "Chile")]
        assert [c.fixture_id for c in pick(cands)] == [1]

    def test_empty_day_is_empty_list_not_error(self):
        assert pick([]) == []
        assert pick([cand(1, 999, "A", "B")]) == []

    def test_more_top_teams_beats_fewer(self):
        cands = [cand(1, 140, "Real Madrid", "Getafe"), cand(2, 140, "Real Madrid", "Barcelona")]
        assert [c.fixture_id for c in pick(cands)] == [2]

    def test_ties_on_top_count_give_several_matches_up_to_limit(self):
        cands = [cand(1, 140, "Real Madrid", "Getafe"), cand(2, 140, "Barcelona", "Sevilla"),
                 cand(3, 140, "Arsenal", "Villarreal")]
        assert len(pick(cands, limit=2)) == 2
        assert len(pick(cands, limit=3)) == 3

    def test_no_top_team_at_all_returns_a_single_match(self):
        cands = [cand(1, 140, "Getafe", "Osasuna"), cand(2, 140, "Girona", "Cadiz")]
        assert len(pick(cands, limit=2)) == 1

    def test_closer_odds_break_the_tie(self):
        cands = [cand(1, 140, "Real Madrid", "Getafe", odds=(1.2, 6.0, 12.0)),
                 cand(2, 140, "Barcelona", "Sevilla", odds=(2.1, 3.3, 3.4))]
        assert pick(cands, limit=1)[0].fixture_id == 2

    def test_table_bonus_never_outweighs_a_top_team(self):
        by_table = cand(1, 39, "Getafe", "Osasuna", home_rank=1, away_rank=2)
        by_name = cand(2, 39, "Arsenal", "Osasuna")
        assert pick([by_table, by_name], limit=1)[0].fixture_id == 2

    def test_table_bonus_orders_otherwise_equal_matches(self):
        low = cand(1, 39, "Getafe", "Osasuna", home_rank=15, away_rank=16)
        high = cand(2, 39, "Girona", "Cadiz", home_rank=1, away_rank=2)
        assert pick([low, high], limit=1)[0].fixture_id == 2

    def test_started_matches_are_skipped(self):
        cands = [cand(1, 2, "Real Madrid", "Barcelona", hours=-1), cand(2, 39, "Arsenal", "Chelsea")]
        assert [c.fixture_id for c in pick(cands)] == [2]

    @pytest.mark.parametrize("odds", [(None, 3.0, 3.0), (2.0, 0, 3.0), (2.0, 3.0, float("nan")),
                                      (1.0, 3.0, 3.0), (2.0, "x", 3.0)])
    def test_matches_without_complete_odds_are_skipped(self, odds):
        assert pick([cand(1, 2, "Real Madrid", "Barcelona", odds=odds)]) == []

    def test_team_names_are_not_resolved_through_the_league_registry(self):
        assert irl.top_team_count(cand(1, 2, "real  madrid", "REAL MADRID"), TOP) == 2

    def test_result_is_deterministic(self):
        cands = [cand(i, 140, "Real Madrid", f"T{i}") for i in range(5)]
        assert pick(cands) == pick(list(reversed(cands)))


# ─── Ставка: разбор ──────────────────────────────────────────────────────────

class TestStakeAndOutcome:
    @pytest.mark.parametrize("raw,key", [("home", "home"), ("1", "home"), ("П1", "home"),
                                         ("X", "draw"), ("х", "draw"), ("draw", "draw"),
                                         (2, "away"), ("away", "away"), (" Away ", "away")])
    def test_outcome_aliases(self, raw, key):
        assert irl.normalize_outcome(raw) == key

    @pytest.mark.parametrize("raw", [None, "", "3", "over", "1x"])
    def test_unknown_outcome(self, raw):
        assert irl.normalize_outcome(raw) is None

    @pytest.mark.parametrize("raw,val", [(1, 1), ("500", 500), (1000, 1000), (10.0, 10)])
    def test_valid_stake(self, raw, val):
        assert irl.parse_stake(raw, 1000) == (True, val)

    @pytest.mark.parametrize("raw,code", [(0, "INVALID_AMOUNT"), (-5, "INVALID_AMOUNT"),
                                          ("abc", "INVALID_AMOUNT"), (None, "INVALID_AMOUNT"),
                                          (1.5, "INVALID_AMOUNT"), (True, "INVALID_AMOUNT"),
                                          (1001, "MAX_BET_EXCEEDED")])
    def test_invalid_stake(self, raw, code):
        ok, err = irl.parse_stake(raw, 1000)
        assert not ok and err["error"] == code

    def test_potential_win_and_payout_ceiling(self):
        assert irl.potential_win(100, 2.5) == 250
        assert irl.potential_win(1000, 20.0, 10_000) == 10_000

    def test_betting_open_only_for_open_status_before_kickoff(self):
        future = NOW + timedelta(hours=1)
        assert irl.betting_open("open", future, NOW)
        assert not irl.betting_open("draft", future, NOW)
        assert not irl.betting_open("closed", future, NOW)
        assert not irl.betting_open("open", NOW, NOW)
        assert not irl.betting_open("open", None, NOW)


# ─── Расчёт: решение ─────────────────────────────────────────────────────────

class TestSettleDecision:
    KICK = datetime(2026, 10, 3, 18, 0)

    def dec(self, status, hg, ag, minutes_after=130):
        return irl.settle_decision(status, hg, ag, self.KICK, self.KICK + timedelta(minutes=minutes_after))

    @pytest.mark.parametrize("hg,ag,res", [(2, 1, "home"), (1, 1, "draw"), (0, 3, "away")])
    def test_ft_result(self, hg, ag, res):
        d = self.dec("FT", hg, ag)
        assert (d.action, d.result) == ("result", res)

    def test_ft_too_early_waits(self):
        assert self.dec("FT", 1, 0, minutes_after=60).action == "wait"

    @pytest.mark.parametrize("status", ["AET", "PEN"])
    def test_extra_time_and_penalties_use_main_time_score(self, status):
        d = self.dec(status, 1, 1, minutes_after=150)
        assert (d.action, d.result) == ("result", "draw")

    @pytest.mark.parametrize("status", ["PST", "CANC", "ABD", "AWD", "WO", "pst"])
    def test_void_statuses(self, status):
        assert self.dec(status, None, None).action == "void"

    @pytest.mark.parametrize("status", ["NS", "1H", "HT", "2H", "LIVE", "", None, "SUSP"])
    def test_unfinished_waits(self, status):
        assert self.dec(status, 0, 0).action == "wait"

    @pytest.mark.parametrize("hg,ag", [(None, 1), (1, None), (-1, 0), (True, 0), ("1", 0)])
    def test_missing_or_invalid_goals_wait(self, hg, ag):
        assert self.dec("FT", hg, ag).action == "wait"

    def test_manual_settlement_threshold(self):
        assert not irl.needs_manual_settlement(self.KICK, self.KICK + timedelta(hours=5, minutes=59))
        assert irl.needs_manual_settlement(self.KICK, self.KICK + timedelta(hours=6))


# ─── БД ──────────────────────────────────────────────────────────────────────

def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def make_user(uid, balance=5000):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                     (uid, f"irl{uid}"))
    database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = ? WHERE user_id = ?", (balance, uid))


def balance_of(uid):
    return database.get_or_create_wallet(uid)["balance"]


_fixture_seq = iter(range(700000, 800000))


def open_match(hours=3, odds=(2.0, 3.0, 4.0), publish=True):
    mid, _ = database.create_irl_draft(next(_fixture_seq), 39, "Premier League", "Arsenal", "Chelsea",
                                       _fmt(now_msk() + timedelta(hours=hours)), *odds)
    if publish:
        assert database.publish_irl_match(mid)[0]
    return mid


@pytest.fixture(autouse=True)
def _no_lockdown(monkeypatch):
    monkeypatch.delenv("LOGOVO_LOCKDOWN", raising=False)


class TestMigration:
    def test_tables_and_registration(self):
        with database.transaction() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?",
                                   (database.MIGRATION_035_IRL_BETTING,)).fetchone()
        assert {"irl_matches", "irl_bets"} <= tables and version

    def test_init_db_is_idempotent(self):
        database.init_db()
        database.init_db()
        with database.transaction() as conn:
            n = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                             (database.MIGRATION_035_IRL_BETTING,)).fetchone()[0]
        assert n == 1

    def test_db_enforces_amount_and_unique(self):
        import sqlite3
        make_user(810001)
        mid = open_match()
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute("INSERT INTO irl_bets (user_id, irl_match_id, outcome, amount, odd, potential_win, created_at)"
                             " VALUES (810001, ?, 'home', 1001, 2.0, 2002, datetime('now','+3 hours'))", (mid,))
        for _ in range(2):
            try:
                with database.transaction() as conn:
                    conn.execute("INSERT INTO irl_bets (user_id, irl_match_id, outcome, amount, odd, potential_win, created_at)"
                                 " VALUES (810001, ?, 'home', 10, 2.0, 20, datetime('now','+3 hours'))", (mid,))
            except sqlite3.IntegrityError:
                break
        else:
            pytest.fail("UNIQUE(user_id, irl_match_id) did not fire")


class TestDrafts:
    def test_create_is_idempotent_and_refreshes_draft_odds(self):
        fid = next(_fixture_seq)
        kick = _fmt(now_msk() + timedelta(hours=4))
        a, created = database.create_irl_draft(fid, 2, "UCL", "A", "B", kick, 2.0, 3.0, 4.0)
        b, created2 = database.create_irl_draft(fid, 2, "UCL", "A", "B", kick, 2.2, 3.1, 3.9)
        assert a == b and created and not created2
        assert database.get_irl_match(a)["odd_home"] == 2.2

    def test_published_match_odds_not_overwritten_by_redraft(self):
        mid = open_match(odds=(2.0, 3.0, 4.0))
        m = database.get_irl_match(mid)
        database.create_irl_draft(m["provider_fixture_id"], 39, "x", "a", "b", m["kickoff_at"], 9.0, 9.0, 9.0)
        assert database.get_irl_match(mid)["odd_home"] == 2.0

    def test_rejects_bad_odds_and_time(self):
        with pytest.raises(ValueError):
            database.create_irl_draft(next(_fixture_seq), 2, "x", "a", "b", _fmt(now_msk()), 1.0, 3.0, 3.0)
        with pytest.raises(ValueError):
            database.create_irl_draft(next(_fixture_seq), 2, "x", "a", "b", "not a date", 2.0, 3.0, 3.0)

    def test_publish_rejects_started_match_and_double_publish(self):
        past, _ = database.create_irl_draft(next(_fixture_seq), 2, "x", "a", "b",
                                            _fmt(now_msk() - timedelta(minutes=5)), 2.0, 3.0, 3.0)
        assert not database.publish_irl_match(past)[0]
        mid = open_match()
        assert not database.publish_irl_match(mid)[0]

    def test_update_odds_only_for_draft_or_open(self):
        mid = open_match()
        assert database.update_irl_odds(mid, 2.5, 3.2, 3.0)
        assert not database.update_irl_odds(mid, 0.5, 3.2, 3.0)
        database.void_irl_match(mid)
        assert not database.update_irl_odds(mid, 2.6, 3.2, 3.0)

    def test_close_started_matches(self):
        mid = open_match(hours=3)
        with database.transaction() as conn:
            conn.execute("UPDATE irl_matches SET kickoff_at = ? WHERE id = ?",
                         (_fmt(now_msk() - timedelta(minutes=1)), mid))
        assert database.close_started_irl_matches() >= 1
        assert database.get_irl_match(mid)["status"] == "closed"


class TestPlaceBet:
    def test_happy_path_debits_and_records(self):
        make_user(820001)
        mid = open_match(odds=(2.0, 3.0, 4.0))
        ok, res = database.place_irl_bet(820001, mid, "draw", 300)
        assert ok, res
        assert res["odd"] == 3.0 and res["potential_win"] == 900 and res["balance"] == 4700
        assert balance_of(820001) == 4700
        with database.transaction() as conn:
            tx = conn.execute("SELECT amount, balance_after FROM coin_transactions "
                              "WHERE reference_type='irl_bet' AND reference_id=? AND transaction_type=?",
                              (res["bet_id"], database.IRL_TX_BET)).fetchone()
        assert (tx["amount"], tx["balance_after"]) == (-300, 4700)

    def test_max_bet_is_enforced(self):
        make_user(820002)
        mid = open_match()
        ok, res = database.place_irl_bet(820002, mid, "home", 1001)
        assert not ok and res["error"] == "MAX_BET_EXCEEDED"
        assert database.place_irl_bet(820002, mid, "home", 1000)[0]

    @pytest.mark.parametrize("amount", [0, -1, "x", 2.5])
    def test_invalid_amounts_do_not_touch_wallet(self, amount):
        make_user(820003)
        mid = open_match()
        ok, res = database.place_irl_bet(820003, mid, "home", amount)
        assert not ok and res["error"] == "INVALID_AMOUNT" and balance_of(820003) == 5000

    def test_one_bet_per_match_but_other_match_ok(self):
        make_user(820004)
        m1, m2 = open_match(), open_match()
        assert database.place_irl_bet(820004, m1, "home", 100)[0]
        ok, res = database.place_irl_bet(820004, m1, "away", 100)
        assert not ok and res["error"] == database.IRL_ALREADY_BET_ERROR
        assert balance_of(820004) == 4900
        assert database.place_irl_bet(820004, m2, "home", 100)[0]

    def test_concurrent_duplicate_places_exactly_one(self):
        make_user(820005)
        mid = open_match()
        results = []
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            results.append(database.place_irl_bet(820005, mid, "home", 100))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sum(1 for ok, _ in results if ok) == 1
        assert balance_of(820005) == 4900
        with database.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM irl_bets WHERE user_id = 820005").fetchone()[0] == 1

    def test_closed_started_and_draft_matches_reject(self):
        make_user(820006)
        draft = open_match(publish=False)
        assert database.place_irl_bet(820006, draft, "home", 10)[1]["error"] == database.IRL_BETTING_CLOSED_ERROR
        started = open_match()
        with database.transaction() as conn:
            conn.execute("UPDATE irl_matches SET kickoff_at = ? WHERE id = ?",
                         (_fmt(now_msk() - timedelta(minutes=1)), started))
        assert database.place_irl_bet(820006, started, "home", 10)[1]["error"] == database.IRL_BETTING_CLOSED_ERROR
        assert balance_of(820006) == 5000

    def test_unknown_match_and_outcome(self):
        make_user(820007)
        assert database.place_irl_bet(820007, 99999999, "home", 10)[1]["error"] == "INVALID_SELECTION"
        assert database.place_irl_bet(820007, "abc", "home", 10)[1]["error"] == "INVALID_SELECTION"
        assert database.place_irl_bet(820007, open_match(), "over", 10)[1]["error"] == "INVALID_SELECTION"

    def test_odds_changed(self):
        make_user(820008)
        mid = open_match(odds=(2.0, 3.0, 4.0))
        database.update_irl_odds(mid, 1.8, 3.0, 4.5)
        ok, res = database.place_irl_bet(820008, mid, "home", 100, client_odd=2.0)
        assert not ok and res["error"] == "ODDS_CHANGED"
        assert (res["old_odd"], res["new_odd"]) == (2.0, 1.8)
        assert balance_of(820008) == 5000
        assert database.place_irl_bet(820008, mid, "home", 100, client_odd=1.8)[0]

    def test_insufficient_balance(self):
        make_user(820009, balance=50)
        ok, res = database.place_irl_bet(820009, open_match(), "home", 100)
        assert not ok and res["error"] == "INSUFFICIENT_BALANCE" and balance_of(820009) == 50

    def test_payout_ceiling_is_capped_on_the_bet(self):
        make_user(820010)
        mid = open_match(odds=(25.0, 3.0, 1.2))
        ok, res = database.place_irl_bet(820010, mid, "home", 1000)
        assert ok and res["potential_win"] == 10_000 and res["payout_capped"]

    def test_personal_ban_and_global_pause(self):
        make_user(820011)
        mid = open_match()
        database.set_betting_ban(820011, 1, "test")
        assert database.place_irl_bet(820011, mid, "home", 10)[1]["error"] == database.BETTING_BANNED_ERROR
        database.lift_betting_ban(820011, 1)
        database.set_betting_pause(1, True)
        try:
            assert database.place_irl_bet(820011, mid, "home", 10)[1]["error"] == database.BETTING_PAUSED_ERROR
        finally:
            database.set_betting_pause(1, False)
        assert database.place_irl_bet(820011, mid, "home", 10)[0]

    def test_lockdown_blocks_regular_users(self, monkeypatch):
        make_user(820012)
        mid = open_match()
        monkeypatch.setenv("LOGOVO_LOCKDOWN", "true")
        ok, res = database.place_irl_bet(820012, mid, "home", 10)
        assert not ok and res["error"] == "LOGOVO_LOCKDOWN" and balance_of(820012) == 5000


class TestSettlement:
    def _setup(self, base, odds=(2.0, 3.0, 4.0)):
        mid = open_match(odds=odds)
        users = {"home": base, "draw": base + 1, "away": base + 2}
        for outcome, uid in users.items():
            make_user(uid)
            assert database.place_irl_bet(uid, mid, outcome, 100)[0]
        return mid, users

    def test_settle_pays_winners_and_reconciles(self):
        mid, users = self._setup(830000)
        ok, res = database.settle_irl_match(mid, "home", 2, 1, actor_id=7)
        assert ok and res == {"won": 1, "lost": 2, "paid": 200}
        assert balance_of(users["home"]) == 5000 - 100 + 200
        assert balance_of(users["draw"]) == 4900 and balance_of(users["away"]) == 4900
        m = database.get_irl_match(mid)
        assert (m["status"], m["result"], m["home_goals"], m["settled_by"]) == ("settled", "home", 2, 7)
        with database.transaction() as conn:
            for uid in users.values():
                net = conn.execute("SELECT COALESCE(SUM(amount),0) FROM coin_transactions "
                                   "WHERE user_id = ? AND reference_type = 'irl_bet'", (uid,)).fetchone()[0]
                assert 5000 + net == balance_of(uid)
            statuses = {r["outcome"]: (r["status"], r["actual_payout"]) for r in
                        conn.execute("SELECT outcome, status, actual_payout FROM irl_bets WHERE irl_match_id = ?", (mid,))}
        assert statuses == {"home": ("won", 200), "draw": ("lost", 0), "away": ("lost", 0)}

    def test_second_settle_pays_nothing(self):
        mid, users = self._setup(831000)
        assert database.settle_irl_match(mid, "draw")[0]
        before = {u: balance_of(u) for u in users.values()}
        ok, _ = database.settle_irl_match(mid, "draw")
        assert not ok
        assert before == {u: balance_of(u) for u in users.values()}

    def test_settle_requires_known_result_and_published_match(self):
        mid = open_match(publish=False)
        assert not database.settle_irl_match(mid, "home")[0]
        assert not database.settle_irl_match(open_match(), "nope")[0]
        assert not database.settle_irl_match(99999999, "home")[0]

    def test_closed_match_can_be_settled(self):
        mid, _ = self._setup(832000)
        with database.transaction() as conn:
            conn.execute("UPDATE irl_matches SET kickoff_at = ?, status = 'closed' WHERE id = ?",
                         (_fmt(now_msk() - timedelta(hours=3)), mid))
        assert database.settle_irl_match(mid, "away")[0]

    def test_settle_capped_payout(self):
        make_user(833000)
        mid = open_match(odds=(25.0, 3.0, 1.2))
        database.place_irl_bet(833000, mid, "home", 1000)
        database.settle_irl_match(mid, "home")
        assert balance_of(833000) == 5000 - 1000 + 10_000

    def test_void_refunds_everyone_and_blocks_later_settle(self):
        mid, users = self._setup(834000)
        ok, res = database.void_irl_match(mid, "postponed", actor_id=7)
        assert ok and res == {"refunded": 3}
        assert all(balance_of(u) == 5000 for u in users.values())
        m = database.get_irl_match(mid)
        assert (m["status"], m["void_reason"]) == ("void", "postponed")
        assert not database.void_irl_match(mid)[0]
        assert not database.settle_irl_match(mid, "home")[0]
        assert all(balance_of(u) == 5000 for u in users.values())

    def test_void_draft_without_bets(self):
        assert database.void_irl_match(open_match(publish=False), "replaced")[0]

    def test_settle_enqueues_notice_once_per_bet(self):
        mid, users = self._setup(835000)
        database.settle_irl_match(mid, "home")
        database.settle_irl_match(mid, "home")
        with database.transaction() as conn:
            n = conn.execute("SELECT COUNT(*) FROM notification_events WHERE source_event_id LIKE 'ibet_%' "
                             "AND user_id IN (?, ?, ?)", tuple(users.values())).fetchone()[0]
        assert n <= 3


class TestQueries:
    def test_user_bets_and_lookup(self):
        make_user(840001)
        mid = open_match()
        database.place_irl_bet(840001, mid, "away", 50)
        bets = database.get_user_irl_bets(840001)
        assert len(bets) == 1 and bets[0]["home"] == "Arsenal" and bets[0]["match_status"] == "open"
        assert database.get_user_irl_bet_for_match(840001, mid)["outcome"] == "away"
        assert database.get_user_irl_bet_for_match(840001, mid + 999) is None

    def test_list_matches_by_day_and_status(self):
        mid = open_match()
        day = database.get_irl_match(mid)["bet_day"]
        assert any(m["id"] == mid for m in database.list_irl_matches(day, ("open",)))
        assert all(m["id"] != mid for m in database.list_irl_matches(day, ("settled",)))


def test_config_defaults_are_sane():
    assert config.IRL_MAX_BET == 1000 or config.IRL_MAX_BET > 0
    assert config.IRL_MAX_MATCHES_PER_DAY >= 1


class TestIrlExpress:
    def test_place_express_valid_and_wallet_debit(self):
        make_user(850001)
        m1 = open_match(odds=(2.0, 3.0, 4.0))
        m2 = open_match(odds=(2.5, 3.2, 2.8))
        m3 = open_match(odds=(1.8, 3.5, 5.0))
        items = [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m2, "outcome": "draw"},
            {"match_id": m3, "outcome": "away"},
        ]
        ok, res = database.place_irl_express(850001, items, 300)
        assert ok, res
        assert res["total_odd"] > 1.0
        assert res["potential_win"] > 300
        assert balance_of(850001) == 4700

        bets = database.get_user_irl_bets(850001)
        assert len(bets) == 1
        assert bets[0]["bet_type"] == "express"
        assert len(bets[0]["items"]) == 3

    def test_express_invalid_legs_count(self):
        make_user(850002)
        m1 = open_match()
        # 1 leg
        ok, res = database.place_irl_express(850002, [{"match_id": m1, "outcome": "home"}], 100)
        assert not ok and res["error"] == database.IRL_INVALID_EXPRESS_LEGS_ERROR

        # 6 legs
        matches = [open_match() for _ in range(6)]
        items = [{"match_id": m, "outcome": "home"} for m in matches]
        ok, res = database.place_irl_express(850002, items, 100)
        assert not ok and res["error"] == database.IRL_INVALID_EXPRESS_LEGS_ERROR

    def test_express_duplicate_match(self):
        make_user(850003)
        m1 = open_match()
        items = [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m1, "outcome": "draw"},
        ]
        ok, res = database.place_irl_express(850003, items, 100)
        assert not ok and res["error"] == database.IRL_DUPLICATE_EXPRESS_MATCH_ERROR

    def test_express_already_bet_on_match(self):
        make_user(850004)
        m1 = open_match()
        m2 = open_match()
        database.place_irl_bet(850004, m1, "home", 100)
        ok, res = database.place_irl_express(850004, [
            {"match_id": m1, "outcome": "draw"},
            {"match_id": m2, "outcome": "away"},
        ], 100)
        assert not ok and res["error"] == database.IRL_ALREADY_BET_ERROR

    def test_express_settle_all_win(self):
        make_user(850005)
        m1 = open_match(odds=(2.0, 3.0, 4.0))
        m2 = open_match(odds=(2.0, 3.0, 4.0))
        ok, res = database.place_irl_express(850005, [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m2, "outcome": "home"},
        ], 200)
        assert ok
        pot_win = res["potential_win"]

        # Settle first match -> still pending
        database.settle_irl_match(m1, "home")
        exp = database.get_user_irl_bets(850005)[0]
        assert exp["status"] == "pending"

        # Settle second match -> won
        database.settle_irl_match(m2, "home")
        exp = database.get_user_irl_bets(850005)[0]
        assert exp["status"] == "won"
        assert balance_of(850005) == 4800 + pot_win

    def test_express_settle_one_lost(self):
        make_user(850006)
        m1 = open_match(odds=(2.0, 3.0, 4.0))
        m2 = open_match(odds=(2.0, 3.0, 4.0))
        ok, res = database.place_irl_express(850006, [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m2, "outcome": "home"},
        ], 200)
        assert ok

        # Settle first match as lost
        database.settle_irl_match(m1, "away")
        exp = database.get_user_irl_bets(850006)[0]
        assert exp["status"] == "lost"
        assert exp["actual_payout"] == 0

    def test_express_void_leg_recalculates(self):
        make_user(850007)
        m1 = open_match(odds=(2.0, 3.0, 4.0))
        m2 = open_match(odds=(3.0, 3.0, 3.0))
        ok, res = database.place_irl_express(850007, [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m2, "outcome": "home"},
        ], 200)
        assert ok

        # Void match 1
        database.void_irl_match(m1, reason="postponed")
        # Settle match 2 as win
        database.settle_irl_match(m2, "home")

        exp = database.get_user_irl_bets(850007)[0]
        assert exp["status"] == "won"
        assert exp["actual_payout"] > 200

    def test_express_all_legs_void_refunds_stake(self):
        make_user(850008)
        m1 = open_match()
        m2 = open_match()
        ok, res = database.place_irl_express(850008, [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m2, "outcome": "home"},
        ], 300)
        assert ok
        assert balance_of(850008) == 4700

        database.void_irl_match(m1, reason="canceled")
        database.void_irl_match(m2, reason="canceled")

        exp = database.get_user_irl_bets(850008)[0]
        assert exp["status"] == "refunded"
        assert balance_of(850008) == 5000

    def test_admin_void_user_express_bet(self):
        make_user(850009)
        m1 = open_match()
        m2 = open_match()
        ok, res = database.place_irl_express(850009, [
            {"match_id": m1, "outcome": "home"},
            {"match_id": m2, "outcome": "home"},
        ], 250)
        assert ok
        assert balance_of(850009) == 4750

        ok_void, res_void = database.void_user_irl_bet("express", res["express_id"])
        assert ok_void
        assert balance_of(850009) == 5000

    def test_summary_and_all_bets_query(self):
        stats = database.get_irl_betting_summary_stats()
        assert "turnover" in stats and "payouts" in stats and "ggr" in stats
        all_bets = database.get_all_irl_bets()
        assert "bets" in all_bets and "total" in all_bets


class TestMatchBatches:
    def test_group_simultaneous_matches_into_batches(self):
        from services.irl_betting import group_matches_into_batches

        matches = [
            {"id": 10, "provider_fixture_id": 501, "kickoff_at": "2026-10-10 14:30:00", "home": "Arsenal", "away": "Leeds"},
            {"id": 12, "provider_fixture_id": 502, "kickoff_at": "2026-10-10 17:00:00", "home": "Chelsea", "away": "Bournemouth"},
            {"id": 11, "provider_fixture_id": 503, "kickoff_at": "2026-10-10 19:30:00", "home": "Man Utd", "away": "Tottenham"},
            {"id": 13, "provider_fixture_id": 504, "kickoff_at": "2026-10-10 19:30:00", "home": "Barcelona", "away": "Getafe"},
            {"id": 14, "provider_fixture_id": 505, "kickoff_at": "2026-10-10 22:00:00", "home": "Real Madrid", "away": "Villarreal"},
        ]

        batches = group_matches_into_batches(matches)
        assert len(batches) == 4

        # 14:30 batch
        assert batches[0].slot_time == "2026-10-10 14:30"
        assert batches[0].count == 1
        assert not batches[0].is_simultaneous
        assert batches[0].fixture_ids == ["501"]

        # 17:00 batch
        assert batches[1].slot_time == "2026-10-10 17:00"
        assert batches[1].count == 1
        assert not batches[1].is_simultaneous
        assert batches[1].fixture_ids == ["502"]

        # 19:30 simultaneous batch
        assert batches[2].slot_time == "2026-10-10 19:30"
        assert batches[2].time_label == "19:30 МСК"
        assert batches[2].count == 2
        assert batches[2].is_simultaneous
        assert batches[2].fixture_ids == ["503", "504"]
        assert [m["id"] for m in batches[2].matches] == [11, 13]

        # 22:00 batch
        assert batches[3].slot_time == "2026-10-10 22:00"
        assert batches[3].count == 1
        assert not batches[3].is_simultaneous
        assert batches[3].fixture_ids == ["505"]

    def test_empty_matches_returns_empty_batches(self):
        from services.irl_betting import group_matches_into_batches
        assert group_matches_into_batches([]) == []


