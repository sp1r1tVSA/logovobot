"""
services/club_smm_service.py

Персональный SMM-генератор постов для Telegram-канала о клубе (ФК «Бешикташ» и др.).

Архитектура:
  1. ТЕКСТ ПОСТОВ: Бесплатные модели OpenRouter (Llama 3.3 70B, Qwen 2.5 72B,
     Mistral Small 24B, DeepSeek R1) и NVIDIA NIM с ротацией и автоматическим
     фолбэком на Gemini и шаблонную аналитику.
  2. ГЕНЕРАЦИЯ ФОТО / АРТОВ: Модели Gemini Image (gemini-3.1-flash-image,
     gemini-2.5-flash-image, imagen-3.0) + фолбэк на графическую карточку клуба.
  3. ДАННЫЕ: Актуальная статистика из SQLite (положение в дивизионе, форма,
     последний и предстоящий матчи, авторы голов/ассистов, MVP).
"""

import base64
import html
import io
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request

import config
import database
from time_utils import now_msk_str
from club_registry import resolve_team_name, teams_match
from services.graphics import club_card_generator

logger = logging.getLogger(__name__)

POST_TARGET_CHARS = 1600
POST_MAX_CHARS = 3800
CAPTION_MAX_CHARS = 1000

# ─── Ротация ключей и моделей ───────────────────────────────────────────────

_openrouter_model_idx = 0
_openrouter_lock = threading.Lock()

def get_ordered_openrouter_models() -> list[str]:
    """Возвращает список бесплатных моделей OpenRouter с ротацией Round-Robin."""
    models = getattr(config, "OPENROUTER_SMM_MODELS", [
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-72b-instruct:free",
        "mistralai/mistral-small-24b-instruct-2501:free",
        "deepseek/deepseek-r1:free",
        "google/gemini-2.0-flash-exp:free",
    ])
    if not models:
        return ["meta-llama/llama-3.3-70b-instruct:free"]
    global _openrouter_model_idx
    with _openrouter_lock:
        idx = _openrouter_model_idx % len(models)
        _openrouter_model_idx += 1
        return models[idx:] + models[:idx]


_nvidia_model_idx = 0
_nvidia_lock = threading.Lock()

def get_ordered_nvidia_models() -> list[str]:
    """Возвращает список моделей NVIDIA NIM с ротацией Round-Robin."""
    models = getattr(config, "NVIDIA_SMM_MODELS", [
        "meta/llama-3.3-70b-instruct",
        "deepseek-ai/deepseek-r1",
        "qwen/qwen2.5-72b-instruct",
        "mistralai/mistral-large-2-instruct",
    ])
    if not models:
        return ["meta/llama-3.3-70b-instruct"]
    global _nvidia_model_idx
    with _nvidia_lock:
        idx = _nvidia_model_idx % len(models)
        _nvidia_model_idx += 1
        return models[idx:] + models[:idx]


_gemini_key_idx = 0
_gemini_key_lock = threading.Lock()

def get_ordered_gemini_keys() -> list[str]:
    """Возвращает список ключей Gemini для генерации фото и текста."""
    keys = getattr(config, "GEMINI_SMM_API_KEYS", [])
    if not keys:
        single = (getattr(config, "GEMINI_SMM_API_KEY", "") or "").strip()
        keys = [k.strip() for k in single.split(",") if k.strip()]
    if not keys:
        keys = getattr(config, "GEMINI_CHAT_API_KEYS", []) or getattr(config, "GEMINI_API_KEYS", [])
    if not keys:
        return []
    if len(keys) == 1:
        return keys
    global _gemini_key_idx
    with _gemini_key_lock:
        idx = _gemini_key_idx % len(keys)
        _gemini_key_idx += 1
        return keys[idx:] + keys[:idx]


# ─── Telegram HTML Sanitizing ───────────────────────────────────────────────

_ALLOWED_TAG_RE = re.compile(r"</?(b|i|u|s|code|a(?:\s+href=\"[^\"]+\")?)>")

def _sanitize_html(text: str) -> str:
    """Очищает HTML для Telegram, сохраняя разрешенные теги и закрывая открытые."""
    out, stack, pos = [], [], 0

    def plain(chunk: str) -> str:
        return html.escape(html.unescape(chunk), quote=False)

    for m in _ALLOWED_TAG_RE.finditer(text):
        out.append(plain(text[pos:m.start()]))
        pos = m.end()
        full_tag = m.group(0)
        tag_name = m.group(1).split()[0]

        if not full_tag.startswith("</"):
            stack.append(tag_name)
            out.append(full_tag)
        elif tag_name in stack:
            while stack:
                opened = stack.pop()
                out.append(f"</{opened}>")
                if opened == tag_name:
                    break
    out.append(plain(text[pos:]))
    out.extend(f"</{t}>" for t in reversed(stack))
    return "".join(out)


def _fit_html(text: str, limit: int) -> str:
    """Санитизирует и обрезает текст по предложению без повреждения HTML-тегов."""
    text = _sanitize_html(text)
    if len(text) <= limit:
        return text

    cut = text[: limit - 40]
    line_end = cut.rfind("\n")
    if line_end >= len(cut) // 2:
        cut = cut[:line_end]
    else:
        sentence_matches = list(re.finditer(r'[.!?…]+(?=\s|$)', cut))
        if sentence_matches and sentence_matches[-1].end() >= len(cut) // 2:
            cut = cut[: sentence_matches[-1].end()]

    if cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]
    if cut.rfind("&") > cut.rfind(";"):
        cut = cut[: cut.rfind("&")]
    return _sanitize_html(cut.rstrip())


def _trim_to_last_sentence(text: str) -> str:
    matches = list(re.finditer(r'[.!?…]+(?=\s|$)', text))
    if matches:
        cut = matches[-1].end()
        if cut >= len(text) // 3:
            return text[:cut].strip()
    return text.rstrip(" ,;:—-") + "…"


# ─── Сбор данных клуба из SQLite ───────────────────────────────────────────

def get_club_smm_payload(team_name: str) -> dict:
    """Сбор статистики, формы и матчей клуба из базы данных."""
    canon = resolve_team_name(team_name) or team_name.strip()
    card_data = database.get_club_card_data(canon)
    division_id = card_data.get("division_id")

    div_name = f"{division_id} дивизион" if division_id else "Лига"
    if division_id:
        div_row = database.get_division(division_id)
        if div_row and div_row.get("name"):
            div_name = div_row["name"]

    last_match_data = None
    next_match_data = None

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Последний матч
        cursor.execute("""
            SELECT id, round_number, player1_team, player2_team, player1_score, player2_score,
                   status, played_at, mvp_player, tournament_type, cup_stage
            FROM matches
            WHERE (player1_team = ? OR player2_team = ?)
              AND status IN ('confirmed', 'completed')
            ORDER BY id DESC LIMIT 1
        """, (canon, canon))
        m_row = cursor.fetchone()

        if m_row:
            m_id = m_row["id"]
            is_p1 = teams_match(m_row["player1_team"], canon)
            my_score = m_row["player1_score"] if is_p1 else m_row["player2_score"]
            opp_score = m_row["player2_score"] if is_p1 else m_row["player1_score"]
            opponent = m_row["player2_team"] if is_p1 else m_row["player1_team"]

            result_type = "win" if my_score > opp_score else ("draw" if my_score == opp_score else "loss")

            cursor.execute("""
                SELECT team_name, player_name, event_type, count
                FROM match_events
                WHERE match_id = ?
            """, (m_id,))
            events = cursor.fetchall()

            my_goals, my_assists, opp_goals = [], [], []
            for ev in events:
                p_name = ev["player_name"]
                cnt = ev["count"] or 1
                if teams_match(ev["team_name"], canon):
                    if ev["event_type"] == "goal":
                        my_goals.append({"player": p_name, "count": cnt})
                    elif ev["event_type"] == "assist":
                        my_assists.append({"player": p_name, "count": cnt})
                else:
                    if ev["event_type"] == "goal":
                        opp_goals.append({"player": p_name, "count": cnt})

            last_match_data = {
                "match_id": m_id,
                "round": m_row["round_number"],
                "stage": m_row["cup_stage"] if m_row["tournament_type"] == "cup" else None,
                "opponent": opponent,
                "is_home": is_p1,
                "my_score": my_score,
                "opp_score": opp_score,
                "result": result_type,
                "mvp_player": m_row["mvp_player"],
                "club_goals": my_goals,
                "club_assists": my_assists,
                "opp_goals": opp_goals,
                "played_at": m_row["played_at"],
            }

        # 2. Следующий матч
        cursor.execute("""
            SELECT id, round_number, player1_team, player2_team, tournament_type, cup_stage,
                   match_date, match_time
            FROM matches
            WHERE (player1_team = ? OR player2_team = ?)
              AND status = 'pending'
            ORDER BY round_number ASC, id ASC LIMIT 1
        """, (canon, canon))
        next_row = cursor.fetchone()

        if next_row:
            is_p1 = teams_match(next_row["player1_team"], canon)
            opp_name = next_row["player2_team"] if is_p1 else next_row["player1_team"]
            opp_stats = None
            if division_id:
                standings = database.get_standings(division_id=division_id)
                for rank, row in enumerate(standings, 1):
                    if teams_match(row["team_name"], opp_name):
                        opp_stats = {
                            "rank": rank,
                            "points": row["points"],
                            "wins": row["wins"],
                            "draws": row["draws"],
                            "losses": row["losses"],
                        }
                        break

            next_match_data = {
                "match_id": next_row["id"],
                "round": next_row["round_number"],
                "stage": next_row["cup_stage"] if next_row["tournament_type"] == "cup" else None,
                "opponent": opp_name,
                "is_home": is_p1,
                "opponent_stats": opp_stats,
                "scheduled_date": next_row["match_date"],
                "scheduled_time": next_row["match_time"],
            }

    is_besiktas = "бешикташ" in canon.lower() or "besiktas" in canon.lower()
    club_identity = {
        "name": canon,
        "nickname": "«Чёрные орлы» (Kara Kartallar)" if is_besiktas else f"ФК «{canon}»",
        "colors": "Чёрно-белые ⚪⚫" if is_besiktas else "Клубные цвета",
        "emojis": "🦅⚪⚫" if is_besiktas else "⚽🔥",
        "hashtags": ["#Besiktas", "#KaraKartal", "#ЛоговоФифарей"] if is_besiktas else [f"#{canon.replace(' ', '')}", "#ЛоговоФифарей"],
    }

    return {
        "club": club_identity,
        "manager": card_data.get("manager"),
        "division": {
            "id": division_id,
            "name": div_name,
        },
        "standings": card_data.get("league_stats"),
        "recent_form": card_data.get("recent_form", []),
        "cup": card_data.get("cup_stats"),
        "top_scorers": card_data.get("top_scorers", []),
        "top_assists": card_data.get("top_assists", []),
        "squad_sample": (card_data.get("squad") or [])[:12],
        "last_match": last_match_data,
        "next_match": next_match_data,
        "generated_at": now_msk_str(),
    }


# ─── Промпты для текстовых моделей ──────────────────────────────────────────

_SMM_BASE_INSTRUCTION = (
    "Ты — персональный пресс-атташе и SMM-менеджер футбольного клуба {club_name} в турнире «Логово Фифарей».\n"
    "Твой канал посвящён исключительно нашему клубу, его матчам, победам, игрокам и борьбе за трофеи.\n"
    "Главный тренер команды: {manager_name}.\n\n"
    "ТОНАЛЬНОСТЬ И СТИЛЬ:\n"
    "- Боевой, страстный, фанатский, энергичный дух («Вперёд, Орлы!», «Только победа!»).\n"
    "- Живой спортивный язык (без канцелярита и скучных отчётов).\n"
    "- Акцент на характер, яркие моменты, красивую игру лидеров.\n"
    "- Обязательно используй клубные эмодзи {club_emojis}.\n"
    "- В конце добавь 2-4 клубных хэштега (например: {club_hashtags}).\n\n"
    "ФОРМАТИРОВАНИЕ (СТРОГО):\n"
    "- Используй ТОЛЬКО Telegram HTML: <b>жирный</b>, <i>курсив</i>, <code>код</code>. "
    "Никакого Markdown! Запрещены символы ** и решётки # в качестве заголовков.\n"
    "- Достоверность: используй ТОЛЬКО те цифры, авторов голов, счёта и соперников, которые переданы в JSON. "
    "Ничего не выдумывай от себя.\n"
)


def _build_system_instruction(payload: dict) -> str:
    club = payload.get("club", {})
    mgr = payload.get("manager") or {}
    mgr_name = f"@{mgr['username']}" if mgr.get("username") else (mgr.get("name") or "@sp1r1tVSA")
    hashtags = " ".join(club.get("hashtags", ["#ЛоговоФифарей"]))

    return _SMM_BASE_INSTRUCTION.format(
        club_name=club.get("name", "Бешикташ"),
        manager_name=mgr_name,
        club_emojis=club.get("emojis", "🦅⚪⚫"),
        club_hashtags=hashtags,
    )


def _get_task_instruction(post_type: str, custom_brief: str = "", for_caption: bool = False) -> str:
    length_rule = (
        f"ДЛИНА: не более {CAPTION_MAX_CHARS} символов (это подпись к фото, пиши ёмко и ярко)."
        if for_caption
        else f"ДЛИНА: около {POST_TARGET_CHARS} символов, 3-5 абзацев с отличной динамикой."
    )

    if post_type == "matchday":
        return (
            "ЗАДАЧА: Напиши зажигательный пост-анонс MATCHDAY (предстоящего матча)!\n"
            "СТРУКТУРА:\n"
            "1. Заголовок MATCHDAY с соперником и турниром.\n"
            "2. Что на кону: турнирная ситуация в дивизионе, очки, форма команд.\n"
            "3. Ключевые персоны: кто должен повести за собой команду в атаке.\n"
            "4. Пламенный призыв к болельщикам поддержать парней!\n"
            f"{length_rule}"
        )
    elif post_type == "recap":
        return (
            "ЗАДАЧА: Напиши яркий пост с итогами последнего сыгранного матча (MATCH RECAP)!\n"
            "СТРУКТУРА:\n"
            "1. Заголовок с результатом и итоговым счётом.\n"
            "2. Ход матча: кто забивал, кто ассистировал, автор решающего гола.\n"
            "3. Лучший игрок (MVP) встречи и его вклад.\n"
            "4. Что этот результат значит для нашего положения в таблице.\n"
            f"{length_rule}"
        )
    elif post_type == "standings":
        return (
            "ЗАДАЧА: Напиши обзор текущего положения клуба в дивизионе и формы команды!\n"
            "СТРУКТУРА:\n"
            "1. Заголовок: текущее место и набранные очки.\n"
            "2. Статистика: победы, ничьи, поражения, разница мячей, победная/беспроигрышная серия.\n"
            "3. Оценка шансов на повышение / борьбу за титул.\n"
            "4. Вдохновляющий посыл на следующие туры.\n"
            f"{length_rule}"
        )
    elif post_type == "spotlight":
        return (
            "ЗАДАЧА: Напиши пост-профайл о лидере команды и лучшем игроке клуба!\n"
            "СТРУКТУРА:\n"
            "1. Заголовок с именем звезды клуба.\n"
            "2. Его статистика: голы, ассисты, MVP-награды.\n"
            "3. За что болельщики любят этого футболиста и его влияние на результаты.\n"
            "4. Пожелание продолжать разрывать соперников.\n"
            f"{length_rule}"
        )
    else:  # custom
        brief_text = f"БРИФ / ТЕМА ОТ ТРЕНЕРА:\n{custom_brief}\n\n" if custom_brief else ""
        return (
            f"ЗАДАЧА: Напиши клубный пост по теме, заданной тренером, опираясь на реальную статистику клуба!\n"
            f"{brief_text}"
            f"{length_rule}"
        )


# ─── Провайдер 1: OpenRouter (Бесплатные нейронки) ──────────────────────────

def _call_openrouter_text(system_text: str, user_text: str, max_tokens: int) -> tuple[str | None, str | None]:
    """Генерация текста через цепочку бесплатных моделей OpenRouter."""
    api_key = getattr(config, "OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None, None

    base_url = getattr(config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    models = get_ordered_openrouter_models()

    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.8,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://logovobot.ru",
                "X-Title": "Logovobot Club SMM",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            msg = choice.get("message", {})
            text = msg.get("content") or msg.get("reasoning") or ""
            if text and len(text.strip()) > 40:
                clean = text.replace("**", "").replace("#", "")
                return clean.strip(), model
        except urllib.error.HTTPError as e:
            logger.warning(f"OpenRouter SMM: model '{model}' HTTP {e.code}, trying next free model...")
            continue
        except Exception as e:
            logger.warning(f"OpenRouter SMM: model '{model}' failed: {e}")
            continue

    return None, None


# ─── Провайдер 2: NVIDIA NIM (build.nvidia.com) ─────────────────────────────

def _call_nvidia_text(system_text: str, user_text: str, max_tokens: int) -> tuple[str | None, str | None]:
    """Генерация текста через бесплатный API NVIDIA NIM."""
    api_key = getattr(config, "NVIDIA_API_KEY", "").strip()
    if not api_key:
        return None, None

    base_url = "https://integrate.api.nvidia.com/v1"
    models = get_ordered_nvidia_models()

    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.8,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = data["choices"][0]
            text = choice.get("message", {}).get("content", "")
            if text and len(text.strip()) > 40:
                clean = text.replace("**", "").replace("#", "")
                return clean.strip(), model
        except Exception as e:
            logger.warning(f"NVIDIA SMM: model '{model}' failed: {e}")
            continue

    return None, None


# ─── Провайдер 3: Gemini Fallback для текста ────────────────────────────────

def _call_gemini_text(system_text: str, user_text: str, max_tokens: int, audio_bytes: bytes = None) -> tuple[str | None, str | None]:
    """Резервная генерация текста через Gemini."""
    keys = get_ordered_gemini_keys()
    if not keys:
        return None, None

    from services.ai.ai_recognizer import _get_gemini_opener
    opener = _get_gemini_opener()
    base_url = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
    models = getattr(config, "GEMINI_SMM_MODELS", ["gemini-2.5-flash", "gemini-3.5-flash-lite"])

    user_parts = []
    if audio_bytes:
        user_parts.append({
            "inline_data": {
                "mime_type": "audio/ogg",
                "data": base64.b64encode(audio_bytes).decode("utf-8"),
            }
        })
    user_parts.append({"text": user_text})

    payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": max_tokens},
    }
    body = json.dumps(payload).encode("utf-8")

    for model in models:
        for key in keys:
            url = f"{base_url}/v1beta/models/{model}:generateContent?key={key}"
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Logovobot/SMM"}
            )
            try:
                with opener.open(req, timeout=25) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                candidate = res.get("candidates", [{}])[0]
                parts = candidate.get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if not text and parts:
                    text = parts[-1].get("text", "").strip()
                if text:
                    return text.replace("**", "").replace("#", ""), model
            except Exception:
                continue

    return None, None


# ─── Главная функция генерации текста ───────────────────────────────────────

def generate_club_post(
    team_name: str,
    post_type: str = "matchday",
    custom_brief: str = "",
    audio_bytes: bytes = None,
    audio_mime: str = "audio/ogg",
    for_caption: bool = False,
) -> str:
    """
    Генерирует текст поста.
    Цепочка исполнения:
      1. Бесплатные модели OpenRouter (Llama 3.3 70B, Qwen 2.5 72B, Mistral, DeepSeek)
      2. NVIDIA NIM (Llama 3.3, Qwen 2.5)
      3. Резерв Gemini
      4. Шаблонный аналитический пост из базы данных
    """
    payload = get_club_smm_payload(team_name)
    system_text = _build_system_instruction(payload)
    task_text = _get_task_instruction(post_type, custom_brief, for_caption)
    user_text = f"{task_text}\n\nАКТУАЛЬНЫЕ ДАННЫЕ КЛУБА (JSON):\n{json.dumps(payload, ensure_ascii=False)}"

    limit = CAPTION_MAX_CHARS if for_caption else POST_MAX_CHARS
    max_tokens = 1000 if for_caption else 2000

    # 1. Пробуем OpenRouter (бесплатные модели)
    text, model_name = _call_openrouter_text(system_text, user_text, max_tokens)
    if text:
        logger.info(f"Club SMM text generated via OpenRouter ({model_name})")
        return _fit_html(text, limit)

    # 2. Пробуем NVIDIA NIM
    text, model_name = _call_nvidia_text(system_text, user_text, max_tokens)
    if text:
        logger.info(f"Club SMM text generated via NVIDIA ({model_name})")
        return _fit_html(text, limit)

    # 3. Резерв: Gemini
    text, model_name = _call_gemini_text(system_text, user_text, max_tokens, audio_bytes=audio_bytes)
    if text:
        logger.info(f"Club SMM text generated via Gemini fallback ({model_name})")
        return _fit_html(text, limit)

    # 4. Фолбэк на шаблонную аналитику
    logger.warning("Club SMM: All AI providers failed. Using database stats template.")
    return _build_fallback_post(payload, post_type, for_caption)


# ─── Генерация фото через Gemini Image ──────────────────────────────────────

def generate_club_ai_photo(team_name: str, post_type: str = "matchday", custom_prompt: str = "") -> io.BytesIO | None:
    """
    Генерирует высококачественное спортивное фото / арт через Google Gemini Image API.
    Фолбэк: если API недоступен, генерирует карточку клуба.
    """
    canon = resolve_team_name(team_name) or team_name
    is_besiktas = "бешикташ" in canon.lower() or "besiktas" in canon.lower()

    if custom_prompt:
        prompt = f"Dynamic football poster, {custom_prompt}, professional sports media photography, 4k"
    elif post_type == "recap":
        prompt = (
            "Epic football match victory celebration poster for Besiktas JK with black and white colors, "
            "majestic eagle crest with glowing eyes, cheering stadium in Istanbul at night, golden confetti, "
            "dramatic stadium floodlights, highly detailed, photorealistic 8k" if is_besiktas else
            f"Epic football match victory celebration poster for {canon}, players cheering, stadium floodlights, dramatic atmosphere, 8k"
        )
    elif post_type == "matchday":
        prompt = (
            "Action matchday football poster for Besiktas JK, majestic black and white eagle soaring over roaring stadium, "
            "dramatic smoke, night game lights, dynamic angle, modern sports graphics style, 4k" if is_besiktas else
            f"Action matchday football poster for {canon}, stadium under lights, dramatic smoke, sports graphics, 4k"
        )
    elif post_type == "spotlight":
        prompt = (
            "Action sports portrait of a football forward striker in black and white kit striking a soccer ball, "
            "dramatic stadium background, motion blur, intense determination, cinematic sports photography" if is_besiktas else
            f"Action sports portrait of a star football player for {canon}, dynamic strike, stadium lights, cinematic"
        )
    else:  # standings / default
        prompt = (
            "Artistic 3D emblem of a black and white eagle rising over a football arena, neon stadium glow, "
            "cinematic championship atmosphere, high end sports banner" if is_besiktas else
            f"Artistic 3D emblem of football club {canon} in arena, championship atmosphere, cinematic"
        )

    keys = get_ordered_gemini_keys()
    models = getattr(config, "GEMINI_IMAGE_MODELS", [
        "gemini-3.1-flash-image",
        "gemini-2.5-flash-image",
        "imagen-3.0-generate-002",
    ])

    from services.ai.ai_recognizer import _get_gemini_opener
    opener = _get_gemini_opener()
    base_url = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")

    for model in models:
        for key in keys:
            # 1. Попытка через generateContent (gemini-3.1-flash-image / gemini-2.5-flash-image)
            if "imagen" not in model:
                url = f"{base_url}/v1beta/models/{model}:generateContent?key={key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseModalities": ["IMAGE"]},
                }
                body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "Logovobot/Image"}
                )
                try:
                    with opener.open(req, timeout=35) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                    candidate = res.get("candidates", [{}])[0]
                    parts = candidate.get("content", {}).get("parts", [])
                    for p in parts:
                        inline = p.get("inlineData") or p.get("inline_data")
                        if inline and inline.get("data"):
                            img_bytes = base64.b64decode(inline["data"])
                            buf = io.BytesIO(img_bytes)
                            buf.seek(0)
                            logger.info(f"Club SMM: Image successfully generated with {model}")
                            return buf
                except Exception as e:
                    logger.warning(f"Gemini Image generateContent '{model}' failed: {e}")
                    continue

            # 2. Попытка через :predict (imagen-3.0-generate-002)
            else:
                url = f"{base_url}/v1beta/models/{model}:predict?key={key}"
                payload = {
                    "instances": [{"prompt": prompt}],
                    "parameters": {"sampleCount": 1, "aspectRatio": "1:1"}
                }
                body = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "Logovobot/Image"}
                )
                try:
                    with opener.open(req, timeout=35) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                    preds = res.get("predictions", [])
                    if preds and preds[0].get("bytesBase64Encoded"):
                        img_bytes = base64.b64decode(preds[0]["bytesBase64Encoded"])
                        buf = io.BytesIO(img_bytes)
                        buf.seek(0)
                        logger.info(f"Club SMM: Image successfully generated with {model}")
                        return buf
                except Exception as e:
                    logger.warning(f"Gemini Image predict '{model}' failed: {e}")
                    continue

    logger.warning("Gemini Image generation unavailable. Falling back to club card graphic.")
    return generate_club_smm_media(team_name)


# ─── Фолбэк на шаблонную аналитику ──────────────────────────────────────────

def _build_fallback_post(payload: dict, post_type: str, for_caption: bool = False) -> str:
    """Шаблонный аналитический пост из базы данных."""
    club = payload.get("club", {})
    canon = club.get("name", "Бешикташ")
    emojis = club.get("emojis", "🦅⚪⚫")
    hashtags = " ".join(club.get("hashtags", ["#Besiktas", "#ЛоговоФифарей"]))
    st = payload.get("standings") or {}
    last_m = payload.get("last_match")
    next_m = payload.get("next_match")

    if post_type == "recap" and last_m:
        res_emoji = "✅ ПОБЕДА!" if last_m["result"] == "win" else ("🤝 НИЧЬЯ" if last_m["result"] == "draw" else "⚡ РЕЗУЛЬТАТ")
        score_line = f"{canon} {last_m['my_score']} : {last_m['opp_score']} {last_m['opponent']}"
        scorers = ", ".join(f"{g['player']} ({g['count']})" for g in last_m["club_goals"]) or "—"
        mvp = f"\n⭐ <b>MVP матча:</b> {last_m['mvp_player']}" if last_m.get("mvp_player") else ""
        return (
            f"{emojis} <b>MATCH RECAP: {res_emoji}</b>\n\n"
            f"📊 <b>Счёт:</b> <code>{score_line}</code>\n"
            f"⚽ <b>Голы:</b> {scorers}{mvp}\n\n"
            f"Парни выложились на все сто процентов. Двигаемся дальше по турнирной сетке!\n\n"
            f"{hashtags}"
        )

    if post_type == "matchday" and next_m:
        opp = next_m["opponent"]
        tour = f"Тур {next_m['round']}" if next_m.get("round", 0) > 0 else (next_m.get("stage") or "Кубок")
        return (
            f"{emojis} <b>MATCHDAY! ВРЕМЯ БИТВЫ!</b>\n\n"
            f"⚔️ <b>{canon}</b> — <b>{opp}</b>\n"
            f"🏆 <b>Турнир:</b> {payload.get('division', {}).get('name', 'Лига')} · {tour}\n\n"
            f"Очередной важнейший матч в борьбе за очки. Настрой только на победу, "
            f"орлы готовы показать свой лучший футбол на поле!\n\n"
            f"Поддержим родной клуб в комментариях! 🔥\n\n"
            f"{hashtags} #Matchday"
        )

    rank = st.get("rank", "—")
    pts = st.get("points", 0)
    w, d, l = st.get("wins", 0), st.get("draws", 0), st.get("losses", 0)
    diff = st.get("goal_diff", 0)
    top_p = (payload.get("top_scorers") or [{}])[0]
    top_name = top_p.get("player_name", "—")
    top_goals = top_p.get("goals", 0)

    return (
        f"{emojis} <b>ПОЛОЖЕНИЕ КЛУБА «{canon.upper()}»</b>\n\n"
        f"📍 <b>Место в дивизионе:</b> #{rank}\n"
        f"📈 <b>Очки:</b> {pts} (В: {w} | Н: {d} | П: {l})\n"
        f"🎯 <b>Разница мячей:</b> {diff:+d}\n"
        f"🔥 <b>Лучший бомбардир:</b> {top_name} ({top_goals} голов)\n\n"
        f"Сезон в самом разгаре — держим темп и идём к поставленным целям!\n\n"
        f"{hashtags}"
    )


# ─── Графическая карточка клуба ─────────────────────────────────────────────

def generate_club_smm_media(team_name: str) -> io.BytesIO | None:
    """Генерация карточки клуба через Pillow."""
    canon = resolve_team_name(team_name) or team_name
    card_data = database.get_club_card_data(canon)
    if not card_data:
        return None
    try:
        buf = club_card_generator.generate_club_card(
            data=card_data,
            avatar_path=None,
            division_id=card_data.get("division_id")
        )
        return buf
    except Exception as e:
        logger.exception(f"Club SMM: Failed to generate club card media: {e}")
        return None
