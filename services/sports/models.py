"""
services/sports/models.py

Logovo.bet — Provider-Neutral Sports Data Models (Phase 8).
Strict Invariants:
1. Provider-independent canonical structures; no raw vendor-specific JSON leaks past the adapter layer.
2. Missing / unobserved values MUST remain None — strictly never substitute fake zeros or synthetic xG.
3. Clean conversion methods between internal database states and external provider representations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class ProviderTeam:
    """Canonical representation of a football club/team."""
    team_id: int | str
    name: str
    code: Optional[str] = None
    logo: Optional[str] = None
    is_home: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderMatch:
    """Canonical representation of a fixture/match."""
    match_id: int | str
    provider: str
    home_team: ProviderTeam
    away_team: ProviderTeam
    status: str  # SCHEDULED, PRE_MATCH, LIVE, HALFTIME, FINISHED, POSTPONED, CANCELLED, ABANDONED, SUSPENDED
    period: str = "pre_match"  # pre_match, 1h, ht, 2h, et, pen, ft
    minute: Optional[int] = None
    home_score: int = 0
    away_score: int = 0
    start_time: Optional[str] = None
    league_id: Optional[int | str] = None
    league_name: Optional[str] = None
    round: Optional[str] = None
    venue: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderEvent:
    """Canonical representation of an in-play match event."""
    provider_event_id: str
    match_id: int | str
    provider: str
    event_type: str  # goal, own_goal, penalty, penalty_missed, yellow_card, red_card, substitution, var, etc.
    minute: int
    added_time: Optional[int] = None
    team_id: Optional[int | str] = None
    team_name: Optional[str] = None
    player_id: Optional[int | str] = None
    player_name: Optional[str] = None
    detail: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderStatistics:
    """Canonical representation of real-time match statistics. None indicates unavailable."""
    match_id: int | str
    provider: str
    possession_home: Optional[float] = None
    possession_away: Optional[float] = None
    shots_home: Optional[int] = None
    shots_away: Optional[int] = None
    shots_on_target_home: Optional[int] = None
    shots_on_target_away: Optional[int] = None
    corners_home: Optional[int] = None
    corners_away: Optional[int] = None
    fouls_home: Optional[int] = None
    fouls_away: Optional[int] = None
    offsides_home: Optional[int] = None
    offsides_away: Optional[int] = None
    yellow_cards_home: Optional[int] = None
    yellow_cards_away: Optional[int] = None
    red_cards_home: Optional[int] = None
    red_cards_away: Optional[int] = None
    dangerous_attacks_home: Optional[int] = None
    dangerous_attacks_away: Optional[int] = None
    attacks_home: Optional[int] = None
    attacks_away: Optional[int] = None
    passes_home: Optional[int] = None
    passes_away: Optional[int] = None
    pass_accuracy_home: Optional[float] = None
    pass_accuracy_away: Optional[float] = None
    xg_home: Optional[float] = None
    xg_away: Optional[float] = None
    saves_home: Optional[int] = None
    saves_away: Optional[int] = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderLineup:
    """Canonical representation of starting squad and tactical formation."""
    match_id: int | str
    provider: str
    team_id: int | str
    team_name: str
    formation: Optional[str] = None
    starting_xi: list[dict[str, Any]] = field(default_factory=list)
    substitutes: list[dict[str, Any]] = field(default_factory=list)
    coach_name: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderInjury:
    """Canonical representation of confirmed player injuries or suspensions."""
    player_id: Optional[int | str] = None
    player_name: str = ""
    team_id: Optional[int | str] = None
    team_name: str = ""
    injury_type: str = ""
    status: str = "out"  # out, doubtful, suspended, recovered
    fixture_id: Optional[int | str] = None
    last_update: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderOdds:
    """Canonical representation of live/pre-match bookmaker odds."""
    match_id: int | str
    provider: str
    bookmaker_id: Optional[int | str] = None
    bookmaker_name: str = ""
    market_key: str = ""  # 1x2, over_under, btts, etc.
    market_name: str = ""
    selections: list[dict[str, Any]] = field(default_factory=list)  # [{"selection_key": "p1", "name": "П1", "odds": 2.10}]
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Legacy Compatibility Wrappers (Phase 6 / 7) ─────────────────────────────

@dataclass
class LiveMatchState:
    match_id: int
    season_id: int = 1
    division_id: int = 1
    status: str = "SCHEDULED"
    period: str = "pre_match"
    minute: Optional[int] = None
    home_score: int = 0
    away_score: int = 0
    provider: str = "none"
    provider_match_id: Optional[str] = None
    version: int = 1
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LiveEvent:
    match_id: int
    provider: str
    provider_event_id: str
    event_type: str
    minute: int
    added_time: Optional[int] = None
    team_id: Optional[int] = None
    team_name: Optional[str] = None
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LiveStatistics:
    match_id: int
    possession_home: Optional[float] = None
    possession_away: Optional[float] = None
    shots_home: Optional[int] = None
    shots_away: Optional[int] = None
    shots_on_target_home: Optional[int] = None
    shots_on_target_away: Optional[int] = None
    corners_home: Optional[int] = None
    corners_away: Optional[int] = None
    fouls_home: Optional[int] = None
    fouls_away: Optional[int] = None
    offsides_home: Optional[int] = None
    offsides_away: Optional[int] = None
    yellow_cards_home: Optional[int] = None
    yellow_cards_away: Optional[int] = None
    red_cards_home: Optional[int] = None
    red_cards_away: Optional[int] = None
    dangerous_attacks_home: Optional[int] = None
    dangerous_attacks_away: Optional[int] = None
    attacks_home: Optional[int] = None
    attacks_away: Optional[int] = None
    passes_home: Optional[int] = None
    passes_away: Optional[int] = None
    pass_accuracy_home: Optional[float] = None
    pass_accuracy_away: Optional[float] = None
    xg_home: Optional[float] = None
    xg_away: Optional[float] = None
    saves_home: Optional[int] = None
    saves_away: Optional[int] = None
    substitutions_home: Optional[int] = None
    substitutions_away: Optional[int] = None
    provider: str = "none"
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── IRL betting (pre-match 1X2) ─────────────────────────────────────────────

@dataclass(frozen=True)
class PrematchFixture:
    """A real-world fixture for IRL betting, provider-neutral.

    `kickoff` is naive MSK. `home_goals`/`away_goals` are the main-time (90 min
    + stoppage) score and stay None until the provider has one — never a fake 0.
    """
    fixture_id: int
    league_id: int
    league_name: str
    home: str
    away: str
    kickoff: datetime
    status_short: str = "NS"
    home_goals: Optional[int] = None
    away_goals: Optional[int] = None
    country: str = ""
    season: Optional[int] = None
    elapsed: Optional[int] = None


@dataclass(frozen=True)
class MatchWinnerOdds:
    """1X2 prices of one bookmaker for one fixture. Complete or not returned at all."""
    fixture_id: int
    bookmaker_id: int
    home: float
    draw: float
    away: float
    updated_at: Optional[datetime] = None
