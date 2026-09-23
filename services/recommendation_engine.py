"""
services/recommendation_engine.py

Logovo.bet — Hot Matches Ranking & Explainable Personalized Recommendations.
Provides:
1. Multi-factor Hot Matches scoring (live status, odds movement, betting volume, H2H rivalry).
2. Explainable personalized betting recommendations based on division, favorites, and market tendencies.
Strictly non-manipulative: recommendations are transparently explained and privacy-safe.
"""

import logging
from typing import Any, Optional
import database

logger = logging.getLogger(__name__)

# Configurable Weights for Hot Score
WEIGHT_IS_LIVE = 40.0
WEIGHT_ODDS_MOVEMENT = 20.0
WEIGHT_BETTING_VOLUME = 20.0
WEIGHT_H2H_RIVALRY = 10.0
WEIGHT_START_PROXIMITY = 10.0


def calculate_match_hot_score(
    is_live: bool,
    odds_movement_count: int,
    betting_volume: int,
    h2h_count: int,
    is_open: bool
) -> float:
    """Compute composite hotness score (0 - 100)."""
    score = 0.0

    if is_live:
        score += WEIGHT_IS_LIVE

    # Odds movement intensity (normalized up to 10 movements)
    mov_norm = min(1.0, odds_movement_count / 10.0)
    score += mov_norm * WEIGHT_ODDS_MOVEMENT

    # Betting volume intensity (normalized up to 20 bets)
    vol_norm = min(1.0, betting_volume / 20.0)
    score += vol_norm * WEIGHT_BETTING_VOLUME

    # Rivalry / H2H factor (normalized up to 5 meetings)
    h2h_norm = min(1.0, h2h_count / 5.0)
    score += h2h_norm * WEIGHT_H2H_RIVALRY

    if is_open:
        score += WEIGHT_START_PROXIMITY

    return round(score, 1)


def _club_matches(club: str, target: str) -> bool:
    """Check if club name matches target using database.teams_match with fallback."""
    if not club or not target:
        return False
    try:
        if database.teams_match(club, target):
            return True
    except Exception:
        pass
    c_norm = club.strip().lower()
    t_norm = target.strip().lower()
    return c_norm in t_norm or t_norm in c_norm


def _line_markets(division_id: Optional[int], season_id: Optional[int]) -> dict[int, dict[str, Any]]:
    """Матчи, которые сейчас стоят в линии Mini App: `match_id` → тайл линии.

    Открытый тур — это ещё не линия: в неё попадают только центральные пары
    (`CENTRAL_MATCHES_PER_ROUND`), и до дедлайна. Поэтому источник один —
    тот же `get_active_bet_markets`, что рисует линию; иначе рекомендации
    предлагают матчи, на которые нельзя поставить. Тайл заодно несёт ники
    тренеров, так что отдельно их искать не нужно.
    """
    markets = database.get_active_bet_markets(division_id=division_id, season_id=season_id)
    return {bm["match_id"]: bm for bm in markets}


def get_hot_matches(
    division_id: Optional[int] = None,
    season_id: Optional[int] = None,
    limit: int = 10
) -> list[dict[str, Any]]:
    """
    Retrieve top hot matches across the league or scoped to a division/season.
    Matches are constrained to the open betting line and ranked by calculated
    composite hot score.
    """
    line_ids = sorted(_line_markets(division_id, season_id))
    if not line_ids:
        return []

    with database.transaction() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in line_ids)
        base_filter = (
            f"WHERE m.id IN ({placeholders}) "
            "AND m.status IN ('open', 'scheduled', 'pending', 'live')"
        )
        params: list[Any] = list(line_ids)

        # Query candidates in chronological round order
        cursor.execute(f"""
            SELECT m.id, m.player1_team, m.player2_team, m.round_number, m.division_id, m.season_id,
                   m.status, m.player1_score, m.player2_score, m.live_minute,
                   lms.status as live_status, lms.period as live_period,
                   (SELECT COUNT(*) FROM odds_movement om WHERE om.match_id = m.id) as mov_count,
                   (SELECT COUNT(*) FROM bet_items bi WHERE bi.match_id = m.id) as bet_count
            FROM matches m
            LEFT JOIN live_match_states lms ON m.id = lms.match_id
            {base_filter}
            ORDER BY m.round_number ASC, m.id ASC
            LIMIT 50
        """, params)
        candidates = [dict(r) for r in cursor.fetchall()]

        scored_matches = []
        for c in candidates:
            is_live = (c.get("live_status") in ("LIVE", "HALFTIME")) or (c.get("status") == "live")
            mov_cnt = c.get("mov_count") or 0
            bet_cnt = c.get("bet_count") or 0
            is_open = c.get("status") in ("open", "scheduled", "pending")

            # Check H2H count
            t1 = c["player1_team"] or ""
            t2 = c["player2_team"] or ""
            cursor.execute("""
                SELECT COUNT(*) FROM matches
                WHERE ((LOWER(player1_team) = LOWER(?) AND LOWER(player2_team) = LOWER(?))
                    OR (LOWER(player1_team) = LOWER(?) AND LOWER(player2_team) = LOWER(?)))
                  AND status IN ('confirmed', 'completed', 'finished')
            """, (t1, t2, t2, t1))
            h2h_cnt = cursor.fetchone()[0]

            hot_score = calculate_match_hot_score(
                is_live=is_live,
                odds_movement_count=mov_cnt,
                betting_volume=bet_cnt,
                h2h_count=h2h_cnt,
                is_open=is_open
            )

            c["hot_score"] = hot_score
            c["is_live"] = is_live
            scored_matches.append(c)

        # Sort descending by hot_score
        scored_matches.sort(key=lambda x: x["hot_score"], reverse=True)
        return scored_matches[:limit]


def get_user_recommendations(
    user_id: int,
    limit: int = 5,
    risk_profile: str = "balanced",
    division_id: Optional[int] = None,
    season_id: Optional[int] = None
) -> list[dict[str, Any]]:
    """
    Generate explainable personalized match/market recommendations for a bettor.
    Matches are strictly the ones in the open betting line (`get_active_bet_markets`):
    the central pairs of open rounds before their deadline — not every match of the round.
    Based on:
    - User's division (or passed division_id)
    - User's favorite teams and personal club
    - User's most frequently chosen market types
    - User risk profile: 'conservative' (higher confidence, lower odds), 'balanced', 'aggressive' (higher potential returns).
    Strict Invariant: Strictly read-only analytical presentation.
    """
    profile = (risk_profile or "balanced").lower()
    if profile not in ("conservative", "balanced", "aggressive"):
        profile = "balanced"

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Fetch user profile
        cursor.execute("SELECT telegram_id, division_id, team_name FROM users WHERE telegram_id = ?", (user_id,))
        user_row = cursor.fetchone()
        user_div_id = user_row["division_id"] if user_row and user_row["division_id"] else 1
        user_team = user_row["team_name"] if user_row and user_row["team_name"] else ""
        target_div_id = division_id if division_id is not None else user_div_id

        # 2. Fetch user favorite clubs
        cursor.execute("SELECT target_id FROM favorites WHERE user_id = ? AND target_type = 'club'", (user_id,))
        fav_clubs = [str(r[0]).strip() for r in cursor.fetchall()]
        if user_team:
            fav_clubs.append(user_team.strip())
        # Deduplicate while preserving order
        seen_favs = set()
        dedup_favs = []
        for fc in fav_clubs:
            if fc and fc.lower() not in seen_favs:
                seen_favs.add(fc.lower())
                dedup_favs.append(fc)
        fav_clubs = dedup_favs

        # 3. Fetch user preferred outcome / market
        cursor.execute("""
            SELECT outcome_type, COUNT(*) as cnt
            FROM bet_items bi
            JOIN user_bets ub ON bi.bet_id = ub.id
            WHERE ub.user_id = ?
            GROUP BY outcome_type
            ORDER BY cnt DESC
            LIMIT 1
        """, (user_id,))
        fav_market_row = cursor.fetchone()
        fav_market = fav_market_row["outcome_type"] if fav_market_row else "p1"

        # 4. Candidates are exactly the matches of the open line
        line = _line_markets(target_div_id, season_id)
        line_ids = sorted(line)
        if not line_ids:
            return []
        placeholders = ",".join("?" for _ in line_ids)
        cursor.execute(f"""
            SELECT m.id, m.player1_team, m.player2_team, m.round_number, m.division_id, m.status,
                   lms.status as live_status, lms.home_score, lms.away_score, lms.minute
            FROM matches m
            LEFT JOIN live_match_states lms ON m.id = lms.match_id
            WHERE m.id IN ({placeholders})
              AND m.status IN ('open', 'scheduled', 'pending', 'live')
            ORDER BY m.round_number ASC, m.id ASC
        """, line_ids)

        available_matches = [dict(r) for r in cursor.fetchall()]

        recommendations = []
        for m in available_matches:
            t1 = m["player1_team"] or ""
            t2 = m["player2_team"] or ""

            reason = ""
            priority = 1

            matched_fav = next((fc for fc in fav_clubs if _club_matches(fc, t1) or _club_matches(fc, t2)), None)
            is_live = (m.get("live_status") in ("LIVE", "HALFTIME")) or (m.get("status") == "live")

            if matched_fav:
                reason = f"⭐ Матч с участием вашего любимого клуба ({matched_fav.capitalize()})."
                priority = 3
            elif is_live:
                reason = "🔥 Матч прямо сейчас в прямом эфире с динамическими коэффициентами."
                priority = 2
            else:
                reason = f"🏆 Центральная игра Тура #{m['round_number']} в вашем Дивизионе {target_div_id}."
                priority = 1

            # Fetch a sample market for quick action
            cursor.execute("""
                SELECT ms.selection_key, ms.selection_name, ms.odds_value
                FROM market_selections ms
                JOIN markets mk ON ms.market_id = mk.id
                WHERE mk.match_id = ? AND mk.status = 'open' AND ms.status = 'active'
                ORDER BY ms.id ASC
                LIMIT 3
            """, (m["id"],))
            quick_odds = [dict(s) for s in cursor.fetchall()]

            # Risk profile odds filtering
            if profile == "conservative":
                quick_odds = [q for q in quick_odds if float(q["odds_value"]) <= 2.50]
            elif profile == "aggressive":
                quick_odds = [q for q in quick_odds if float(q["odds_value"]) >= 1.60]

            recommendations.append({
                "match_id": m["id"],
                "division_id": m["division_id"] or target_div_id,
                "round_number": m["round_number"],
                "player1_team": m["player1_team"],
                "player2_team": m["player2_team"],
                "player1_username": line[m["id"]].get("player1_username"),
                "player2_username": line[m["id"]].get("player2_username"),
                "status": m["status"],
                "reason": reason,
                "priority": priority,
                "risk_profile": profile,
                "suggested_odds": quick_odds
            })

        # Separate into categories:
        # 1. Favorite club matches (priority 3)
        # 2. Live matches (priority 2)
        # 3. Other central matches (priority 1)
        fav_recs = [r for r in recommendations if r["priority"] == 3]
        live_recs = [r for r in recommendations if r["priority"] == 2]
        other_recs = [r for r in recommendations if r["priority"] == 1]

        # If user has matches of their favorite / own club in the open tours,
        # recommendations exclusively showcase those matches (plus live matches if any),
        # so the user sees ONLY their matches of the currently open tours.
        if fav_recs:
            result = fav_recs + live_recs
        else:
            result = live_recs + other_recs

        return result[:limit]

