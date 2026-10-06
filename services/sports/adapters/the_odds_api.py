"""services/sports/adapters/the_odds_api.py

Logovo.bet — The Odds API Adapter (the-odds-api.com).
Implements pre-match fixtures, 1X2 market odds, and score settlements for real-world (IRL) betting.

Key features:
1. Low-quota optimization: One call to /sports/{sport}/odds retrieves all fixtures and odds
   for the entire league, caching odds so subsequent odds requests use 0 network calls.
2. Direct 1X2 market parsing from premier bookmakers (Pinnacle, Bet365, 1xBet, etc.).
3. Score extraction from /sports/{sport}/scores to determine completed status and final score.
4. Safe rate limiting, circuit breaker, and quota tracking via response headers.
5. Strict adherence to project invariants: zero hallucinations, MSK time compliance.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any, Optional

import config
from time_utils import now_msk, parse_msk
from services.sports.adapters.base import SportsDataProvider
from services.sports.cache import ProviderCache
from services.sports.circuit import ProviderCircuitBreaker
from services.sports.health import get_health_monitor
from services.sports.limiter import ProviderRateLimiter
from services.sports.models import (
    LiveEvent,
    LiveMatchState,
    LiveStatistics,
    MatchWinnerOdds,
    PrematchFixture,
    ProviderEvent,
    ProviderInjury,
    ProviderLineup,
    ProviderMatch,
    ProviderOdds,
    ProviderStatistics,
)

logger = logging.getLogger(__name__)

# Mapping from API-Sports league IDs (used in IRL_COMPETITION_PRIORITY) to The Odds API sport keys
DEFAULT_API_SPORTS_TO_ODDS_SPORT: dict[int, str] = {
    1: "soccer_fifa_world_cup",
    2: "soccer_uefa_champs_league",
    3: "soccer_uefa_europa_league",
    4: "soccer_uefa_european_championship",
    5: "soccer_uefa_nations_league",
    9: "soccer_conmebol_copa_america",
    32: "soccer_fifa_world_cup_qual_europe",
    39: "soccer_epl",
    61: "soccer_france_ligue_one",
    78: "soccer_germany_bundesliga",
    135: "soccer_italy_serie_a",
    140: "soccer_spain_la_liga",
}

DEFAULT_ODDS_SPORT_TO_API_SPORTS: dict[str, int] = {
    v: k for k, v in DEFAULT_API_SPORTS_TO_ODDS_SPORT.items()
}

SPORT_TITLES: dict[str, str] = {
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_uefa_champs_league": "UEFA Champions League",
    "soccer_uefa_europa_league": "UEFA Europa League",
    "soccer_uefa_nations_league": "UEFA Nations League",
    "soccer_fifa_world_cup": "FIFA World Cup",
    "soccer_uefa_european_championship": "Euro",
    "soccer_conmebol_copa_america": "Copa America",
}


class TheOddsApiProvider(SportsDataProvider):
    """Adapter for The Odds API (https://the-odds-api.com)."""

    BASE_URL = "https://api.the-odds-api.com/v4"
    _ODD_MAX = 500.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        rate_limit_rpm: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        bookmaker: Optional[str] = None,
        regions: Optional[str] = None,
    ) -> None:
        self.api_key = (api_key or getattr(config, "ODDS_API_KEY", "")).strip()
        self.base_url = (base_url or getattr(config, "ODDS_API_BASE_URL", self.BASE_URL)).rstrip("/")
        self.timeout_sec = timeout_seconds or getattr(config, "ODDS_API_TIMEOUT_SECONDS", 10.0)
        self.preferred_bookmaker = (
            bookmaker or getattr(config, "ODDS_API_BOOKMAKER", "pinnacle")
        ).strip().lower()
        self.regions = (regions or getattr(config, "ODDS_API_REGIONS", "eu")).strip().lower()

        rpm = rate_limit_rpm or getattr(config, "ODDS_API_RATE_LIMIT_RPM", 30)
        self.rate_limiter = ProviderRateLimiter(requests_per_minute=rpm)
        self.circuit_breaker = ProviderCircuitBreaker(max_failures=5, cooldown_seconds=60.0)
        self.cache = ProviderCache(default_ttl_seconds=getattr(config, "ODDS_API_CACHE_TTL_SECONDS", 300))
        self.health_monitor = get_health_monitor()

        self._requests_remaining: Optional[int] = None
        self._requests_used: Optional[int] = None
        self._quota_exhausted: bool = False
        self._quota_error_msg: Optional[str] = None

        # In-memory mapping of fixture_id -> MatchWinnerOdds & fixture_id -> sport_key
        self._odds_cache: dict[str, MatchWinnerOdds] = {}
        self._fixture_sport_map: dict[str, str] = {}

    @property
    def provider_name(self) -> str:
        return "the_odds_api"

    @property
    def circuit_open(self) -> bool:
        return self.circuit_breaker.state == "OPEN"

    @property
    def is_connected(self) -> bool:
        return bool(self.api_key and not self.circuit_open and not self._quota_exhausted)

    def is_quota_exhausted(self) -> bool:
        return self._quota_exhausted

    # ── HTTP transport ────────────────────────────────────────────────────────

    async def _fetch_json(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        cache_ttl: Optional[float] = None,
    ) -> Optional[Any]:
        """Dispatches an authenticated GET request with rate limiting and circuit breaker."""
        if not self.api_key:
            logger.warning("TheOddsApi call skipped: ODDS_API_KEY is not configured.")
            return None

        if self._quota_exhausted:
            logger.warning("TheOddsApi call skipped: quota exhausted.")
            return None

        clean_ep = endpoint.strip().lstrip("/")
        cache_key = f"{clean_ep}:{sorted(params.items()) if params else 'no_params'}"
        cached = self.cache.get(self.provider_name, cache_key)
        if cached is not None:
            return cached

        if not self.circuit_breaker.can_execute():
            raise RuntimeError("TheOddsApi circuit breaker is OPEN. Calls short-circuited.")

        await self.rate_limiter.acquire()

        import aiohttp

        query_params = dict(params or {})
        query_params["apiKey"] = self.api_key

        url = f"{self.base_url}/{clean_ep}"
        start_time = time.monotonic()
        status_code = 0

        headers = {
            "Accept": "application/json",
            "User-Agent": "Logovobot/8.0 (TheOddsApiAdapter)",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=query_params) as resp:
                    status_code = resp.status
                    latency_ms = (time.monotonic() - start_time) * 1000.0

                    # Parse remaining quota headers
                    rem = resp.headers.get("x-requests-remaining")
                    if rem is not None and rem.isdigit():
                        self._requests_remaining = int(rem)
                        if self._requests_remaining <= 0:
                            self._quota_exhausted = True
                            self._quota_error_msg = "Monthly request limit reached (0 remaining)."
                            logger.warning("TheOddsApi monthly quota exhausted (0 remaining).")

                    used = resp.headers.get("x-requests-used")
                    if used is not None and used.isdigit():
                        self._requests_used = int(used)

                    if status_code == 401:
                        self.circuit_breaker.record_failure()
                        logger.error("TheOddsApi authentication failed (invalid apiKey).")
                        return None

                    if status_code == 429:
                        self._quota_exhausted = True
                        self._quota_error_msg = "Rate limit / Quota 429 received from The Odds API."
                        self.circuit_breaker.record_failure()
                        logger.warning("TheOddsApi 429 quota exhausted.")
                        return None

                    if status_code != 200:
                        self.circuit_breaker.record_failure()
                        text = await resp.text()
                        logger.warning("TheOddsApi HTTP %s from %s: %s", status_code, clean_ep, text[:200])
                        return None

                    data = await resp.json()
                    self.circuit_breaker.record_success()
                    self.health_monitor.record_call(
                        provider=self.provider_name,
                        endpoint=clean_ep,
                        success=True,
                        status_code=status_code,
                        latency_ms=latency_ms,
                    )
                    self.cache.set(self.provider_name, cache_key, data, ttl_seconds=cache_ttl)
                    return data
        except Exception as e:
            self.circuit_breaker.record_failure(e)
            latency_ms = (time.monotonic() - start_time) * 1000.0
            self.health_monitor.record_call(
                provider=self.provider_name,
                endpoint=clean_ep,
                success=False,
                status_code=status_code or 500,
                latency_ms=latency_ms,
                error_msg=str(e),
            )
            logger.warning("TheOddsApi request %s failed: %s", clean_ep, e)
            return None

    # ── IRL Pre-Match Contracts ───────────────────────────────────────────────

    async def get_prematch_fixtures(
        self, date: str, league_ids: Optional[list[int | str]] = None
    ) -> Optional[list[PrematchFixture]]:
        """Fetch all fixtures of one MSK day for the given leagues/sports.

        Returns None if provider is unreachable or failed; returns [] if genuinely empty.
        Also parses and caches match winner odds to avoid follow-up requests.
        """
        sports_to_query: list[tuple[int | str, str]] = []
        if league_ids:
            for lid in league_ids:
                if isinstance(lid, int) and lid in DEFAULT_API_SPORTS_TO_ODDS_SPORT:
                    sports_to_query.append((lid, DEFAULT_API_SPORTS_TO_ODDS_SPORT[lid]))
                elif isinstance(lid, str):
                    clean_sport = lid.strip()
                    mapped_int = DEFAULT_ODDS_SPORT_TO_API_SPORTS.get(clean_sport, 0)
                    sports_to_query.append((mapped_int or clean_sport, clean_sport))
        else:
            for lid, sk in DEFAULT_API_SPORTS_TO_ODDS_SPORT.items():
                sports_to_query.append((lid, sk))

        results: list[PrematchFixture] = []
        any_success = False

        for original_lid, sport_key in sports_to_query:
            endpoint = f"sports/{sport_key}/odds"
            params = {
                "regions": self.regions,
                "markets": "h2h",
                "dateFormat": "iso",
                "oddsFormat": "decimal",
            }
            try:
                data = await self._fetch_json(endpoint, params=params, cache_ttl=300)
            except Exception as e:
                logger.warning("TheOddsApi %s failed: %s", endpoint, e)
                data = None

            if data is None:
                continue

            any_success = True
            if not isinstance(data, list):
                continue

            for item in data:
                fx = self._normalize_fixture(item, original_lid, sport_key)
                if fx is None:
                    continue

                # Filter by MSK day
                if fx.kickoff.date().isoformat() != date:
                    continue

                # Parse and cache odds for this fixture
                odds = self._extract_match_winner_odds(item, fx.fixture_id)
                if odds is not None:
                    self._odds_cache[str(fx.fixture_id)] = odds

                self._fixture_sport_map[str(fx.fixture_id)] = sport_key
                results.append(fx)

        if not any_success and sports_to_query:
            # All attempted queries failed (e.g. network down or bad key)
            return None

        return results

    async def get_prematch_fixture(self, fixture_id: int | str) -> Optional[PrematchFixture]:
        """Fetch a single fixture with final/current main-time score."""
        str_fid = str(fixture_id)
        sport_key = self._fixture_sport_map.get(str_fid)

        # If sport_key is unknown, check priority sports
        sports_to_check = [sport_key] if sport_key else list(DEFAULT_ODDS_SPORT_TO_API_SPORTS.keys())

        for sk in sports_to_check:
            if not sk:
                continue
            endpoint = f"sports/{sk}/scores"
            params = {
                "daysFrom": "2",
                "dateFormat": "iso",
            }
            try:
                data = await self._fetch_json(endpoint, params=params, cache_ttl=60)
            except Exception as e:
                logger.warning("TheOddsApi %s failed: %s", endpoint, e)
                data = None

            if not isinstance(data, list):
                continue

            for item in data:
                if str(item.get("id")) == str_fid:
                    lid = DEFAULT_ODDS_SPORT_TO_API_SPORTS.get(sk, 0)
                    self._fixture_sport_map[str_fid] = sk
                    return self._normalize_fixture(item, lid, sk)

        return None

    async def get_match_winner_odds(
        self, fixture_id: int | str, bookmaker_id: int | str
    ) -> Optional[MatchWinnerOdds]:
        """Pre-match 1X2 odds of preferred bookmaker. Complete or None."""
        str_fid = str(fixture_id)
        if str_fid in self._odds_cache:
            return self._odds_cache[str_fid]

        # If not cached, attempt single event query or sport query
        sport_key = self._fixture_sport_map.get(str_fid)
        if not sport_key:
            return None

        endpoint = f"sports/{sport_key}/events/{str_fid}/odds"
        params = {
            "regions": self.regions,
            "markets": "h2h",
            "dateFormat": "iso",
            "oddsFormat": "decimal",
        }
        try:
            data = await self._fetch_json(endpoint, params=params, cache_ttl=60)
        except Exception as e:
            logger.warning("TheOddsApi %s failed: %s", endpoint, e)
            data = None

        if isinstance(data, dict):
            odds = self._extract_match_winner_odds(data, fixture_id)
            if odds is not None:
                self._odds_cache[str_fid] = odds
                return odds

        return None

    async def get_standings(self, competition_id: int | str, season_id: int | str) -> list[dict[str, Any]]:
        """The Odds API does not host league standings. Returns empty list gracefully."""
        return []

    # ── Parsing helpers ───────────────────────────────────────────────────────

    def _normalize_fixture(
        self, item: dict[str, Any], league_id: int | str, sport_key: str
    ) -> Optional[PrematchFixture]:
        if not isinstance(item, dict):
            return None

        fid = item.get("id")
        home = item.get("home_team")
        away = item.get("away_team")
        commence = item.get("commence_time")

        if not fid or not home or not away or not commence:
            return None

        kickoff = parse_msk(commence)
        if kickoff is None:
            return None

        league_title = SPORT_TITLES.get(sport_key) or item.get("sport_title") or sport_key

        # Score parsing (from /scores endpoint)
        completed = bool(item.get("completed", False))
        status_short = "FT" if completed else "NS"
        home_goals: Optional[int] = None
        away_goals: Optional[int] = None

        raw_scores = item.get("scores")
        if isinstance(raw_scores, list) and raw_scores:
            for s in raw_scores:
                name = s.get("name")
                val = s.get("score")
                try:
                    score_int = int(val) if val is not None else None
                except (ValueError, TypeError):
                    score_int = None

                if name == home:
                    home_goals = score_int
                elif name == away:
                    away_goals = score_int

        return PrematchFixture(
            fixture_id=str(fid),
            league_id=league_id,
            league_name=str(league_title),
            home=str(home).strip(),
            away=str(away).strip(),
            kickoff=kickoff,
            status_short=status_short,
            home_goals=home_goals,
            away_goals=away_goals,
        )

    def _extract_match_winner_odds(
        self, item: dict[str, Any], fixture_id: int | str
    ) -> Optional[MatchWinnerOdds]:
        """Finds 1X2 market prices for preferred bookmaker, falling back to any complete bookmaker."""
        bookmakers = item.get("bookmakers")
        if not isinstance(bookmakers, list) or not bookmakers:
            return None

        home_team = item.get("home_team")
        away_team = item.get("away_team")

        # 1. Try preferred bookmaker
        for bm in bookmakers:
            key = str(bm.get("key", "")).lower()
            if key == self.preferred_bookmaker:
                odds = self._parse_bookmaker_h2h(bm, fixture_id, home_team, away_team)
                if odds is not None:
                    return odds

        # 2. Try common major bookmakers
        common_majors = ("pinnacle", "bet365", "1xbet", "williamhill", "unibet", "bovada")
        for preferred in common_majors:
            for bm in bookmakers:
                key = str(bm.get("key", "")).lower()
                if key == preferred:
                    odds = self._parse_bookmaker_h2h(bm, fixture_id, home_team, away_team)
                    if odds is not None:
                        return odds

        # 3. Fallback to any valid bookmaker
        for bm in bookmakers:
            odds = self._parse_bookmaker_h2h(bm, fixture_id, home_team, away_team)
            if odds is not None:
                return odds

        return None

    def _parse_bookmaker_h2h(
        self, bm: dict[str, Any], fixture_id: int | str, home_team: str, away_team: str
    ) -> Optional[MatchWinnerOdds]:
        markets = bm.get("markets")
        if not isinstance(markets, list):
            return None

        bm_key = str(bm.get("key", "unknown"))

        for m in markets:
            if m.get("key") != "h2h":
                continue
            outcomes = m.get("outcomes")
            if not isinstance(outcomes, list) or len(outcomes) < 3:
                continue

            home_odd: Optional[float] = None
            draw_odd: Optional[float] = None
            away_odd: Optional[float] = None

            for oc in outcomes:
                name = oc.get("name")
                price = self._parse_odd(oc.get("price"))
                if price is None:
                    continue

                if name == home_team:
                    home_odd = price
                elif name == away_team:
                    away_odd = price
                elif str(name).strip().lower() == "draw":
                    draw_odd = price

            if home_odd is not None and draw_odd is not None and away_odd is not None:
                update_str = m.get("last_update") or bm.get("last_update")
                updated_at = parse_msk(update_str) if update_str else now_msk()
                return MatchWinnerOdds(
                    fixture_id=str(fixture_id),
                    bookmaker_id=bm_key,
                    home=home_odd,
                    draw=draw_odd,
                    away=away_odd,
                    updated_at=updated_at,
                )

        return None

    @classmethod
    def _parse_odd(cls, value: Any) -> Optional[float]:
        try:
            odd = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(odd) or odd <= 1.0 or odd > cls._ODD_MAX:
            return None
        return round(odd, 2)

    # ── Default / Legacy Inactive Contracts ──────────────────────────────────

    def get_provider_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "connected": self.is_connected,
            "quota_exhausted": self._quota_exhausted,
            "requests_remaining": self._requests_remaining,
            "requests_used": self._requests_used,
            "circuit_state": self.circuit_breaker.state,
        }

    async def get_matches(self, division_id: Optional[int] = None, season_id: Optional[int] = None) -> list[LiveMatchState]:
        return []

    async def get_match(self, match_id: int) -> Optional[LiveMatchState]:
        return None

    async def get_live_matches(self) -> list[LiveMatchState]:
        return []

    async def get_match_events(self, match_id: int) -> list[LiveEvent]:
        return []

    async def get_match_statistics(self, match_id: int) -> Optional[LiveStatistics]:
        return None

    async def get_match_odds(self, match_id: int) -> list[dict[str, Any]]:
        return []

    async def get_fixtures(self, division_id: Optional[int] = None, season_id: Optional[int] = None, date: Optional[str] = None) -> list[ProviderMatch]:
        return []

    async def get_fixture(self, match_id: int | str) -> Optional[ProviderMatch]:
        return None

    async def get_live_fixtures(self) -> list[ProviderMatch]:
        return []

    async def get_events(self, match_id: int | str) -> list[ProviderEvent]:
        return []

    async def get_statistics(self, match_id: int | str) -> Optional[ProviderStatistics]:
        return None

    async def get_lineups(self, match_id: int | str) -> list[ProviderLineup]:
        return []

    async def get_injuries(self, match_id: int | str) -> list[ProviderInjury]:
        return []

    async def get_odds(self, match_id: int | str) -> list[ProviderOdds]:
        return []
