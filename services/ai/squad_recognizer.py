"""
Perceptual OCR for squad / lineup screenshots (EA FC, FIFA Mobile, eFootball).

The model only reports what is visibly printed on the screenshot — player names
and the position label rendered next to each of them. Matching those names
against an existing roster, deduplication and enrichment happen deterministically
in Python/SQLite afterwards, never inside the prompt.
"""

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

import config
from services.ai.ai_recognizer import (
    GEMINI_MODELS,
    _get_gemini_opener,
    clean_json_response,
    clean_player_name,
    get_ordered_ocr_keys,
    get_ordered_ocr_models,
)
import unicodedata

logger = logging.getLogger(__name__)

MAX_SQUAD_PLAYERS = 40

PROMPT_MAIN_TEXT = """
You are an expert OCR system for football / soccer squad and lineup screens (EA Sports FC, FIFA Mobile, eFootball).
Extract the STARTING 11 LINEUP shown on the pitch.

Return JSON strictly matching this schema:
{
  "players": [
    {"name": "SURNAME or FULL NAME", "position": "ST"}
  ]
}

Strict Rules:
1. ONLY extract players whose name is CLEARLY and FULLY printed in text on their card on the pitch.
2. CRITICAL - CUT-OFF CARDS: On formation pitch screens, cards at the bottom edge (substitutes/bench) are often cut off horizontally by the screen edge, showing only ratings or headshots while their name banner is invisible below the viewport. DO NOT extract or guess cut-off cards! Never guess or hallucinate player names from faces, hair, ratings, or club rosters when their text name is not visibly printed.
3. NO DUPLICATES: Never output the same player twice (e.g., both as short and full name like 'VINI JR.' and 'VINÍCIUS JÚNIOR'). Each footballer must appear at most once.
4. Drop kit numbers, ratings (e.g. 112, 107, 84), chemistry values, club badges, and emojis.
5. 'position' must be the 2-4 letter abbreviation printed near the player (e.g. ST, CF, LW, RW, CAM, CM, CDM, LM, RM, CB, LB, RB, LWB, RWB, GK, or Russian equivalents: ВР, ЦЗ, ПЗ, ЛЗ, ЦОП, ЦП, ЦАП, ЛП, ПП, ЛВ, ПВ, НАП, ФРВ). If no position is printed, use null.
6. Return ONLY valid JSON without code fences or extra text.
"""

PROMPT_RESERVES_TEXT = """
You are an expert OCR system for football / soccer squad and lineup screens (EA Sports FC, FIFA Mobile, eFootball).
The user is uploading their BENCH / RESERVES (резервисты / скамейка запасных).

In EA Sports FC Mobile, when the "РЕЗЕРВИСТЫ" (Reserves) menu is open, a horizontal tray of up to 7 substitute slots appears at the bottom of the screen (empty slots have a '+' sign), while the starting 11 players are visible in the background on the grass pitch.

CRITICAL RULES:
1. Extract ONLY players from the bottom RESERVES TRAY / DRAWER (the substitute slots row at the bottom edge of the screen).
2. DO NOT extract players from the main grass pitch (starting XI). COMPLETELY IGNORE all players on the pitch!
3. Any cards placed on the football field / green grass are STARTING PLAYERS and MUST BE IGNORED 100%.
4. Empty slots in the reserves tray showing '+' must be skipped.
5. Read only visibly printed names in the reserves tray. Drop kit numbers, ratings, badges.
6. 'position' must be the 2-4 letter abbreviation printed near the reserve player (e.g. ST, LW, RW, CAM, CM, CDM, CB, LB, RB, GK, or Russian equivalents: ФРВ, ЛП, ПП, ЦАП, ЦП, ЦОП, ЛЗ, ПЗ, ЦЗ, ВР).
7. If no players are placed in the reserves tray (only '+' slots), return {"players": []}.
8. Return JSON strictly matching this schema:
{
  "players": [
    {"name": "SURNAME or FULL NAME", "position": "ST"}
  ]
}
9. Return ONLY valid JSON without code fences or extra text.
"""

# Backward compatibility alias
PROMPT_TEXT = PROMPT_MAIN_TEXT


MAX_PLAYER_NAME_LEN = 50

from services.player_names import (
    ALIAS_TOKEN_MAP,
    is_same_footballer,
    normalize_footballer_name,
    normalize_player_name_key,
)


def _parse_players(payload: dict) -> list[dict]:
    """Turn the model's raw `players` array into `[{player_name, position}]`.

    The model is free-form, so every row is treated as untrusted: it may be a
    bare string instead of an object, may key the name as `player_name`/`pos`,
    and may hand back a wall of garbled pixels as a name. A row survives only
    when it cleans up to a name that holds at least one letter and is no longer
    than MAX_PLAYER_NAME_LEN — otherwise OCR noise lands in the roster as a
    "player" nobody can delete by name.

    Deduplication checks for exact normalized matches as well as alias/substring
    equivalences (e.g. 'VINI JR.' and 'VINÍCIUS JÚNIOR').
    """
    raw_list = payload.get("players")
    if not isinstance(raw_list, list):
        return []

    result: list[dict] = []
    seen: set[str] = set()
    for item in raw_list:
        if isinstance(item, dict):
            raw_name = item.get("name") or item.get("player_name") or ""
            raw_pos = item.get("position") or item.get("pos")
        else:
            raw_name, raw_pos = str(item), None

        cleaned_name = clean_player_name(str(raw_name))
        if not cleaned_name or len(cleaned_name) > MAX_PLAYER_NAME_LEN:
            continue
        if not re.search(r"[^\W\d_]", cleaned_name):
            continue

        norm_key = cleaned_name.casefold()
        if norm_key in seen:
            continue

        pos_str = str(raw_pos).strip() if raw_pos else None

        # Check against already added players for alias/name overlap
        is_dup = False
        for p in result:
            if is_same_footballer(cleaned_name, p["player_name"]):
                is_dup = True
                # If existing entry has no position but this candidate has one, enrich it
                if not p.get("position") and pos_str:
                    p["position"] = pos_str
                break
        if is_dup:
            continue

        seen.add(norm_key)

        result.append({
            "player_name": cleaned_name,
            "position": pos_str or None,
        })
        if len(result) >= MAX_SQUAD_PLAYERS:
            break
    return result


def recognize_squad_screenshot_bytes(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    api_key: str | None = None,
    is_reserves: bool = False,
) -> list[dict] | None:
    """
    Read player names and printed positions off a squad screenshot.

    Returns `[{"player_name": str, "position": str | None}, ...]`, an empty list
    when the image holds no readable squad, or None when every model failed.
    """
    keys_to_try = get_ordered_ocr_keys(api_key)
    if not keys_to_try:
        logger.error("GEMINI_API_KEY is empty or not set!")
        return None
    if not image_bytes:
        return None

    opener = _get_gemini_opener()
    base_url = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")

    prompt = PROMPT_RESERVES_TEXT if is_reserves else PROMPT_MAIN_TEXT

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                }},
            ]
        }],
        "generationConfig": {"temperature": 0.0},
    }
    body = json.dumps(payload).encode("utf-8")

    for m_name in get_ordered_ocr_models():
        for target_api_key in keys_to_try:
            try:
                req = urllib.request.Request(
                    f"{base_url}/v1beta/models/{m_name}:generateContent?key={target_api_key}",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    },
                )
                with opener.open(req, timeout=30) as response:
                    res_json = json.loads(response.read().decode("utf-8"))

                candidates = res_json.get("candidates", [])
                if not candidates or "content" not in candidates[0]:
                    logger.warning(f"Gemini model '{m_name}' returned no candidates for squad OCR")
                    continue

                text_content = candidates[0]["content"]["parts"][0]["text"]
                parsed_data = json.loads(clean_json_response(text_content))
                if not isinstance(parsed_data, dict):
                    logger.warning(f"Gemini model '{m_name}' returned non-dict JSON for squad OCR")
                    continue

                players = _parse_players(parsed_data)
                logger.info(f"Squad OCR ({m_name}) recognized {len(players)} player(s)")
                return players

            except urllib.error.HTTPError as e:
                error_body = e.read().decode("utf-8", errors="ignore")
                key_suffix = f"...{target_api_key[-4:]}" if len(target_api_key) > 4 else "***"
                logger.warning(f"Gemini model '{m_name}' (key {key_suffix}) HTTP {e.code}: {error_body[:300]}")
                if e.code in (429, 403, 503):
                    continue
                continue
            except Exception as e:
                logger.exception(f"Gemini model '{m_name}' squad recognition error: {e}")
                continue

    logger.error("All Gemini models and keys failed for squad recognition.")
    return None
