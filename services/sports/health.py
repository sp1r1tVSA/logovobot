"""
services/sports/health.py

Logovo.bet — Sports Provider Health & Telemetry Monitor (Phase 8).
Tracks latency, error rates, circuit breaker state, and quota usage.
Strict Invariant: Secret tokens and API keys are NEVER included in telemetry or logs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import database

logger = logging.getLogger(__name__)


class ProviderHealthMonitor:
    """Singleton telemetry collector for active sports data provider."""

    def __init__(self) -> None:
        self.requests_count = 0
        self.errors_count = 0
        self.last_latency_ms = 0.0
        self.last_sync_time: Optional[str] = None
        self.last_success_time: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_status_code: Optional[int] = None

    def record_request(
        self,
        provider: str,
        endpoint: str,
        latency_ms: float,
        status_code: int,
        records_count: int = 0,
        error_message: Optional[str] = None
    ) -> None:
        """Record outbound network request outcome and persist to audit log."""
        self.requests_count += 1
        self.last_latency_ms = round(latency_ms, 2)
        self.last_status_code = status_code
        self.last_sync_time = datetime.now(timezone.utc).isoformat()

        if 200 <= status_code < 300:
            self.last_success_time = self.last_sync_time
        else:
            self.errors_count += 1
            self.last_error = error_message or f"HTTP {status_code}"

        # Persist audit record to SQLite asynchronously/safely
        try:
            database.record_provider_sync_log(
                provider=provider,
                endpoint=endpoint,
                status_code=status_code,
                records_count=records_count,
                latency_ms=latency_ms,
                error_message=error_message
            )
        except Exception as e:
            logger.debug(f"Could not write provider sync log: {e}")

    record_call = record_request

    def get_summary(
        self,
        provider_name: str,
        is_connected: bool,
        circuit_state: dict[str, Any],
        rate_limiter_stats: dict[str, Any],
        cache_stats: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate structured health report without disclosing secrets."""
        stale_matches = database.get_stale_provider_matches_count(stale_threshold_seconds=120)
        error_rate = round(self.errors_count / self.requests_count, 3) if self.requests_count > 0 else 0.0

        if circuit_state.get("is_open"):
            health_status = "CIRCUIT_OPEN"
        elif not is_connected:
            health_status = "UNCONFIGURED" if provider_name == "api_sports" else "UNAVAILABLE"
        elif circuit_state.get("consecutive_failures", 0) > 0 or (self.requests_count > 0 and error_rate > 0.20):
            health_status = "DEGRADED"
        else:
            health_status = "HEALTHY"

        return {
            "provider": provider_name,
            "status": health_status,
            "connected": is_connected,
            "last_sync": self.last_sync_time,
            "last_success": self.last_success_time,
            "last_error": self.last_error,
            "last_latency_ms": self.last_latency_ms,
            "requests_count": self.requests_count,
            "errors_count": self.errors_count,
            "error_rate": error_rate,
            "stale_matches_count": stale_matches,
            "circuit_breaker": circuit_state,
            "rate_limiter": rate_limiter_stats,
            "cache": cache_stats,
        }


_GLOBAL_HEALTH_MONITOR = ProviderHealthMonitor()


def get_health_monitor() -> ProviderHealthMonitor:
    """Fetch global provider health monitor singleton."""
    return _GLOBAL_HEALTH_MONITOR
