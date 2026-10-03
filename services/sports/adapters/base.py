"""
services/sports/adapters/base.py

Logovo.bet — Base Abstract Sports Data Provider Interface (Phase 8).
Establishes standardized contracts for live fixtures, event streams, statistics, lineups, injuries, and odds.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

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
    MatchWinnerOdds,
    PrematchFixture,
)


class SportsDataProvider(ABC):
    """Abstract interface for all sports data providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique provider identifier (e.g. 'null', 'mock', 'api_sports')."""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Indicates whether provider is actively connected and authenticated."""
        ...

    # ── Legacy & Live Match Lifecycle Contracts ──────────────────────────────

    @abstractmethod
    async def get_matches(self, division_id: Optional[int] = None, season_id: Optional[int] = None) -> list[LiveMatchState]:
        """Fetch matches optionally scoped by division and season."""
        ...

    @abstractmethod
    async def get_match(self, match_id: int) -> Optional[LiveMatchState]:
        """Fetch single match current state."""
        ...

    @abstractmethod
    async def get_live_matches(self) -> list[LiveMatchState]:
        """Fetch all currently active live matches."""
        ...

    @abstractmethod
    async def get_match_events(self, match_id: int) -> list[LiveEvent]:
        """Fetch real-time event log for a match."""
        ...

    @abstractmethod
    async def get_match_statistics(self, match_id: int) -> Optional[LiveStatistics]:
        """Fetch real-time statistics for a match. Unavailable metrics MUST be None."""
        ...

    @abstractmethod
    async def get_match_odds(self, match_id: int) -> list[dict[str, Any]]:
        """Fetch current live odds for a match from provider."""
        ...

    @abstractmethod
    def get_provider_status(self) -> dict[str, Any]:
        """Return provider availability, health, and latency stats."""
        ...

    def get_sync_status(self) -> dict[str, Any]:
        """Alias for get_provider_status."""
        return self.get_provider_status()

    # ── Phase 8 Provider-Neutral Contracts ───────────────────────────────────

    @abstractmethod
    async def get_fixtures(
        self,
        division_id: Optional[int] = None,
        season_id: Optional[int] = None,
        date: Optional[str] = None
    ) -> list[ProviderMatch]:
        """Fetch provider-neutral fixture list."""
        ...

    @abstractmethod
    async def get_fixture(self, match_id: int | str) -> Optional[ProviderMatch]:
        """Fetch single provider-neutral fixture."""
        ...

    @abstractmethod
    async def get_live_fixtures(self) -> list[ProviderMatch]:
        """Fetch live provider-neutral fixtures."""
        ...

    @abstractmethod
    async def get_events(self, match_id: int | str) -> list[ProviderEvent]:
        """Fetch normalized provider-neutral event stream."""
        ...

    @abstractmethod
    async def get_statistics(self, match_id: int | str) -> Optional[ProviderStatistics]:
        """Fetch normalized provider-neutral statistics."""
        ...

    @abstractmethod
    async def get_lineups(self, match_id: int | str) -> list[ProviderLineup]:
        """Fetch starting lineups, formations, and substitutes."""
        ...

    @abstractmethod
    async def get_injuries(self, match_id: int | str) -> list[ProviderInjury]:
        """Fetch confirmed player injuries / suspensions."""
        ...

    @abstractmethod
    async def get_odds(self, match_id: int | str) -> list[ProviderOdds]:
        """Fetch normalized live/pre-match bookmaker odds."""
        ...

    @abstractmethod
    async def get_standings(self, competition_id: int | str, season_id: int | str) -> list[dict[str, Any]]:
        """Fetch league standings table."""
        ...

    # ── IRL betting (optional: providers without pre-match data return None) ─

    async def get_prematch_fixtures(
        self, date: str, league_ids: Optional[list[int]] = None
    ) -> Optional[list[PrematchFixture]]:
        """Fixtures of one MSK day. None = provider unavailable; [] = a genuinely empty day."""
        return None

    async def get_prematch_fixture(self, fixture_id: int | str) -> Optional[PrematchFixture]:
        """One fixture with its status and main-time score. None = unavailable or unknown."""
        return None

    async def get_match_winner_odds(
        self, fixture_id: int | str, bookmaker_id: int
    ) -> Optional[MatchWinnerOdds]:
        """1X2 odds of one bookmaker. None = unavailable or incomplete."""
        return None
