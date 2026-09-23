"""
services/round_preview.py

Сборка данных и текстов для автопостинга в топик АНАЛИТИКА:
  * превью тура  — вероятности по каждой паре, матч тура, форма, серии;
  * итоги тура   — результаты, игрок тура, разгром тура, движение по таблице.

Новой математики здесь нет: вероятности берутся из готового
services.ensemble_engine.EnsemblePredictionEngine, форма и таблица — из database.
Модуль собирает ТОЛЬКО числа, а весь текст пишет Gemini в голосе «Темшика»
(с шаблонным фолбэком, чтобы тур не остался без публикации).

Модуль синхронный (как и services/ai/ai_chat.py — там urllib, не aiohttp),
поэтому из джобов вызывается через asyncio.to_thread.
"""

import html
import json
import logging
import os
import random
import re
import urllib.error
import urllib.request

import config
import database
from services.ai.ai_chat import (
    GEMINI_CHAT_MODELS,
    get_ordered_chat_keys,
    get_ordered_chat_models,
)

logger = logging.getLogger(__name__)

# Те же 3 модели с ротацией Round-Robin, что и в services/ai/ai_chat.py
CANDIDATE_MODELS = GEMINI_CHAT_MODELS

# Превью — одна строка на пару и «матч тура»: ~1000-1500 символов на 8 пар.
# PREVIEW_MAX_CHARS — жёсткий потолок, модели ставится цель пониже.
PREVIEW_MAX_CHARS = 2000
PREVIEW_TARGET_CHARS = 1500
CAPTION_MAX_CHARS = 1000


# ─── Payload builders ──────────────────────────────────────────────────────

def _form_streak(form: list[str]) -> dict:
    """Текущая серия по последним матчам (form[0] — самый свежий)."""
    if not form:
        return {"type": None, "length": 0}
    head = form[0]
    length = 0
    for r in form:
        if r != head:
            break
        length += 1
    kind = {"W": "wins", "D": "draws", "L": "losses"}.get(head)
    return {"type": kind, "length": length}


def _team_row(standings: list[dict], team_name: str) -> dict | None:
    for idx, row in enumerate(standings, start=1):
        if row.get("team_name") == team_name:
            enriched = dict(row)
            enriched["position"] = idx
            return enriched
    # Фолбэк через каноническое имя (таблица хранит уже канонические названия)
    canon = database.resolve_team_name(team_name) or team_name
    for idx, row in enumerate(standings, start=1):
        if row.get("team_name") == canon:
            enriched = dict(row)
            enriched["position"] = idx
            return enriched
    return None


def _team_block(standings: list[dict], form_map: dict, team_name: str) -> dict:
    row = _team_row(standings, team_name) or {}
    # get_teams_recent_form ключует словарь названием в нижнем регистре
    canon = database.resolve_team_name(team_name) or team_name
    form = list(form_map.get(team_name.lower()) or form_map.get(canon.lower()) or [])
    return {
        "name": team_name,
        "position": row.get("position"),
        "points": row.get("points"),
        "played": row.get("played"),
        "goals_scored": row.get("goals_scored"),
        "goals_conceded": row.get("goals_conceded"),
        "form": form,
        "streak": _form_streak(form),
    }


def build_preview_payload(division_id: int, round_number: int, season_id: int | None = None) -> dict:
    """Чистые числа для превью тура. Текста здесь нет — его пишет Gemini."""
    division = database.get_division(division_id) or {}
    standings = database.get_standings(division_id=division_id, season_id=season_id)
    form_map = database.get_teams_recent_form(5, division_id=division_id, season_id=season_id)
    matches = database.get_matches_by_round(round_number, division_id=division_id, season_id=season_id)
    round_info = database.get_round_info(round_number, division_id=division_id, season_id=season_id) or {}

    fixtures = []
    for m in matches:
        if m.get("status") in ("confirmed", "completed"):
            continue
        t1 = m.get("player1_team") or ""
        t2 = m.get("player2_team") or ""
        if not t1 or not t2:
            continue

        fixture = {
            "match_id": m.get("id"),
            "team1": _team_block(standings, form_map, t1),
            "team2": _team_block(standings, form_map, t2),
            "player1": m.get("player1_nickname"),
            "player2": m.get("player2_nickname"),
            "prediction": None,
        }

        try:
            from services.ensemble_engine import EnsemblePredictionEngine
            p = EnsemblePredictionEngine.predict_match(m["id"], save_to_db=False)
            fixture["prediction"] = {
                "p1": round(p["home_probability"] * 100, 1),
                "px": round(p["draw_probability"] * 100, 1),
                "p2": round(p["away_probability"] * 100, 1),
                "xg1": p["expected_goals"]["team1"],
                "xg2": p["expected_goals"]["team2"],
            }
        except Exception as e:
            # Недостаточно данных для прогноза — пара всё равно попадает в превью
            logger.warning(f"Round preview: no prediction for match #{m.get('id')}: {e}")

        fixtures.append(fixture)

    # «Матч тура» — самая непредсказуемая пара среди тех, где прогноз посчитан
    match_of_the_round = None
    scored = [f for f in fixtures if f["prediction"]]
    if scored:
        def spread(f):
            pr = f["prediction"]
            return max(pr["p1"], pr["px"], pr["p2"]) - min(pr["p1"], pr["px"], pr["p2"])
        best = min(scored, key=spread)
        match_of_the_round = {
            "team1": best["team1"]["name"],
            "team2": best["team2"]["name"],
            "spread": round(spread(best), 1),
        }

    return {
        "kind": "preview",
        "division_id": division_id,
        "division_name": division.get("name") or "Дивизион",
        "round_number": round_number,
        "deadline": round_info.get("deadline"),
        "fixtures": fixtures,
        "match_of_the_round": match_of_the_round,
        "leaders": [
            {"position": i, "team": r.get("team_name"), "points": r.get("points")}
            for i, r in enumerate(standings[:3], start=1)
        ],
    }


def build_digest_payload(division_id: int, round_number: int, season_id: int | None = None) -> dict:
    """Чистые числа для итогов тура: результаты, игрок тура, разгром, движение."""
    division = database.get_division(division_id) or {}
    matches = [
        m for m in database.get_matches_by_round(round_number, division_id=division_id, season_id=season_id)
        if m.get("status") != "cancelled"
    ]
    played = [
        m for m in matches
        if m.get("status") == "confirmed"
        and m.get("player1_score") is not None
        and m.get("player2_score") is not None
    ]

    results = []
    for m in played:
        s1 = int(m["player1_score"])
        s2 = int(m["player2_score"])
        results.append({
            "match_id": m.get("id"),
            "team1": m.get("player1_team") or "",
            "team2": m.get("player2_team") or "",
            "score1": s1,
            "score2": s2,
            "margin": abs(s1 - s2),
            "total_goals": s1 + s2,
            "mvp_player": (m.get("mvp_player") or "").strip() or None,
        })

    # Разгром тура — максимальная разница, при равенстве больше голов
    rout = None
    if results:
        rout = max(results, key=lambda r: (r["margin"], r["total_goals"]))
        if rout["margin"] < 2:
            rout = None

    # Игрок тура — лучший по Г+П (already sorted by the DB layer)
    player_stats = database.get_round_player_stats(round_number, division_id=division_id, season_id=season_id)
    player_of_the_round = dict(player_stats[0]) if player_stats else None

    # Обладатель золотой короны тура: больше всего наград «Игрок матча». При
    # равенстве корона одна на всех — в этом случае выделять некого, и поле
    # остаётся пустым, чтобы Темшик не назвал случайного из них лучшим.
    # Счёт ведётся по ключу имени, чтобы «Kökçü» и «KOKCU» были одной короной.
    crown_counts: dict[str, int] = {}
    crown_names: dict[str, str] = {}
    for r in results:
        if r["mvp_player"]:
            key = database.normalize_player_name_key(r["mvp_player"]) or r["mvp_player"].lower()
            crown_names.setdefault(key, r["mvp_player"])
            crown_counts[key] = crown_counts.get(key, 0) + 1
    mvp_of_the_round = None
    if crown_counts:
        best = max(crown_counts.values())
        leaders = [key for key, cnt in crown_counts.items() if cnt == best]
        if len(leaders) == 1:
            mvp_of_the_round = {"player_name": crown_names[leaders[0]], "mvp_count": best}

    # Движение по таблице: срез до тура vs срез до предыдущего тура
    after = database.get_standings(division_id=division_id, season_id=season_id, up_to_round=round_number)
    before = database.get_standings(division_id=division_id, season_id=season_id, up_to_round=round_number - 1)
    before_pos = {row.get("team_name"): idx for idx, row in enumerate(before, start=1)}

    table = []
    for idx, row in enumerate(after, start=1):
        prev = before_pos.get(row.get("team_name"))
        table.append({
            "position": idx,
            "team": row.get("team_name"),
            "played": row.get("played"),
            "points": row.get("points"),
            "goals_scored": row.get("goals_scored"),
            "goals_conceded": row.get("goals_conceded"),
            "previous_position": prev,
            "movement": (prev - idx) if prev else 0,
        })

    movers = sorted([t for t in table if t["movement"]], key=lambda t: abs(t["movement"]), reverse=True)[:4]

    return {
        "kind": "digest",
        "division_id": division_id,
        "division_name": division.get("name") or "Дивизион",
        "round_number": round_number,
        "results": results,
        "matches_total": len(matches),
        "matches_played": len(played),
        "goals_total": sum(r["total_goals"] for r in results),
        "rout": rout,
        "player_of_the_round": player_of_the_round,
        "mvp_of_the_round": mvp_of_the_round,
        "table": table,
        "movers": movers,
        "leader": table[0] if table else None,
    }


# ─── Компактные payload-ы для модели ───────────────────────────────────────
# Полный payload нужен шаблонам и картинке; модели хватает сжатой выжимки —
# меньше входа, меньше соблазна пересказывать всё подряд.

def _preview_for_model(payload: dict) -> dict:
    def side(team: dict) -> dict:
        return {
            "club": team["name"],
            "pos": team.get("position"),
            "pts": team.get("points"),
            "form": "".join(team.get("form") or []),
        }

    fixtures = []
    for f in payload["fixtures"]:
        item = {"home": side(f["team1"]), "away": side(f["team2"])}
        pr = f.get("prediction")
        if pr:
            item["p1_x_p2"] = [pr["p1"], pr["px"], pr["p2"]]
            item["xg"] = f"{pr['xg1']}:{pr['xg2']}"
        fixtures.append(item)
    return {
        "division": payload["division_name"],
        "round": payload["round_number"],
        "fixtures": fixtures,
        "match_of_the_round": payload.get("match_of_the_round"),
    }


def _digest_for_model(payload: dict) -> dict:
    # Полную таблицу читатель видит на картинке — модели хватит лидеров и движения.
    return {
        "division": payload["division_name"],
        "round": payload["round_number"],
        "matches_played": payload["matches_played"],
        "matches_total": payload["matches_total"],
        "goals_total": payload["goals_total"],
        "results": [
            {k: r[k] for k in ("team1", "score1", "score2", "team2", "mvp_player")}
            for r in payload["results"]
        ],
        "rout": payload.get("rout"),
        "player_of_the_round": payload.get("player_of_the_round"),
        "mvp_of_the_round": payload.get("mvp_of_the_round"),
        "top3": [
            {k: t[k] for k in ("position", "team", "points")}
            for t in payload["table"][:3]
        ],
        "movers": [
            {k: t[k] for k in ("team", "previous_position", "position")}
            for t in payload["movers"]
        ],
    }


# ─── Telegram HTML: санитайз и обрезка без поломки разметки ─────────────────
# Пост уходит с parse_mode=HTML: незакрытый <b>, обрезанный посередине тег или
# голый «<» дают BadRequest, пост не записывается, и джоба заново дёргает Gemini.

_ALLOWED_TAG_RE = re.compile(r"</?(b|i|u|s|code)>")


def _sanitize_html(text: str) -> str:
    """Keep only <b>/<i>/<u>/<s>/<code>, escape everything else, balance the tags."""
    out, stack, pos = [], [], 0

    def plain(chunk: str) -> str:
        return html.escape(html.unescape(chunk), quote=False)

    for m in _ALLOWED_TAG_RE.finditer(text):
        out.append(plain(text[pos:m.start()]))
        pos = m.end()
        tag = m.group(1)
        if not m.group(0).startswith("</"):
            stack.append(tag)
            out.append(m.group(0))
        elif tag in stack:
            while stack:
                opened = stack.pop()
                out.append(f"</{opened}>")
                if opened == tag:
                    break
        # A stray closing tag is dropped.
    out.append(plain(text[pos:]))
    out.extend(f"</{t}>" for t in reversed(stack))
    return "".join(out)


def _fit_html(text: str, limit: int) -> str:
    """Sanitize, and if too long cut at a line or sentence end — never inside a tag."""
    text = _sanitize_html(text)
    if len(text) <= limit:
        return text

    # Запас под закрывающие теги, которые допишет повторный санитайз.
    cut = text[: limit - 30]
    line_end = cut.rfind("\n")
    if line_end >= len(cut) // 2:
        cut = cut[:line_end]
    else:
        sentence_end = max(cut.rfind(ch) for ch in (".", "!", "?", "…"))
        if sentence_end >= len(cut) // 2:
            cut = cut[: sentence_end + 1]
    # Не оставлять половину тега или сущности на конце.
    if cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]
    if cut.rfind("&") > cut.rfind(";"):
        cut = cut[: cut.rfind("&")]
    return _sanitize_html(cut.rstrip())


# ─── Gemini text generation ────────────────────────────────────────────────

_COMMON_RULES = (
    "ЖЁСТКИЕ ПРАВИЛА (нарушать нельзя):\n"
    "- Используй ТОЛЬКО те числа, которые есть в переданном JSON. Ничего не досчитывай и не выдумывай: "
    "ни очков, ни счетов, ни процентов, ни имён игроков и клубов, которых нет в данных.\n"
    "- Если какого-то показателя в данных нет (null) — просто не упоминай его.\n"
    "- Разметка только Telegram HTML: <b>жирный</b>, <i>курсив</i>. Никакого Markdown, никаких ** и #.\n"
    "- Пиши по-русски, живо, но коротко: без вступлений, воды и «дисклеймеров».\n"
)

_PREVIEW_INSTRUCTION = (
    "Ты — Темшик, аналитик и голос лиги «Логово Фифарей»: душевный 30+ мужик, батейный юмор, "
    "но по цифрам — строгий аналитик.\n\n"
    "ЗАДАЧА: короткое превью тура для топика АНАЛИТИКА по переданному JSON.\n"
    "СТРУКТУРА:\n"
    "1. Заголовок одной строкой: номер тура и дивизион.\n"
    "2. По каждой паре РОВНО ОДНА строка: ⚽️ <b>Хозяева</b> — <b>Гости</b> · П1/Х/П2 в процентах · "
    "xG как ожидаемый счёт · максимум 3-5 слов своей оценки (по месту, очкам или форме).\n"
    "3. «🔥 Матч тура» — одно предложение, почему эта пара.\n"
    "4. Концовка — одна короткая фраза.\n\n"
    f"{_COMMON_RULES}"
    f"- Весь пост — не больше {PREVIEW_TARGET_CHARS} символов.\n"
)

_DIGEST_INSTRUCTION = (
    "Ты — Темшик, аналитик и голос лиги «Логово Фифарей»: душевный 30+ мужик, батейный юмор, "
    "но по цифрам — строгий аналитик.\n\n"
    "ЗАДАЧА: написать ПОДПИСЬ к картинке с итогами тура по переданному JSON. "
    "Картинку читатель уже видит: там результаты с игроками матчей, игрок тура и движение по таблице — "
    "не пересказывай их построчно, а выдели главное.\n"
    "СТРУКТУРА: заголовок с номером тура → 3-5 строк про самое интересное "
    "(разгром тура, игрок тура, кто взлетел и кто просел, лидер) → короткая концовка.\n\n"
    f"{_COMMON_RULES}"
    f"- Уложись в {CAPTION_MAX_CHARS} символов — это подпись к фото, лимит Telegram жёсткий.\n"
)


def _call_gemini(system_text: str, payload: dict, max_output_tokens: int, api_key: str | None = None) -> str | None:
    """Один вызов Gemini по той же механике, что и services/ai/ai_chat.py."""
    keys_to_try = get_ordered_chat_keys(api_key)
    if not keys_to_try:
        logger.warning("Round analytics: GEMINI_CHAT_API_KEY is not set, falling back to a template.")
        return None

    request_payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": "ДАННЫЕ (JSON):\n" + json.dumps(payload, ensure_ascii=False)}]
        }],
        "generationConfig": {
            "temperature": 0.75,
            "maxOutputTokens": max_output_tokens,
        }
    }
    payload_bytes = json.dumps(request_payload).encode("utf-8")

    from services.ai.ai_recognizer import _get_gemini_opener
    opener = _get_gemini_opener()
    base_url = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")

    for model_name in get_ordered_chat_models():
        for target_key in keys_to_try:
            url = f"{base_url}/v1beta/models/{model_name}:generateContent?key={target_key}"
            req = urllib.request.Request(
                url,
                data=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                }
            )
            try:
                with opener.open(req, timeout=30) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not result.get("candidates"):
                    logger.warning(f"Round analytics: no candidates from '{model_name}'.")
                    continue
                text = result["candidates"][0]["content"]["parts"][0]["text"]
                return text.replace("**", "").replace("##", "").strip()
            except urllib.error.HTTPError as e:
                key_suffix = f"...{target_key[-4:]}" if len(target_key) > 4 else "***"
                logger.warning(f"Round analytics: model '{model_name}' (key {key_suffix}) HTTP {e.code}, trying fallback.")
                if e.code in (400, 403, 404, 429, 503):
                    continue
                continue
            except Exception:
                logger.exception(f"Round analytics: unexpected error calling model '{model_name}'.")
                continue

    return None


def generate_preview_text(payload: dict) -> str:
    text = _call_gemini(_PREVIEW_INSTRUCTION, _preview_for_model(payload), max_output_tokens=900)
    if not text:
        return _fallback_preview_text(payload)
    return _fit_html(text, PREVIEW_MAX_CHARS)


def generate_digest_caption(payload: dict) -> str:
    text = _call_gemini(_DIGEST_INSTRUCTION, _digest_for_model(payload), max_output_tokens=500)
    if not text:
        return _fallback_digest_caption(payload)
    return _fit_html(text, CAPTION_MAX_CHARS)


# ─── Фолбэки (Gemini недоступен — тур всё равно получает публикацию) ────────

def _fallback_preview_text(payload: dict) -> str:
    lines = [
        f"📈 <b>ПРЕВЬЮ ТУРА {payload['round_number']}</b>",
        f"<i>{html.escape(str(payload['division_name']))}</i>",
        ""
    ]
    for f in payload["fixtures"]:
        t1 = html.escape(f["team1"]["name"])
        t2 = html.escape(f["team2"]["name"])
        pr = f["prediction"]
        if pr:
            lines.append(f"⚽️ <b>{t1}</b> — <b>{t2}</b>")
            lines.append(f"    {pr['p1']}% / {pr['px']}% / {pr['p2']}%  ·  xG {pr['xg1']}:{pr['xg2']}")
        else:
            lines.append(f"⚽️ <b>{t1}</b> — <b>{t2}</b>")

    motr = payload.get("match_of_the_round")
    if motr:
        lines.append("")
        lines.append(f"🔥 <b>Матч тура:</b> {html.escape(motr['team1'])} — {html.escape(motr['team2'])}")

    return _fit_html("\n".join(lines), PREVIEW_MAX_CHARS)


def _fallback_digest_caption(payload: dict) -> str:
    lines = [f"📈 <b>ИТОГИ ТУРА {payload['round_number']}</b>"]
    lines.append(f"Сыграно матчей: {payload['matches_played']} · голов: {payload['goals_total']}")

    rout = payload.get("rout")
    if rout:
        lines.append(f"💥 Разгром тура: {html.escape(rout['team1'])} {rout['score1']}:{rout['score2']} {html.escape(rout['team2'])}")

    potr = payload.get("player_of_the_round")
    if potr:
        lines.append(f"⭐ Игрок тура: {html.escape(str(potr['player_name']))} ({potr['goals']}+{potr['assists']})")

    leader = payload.get("leader")
    if leader:
        lines.append(f"👑 Лидер: {html.escape(str(leader['team']))} — {leader['points']} очков")

    return _fit_html("\n".join(lines), CAPTION_MAX_CHARS)
