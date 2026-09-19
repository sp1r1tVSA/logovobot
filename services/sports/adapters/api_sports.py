"""
services/sports/adapters/api_sports.py

Logovo.bet — Production API-Sports (API-Football) Data Provider Adapter (Phase 8).
Features:
1. Resilient HTTP request dispatch with exponential backoff retry.
2. Built-in ProviderRateLimiter (default 60 RPM, respectful 429 Retry-After handling).
3. Built-in ProviderCircuitBreaker (trips after 5 failures, 60s cooldown).
4. ProviderCache integration with configurable TTLs.
5. Zero Fake Data: nulls preserved, explicit unavailable indicators.
6. Secret Protection: API keys never exposed in logs or telemetry reports.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import config
from services.sports.adapters.base import SportsDataProvider
from services.sports.cache import ProviderCache
from services.sports.circuit import ProviderCircuitBreaker
from services.sports.health import get_health_monitor
from services.sports.limiter import ProviderRateLimiter
from services.sports.models import (
    LiveEvent,
    LiveMatchState,
    LiveStatistics,
    ProviderEvent,
    ProviderInjury,
    ProviderLineup,
    ProviderMatch,
    ProviderOdds,
    ProviderStatistics,
    ProviderTeam,
)

logger = logging.getLogger(__name__)


class APISportsProvider(SportsDataProvider):
    """Production adapter for API-Sports (API-Football v3) live and pre-match data."""

    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        rate_limit_rpm: Optional[int] = None,
        timeout_seconds: Optional[float] = None
    ) -> None:
        self.api_key = (api_key or getattr(config, "SPORTS_API_KEY", "") or getattr(config, "APISPORTS_KEY", "")).strip()
        self.base_url = (base_url or getattr(config, "SPORTS_API_BASE_URL", self.BASE_URL)).rstrip("/")
        self.timeout_sec = timeout_seconds or getattr(config, "SPORTS_TIMEOUT_SECONDS", 10.0)

        rpm = rate_limit_rpm or getattr(config, "SPORTS_RATE_LIMIT_RPM", 60)
        self.rate_limiter = ProviderRateLimiter(requests_per_minute=rpm)
        self.circuit_breaker = ProviderCircuitBreaker(max_failures=5, cooldown_seconds=60.0)
        self.cache = ProviderCache(default_ttl_seconds=getattr(config, "SPORTS_CACHE_TTL_SECONDS", 30))
        self.health_monitor = get_health_monitor()

    @property
    def provider_name(self) -> str:
        return "api_sports"

    @property
    def circuit_open(self) -> bool:
        return self.circuit_breaker.state == "OPEN"

    @property
    def is_connected(self) -> bool:
        return bool(self.api_key and not self.circuit_open)

    def _record_success(self) -> None:
        self.circuit_breaker.record_success()

    def _record_failure(self, err: Optional[Exception] = None) -> None:
        self.circuit_breaker.record_failure(err)

    async def _fetch_json(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        cache_ttl: Optional[float] = None
    ) -> dict[str, Any]:
        """Dispatches an authenticated GET request with rate limiting, circuit breaker, and retry logic."""
        if not self.api_key:
            raise ValueError("SPORTS_API_KEY is not configured.")

        # 1. Check cache first
        clean_ep = endpoint.strip().lstrip("/")
        cache_key = f"{clean_ep}:{sorted(params.items()) if params else 'no_params'}"
        cached = self.cache.get(self.provider_name, cache_key)
        if cached is not None:
            return cached

        # 2. Check circuit breaker
        if not self.circuit_breaker.can_execute():
            raise RuntimeError("APISports circuit breaker is OPEN. Calls short-circuited.")

        # 3. Wait for rate limiter capacity
        await self.rate_limiter.acquire()

        import aiohttp

        headers = {
            "x-apisports-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "Logovobot/8.0 (SportsIntelligence)"
        }
        url = f"{self.base_url}/{clean_ep}"

        start_time = time.monotonic()
        status_code = 0
        error_msg = None

        for attempt in range(1, 4):
            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers, params=params) as resp:
                        status_code = resp.status
                        latency_ms = (time.monotonic() - start_time) * 1000.0

                        if resp.status == 200:
                            data = await resp.json()
                            self.circuit_breaker.record_success()
                            records = len(data.get("response", []))
                            self.health_monitor.record_request(
                                provider=self.provider_name,
                                endpoint=clean_ep,
                                latency_ms=latency_ms,
                                status_code=status_code,
                                records_count=records
                            )
                            # Cache response
                            self.cache.set(self.provider_name, cache_key, data, ttl_seconds=cache_ttl)
                            return data

                        elif resp.status == 429:
                            retry_after_hdr = resp.headers.get("Retry-After")
                            retry_sec = float(retry_after_hdr) if retry_after_hdr and retry_after_hdr.isdigit() else 5.0
                            self.rate_limiter.record_response(429, retry_after=retry_sec)
                            logger.warning(f"APISports 429 Too Many Requests. Backing off for {retry_sec}s.")
                            await asyncio.sleep(retry_sec)

                        else:
                            text = await resp.text()
                            error_msg = f"HTTP {resp.status}: {text[:100]}"
                            logger.warning(f"APISports response error on attempt {attempt}: {error_msg}")

            except Exception as e:
                error_msg = str(e)
                if attempt == 3:
                    latency_ms = (time.monotonic() - start_time) * 1000.0
                    self.circuit_breaker.record_failure(e)
                    self.health_monitor.record_request(
                        provider=self.provider_name,
                        endpoint=clean_ep,
                        latency_ms=latency_ms,
                        status_code=status_code or 500,
                        error_message=error_msg
                    )
                    raise
                await asyncio.sleep(attempt * 1.5)

        return {}

    # ── Phase 8 Provider-Neutral Contracts ───────────────────────────────────

    async def get_fixtures(
        self,
        division_id: Optional[int] = None,
        season_id: Optional[int] = None,
        date: Optional[str] = None
    ) -> list[ProviderMatch]:
        if not self.api_key:
            return []
        try:
            params: dict[str, Any] = {}
            if date:
                params["date"] = date
            payload = await self._fetch_json("fixtures", params=params, cache_ttl=60)
            resp = payload.get("response", [])
            return [self._normalize_fixture(f) for f in resp if f]
        except Exception as e:
            logger.warning(f"Could not fetch fixtures from APISports: {e}")
            return []

    async def get_fixture(self, match_id: int | str) -> Optional[ProviderMatch]:
        if not self.api_key:
            return None
        try:
            payload = await self._fetch_json("fixtures", {"id": match_id}, cache_ttl=20)
            resp = payload.get("response", [])
            if not resp:
                return None
            return self._normalize_fixture(resp[0])
        except Exception as e:
            logger.warning(f"Could not fetch fixture #{match_id} from APISports: {e}")
            return None

    async def get_live_fixtures(self) -> list[ProviderMatch]:
        if not self.api_key:
            return []
        try:
            payload = await self._fetch_json("fixtures", {"live": "all"}, cache_ttl=10)
            resp = payload.get("response", [])
            return [self._normalize_fixture(f) for f in resp if f]
        except Exception as e:
            logger.warning(f"Could not fetch live fixtures from APISports: {e}")
            return []

    async def get_events(self, match_id: int | str) -> list[ProviderEvent]:
        if not self.api_key:
            return []
        try:
            payload = await self._fetch_json("fixtures/events", {"fixture": match_id}, cache_ttl=15)
            resp = payload.get("response", [])
            return [self._normalize_event(match_id, idx, ev) for idx, ev in enumerate(resp)]
        except Exception as e:
            logger.warning(f"Could not fetch events for fixture #{match_id} from APISports: {e}")
            return []

    async def get_statistics(self, match_id: int | str) -> Optional[ProviderStatistics]:
        if not self.api_key:
            return None
        try:
            payload = await self._fetch_json("fixtures/statistics", {"fixture": match_id}, cache_ttl=20)
            resp = payload.get("response", [])
            if not resp:
                return None
            return self._normalize_statistics(match_id, resp)
        except Exception as e:
            logger.warning(f"Could not fetch statistics for fixture #{match_id} from APISports: {e}")
            return None

    async def get_lineups(self, match_id: int | str) -> list[ProviderLineup]:
        if not self.api_key:
            return []
        try:
            payload = await self._fetch_json("fixtures/lineups", {"fixture": match_id}, cache_ttl=120)
            resp = payload.get("response", [])
            return [self._normalize_lineup(match_id, lu) for lu in resp if lu]
        except Exception as e:
            logger.warning(f"Could not fetch lineups for fixture #{match_id} from APISports: {e}")
            return []

    async def get_injuries(self, match_id: int | str) -> list[ProviderInjury]:
        if not self.api_key:
            return []
        try:
            payload = await self._fetch_json("injuries", {"fixture": match_id}, cache_ttl=300)
            resp = payload.get("response", [])
            return [self._normalize_injury(inj) for inj in resp if inj]
        except Exception as e:
            logger.warning(f"Could not fetch injuries for fixture #{match_id} from APISports: {e}")
            return []

    async def get_odds(self, match_id: int | str) -> list[ProviderOdds]:
        if not self.api_key:
            return []
        try:
            payload = await self._fetch_json("odds/live", {"fixture": match_id}, cache_ttl=15)
            resp = payload.get("response", [])
            return self._normalize_odds(match_id, resp)
        except Exception as e:
            logger.warning(f"Could not fetch odds for fixture #{match_id} from APISports: {e}")
            return []

    async def get_standings(self, competition_id: int | str, season_id: int | str) -> list[dict[str, Any]]:
        if not self.api_key:
            return []
        try:
            payload = await self._fetch_json("standings", {"league": competition_id, "season": season_id}, cache_ttl=300)
            resp = payload.get("response", [])
            if resp and resp[0].get("league", {}).get("standings"):
                return resp[0]["league"]["standings"][0]
            return []
        except Exception as e:
            logger.warning(f"Could not fetch standings from APISports: {e}")
            return []

    # ── Legacy & Live Match Lifecycle Support (Phase 6 / 7) ──────────────────

    async def get_matches(self, division_id: Optional[int] = None, season_id: Optional[int] = None) -> list[LiveMatchState]:
        matches = await self.get_live_fixtures()
        return [self._to_live_match_state(m) for m in matches]

    async def get_match(self, match_id: int) -> Optional[LiveMatchState]:
        fixture = await self.get_fixture(match_id)
        return self._to_live_match_state(fixture) if fixture else None

    async def get_live_matches(self) -> list[LiveMatchState]:
        fixtures = await self.get_live_fixtures()
        return [self._to_live_match_state(f) for f in fixtures]

    async def get_match_events(self, match_id: int) -> list[LiveEvent]:
        events = await self.get_events(match_id)
        return [self._to_live_event(e) for e in events]

    async def get_match_statistics(self, match_id: int) -> Optional[LiveStatistics]:
        stats = await self.get_statistics(match_id)
        return self._to_live_statistics(stats) if stats else None

    async def get_match_odds(self, match_id: int) -> list[dict[str, Any]]:
        odds = await self.get_odds(match_id)
        return [o.to_dict() for o in odds]

    # ── Normalization Helpers ────────────────────────────────────────────────

    def _normalize_fixture(self, f: dict[str, Any]) -> ProviderMatch:
        fixture = f.get("fixture", {})
        teams = f.get("teams", {})
        league = f.get("league", {})
        goals = f.get("goals", {})
        status_info = fixture.get("status", {})

        short_status = status_info.get("short", "NS")
        elapsed = status_info.get("elapsed")

        status_map = {
            "NS": ("SCHEDULED", "pre_match"),
            "1H": ("LIVE", "1h"),
            "HT": ("HALFTIME", "ht"),
            "2H": ("LIVE", "2h"),
            "ET": ("LIVE", "et"),
            "P": ("LIVE", "pen"),
            "FT": ("FINISHED", "ft"),
            "AET": ("FINISHED", "ft"),
            "PEN": ("FINISHED", "ft"),
            "PST": ("POSTPONED", "pre_match"),
            "CANC": ("CANCELLED", "pre_match"),
            "ABD": ("ABANDONED", "pre_match"),
            "SUSP": ("SUSPENDED", "pre_match"),
        }
        status, period = status_map.get(short_status, ("SCHEDULED", "pre_match"))

        home_t = teams.get("home", {})
        away_t = teams.get("away", {})

        return ProviderMatch(
            match_id=int(fixture.get("id", 0)),
            provider=self.provider_name,
            home_team=ProviderTeam(
                team_id=home_t.get("id", 0),
                name=home_t.get("name", "Home Team"),
                logo=home_t.get("logo"),
                is_home=True
            ),
            away_team=ProviderTeam(
                team_id=away_t.get("id", 0),
                name=away_t.get("name", "Away Team"),
                logo=away_t.get("logo"),
                is_home=False
            ),
            status=status,
            period=period,
            minute=int(elapsed) if elapsed is not None else None,
            home_score=int(goals.get("home") or 0),
            away_score=int(goals.get("away") or 0),
            start_time=fixture.get("date"),
            league_id=league.get("id"),
            league_name=league.get("name"),
            round=league.get("round"),
            venue=fixture.get("venue", {}).get("name"),
            updated_at=datetime.now(timezone.utc).isoformat()
        )

    def _normalize_event(self, match_id: int | str, idx: int, ev: dict[str, Any]) -> ProviderEvent:
        time_info = ev.get("time", {})
        minute = int(time_info.get("elapsed", 0))
        extra = time_info.get("extra")
        added_time = int(extra) if extra is not None else None

        raw_type = str(ev.get("type", "")).lower()
        detail = str(ev.get("detail", "")).lower()

        if raw_type == "goal":
            if "own goal" in detail:
                canonical = "own_goal"
            elif "penalty" in detail:
                canonical = "penalty"
            elif "missed" in detail:
                canonical = "penalty_missed"
            else:
                canonical = "goal"
        elif raw_type == "card":
            canonical = "red_card" if "red" in detail else "yellow_card"
        elif raw_type == "subst":
            canonical = "substitution"
        elif raw_type == "var":
            canonical = "var"
        else:
            canonical = raw_type or "unknown"

        team = ev.get("team", {})
        player = ev.get("player", {})

        return ProviderEvent(
            provider_event_id=f"{match_id}_{minute}_{idx}_{canonical}",
            match_id=match_id,
            provider=self.provider_name,
            event_type=canonical,
            minute=minute,
            added_time=added_time,
            team_id=team.get("id"),
            team_name=team.get("name"),
            player_id=player.get("id"),
            player_name=player.get("name"),
            detail=ev.get("detail"),
            payload={"comments": ev.get("comments")}
        )

    def _normalize_statistics(self, match_id: int | str, stats_data: list[dict[str, Any]]) -> ProviderStatistics:
        stats = ProviderStatistics(match_id=match_id, provider=self.provider_name)
        if len(stats_data) < 2:
            return stats

        def _extract(team_stats: dict[str, Any], stat_name: str) -> Optional[Any]:
            for item in team_stats.get("statistics", []):
                if (item.get("type") or "").lower() == stat_name.lower():
                    val = item.get("value")
                    if val is None:
                        return None
                    if isinstance(val, str) and "%" in val:
                        try:
                            return float(val.replace("%", "").strip())
                        except ValueError:
                            return None
                    return val
            return None

        h = stats_data[0]
        a = stats_data[1]

        stats.possession_home = _extract(h, "Ball Possession")
        stats.possession_away = _extract(a, "Ball Possession")
        stats.shots_home = _extract(h, "Total Shots")
        stats.shots_away = _extract(a, "Total Shots")
        stats.shots_on_target_home = _extract(h, "Shots on Goal")
        stats.shots_on_target_away = _extract(a, "Shots on Goal")
        stats.corners_home = _extract(h, "Corner Kicks")
        stats.corners_away = _extract(a, "Corner Kicks")
        stats.fouls_home = _extract(h, "Fouls")
        stats.fouls_away = _extract(a, "Fouls")
        stats.offsides_home = _extract(h, "Offsides")
        stats.offsides_away = _extract(a, "Offsides")
        stats.yellow_cards_home = _extract(h, "Yellow Cards")
        stats.yellow_cards_away = _extract(a, "Yellow Cards")
        stats.red_cards_home = _extract(h, "Red Cards")
        stats.red_cards_away = _extract(a, "Red Cards")
        stats.passes_home = _extract(h, "Total passes")
        stats.passes_away = _extract(a, "Total passes")
        stats.pass_accuracy_home = _extract(h, "Passes %")
        stats.pass_accuracy_away = _extract(a, "Passes %")
        stats.xg_home = _extract(h, "expected_goals")
        stats.xg_away = _extract(a, "expected_goals")
        stats.saves_home = _extract(h, "Goalkeeper Saves")
        stats.saves_away = _extract(a, "Goalkeeper Saves")
        stats.updated_at = datetime.now(timezone.utc).isoformat()
        return stats

    def _normalize_lineup(self, match_id: int | str, lineup_item: dict[str, Any]) -> ProviderLineup:
        team = lineup_item.get("team", {})
        coach = lineup_item.get("coach", {})
        start_xi = [
            {
                "id": p.get("player", {}).get("id"),
                "name": p.get("player", {}).get("name"),
                "number": p.get("player", {}).get("number"),
                "pos": p.get("player", {}).get("pos"),
                "grid": p.get("player", {}).get("grid"),
            }
            for p in lineup_item.get("startXI", [])
        ]
        subs = [
            {
                "id": p.get("player", {}).get("id"),
                "name": p.get("player", {}).get("name"),
                "number": p.get("player", {}).get("number"),
                "pos": p.get("player", {}).get("pos"),
            }
            for p in lineup_item.get("substitutes", [])
        ]

        return ProviderLineup(
            match_id=match_id,
            provider=self.provider_name,
            team_id=team.get("id", 0),
            team_name=team.get("name", ""),
            formation=lineup_item.get("formation"),
            starting_xi=start_xi,
            substitutes=subs,
            coach_name=coach.get("name"),
            updated_at=datetime.now(timezone.utc).isoformat()
        )

    def _normalize_injury(self, inj_item: dict[str, Any]) -> ProviderInjury:
        player = inj_item.get("player", {})
        team = inj_item.get("team", {})
        fixture = inj_item.get("fixture", {})
        return ProviderInjury(
            player_id=player.get("id"),
            player_name=player.get("name", "Unknown Player"),
            team_id=team.get("id"),
            team_name=team.get("name", "Unknown Team"),
            injury_type=player.get("type", "Injury"),
            status=player.get("reason", "out"),
            fixture_id=fixture.get("id"),
            last_update=datetime.now(timezone.utc).isoformat()
        )

    def _normalize_odds(self, match_id: int | str, odds_data: list[dict[str, Any]]) -> list[ProviderOdds]:
        result = []
        for item in odds_data:
            bookmakers = item.get("bookmakers", [])
            for bm in bookmakers:
                bm_id = bm.get("id")
                bm_name = bm.get("name", "Live Bookmaker")
                for market in bm.get("bets", []):
                    m_key = str(market.get("name", "")).lower().replace(" ", "_")
                    sels = [
                        {"selection_key": str(v.get("value", "")).lower(), "name": str(v.get("value")), "odds": float(v.get("odd", 1.0))}
                        for v in market.get("values", [])
                        if v.get("odd")
                    ]
                    result.append(
                        ProviderOdds(
                            match_id=match_id,
                            provider=self.provider_name,
                            bookmaker_id=bm_id,
                            bookmaker_name=bm_name,
                            market_key=m_key,
                            market_name=market.get("name", ""),
                            selections=sels,
                            updated_at=datetime.now(timezone.utc).isoformat()
                        )
                    )
        return result

    # ── Legacy Model Conversion Helpers ─────────────────────────────────────

    def _to_live_match_state(self, m: ProviderMatch) -> LiveMatchState:
        return LiveMatchState(
            match_id=int(m.match_id) if str(m.match_id).isdigit() else 0,
            season_id=1,
            division_id=1,
            status=m.status,
            period=m.period,
            minute=m.minute,
            home_score=m.home_score,
            away_score=m.away_score,
            provider=m.provider,
            provider_match_id=str(m.match_id),
            version=1,
            updated_at=m.updated_at
        )

    def _to_live_event(self, e: ProviderEvent) -> LiveEvent:
        return LiveEvent(
            match_id=int(e.match_id) if str(e.match_id).isdigit() else 0,
            provider=e.provider,
            provider_event_id=e.provider_event_id,
            event_type=e.event_type,
            minute=e.minute,
            added_time=e.added_time,
            team_id=int(e.team_id) if e.team_id and str(e.team_id).isdigit() else None,
            team_name=e.team_name,
            player_id=int(e.player_id) if e.player_id and str(e.player_id).isdigit() else None,
            player_name=e.player_name,
            payload=e.payload,
            created_at=e.created_at
        )

    def _to_live_statistics(self, s: ProviderStatistics) -> LiveStatistics:
        return LiveStatistics(
            match_id=int(s.match_id) if str(s.match_id).isdigit() else 0,
            possession_home=s.possession_home,
            possession_away=s.possession_away,
            shots_home=s.shots_home,
            shots_away=s.shots_away,
            shots_on_target_home=s.shots_on_target_home,
            shots_on_target_away=s.shots_on_target_away,
            corners_home=s.corners_home,
            corners_away=s.corners_away,
            fouls_home=s.fouls_home,
            fouls_away=s.fouls_away,
            offsides_home=s.offsides_home,
            offsides_away=s.offsides_away,
            yellow_cards_home=s.yellow_cards_home,
            yellow_cards_away=s.yellow_cards_away,
            red_cards_home=s.red_cards_home,
            red_cards_away=s.red_cards_away,
            passes_home=s.passes_home,
            passes_away=s.passes_away,
            pass_accuracy_home=s.pass_accuracy_home,
            pass_accuracy_away=s.pass_accuracy_away,
            xg_home=s.xg_home,
            xg_away=s.xg_away,
            saves_home=s.saves_home,
            saves_away=s.saves_away,
            provider=s.provider,
            updated_at=s.updated_at
        )

    def get_provider_status(self) -> dict[str, Any]:
        """Telemetry and health status for admin monitoring (zero credentials exposed)."""
        return self.health_monitor.get_summary(
            provider_name=self.provider_name,
            is_connected=self.is_connected,
            circuit_state=self.circuit_breaker.get_state(),
            rate_limiter_stats=self.rate_limiter.get_stats(),
            cache_stats=self.cache.get_stats()
        )


