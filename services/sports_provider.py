"""
services/sports_provider.py

Logovo.bet — Sports Data Provider Facade (Phase 8 Backward Compatibility Module).
Re-exports canonical interfaces, models, adapters, and factories from services.sports.
"""

from __future__ import annotations

from services.sports import (
    EXPIRED,
    FRESH,
    STALE,
    UNAVAILABLE,
    APISportsProvider,
    LiveEvent,
    LiveMatchState,
    LiveStatistics,
    MockSportsDataProvider,
    NullSportsDataProvider,
    TheOddsApiProvider,
    ProviderCache,
    ProviderCircuitBreaker,
    ProviderEvent,
    ProviderHealthMonitor,
    ProviderInjury,
    ProviderLineup,
    ProviderMatch,
    ProviderOdds,
    ProviderRateLimiter,
    ProviderStatistics,
    ProviderTeam,
    SportsDataProvider,
    evaluate_match_freshness,
    get_health_monitor,
    get_sports_data_provider,
    get_sports_provider,
    set_sports_provider,
    sync_provider_odds,
    validate_odd_value,
)


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

