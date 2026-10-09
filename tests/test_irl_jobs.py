"""tests/test_irl_jobs.py — background jobs of IRL betting (fake provider, fake bot, frozen clock)."""

import asyncio
import inspect
from datetime import datetime, timedelta

import pytest

import config
import database
from services import irl_jobs
from services.sports.models import MatchWinnerOdds, PrematchFixture

NOW = datetime(2026, 10, 3, 10, 0, 0)        # 10:00 MSK: after preview (9), before publish (12)
DAY = NOW.date().isoformat()


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, now):
        self.now = now


@pytest.fixture
def clock(monkeypatch):
    c = Clock(NOW)
    monkeypatch.setattr(database, "now_msk", lambda: c.now)
    monkeypatch.setattr(database, "now_msk_str", lambda fmt="%Y-%m-%d %H:%M:%S": c.now.strftime(fmt))
    monkeypatch.setattr(irl_jobs, "now_msk", lambda: c.now)
    return c


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.delenv("LOGOVO_LOCKDOWN", raising=False)
    monkeypatch.setattr(config, "IRL_ENABLED", True)
    monkeypatch.setattr(config, "IRL_BOOKMAKER_ID", 8)
    monkeypatch.setattr(config, "IRL_COMPETITION_PRIORITY", [2, 39, 140])
    monkeypatch.setattr(config, "IRL_TOP_TEAMS", ["Arsenal", "Chelsea", "Real Madrid", "Barcelona"])
    monkeypatch.setattr(config, "IRL_MAX_MATCHES_PER_DAY", 2)
    monkeypatch.setattr(config, "IRL_PREVIEW_HOUR_MSK", 9)
    monkeypatch.setattr(config, "IRL_AUTO_PUBLISH_HOUR_MSK", 12)
    monkeypatch.setattr(config, "IRL_AUTO_PUBLISH", True)
    monkeypatch.setattr(config, "ADMIN_IDS", [111, 222], raising=False)
    irl_jobs.reset_state()
    with database.transaction() as conn:
        conn.execute("DELETE FROM irl_bets")
        conn.execute("DELETE FROM irl_matches")
        conn.execute("DELETE FROM irl_notices")
    yield
    irl_jobs.reset_state()


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))

    def texts(self):
        return [t for _, t in self.sent]


def fx(fid, league=39, home="Arsenal", away="Chelsea", hours=8, status="NS", goals=(None, None), season=2026):
    return PrematchFixture(fixture_id=fid, league_id=league, league_name=f"L{league}", home=home, away=away,
                           kickoff=NOW + timedelta(hours=hours), status_short=status,
                           home_goals=goals[0], away_goals=goals[1], season=season)


class FakeProvider:
    def __init__(self, fixtures=(), odds=None, standings=None, results=None):
        self.fixtures = None if fixtures is None else list(fixtures)
        self.odds = odds or {}                  # fixture_id -> (h, d, a) | None
        self.standings = standings or {}        # league_id -> rows
        self.results = results or {}            # fixture_id -> PrematchFixture | None
        self.calls = []

    async def get_prematch_fixtures(self, date, league_ids=None):
        self.calls.append(("fixtures", date))
        if self.fixtures is None:
            return None
        return [f for f in self.fixtures if league_ids is None or f.league_id in league_ids]

    async def get_match_winner_odds(self, fixture_id, bookmaker_id):
        self.calls.append(("odds", int(fixture_id)))
        o = self.odds.get(int(fixture_id))
        return None if o is None else MatchWinnerOdds(int(fixture_id), bookmaker_id, *o)

    async def get_standings(self, league_id, season):
        self.calls.append(("standings", league_id))
        return self.standings.get(league_id, [])

    async def get_prematch_fixture(self, fixture_id):
        self.calls.append(("fixture", int(fixture_id)))
        return self.results.get(int(fixture_id))

    async def get_prematch_fixtures_batch(self, fixture_ids):
        self.calls.append(("batch_fixtures", [int(fid) for fid in fixture_ids]))
        return {str(fid): self.results.get(int(fid)) for fid in fixture_ids}


def day_matches(statuses=None):
    return database.list_irl_matches(bet_day=DAY, statuses=statuses)


def make_user(uid, balance=5000):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                     (uid, f"irlj{uid}"))
    database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = ? WHERE user_id = ?", (balance, uid))


# ─── Pick ────────────────────────────────────────────────────────────────────

class TestPick:
    def test_picks_top_match_drafts_it_and_sends_one_preview(self, clock):
        p = FakeProvider([fx(1), fx(2, home="Everton", away="Fulham")],
                         odds={1: (2.1, 3.4, 3.3), 2: (1.9, 3.5, 4.0)})
        bot = FakeBot()
        rep = run(irl_jobs.run_pick(bot, p))
        assert rep["picked"] == 1 and rep["published"] == 0
        (m,) = day_matches()
        assert (m["provider_fixture_id"], m["status"], m["picked_by"]) == ("1", "draft", "auto")
        assert len(bot.sent) == 2 and "Arsenal" in bot.texts()[0] and "12:00" in bot.texts()[0]
        # A second pass does not pick again and does not repeat the preview.
        run(irl_jobs.run_pick(bot, p))
        assert len(day_matches()) == 1 and len(bot.sent) == 2

    def test_nothing_before_the_preview_hour(self, clock):
        clock.now = NOW.replace(hour=8)
        p = FakeProvider([fx(1)], odds={1: (2.0, 3.0, 4.0)})
        run(irl_jobs.run_pick(FakeBot(), p))
        assert p.calls == [] and day_matches() == []

    def test_first_priority_league_wins_and_lower_leagues_are_not_queried(self, clock):
        p = FakeProvider([fx(1, league=2, home="Real Madrid", away="Inter"), fx(2, league=39)],
                         odds={1: (2.0, 3.5, 3.6), 2: (2.0, 3.4, 3.8)})
        run(irl_jobs.run_pick(FakeBot(), p))
        assert [m["provider_fixture_id"] for m in day_matches()] == ["1"]
        assert ("odds", 2) not in p.calls

    def test_two_matches_with_equal_top_count(self, clock):
        p = FakeProvider([fx(1), fx(2, home="Real Madrid", away="Barcelona"), fx(3, home="A", away="B")],
                         odds={1: (2.0, 3.4, 3.8), 2: (2.2, 3.5, 3.1), 3: (2.0, 3.0, 4.0)})
        rep = run(irl_jobs.run_pick(FakeBot(), p))
        assert rep["picked"] == 2
        assert {m["provider_fixture_id"] for m in day_matches()} == {"1", "2"}

    def test_table_bonus_uses_standings_of_the_season(self, clock):
        p = FakeProvider([fx(1, home="Everton", away="Fulham"), fx(2, home="Leeds", away="Wolves")],
                         odds={1: (2.0, 3.0, 4.0), 2: (2.0, 3.0, 4.0)},
                         standings={39: [{"rank": 1, "team": {"name": "Leeds"}},
                                         {"rank": 2, "team": {"name": "Wolves"}}, {"bad": True}]})
        run(irl_jobs.run_pick(FakeBot(), p))
        assert [m["home"] for m in day_matches()] == ["Leeds"]
        assert ("standings", 39) in p.calls

    def test_provider_outage_is_not_an_empty_day(self, clock):
        bot = FakeBot()
        rep = run(irl_jobs.run_pick(bot, FakeProvider(fixtures=None)))
        assert rep["note"] == "provider_down"
        assert all("провайдер" in t for t in bot.texts()) and not any("нет подходящих" in t for t in bot.texts())

    def test_empty_day_notice_is_sent_once(self, clock):
        bot = FakeBot()
        rep = run(irl_jobs.run_pick(bot, FakeProvider([])))
        assert rep["note"] == "no_matches" and len(bot.sent) == 2
        clock.now += irl_jobs.PICK_RETRY
        run(irl_jobs.run_pick(bot, FakeProvider([])))
        assert len(bot.sent) == 2

    def test_retry_is_throttled(self, clock):
        p = FakeProvider([])
        run(irl_jobs.run_pick(FakeBot(), p))
        n = len(p.calls)
        clock.now += timedelta(minutes=10)
        assert run(irl_jobs.run_pick(FakeBot(), p))["note"] == "throttled"
        assert len(p.calls) == n

    def test_odds_failure_waits_until_publish_hour_then_says_no_odds(self, clock):
        p = FakeProvider([fx(1)], odds={1: None})
        bot = FakeBot()
        assert run(irl_jobs.run_pick(bot, p))["note"] == "odds_unavailable" and bot.sent == []
        clock.now = NOW.replace(hour=12, minute=5)
        assert run(irl_jobs.run_pick(bot, p))["note"] == "no_odds"
        assert any("коэффициентов" in t for t in bot.texts())

    def test_waits_for_odds_of_a_higher_priority_league(self, clock):
        p = FakeProvider([fx(1, league=2, home="Real Madrid", away="Inter"), fx(2, league=39)],
                         odds={1: None, 2: (2.0, 3.4, 3.8)})
        assert run(irl_jobs.run_pick(FakeBot(), p))["note"] == "waiting_for_higher_odds"
        assert day_matches() == []
        clock.now = NOW.replace(hour=12)
        run(irl_jobs.run_pick(FakeBot(), p))
        assert [m["provider_fixture_id"] for m in day_matches()] == ["2"]

    def test_started_matches_and_known_fixtures_are_skipped(self, clock):
        database.create_irl_draft(5, 39, "L39", "Arsenal", "Chelsea", NOW + timedelta(days=2), 2, 3, 4)
        p = FakeProvider([fx(4, hours=-1), fx(5)], odds={4: (2, 3, 4), 5: (2, 3, 4)})
        assert run(irl_jobs.run_pick(FakeBot(), p))["note"] == "no_matches"
        assert ("odds", 4) not in p.calls and ("odds", 5) not in p.calls

    def test_no_bookmaker_configured(self, clock, monkeypatch):
        monkeypatch.setattr(config, "IRL_BOOKMAKER_ID", 0)
        p = FakeProvider([fx(1)], odds={1: (2, 3, 4)})
        bot = FakeBot()
        assert run(irl_jobs.run_pick(bot, p))["note"] == "no_bookmaker"
        assert p.calls == [] and "IRL_BOOKMAKER_ID" in bot.texts()[0]

    def test_disabled_does_nothing(self, clock, monkeypatch):
        monkeypatch.setattr(config, "IRL_ENABLED", False)
        p = FakeProvider([fx(1)], odds={1: (2, 3, 4)})
        assert run(irl_jobs.run_pick(FakeBot(), p))["note"] == "disabled" and p.calls == []

    def test_auto_pick_disabled_does_not_pick(self, clock, monkeypatch):
        monkeypatch.setattr(config, "IRL_AUTO_PICK", False)
        p = FakeProvider([fx(1)], odds={1: (2, 3, 4)})
        bot = FakeBot()
        rep = run(irl_jobs.run_pick(bot, p))
        assert rep["picked"] == 0
        assert p.calls == []
        assert day_matches() == []

    def test_undelivered_preview_is_retried_on_the_next_pass(self, clock):
        p = FakeProvider([fx(1)], odds={1: (2, 3, 4)})
        run(irl_jobs.run_pick(FakeBot(fail_for={111, 222}), p))
        assert not database.irl_notice_sent(f"preview:{DAY}")
        bot = FakeBot(fail_for={111})
        run(irl_jobs.run_pick(bot, p))
        assert database.irl_notice_sent(f"preview:{DAY}")
        assert len(bot.sent) == 1 and "Arsenal" in bot.texts()[0]
        assert len(day_matches()) == 1                    # retried the message, not the pick


# ─── Publish ─────────────────────────────────────────────────────────────────

class TestPublish:
    def test_publishes_at_the_publish_hour(self, clock):
        p = FakeProvider([fx(1)], odds={1: (2, 3, 4)})
        run(irl_jobs.run_pick(FakeBot(), p))
        clock.now = NOW.replace(hour=12)
        assert run(irl_jobs.run_pick(FakeBot(), p))["published"] == 1
        assert day_matches()[0]["status"] == "open"

    def test_publishes_early_before_an_early_kickoff(self, clock):
        p = FakeProvider([fx(1, hours=0.4)], odds={1: (2, 3, 4)})    # 10:24, deadline 09:54
        rep = run(irl_jobs.run_pick(FakeBot(), p))
        assert rep["published"] == 1 and day_matches()[0]["status"] == "open"

    def test_dry_run_never_publishes(self, clock, monkeypatch):
        monkeypatch.setattr(config, "IRL_AUTO_PUBLISH", False)
        p = FakeProvider([fx(1)], odds={1: (2, 3, 4)})
        bot = FakeBot()
        run(irl_jobs.run_pick(bot, p))
        clock.now = NOW.replace(hour=13)
        assert run(irl_jobs.run_pick(bot, p))["published"] == 0
        assert day_matches()[0]["status"] == "draft"
        assert "Автопубликация выключена" in bot.texts()[0]

    def test_unpublished_draft_expires_at_kickoff(self, clock, monkeypatch):
        monkeypatch.setattr(config, "IRL_AUTO_PUBLISH", False)
        p = FakeProvider([fx(1, hours=1)], odds={1: (2, 3, 4)})
        run(irl_jobs.run_pick(FakeBot(), p))
        clock.now = NOW + timedelta(hours=1)
        assert run(irl_jobs.run_pick(FakeBot(), p))["expired"] == 1
        assert day_matches()[0]["status"] == "void"


# ─── Odds refresh ────────────────────────────────────────────────────────────

class TestOddsRefresh:
    def test_updates_changed_odds_and_keeps_old_on_failure(self, clock):
        a, _ = database.create_irl_draft(11, 39, "L39", "A", "B", NOW + timedelta(hours=5), 2, 3, 4)
        b, _ = database.create_irl_draft(12, 39, "L39", "C", "D", NOW + timedelta(hours=5), 2, 3, 4)
        far, _ = database.create_irl_draft(13, 39, "L39", "E", "F", NOW + timedelta(days=2), 2, 3, 4)
        p = FakeProvider(odds={11: (2.2, 3.1, 3.5), 12: None, 13: (5, 5, 5)})
        assert run(irl_jobs.run_odds_refresh(p)) == 1
        assert database.get_irl_match(a)["odd_home"] == 2.2
        assert database.get_irl_match(b)["odd_home"] == 2.0
        assert ("odds", 13) not in p.calls and database.get_irl_match(far)["odd_home"] == 2.0

    def test_same_odds_are_not_rewritten(self, clock):
        database.create_irl_draft(14, 39, "L39", "A", "B", NOW + timedelta(hours=5), 2, 3, 4)
        assert run(irl_jobs.run_odds_refresh(FakeProvider(odds={14: (2.0, 3.0, 4.0)}))) == 0

    def test_started_and_settled_matches_are_left_alone(self, clock):
        database.create_irl_draft(15, 39, "L39", "A", "B", NOW - timedelta(minutes=5), 2, 3, 4)
        p = FakeProvider(odds={15: (9, 9, 9)})
        assert run(irl_jobs.run_odds_refresh(p)) == 0 and p.calls == []


# ─── Settle ──────────────────────────────────────────────────────────────────

def _open_and_bet(fid, hours_ago=3, outcome="home", uid=None):
    clock_now = database.now_msk()
    mid, _ = database.create_irl_draft(fid, 39, "L39", "Arsenal", "Chelsea",
                                       clock_now + timedelta(hours=1), 2.0, 3.0, 4.0)
    assert database.publish_irl_match(mid)[0]
    if uid:
        make_user(uid)
        ok, info = database.place_irl_bet(uid, mid, outcome, 500)
        assert ok, info
    with database.transaction() as conn:
        conn.execute("UPDATE irl_matches SET kickoff_at = ? WHERE id = ?",
                     ((clock_now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S"), mid))
    return mid


def result(fid, status="FT", goals=(2, 1)):
    return PrematchFixture(fixture_id=fid, league_id=39, league_name="L39", home="Arsenal", away="Chelsea",
                           kickoff=NOW - timedelta(hours=3), status_short=status,
                           home_goals=goals[0], away_goals=goals[1])


class TestSettle:
    def test_finished_match_is_settled_and_admins_told(self, clock):
        mid = _open_and_bet(21, uid=930001)
        bot = FakeBot()
        rep = run(irl_jobs.run_settle(bot, FakeProvider(results={21: result(21)})))
        assert rep["settled"] == 1
        m = database.get_irl_match(mid)
        assert (m["status"], m["result"], m["home_goals"], m["away_goals"]) == ("settled", "home", 2, 1)
        assert database.get_user_irl_bet_for_match(930001, mid)["status"] == "won"
        assert any("рассчитан" in t and "П1" in t for t in bot.texts())

    def test_penalties_settle_on_the_main_time_draw(self, clock):
        mid = _open_and_bet(22, uid=930002, outcome="draw")
        run(irl_jobs.run_settle(FakeBot(), FakeProvider(results={22: result(22, "PEN", (1, 1))})))
        assert database.get_irl_match(mid)["result"] == "draw"
        assert database.get_user_irl_bet_for_match(930002, mid)["status"] == "won"

    def test_postponed_match_is_voided_with_refund(self, clock):
        mid = _open_and_bet(23, uid=930003)
        rep = run(irl_jobs.run_settle(FakeBot(), FakeProvider(results={23: result(23, "PST", (None, None))})))
        assert rep["voided"] == 1 and database.get_irl_match(mid)["status"] == "void"
        assert database.get_user_irl_bet_for_match(930003, mid)["status"] == "refunded"

    def test_running_or_unknown_waits(self, clock):
        a = _open_and_bet(24)
        b = _open_and_bet(25)
        rep = run(irl_jobs.run_settle(FakeBot(), FakeProvider(results={24: result(24, "2H", (1, 0))})))
        assert rep == {"settled": 0, "voided": 0, "manual": 0}
        assert database.get_irl_match(a)["status"] == database.get_irl_match(b)["status"] == "closed"

    def test_early_ft_flag_waits(self, clock):
        mid = _open_and_bet(26, hours_ago=1)
        run(irl_jobs.run_settle(FakeBot(), FakeProvider(results={26: result(26)})))
        assert database.get_irl_match(mid)["status"] == "closed"

    def test_long_unsettled_match_goes_to_admins_once(self, clock):
        mid = _open_and_bet(27, hours_ago=7, uid=930004)
        bot = FakeBot()
        assert run(irl_jobs.run_settle(bot, FakeProvider()))["manual"] == 1
        assert any(f"/irl_settle {mid}" in t and "500" in t for t in bot.texts())
        assert run(irl_jobs.run_settle(bot, FakeProvider()))["manual"] == 0
        assert database.get_irl_match(mid)["status"] == "closed"

    def test_not_started_matches_are_not_queried(self, clock):
        database.create_irl_draft(28, 39, "L39", "A", "B", NOW + timedelta(hours=2), 2, 3, 4)
        p = FakeProvider()
        run(irl_jobs.run_settle(FakeBot(), p))
        assert p.calls == []

    def test_match_not_polled_in_first_40_minutes(self, clock):
        # 20 minutes from kickoff: no query should be dispatched
        mid = _open_and_bet(29, hours_ago=0.33)
        p = FakeProvider(results={29: result(29, "1H", (1, 0))})
        run(irl_jobs.run_settle(FakeBot(), p))
        assert p.calls == []

    def test_live_window_updates_score_without_settling(self, clock):
        # 50 minutes from kickoff: halftime window polls once and updates score
        mid = _open_and_bet(30, hours_ago=0.83)
        p = FakeProvider(results={30: result(30, "1H", (1, 0))})
        run(irl_jobs.run_settle(FakeBot(), p))
        assert len(p.calls) == 1
        m = database.get_irl_match(mid)
        assert m["status"] == "closed"
        assert m["home_goals"] == 1 and m["away_goals"] == 0

    def test_quota_exhausted_alerts_admins_once(self, clock):
        mid = _open_and_bet(31, hours_ago=3)
        p = FakeProvider(results={31: result(31)})
        p.is_quota_exhausted = lambda: True
        bot = FakeBot()
        run(irl_jobs.run_settle(bot, p))
        assert any("суточный лимит запросов к API-Sports" in t for t in bot.texts())
        initial_sent = len(bot.texts())
        assert initial_sent == len(config.ADMIN_IDS)
        # Second call does not re-alert (notify_once)
        run(irl_jobs.run_settle(bot, p))
        assert len(bot.texts()) == initial_sent

    def test_simultaneous_matches_settle_in_single_batch_call(self, clock):
        mid1 = _open_and_bet(32, hours_ago=2, uid=930032)
        mid2 = _open_and_bet(33, hours_ago=2, uid=930033)
        p = FakeProvider(results={32: result(32, "FT", (2, 0)), 33: result(33, "FT", (1, 1))})
        bot = FakeBot()
        rep = run(irl_jobs.run_settle(bot, p))
        assert rep["settled"] == 2
        batch_calls = [c for c in p.calls if c[0] == "batch_fixtures"]
        assert len(batch_calls) == 1
        assert set(batch_calls[0][1]) == {32, 33}



# ─── Wiring ──────────────────────────────────────────────────────────────────

class TestRegistration:
    class _JobQueue:
        def __init__(self):
            self.jobs = {}

        def run_repeating(self, callback, interval, first, name):
            self.jobs[name] = (callback, interval)

    class _App:
        def __init__(self, jq):
            self.job_queue = jq

    def _register(self):
        import main
        jq = self._JobQueue()
        main.register_jobs(self._App(jq))
        return jq.jobs

    def test_jobs_registered_only_when_enabled(self, monkeypatch):
        jobs = self._register()
        assert inspect.unwrap(jobs["irl_pick"][0]) is irl_jobs.job_irl_pick
        assert inspect.unwrap(jobs["irl_odds_refresh"][0]) is irl_jobs.job_irl_odds_refresh
        assert inspect.unwrap(jobs["irl_settle"][0]) is irl_jobs.job_irl_settle
        monkeypatch.setattr(config, "IRL_ENABLED", False)
        assert not {"irl_pick", "irl_odds_refresh", "irl_settle"} & set(self._register())
