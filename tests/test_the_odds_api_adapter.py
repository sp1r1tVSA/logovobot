"""tests/test_the_odds_api_adapter.py — Tests for The Odds API adapter (the-odds-api.com)."""

import asyncio
from typing import Any

import pytest

import config
from services.sports.adapters.the_odds_api import (
    DEFAULT_API_SPORTS_TO_ODDS_SPORT,
    DEFAULT_ODDS_SPORT_TO_API_SPORTS,
    TheOddsApiProvider,
)
from services.sports.models import MatchWinnerOdds, PrematchFixture


def run(coro):
    return asyncio.run(coro)


def make_odds_item(
    event_id: str = "evt100",
    sport_key: str = "soccer_epl",
    home: str = "Arsenal",
    away: str = "Chelsea",
    commence_time: str = "2026-10-06T16:30:00Z",  # 19:30 MSK
    bm_key: str = "pinnacle",
    prices: tuple[float, float, float] = (1.95, 3.60, 3.80),
) -> dict[str, Any]:
    return {
        "id": event_id,
        "sport_key": sport_key,
        "sport_title": "EPL",
        "commence_time": commence_time,
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": bm_key,
                "title": bm_key.capitalize(),
                "last_update": "2026-10-06T12:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-10-06T12:00:00Z",
                        "outcomes": [
                            {"name": home, "price": prices[0]},
                            {"name": "Draw", "price": prices[1]},
                            {"name": away, "price": prices[2]},
                        ],
                    }
                ],
            }
        ],
    }


def make_score_item(
    event_id: str = "evt100",
    sport_key: str = "soccer_epl",
    home: str = "Arsenal",
    away: str = "Chelsea",
    completed: bool = True,
    home_score: int = 2,
    away_score: int = 1,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "sport_key": sport_key,
        "sport_title": "EPL",
        "commence_time": "2026-10-06T16:30:00Z",
        "completed": completed,
        "home_team": home,
        "away_team": away,
        "scores": [
            {"name": home, "score": str(home_score)},
            {"name": away, "score": str(away_score)},
        ],
    }


class FakeOddsApi(TheOddsApiProvider):
    """Test adapter overriding _fetch_json to simulate canned responses and failures."""

    def __init__(self, canned_data: Any = None, boom: bool = False, headers: dict = None):
        super().__init__(api_key="test-odds-api-key")
        self.canned_data = canned_data
        self.boom = boom
        self.simulated_headers = headers or {}
        self.calls: list[tuple[str, dict]] = []

    async def _fetch_json(self, endpoint: str, params: dict = None, cache_ttl: float = None) -> Any:
        self.calls.append((endpoint, dict(params or {})))
        if self.boom:
            raise RuntimeError("Network down")
        if "x-requests-remaining" in self.simulated_headers:
            rem = int(self.simulated_headers["x-requests-remaining"])
            self._requests_remaining = rem
            if rem <= 0:
                self._quota_exhausted = True
        return self.canned_data


class TestTheOddsApiPrematch:
    def test_fixtures_filtered_by_msk_date(self):
        # 16:30 UTC = 19:30 MSK on 2026-10-06
        item1 = make_odds_item("e1", commence_time="2026-10-06T16:30:00Z")
        # 22:30 UTC = 01:30 MSK on 2026-10-07 (next day in MSK!)
        item2 = make_odds_item("e2", commence_time="2026-10-06T22:30:00Z")

        provider = FakeOddsApi([item1, item2])
        fixtures = run(provider.get_prematch_fixtures("2026-10-06", [39]))
        assert fixtures is not None
        assert len(fixtures) == 1
        assert fixtures[0].fixture_id == "e1"
        assert fixtures[0].league_id == 39
        assert fixtures[0].home == "Arsenal"
        assert fixtures[0].away == "Chelsea"
        assert fixtures[0].kickoff.hour == 19
        assert fixtures[0].kickoff.minute == 30

    def test_odds_cached_from_prematch_call(self):
        """Single fixtures call must populate odds cache so get_match_winner_odds makes 0 extra calls."""
        item = make_odds_item("e1", prices=(2.10, 3.40, 3.50))
        provider = FakeOddsApi([item])

        fixtures = run(provider.get_prematch_fixtures("2026-10-06", [39]))
        assert len(fixtures) == 1
        initial_calls_count = len(provider.calls)

        # Call get_match_winner_odds for e1
        odds = run(provider.get_match_winner_odds("e1", 1))
        assert odds is not None
        assert odds.home == 2.10
        assert odds.draw == 3.40
        assert odds.away == 3.50
        assert len(provider.calls) == initial_calls_count  # Zero new network calls!

    def test_network_failure_returns_none(self):
        provider = FakeOddsApi(boom=True)
        assert run(provider.get_prematch_fixtures("2026-10-06", [39])) is None

    def test_empty_matches_returns_empty_list(self):
        provider = FakeOddsApi([])
        assert run(provider.get_prematch_fixtures("2026-10-06", [39])) == []

    def test_upcoming_match_fixture_cached_from_prematch_call(self):
        """When get_prematch_fixtures runs, get_prematch_fixture returns upcoming fixture from cache without extra API calls."""
        # Future kickoff
        item = make_odds_item("e1", commence_time="2099-10-06T16:30:00Z")
        provider = FakeOddsApi([item])

        fixtures = run(provider.get_prematch_fixtures("2099-10-06", [39]))
        assert len(fixtures) == 1
        initial_calls_count = len(provider.calls)

        fx = run(provider.get_prematch_fixture("e1"))
        assert fx is not None
        assert fx.fixture_id == "e1"
        assert fx.home == "Arsenal"
        assert fx.away == "Chelsea"
        assert len(provider.calls) == initial_calls_count  # Zero new API calls!


class TestTheOddsApiScores:
    def test_finished_match_score_extraction(self):
        item = make_score_item("e1", completed=True, home_score=3, away_score=1)
        provider = FakeOddsApi([item])
        provider._fixture_sport_map["e1"] = "soccer_epl"

        fx = run(provider.get_prematch_fixture("e1"))
        assert fx is not None
        assert fx.status_short == "FT"
        assert fx.home_goals == 3
        assert fx.away_goals == 1

    def test_in_play_or_uncompleted_match(self):
        item = make_score_item("e1", completed=False, home_score=0, away_score=0)
        provider = FakeOddsApi([item])
        provider._fixture_sport_map["e1"] = "soccer_epl"

        fx = run(provider.get_prematch_fixture("e1"))
        assert fx is not None
        assert fx.status_short == "NS"
        assert fx.home_goals == 0
        assert fx.away_goals == 0


class TestTheOddsApiQuota:
    def test_quota_exhausted_flag(self):
        provider = FakeOddsApi([], headers={"x-requests-remaining": "0"})
        run(provider.get_prematch_fixtures("2026-10-06", [39]))
        assert provider.is_quota_exhausted() is True
        assert provider.is_connected is False


class TestTheOddsApiProviderFactory:
    def test_provider_factory_resolution(self, monkeypatch):
        from services.sports import get_sports_provider, set_sports_provider

        set_sports_provider(None)
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.delenv("ENV", raising=False)
        monkeypatch.setattr(config, "SPORTS_PROVIDER", "the_odds_api")
        monkeypatch.setattr(config, "ODDS_API_KEY", "test-key-123")

        p = get_sports_provider()
        assert isinstance(p, TheOddsApiProvider)
        assert p.api_key == "test-key-123"
        set_sports_provider(None)
