"""
services/external_squad_lookup.py

Intelligent external roster scanner and verification service for footballers.
When OCR identifies a goalscorer or assist maker who is missing from the club's registered squad,
this service queries external official football databases:
1. FotMob Search API (apigw.fotmob.com)
2. TheSportsDB API
3. Wikipedia REST API
4. Google Gemini AI (intelligent fallback for transliteration / multi-lingual queries)

Provides canonical player naming and authentic EA FC positions (ST, LW, RW, CAM, CM, CB, etc.).
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

import club_registry
import config
import database
from services.ai.ai_recognizer import clean_player_name
from services.player_names import is_same_footballer, normalize_player_name_key
from services.player_positions import detect_player_position

logger = logging.getLogger(__name__)


def _lookup_fotmob(raw_name: str, team_name: str) -> dict | None:
    """Query FotMob Search API to find player and verify club membership."""
    try:
        clean = clean_player_name(raw_name)
        if not clean or len(clean) < 3:
            return None

        url = f"https://apigw.fotmob.com/searchapi/suggest?term={urllib.parse.quote(clean)}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("squadMemberSuggest", []):
                for opt in item.get("options", []):
                    p_text = opt.get("text", "")
                    p_name = p_text.split("|")[0].strip() if p_text else ""
                    payload = opt.get("payload") or {}
                    p_team = payload.get("teamName", "")
                    if payload.get("isCoach"):
                        continue
                    if p_name and p_team and club_registry.teams_match(p_team, team_name):
                        pos = detect_player_position(p_name, team_name)
                        return {
                            "raw_name": raw_name,
                            "player_name": p_name,
                            "position": pos,
                            "team_name": team_name,
                            "source": "FotMob",
                            "external_team": p_team,
                        }
    except Exception as e:
        logger.debug(f"FotMob lookup failed for '{raw_name}' in '{team_name}': {e}")
    return None


def _lookup_thesportsdb(raw_name: str, team_name: str) -> dict | None:
    """Query TheSportsDB API to find player and verify club membership."""
    try:
        clean = clean_player_name(raw_name)
        if not clean or len(clean) < 3:
            return None

        url = f"https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p={urllib.parse.quote(clean)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for p in data.get("player") or []:
                p_name = (p.get("strPlayer") or "").strip()
                p_team = (p.get("strTeam") or "").strip()
                if p_name and p_team and club_registry.teams_match(p_team, team_name):
                    pos = detect_player_position(p_name, team_name)
                    return {
                        "raw_name": raw_name,
                        "player_name": p_name,
                        "position": pos,
                        "team_name": team_name,
                        "source": "TheSportsDB",
                        "external_team": p_team,
                    }
    except Exception as e:
        logger.debug(f"TheSportsDB lookup failed for '{raw_name}' in '{team_name}': {e}")
    return None


def _lookup_wikipedia(raw_name: str, team_name: str) -> dict | None:
    """Query Wikipedia REST API to check if player is associated with team."""
    try:
        clean = clean_player_name(raw_name)
        if not clean or len(clean) < 3:
            return None

        candidates = [clean, f"{clean}_(footballer)", f"{clean}_(soccer)"]
        for c in candidates:
            url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(c.replace(' ', '_'))}"
            req = urllib.request.Request(url, headers={"User-Agent": "Logovobot/1.0 (contact@logovo.bot)"})
            try:
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    text = (data.get("description", "") + " " + data.get("extract", "")).lower()
                    title = data.get("title", clean)
                    # Check if team or canonical team name is mentioned in bio
                    resolved_team = club_registry.resolve_team_name(team_name) or team_name
                    if resolved_team.lower() in text or team_name.lower() in text:
                        pos = detect_player_position(title, team_name)
                        return {
                            "raw_name": raw_name,
                            "player_name": title,
                            "position": pos,
                            "team_name": team_name,
                            "source": "Wikipedia",
                            "external_team": resolved_team,
                        }
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Wikipedia lookup failed for '{raw_name}' in '{team_name}': {e}")
    return None


def _lookup_gemini(raw_name: str, team_name: str) -> dict | None:
    """Fallback to Gemini AI to verify if footballer belongs to club."""
    api_key = getattr(config, "GEMINI_API_KEY", None)
    if not api_key:
        return None

    try:
        clean = clean_player_name(raw_name)
        if not clean or len(clean) < 3:
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        prompt = (
            f"You are a football roster expert. Does the footballer '{clean}' play for the club '{team_name}' "
            f"(or has recently played in 2024-2026)?\n"
            f"Return ONLY valid JSON matching this schema:\n"
            f'{{"found": true, "player_name": "Full Name in Latin", "position": "ST"}}\n'
            f'or {{"found": false}} if the player does not belong to this team.'
        )
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    text = parts[0].get("text", "")
                    match = re.search(r"\{.*\}", text, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group(0))
                        if parsed.get("found"):
                            p_name = parsed.get("player_name") or clean
                            pos = parsed.get("position") or detect_player_position(p_name, team_name)
                            return {
                                "raw_name": raw_name,
                                "player_name": p_name,
                                "position": pos,
                                "team_name": team_name,
                                "source": "AI (Gemini)",
                                "external_team": team_name,
                            }
    except Exception as e:
        logger.debug(f"Gemini AI lookup failed for '{raw_name}' in '{team_name}': {e}")
    return None


def lookup_external_club_player(raw_name: str, team_name: str) -> dict | None:
    """
    Search external official rosters (FotMob -> TheSportsDB -> Wikipedia -> Gemini)
    to verify player membership in team and determine canonical name and position.
    """
    if not raw_name or not team_name:
        return None

    # 1. FotMob
    res = _lookup_fotmob(raw_name, team_name)
    if res:
        return res

    # 2. TheSportsDB
    res = _lookup_thesportsdb(raw_name, team_name)
    if res:
        return res

    # 3. Wikipedia
    res = _lookup_wikipedia(raw_name, team_name)
    if res:
        return res

    # 4. Gemini AI fallback
    res = _lookup_gemini(raw_name, team_name)
    if res:
        return res

    return None


def find_new_goal_action_players(
    h_goals: dict | list | None,
    a_goals: dict | list | None,
    h_assists: dict | list | None,
    a_assists: dict | list | None,
    home_team: str,
    away_team: str,
) -> list[dict]:
    """
    Scan all goalscorers and assist providers for home and away sides.
    If any player is NOT in squad_players for that club, verify them through
    the external official databases and return a list of pending player dicts.
    """
    def _extract_names_and_counts(data) -> dict[str, int]:
        counts = {}
        if not data:
            return counts
        if isinstance(data, dict):
            for k, v in data.items():
                if k and str(k).strip() and int(v) > 0:
                    counts[str(k).strip()] = int(v)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (tuple, list)) and len(item) >= 2:
                    p, c = str(item[0]).strip(), int(item[1])
                    if p and c > 0:
                        counts[p] = counts.get(p, 0) + c
                elif item and str(item).strip():
                    p = str(item).strip()
                    counts[p] = counts.get(p, 0) + 1
        return counts

    h_g_map = _extract_names_and_counts(h_goals)
    a_g_map = _extract_names_and_counts(a_goals)
    h_a_map = _extract_names_and_counts(h_assists)
    a_a_map = _extract_names_and_counts(a_assists)

    home_players = set(h_g_map.keys()) | set(h_a_map.keys())
    away_players = set(a_g_map.keys()) | set(a_a_map.keys())

    home_squad = database.get_squad(home_team) or []
    away_squad = database.get_squad(away_team) or []

    def _player_in_squad(p_name: str, team: str, squad: list[str]) -> bool:
        if database.find_player_in_squad(p_name, team):
            return True
        p_clean = p_name.strip().lower()
        p_norm = normalize_player_name_key(p_name)
        for sp in squad:
            sp_clean = sp.strip().lower()
            if sp_clean == p_clean or normalize_player_name_key(sp) == p_norm:
                return True
            if is_same_footballer(p_name, sp):
                return True
        return False

    new_players = []
    seen_keys = set()

    for p in home_players:
        if not p or _player_in_squad(p, home_team, home_squad):
            continue
        lookup = lookup_external_club_player(p, home_team)
        goals = h_g_map.get(p, 0)
        assists = h_a_map.get(p, 0)
        if lookup:
            player_name = lookup["player_name"]
            position = lookup["position"]
            source = lookup["source"]
        else:
            player_name = clean_player_name(p) or p
            position = detect_player_position(player_name, home_team, fallback_goals=goals, fallback_assists=assists)
            source = "Автоопределение"

        key = (normalize_player_name_key(player_name), normalize_player_name_key(home_team))
        if key not in seen_keys:
            seen_keys.add(key)
            new_players.append({
                "raw_name": p,
                "player_name": player_name,
                "position": position,
                "team_name": home_team,
                "source": source,
                "goals": goals,
                "assists": assists,
            })

    for p in away_players:
        if not p or _player_in_squad(p, away_team, away_squad):
            continue
        lookup = lookup_external_club_player(p, away_team)
        goals = a_g_map.get(p, 0)
        assists = a_a_map.get(p, 0)
        if lookup:
            player_name = lookup["player_name"]
            position = lookup["position"]
            source = lookup["source"]
        else:
            player_name = clean_player_name(p) or p
            position = detect_player_position(player_name, away_team, fallback_goals=goals, fallback_assists=assists)
            source = "Автоопределение"

        key = (normalize_player_name_key(player_name), normalize_player_name_key(away_team))
        if key not in seen_keys:
            seen_keys.add(key)
            new_players.append({
                "raw_name": p,
                "player_name": player_name,
                "position": position,
                "team_name": away_team,
                "source": source,
                "goals": goals,
                "assists": assists,
            })

    return new_players
