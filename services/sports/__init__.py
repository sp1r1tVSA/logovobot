"""
services/sports/__init__.py

Logovo.bet — Sports Data Services Package (Phase 8).
Unifies provider abstractions, concrete adapters, caching, rate limiting, and telemetry.
"""

from __future__ import annotations

import os
from typing import Optional

import config
from services.sports.adapters.api_sports import APISportsProvider
from services.sports.adapters.base import SportsDataProvider
from services.sports.adapters.mock_provider import MockSportsDataProvider
from services.sports.adapters.null_provider import NullSportsDataProvider
from services.sports.adapters.the_odds_api import TheOddsApiProvider
from services.sports.cache import ProviderCache
from services.sports.circuit import ProviderCircuitBreaker
from services.sports.freshness import (
    EXPIRED,
    FRESH,
    STALE,
    UNAVAILABLE,
    evaluate_match_freshness,
)
from services.sports.health import ProviderHealthMonitor, get_health_monitor
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
from services.sports.odds_sync import sync_provider_odds, validate_odd_value


_GLOBAL_PROVIDER: Optional[SportsDataProvider] = None


def get_sports_provider() -> SportsDataProvider:
    """
    Returns the active SportsDataProvider singleton.
    In testing environment (or if configured), returns MockSportsDataProvider.
    If provider is explicitly set to 'the_odds_api' or ODDS_API_KEY is present without SPORTS_API_KEY,
    initializes TheOddsApiProvider.
    If SPORTS_API_KEY is present, initializes APISportsProvider.
    Otherwise defaults to NullSportsDataProvider ("LIVE DATA UNAVAILABLE").
    """
    global _GLOBAL_PROVIDER
    if _GLOBAL_PROVIDER is not None:
        return _GLOBAL_PROVIDER

    if os.getenv("TESTING") == "1" or os.getenv("ENV") == "test":
        _GLOBAL_PROVIDER = MockSportsDataProvider()
        return _GLOBAL_PROVIDER

    provider_setting = getattr(config, "SPORTS_PROVIDER", "auto").strip().lower()
    if provider_setting == "null":
        _GLOBAL_PROVIDER = NullSportsDataProvider(reason="Configured to NULL provider.")
        return _GLOBAL_PROVIDER

    if provider_setting in ("odds_api", "the_odds_api", "theoddsapi"):
        odds_api_key = getattr(config, "ODDS_API_KEY", "").strip() or os.getenv("ODDS_API_KEY", "").strip()
        if odds_api_key:
            _GLOBAL_PROVIDER = TheOddsApiProvider(api_key=odds_api_key)
        else:
            _GLOBAL_PROVIDER = NullSportsDataProvider(reason="LIVE DATA UNAVAILABLE: ODDS_API_KEY not configured.")
        return _GLOBAL_PROVIDER

    api_keys = getattr(config, "SPORTS_API_KEYS", None)
    api_key = getattr(config, "SPORTS_API_KEY", "").strip() or getattr(config, "APISPORTS_KEY", "").strip()
    if api_keys or api_key:
        _GLOBAL_PROVIDER = APISportsProvider(api_key=api_key, api_keys=api_keys)
    else:
        odds_api_key = getattr(config, "ODDS_API_KEY", "").strip() or os.getenv("ODDS_API_KEY", "").strip()
        if odds_api_key:
            _GLOBAL_PROVIDER = TheOddsApiProvider(api_key=odds_api_key)
        else:
            _GLOBAL_PROVIDER = NullSportsDataProvider(reason="LIVE DATA UNAVAILABLE: No live provider configured.")

    return _GLOBAL_PROVIDER


def set_sports_provider(provider: Optional[SportsDataProvider]) -> None:
    """Override provider singleton (primarily for test harnesses)."""
    global _GLOBAL_PROVIDER
    _GLOBAL_PROVIDER = provider


get_sports_data_provider = get_sports_provider

__all__ = [
    "SportsDataProvider",
    "NullSportsDataProvider",
    "MockSportsDataProvider",
    "APISportsProvider",
    "TheOddsApiProvider",
    "ProviderTeam",
    "ProviderMatch",
    "ProviderEvent",
    "ProviderStatistics",
    "ProviderLineup",
    "ProviderInjury",
    "ProviderOdds",
    "LiveMatchState",
    "LiveEvent",
    "LiveStatistics",
    "ProviderRateLimiter",
    "ProviderCircuitBreaker",
    "ProviderCache",
    "ProviderHealthMonitor",
    "get_health_monitor",
    "evaluate_match_freshness",
    "FRESH",
    "STALE",
    "EXPIRED",
    "UNAVAILABLE",
    "get_sports_provider",
    "set_sports_provider",
    "get_sports_data_provider",
    "sync_provider_odds",
    "validate_odd_value",
]

