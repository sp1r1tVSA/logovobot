"""
services/club_smm_service.py

Персональный SMM-генератор постов для Telegram-канала о клубе (ФК «Бешикташ» и др.).

Архитектура:
  1. ТЕКСТ ПОСТОВ: Бесплатные модели OpenRouter (Llama 3.3 70B, Qwen 2.5 72B,
     Mistral Small 24B, DeepSeek R1) с ротацией и автоматическим фолбэком
     на бесплатные Gemini (Flash Lite 500 RPD) и шаблонную аналитику.
  2. ГЕНЕРАЦИЯ ФОТО / АРТОВ: Бесплатный Flux AI (Pollinations) + Gemini Image
     + фолбэк на графическую карточку клуба (Pillow Retina).
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

POST_TARGET_CHARS = 350
POST_MAX_CHARS = 700
CAPTION_MAX_CHARS = 450

# ─── Ротация ключей и моделей ───────────────────────────────────────────────

_openrouter_model_idx = 0
_openrouter_lock = threading.Lock()

DEPRECATED_OPENROUTER_MODELS = {
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-small-24b-instruct-2501:free",
    "deepseek/deepseek-r1:free",
}

_dead_openrouter_models: set[str] = set(DEPRECATED_OPENROUTER_MODELS)

GUARANTEED_OPENROUTER_MODELS = [
    "stealth/space-bunny-alpha",
    "openrouter/free",
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3.5-lightning:free",
    "google/gemma-4-26b-a4b-it:free",
]

def get_ordered_openrouter_models() -> list[str]:
    """Возвращает список актуальных бесплатных моделей OpenRouter с ротацией Round-Robin."""
    raw_models = getattr(config, "OPENROUTER_SMM_MODELS", []) or GUARANTEED_OPENROUTER_MODELS
    # Отсеиваем устаревшие и заведомо вернувшие 404 модели
    models = [m for m in raw_models if m not in _dead_openrouter_models]
    if not models:
        models = [m for m in GUARANTEED_OPENROUTER_MODELS if m not in _dead_openrouter_models]
    if not models:
        models = ["openrouter/free"]

    global _openrouter_model_idx
    with _openrouter_lock:
        idx = _openrouter_model_idx % len(models)
        _openrouter_model_idx += 1
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


_META_REASONING_PATTERNS = [
    r"^нужно\s+(?:ответить|написать|составить)",
    r"^задача:\s*написать",
    r"^требования:\s*",
    r"^пользователь\s+(?:просит|хочет|задал)",
    r"^главный\s+принцип:\s*",
    r"^в\s+задаче\s+явно",
    r"^(?:let's\s+think|i\s+need\s+to|the\s+user\s+wants|here\s+is\s+my\s+reasoning)",
    r"^мысли:\s*",
    r"^рассуждения:\s*",
]

def _is_meta_reasoning(text: str) -> bool:
    """Проверяет, не является ли текст утекшими рассуждениями / цепочкой мыслей модели."""
    if not text or not isinstance(text, str):
        return False
    lowered = text.strip().lower()
    for pattern in _META_REASONING_PATTERNS:
        if re.search(pattern, lowered, flags=re.MULTILINE):
            return True
    return False


def _clean_smm_text(raw_text: str | None) -> str:
    """
    Очищает текст от блоков рассуждений (<think>, <thought>, <reasoning>),
    markdown-символов жирности (**) и markdown-заголовков (# Заголовок),
    сохраняя клубные хэштеги (#Besiktas).
    """
    if not raw_text or not isinstance(raw_text, str):
        return ""
    # 1. Удаляем закрытые блоки рассуждений
    cleaned = re.sub(r"<(?:think|thought|reasoning)>.*?</(?:think|thought|reasoning)>", "", raw_text, flags=re.S | re.I)
    # 2. Удаляем незакрытый блок рассуждений, если модель прервалась по токенам
    cleaned = re.sub(r"<(?:think|thought|reasoning)>.*$", "", cleaned, flags=re.S | re.I)
    # 3. Убираем markdown жирный шрифт (**)
    cleaned = cleaned.replace("**", "")
    # 4. Убираем markdown заголовки (# Заголовок), сохраняя хэштеги (#Besiktas)
    lines = []
    for line in cleaned.splitlines():
        stripped_line = re.sub(r"^#{1,6}\s+", "", line)
        lines.append(stripped_line)
    cleaned = "\n".join(lines)
    return cleaned.strip()


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
    "Твой канал посвящён нашему клубу, его матчам, победам, игрокам и борьбе за трофеи.\n"
    "Главный тренер команды: {manager_name}.\n"
    "{user_context}\n"
    "СТРУКТУРА И ОБЪЁМ ПОСТА (СТРОГО):\n"
    "- 1 строка: яркий заголовок с эмодзи {club_emojis}.\n"
    "- Основной текст: ровно ОДИН плотный энергичный абзац (3-4 коротких предложения, суммарно около 300-400 символов). "
    "Никаких длинных сочинений, списков, пунктов или рассуждений.\n"
    "- В конце: 2-3 хэштега через пробел (например: {club_hashtags}).\n\n"
    "ТОНАЛЬНОСТЬ И СТИЛЬ:\n"
    "- Боевой, страстный, фанатский, энергичный дух («Вперёд, Орлы!», «Только победа!»).\n"
    "- Живой спортивный язык без канцелярита.\n"
    "- Обязательно используй клубные эмодзи {club_emojis}.\n\n"
    "ФОРМАТИРОВАНИЕ:\n"
    "- Используй ТОЛЬКО Telegram HTML: <b>жирный</b>, <i>курсив</i>, <code>код</code>. "
    "Никакого Markdown! Запрещены символы ** и решётки # в качестве заголовков.\n"
    "- Достоверность: используй ТОЛЬКО те цифры, авторов голов, счёта и соперников, которые переданы в JSON. "
    "Ничего не выдумывай от себя.\n"
    "- СТРОГО: выдавай СРАЗУ готовый текст поста для Telegram-канала без каких-либо служебных пояснений, мыслей и вступительных слов.\n"
)


def _build_system_instruction(payload: dict) -> str:
    club = payload.get("club", {})
    mgr = payload.get("manager") or {}
    req_user = payload.get("request_user", "")
    mgr_name = f"@{mgr['username']}" if mgr.get("username") else (mgr.get("name") or req_user or "@sp1r1tVSA")
    hashtags = " ".join(club.get("hashtags", ["#ЛоговоФифарей"]))
    user_ctx = f"Пользователь / тренер: {req_user}." if req_user else ""

    return _SMM_BASE_INSTRUCTION.format(
        club_name=club.get("name", "Бешикташ"),
        manager_name=mgr_name,
        club_emojis=club.get("emojis", "🦅⚪⚫"),
        club_hashtags=hashtags,
        user_context=user_ctx,
    )


def _get_task_instruction(post_type: str, custom_brief: str = "", for_caption: bool = False) -> str:
    format_rule = (
        "ТРЕБОВАНИЕ К ФОРМАТУ: 1 строка заголовок -> 1 плотный абзац (3-4 предложения, до 350-400 символов) -> хэштеги. "
        "Пиши сразу готовый текст поста."
    )

    if post_type == "matchday":
        return (
            "ЗАДАЧА: Напиши короткий боевой анонс MATCHDAY.\n"
            "Суть: соперник, турнир, важность победы и призыв поддержать орлов.\n"
            f"{format_rule}"
        )
    elif post_type == "recap":
        return (
            "ЗАДАЧА: Напиши короткие итоги последнего матча.\n"
            "Суть: итоговый счёт, кто забил/MVP и победные эмоции команды.\n"
            f"{format_rule}"
        )
    elif post_type == "standings":
        return (
            "ЗАДАЧА: Напиши короткий обзор таблицы и формы команды.\n"
            "Суть: место в дивизионе, очки, серия/форма и настрой рвать дальше.\n"
            f"{format_rule}"
        )
    elif post_type == "spotlight":
        return (
            "ЗАДАЧА: Напиши короткий пост о лидере команды.\n"
            "Суть: имя звезды клуба, голы/ассисты и влияние на игру.\n"
            f"{format_rule}"
        )
    else:  # custom
        brief_text = f"ТЕМА ПОСТА ОТ ТРЕНЕРА:\n{custom_brief}\n\n" if custom_brief else ""
        return (
            "ЗАДАЧА: Напиши короткий клубный пост для публикации по теме тренера.\n"
            f"{brief_text}"
            f"{format_rule}"
        )


# ─── Провайдер 1: OpenRouter (Бесплатные нейронки) ──────────────────────────

def _call_openrouter_text(system_text: str, user_text: str, max_tokens: int) -> tuple[str | None, str | None]:
    """Генерация текста через цепочку бесплатных моделей OpenRouter."""
    api_key = getattr(config, "OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None, None

    base_url = getattr(config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    models = get_ordered_openrouter_models()
    budget_tokens = max(max_tokens, 1500)

    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.8,
            "max_tokens": budget_tokens,
            "reasoning": {"effort": "none", "exclude": True},
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
            # Берем исключительно content, ни в коем случае не reasoning
            raw_text = msg.get("content") or ""
            clean = _clean_smm_text(raw_text)
            if clean and len(clean.strip()) > 30 and not _is_meta_reasoning(clean):
                return clean.strip(), model
            else:
                logger.warning(
                    f"OpenRouter SMM: model '{model}' returned empty or reasoning-only content (len={len(clean)}). Trying next..."
                )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _dead_openrouter_models.add(model)
                logger.warning(f"OpenRouter SMM: model '{model}' HTTP 404 (disabled from roster), trying next free model...")
            else:
                logger.warning(f"OpenRouter SMM: model '{model}' HTTP {e.code}, trying next free model...")
            continue
        except Exception as e:
            logger.warning(f"OpenRouter SMM: model '{model}' failed: {e}")
            continue

    # Резервная попытка через мета-модель openrouter/free, если все остальные вернули ошибки
    if "openrouter/free" not in models and "openrouter/free" not in _dead_openrouter_models:
        logger.info("OpenRouter SMM: attempting guaranteed fallback to 'openrouter/free'...")
        payload = {
            "model": "openrouter/free",
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.8,
            "max_tokens": budget_tokens,
            "reasoning": {"effort": "none", "exclude": True},
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
            raw_text = msg.get("content") or ""
            clean = _clean_smm_text(raw_text)
            if clean and len(clean.strip()) > 30 and not _is_meta_reasoning(clean):
                return clean.strip(), "openrouter/free"
        except Exception as e:
            logger.warning(f"OpenRouter SMM: fallback 'openrouter/free' failed: {e}")

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
    budget_tokens = max(max_tokens, 1500)

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
        "generationConfig": {
            "temperature": 0.8,
            "maxOutputTokens": budget_tokens,
            "thinkingConfig": {"thinkingBudget": 0},
        },
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
                if not res.get("candidates"):
                    continue
                candidate = res["candidates"][0]
                # Отфильтровываем служебные блоки мыслей (thought)
                parts = candidate.get("content", {}).get("parts", [])
                text_parts = [p.get("text", "") for p in parts if not p.get("thought")]
                text = "".join(text_parts).strip()
                if not text:
                    logger.warning(f"Gemini SMM: model '{model}' generated only thoughts, skipping...")
                    continue
                clean = _clean_smm_text(text)
                if clean and len(clean.strip()) > 30 and not _is_meta_reasoning(clean):
                    return clean.strip(), model
            except urllib.error.HTTPError as e:
                logger.warning(f"Gemini SMM: model '{model}' HTTP {e.code}, trying next...")
                continue
            except Exception as e:
                logger.warning(f"Gemini SMM: model '{model}' failed: {e}")
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
    user_name: str = "",
) -> str:
    """
    Генерирует текст поста.
    Цепочка исполнения:
      1. Бесплатные модели OpenRouter (Llama 3.3 70B, Qwen 2.5 72B, Mistral, DeepSeek)
      2. Бесплатные модели Gemini (Flash Lite 500 RPD)
      3. Шаблонный аналитический пост из базы данных
    """
    payload = get_club_smm_payload(team_name)
    if user_name:
        payload["request_user"] = user_name
    system_text = _build_system_instruction(payload)
    task_text = _get_task_instruction(post_type, custom_brief, for_caption)
    user_text = f"{task_text}\n\nАКТУАЛЬНЫЕ ДАННЫЕ КЛУБА (JSON):\n{json.dumps(payload, ensure_ascii=False)}"

    limit = CAPTION_MAX_CHARS if for_caption else POST_MAX_CHARS
    max_tokens = 220 if for_caption else 350

    # 1. Пробуем OpenRouter (бесплатные модели)
    text, model_name = _call_openrouter_text(system_text, user_text, max_tokens)
    if text:
        logger.info(f"Club SMM text generated via OpenRouter ({model_name})")
        return _fit_html(text, limit)

    # 2. Резерв: Gemini (бесплатные Flash Lite модели с квотой 500 запросов/день)
    text, model_name = _call_gemini_text(system_text, user_text, max_tokens, audio_bytes=audio_bytes)
    if text:
        logger.info(f"Club SMM text generated via Gemini ({model_name})")
        return _fit_html(text, limit)

    # 4. Фолбэк на шаблонную аналитику
    logger.warning("Club SMM: All AI providers failed. Using database stats template.")
    return _build_fallback_post(payload, post_type, for_caption, custom_brief=custom_brief)


# ─── Генерация фото через OpenRouter Image API ──────────────────────────────

def _call_openrouter_image(prompt: str) -> tuple[io.BytesIO | None, str | None]:
    """
    Генерирует изображение через OpenRouter Unified Image API (POST /api/v1/images).
    Использует OPENROUTER_API_KEY и модели (recraft/recraft-v4.1-flash, flux.2-klein-4b и др.).
    """
    api_key = getattr(config, "OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None, None

    models = getattr(config, "OPENROUTER_IMAGE_MODELS", [
        "inclusionai/ming-image-0.1-design",
        "recraft/recraft-v4.1-flash",
        "black-forest-labs/flux.2-klein-4b",
        "sourceful/riverflow-v2.5-fast",
        "recraft/recraft-v3",
    ])

    base_url = getattr(config, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    url = f"{base_url}/images"

    for model in models:
        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
        }
        if "ming" not in model:
            payload["aspect_ratio"] = "1:1"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://logovobot.local",
                "X-Title": "Logovobot Club SMM",
                "User-Agent": "Logovobot/Image",
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", [])
            for item in items:
                b64 = item.get("b64_json")
                if b64:
                    img_bytes = base64.b64decode(b64)
                    buf = io.BytesIO(img_bytes)
                    buf.seek(0)
                    logger.info(f"Club SMM: Image successfully generated with OpenRouter ({model})")
                    return buf, model
                img_url = item.get("url")
                if img_url:
                    img_req = urllib.request.Request(img_url, headers={"User-Agent": "Logovobot/Image"})
                    with urllib.request.urlopen(img_req, timeout=30) as img_resp:
                        img_bytes = img_resp.read()
                    buf = io.BytesIO(img_bytes)
                    buf.seek(0)
                    logger.info(f"Club SMM: Image downloaded from OpenRouter ({model})")
                    return buf, model
        except urllib.error.HTTPError as e:
            err_msg = ""
            try:
                err_msg = e.read().decode("utf-8")
            except Exception:
                pass
            logger.warning(f"OpenRouter Image: model '{model}' HTTP {e.code}: {err_msg}")
            continue
        except Exception as e:
            logger.warning(f"OpenRouter Image: model '{model}' error: {e}")
            continue

    return None, None


# ─── Генерация спортивных постеров и клубного арта ──────────────────────────

def generate_club_ai_photo(team_name: str, post_type: str = "matchday", custom_prompt: str = "") -> io.BytesIO | None:
    """
    Генерирует визуал клуба для SMM-поста.
    Использует Pillow Retina club card как основной и единственный надёжный метод.
    Попытка через OpenRouter Image API оставлена — активируется автоматически
    если OPENROUTER_API_KEY пополнен (>$0 баланса).
    """
    # 1. Попытка через OpenRouter Image API (активна только при наличии баланса)
    canon = resolve_team_name(team_name) or team_name
    is_besiktas = "бешикташ" in canon.lower() or "besiktas" in canon.lower()
    soccer_guard = (
        "European soccer association football, classic round soccer ball, "
        "green grass pitch, no helmets, no rugby, no american football pads"
    )
    if custom_prompt:
        prompt = (
            f"Epic European soccer matchday poster for Besiktas JK, {custom_prompt}, "
            f"majestic black eagle, black and white club colors, roaring soccer stadium floodlights, "
            f"{soccer_guard}, dynamic sports media photography, 4k" if is_besiktas else
            f"Epic European soccer match poster for {canon}, {custom_prompt}, "
            f"stadium floodlights, {soccer_guard}, dynamic sports graphics, 4k"
        )
    elif post_type == "recap":
        prompt = (
            "Epic European soccer match victory celebration poster for Besiktas JK with black and white colors, "
            "majestic black eagle crest with glowing eyes, cheering soccer stadium in Istanbul at night, golden confetti, "
            f"green grass pitch, {soccer_guard}, dramatic stadium floodlights, highly detailed, photorealistic 8k" if is_besiktas else
            f"Epic European soccer match victory celebration poster for {canon}, players cheering on green grass, stadium floodlights, {soccer_guard}, 8k"
        )
    elif post_type == "matchday":
        prompt = (
            "Epic European soccer matchday poster for Besiktas JK, majestic black eagle soaring over roaring soccer stadium, "
            f"green grass pitch, {soccer_guard}, dramatic smoke, night game lights, dynamic angle, modern sports graphics style, 4k" if is_besiktas else
            f"Epic European soccer matchday poster for {canon}, soccer stadium under lights, {soccer_guard}, dramatic smoke, sports graphics, 4k"
        )
    elif post_type == "spotlight":
        prompt = (
            "Action sports portrait of a European soccer player in black and white kit kicking a round soccer ball on grass, "
            f"dramatic soccer arena background, motion blur, {soccer_guard}, cinematic sports photography, 4k" if is_besiktas else
            f"Action sports portrait of a soccer player for {canon}, kicking soccer ball on pitch, {soccer_guard}, stadium lights, cinematic 4k"
        )
    elif post_type == "stage":
        prompt = (
            f"Epic European soccer tournament stage poster for Besiktas JK, {custom_prompt or 'playoff battle'}, "
            f"black and white team colors, majestic black eagle, roaring stadium floodlights, {soccer_guard}, modern sports art, 4k" if is_besiktas else
            f"Epic European soccer tournament poster for {canon}, {custom_prompt or 'playoff battle'}, {soccer_guard}, dramatic stadium lights, 4k"
        )
    else:  # standings / default
        prompt = (
            "Artistic 3D emblem of a majestic black eagle rising over a European soccer stadium arena, green grass pitch, "
            f"neon stadium glow, {soccer_guard}, cinematic championship atmosphere, high end sports banner, 4k" if is_besiktas else
            f"Artistic 3D emblem of soccer club {canon} in arena, {soccer_guard}, championship atmosphere, cinematic 4k"
        )

    buf, or_model = _call_openrouter_image(prompt)
    if buf:
        logger.info(f"Club SMM: AI photo via OpenRouter ({or_model})")
        return buf

    # 2. Pillow Retina club card — основной надёжный метод (без внешних зависимостей)
    logger.info(f"Club SMM: Using Pillow club card for '{team_name}' (post_type={post_type})")
    return generate_club_smm_media(team_name)


# ─── Фолбэк на шаблонную аналитику ──────────────────────────────────────────

def _build_fallback_post(payload: dict, post_type: str, for_caption: bool = False, custom_brief: str = "") -> str:
    """Шаблонный аналитический пост из базы данных."""
    club = payload.get("club", {})
    canon = club.get("name", "Бешикташ")
    emojis = club.get("emojis", "🦅⚪⚫")
    hashtags = " ".join(club.get("hashtags", ["#Besiktas", "#ЛоговоФифарей"]))
    st = payload.get("standings") or {}
    last_m = payload.get("last_match")
    next_m = payload.get("next_match")

    if post_type == "custom" and custom_brief:
        clean_brief = custom_brief.replace("\n", " ").strip()
        return (
            f"{emojis} <b>КЛУБНЫЕ НОВОСТИ: {canon.upper()}</b>\n\n"
            f"⚡ {clean_brief}! «Чёрные орлы» открывают новую главу в турнире «Логово Фифарей». "
            f"Впереди тактическая перезагрузка, максимальная концентрация на победах и бескомпромиссная битва "
            f"за высшие места в таблице. Болельщики, только вперёд!\n\n"
            f"{hashtags}"
        )

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


# ─── Посты по конкретным турам лиги и стадиям кубка ─────────────────────────

def get_club_stages_and_rounds(team_name: str) -> dict:
    """
    Возвращает список всех сыгранных и предстоящих кубковых стадий и туров лиги для данного клуба.
    """
    canon = resolve_team_name(team_name) or team_name
    cup_stages = []
    league_rounds = []

    with database.transaction() as conn:
        cursor = conn.cursor()

        # 1. Кубковые матчи клуба
        cursor.execute("""
            SELECT id, cup_stage, player1_team, player2_team, player1_score, player2_score, status
            FROM matches
            WHERE (player1_team = ? OR player2_team = ?) AND tournament_type = 'cup'
            ORDER BY id ASC
        """, (canon, canon))
        cup_matches = cursor.fetchall()

        stages_dict = {}
        for m in cup_matches:
            st = m["cup_stage"] or "Кубок"
            if st not in stages_dict:
                stages_dict[st] = []
            stages_dict[st].append(m)

        for st, m_list in stages_dict.items():
            first = m_list[0]
            is_p1 = teams_match(first["player1_team"], canon)
            opp = first["player2_team"] if is_p1 else first["player1_team"]
            all_done = all(m["status"] in ("confirmed", "completed") for m in m_list)
            any_done = any(m["status"] in ("confirmed", "completed") for m in m_list)

            my_wins = 0
            opp_wins = 0
            scores = []
            for m in m_list:
                if m["status"] in ("confirmed", "completed"):
                    p1_sc = m["player1_score"] or 0
                    p2_sc = m["player2_score"] or 0
                    p1_is_me = teams_match(m["player1_team"], canon)
                    my_sc = p1_sc if p1_is_me else p2_sc
                    opp_sc = p2_sc if p1_is_me else p1_sc
                    scores.append(f"{my_sc}:{opp_sc}")
                    if my_sc > opp_sc:
                        my_wins += 1
                    elif opp_sc > my_sc:
                        opp_wins += 1

            status_label = "completed" if all_done else ("in_progress" if any_done else "pending")
            cup_stages.append({
                "stage": st,
                "opponent": opp,
                "status": status_label,
                "score_series": f"{my_wins}:{opp_wins}" if any_done else None,
                "match_scores": scores,
                "match_count": len(m_list),
            })

        # 2. Туры лиги
        cursor.execute("""
            SELECT id, round_number, player1_team, player2_team, player1_score, player2_score, status
            FROM matches
            WHERE (player1_team = ? OR player2_team = ?) AND tournament_type = 'league'
            ORDER BY round_number ASC, id ASC
        """, (canon, canon))
        for m in cursor.fetchall():
            rnd = m["round_number"]
            is_p1 = teams_match(m["player1_team"], canon)
            opp = m["player2_team"] if is_p1 else m["player1_team"]
            done = m["status"] in ("confirmed", "completed")
            my_sc = m["player1_score"] if is_p1 else m["player2_score"]
            opp_sc = m["player2_score"] if is_p1 else m["player1_score"]
            league_rounds.append({
                "round": rnd,
                "opponent": opp,
                "status": "completed" if done else "pending",
                "score": f"{my_sc}:{opp_sc}" if done else None,
            })

    return {"cup_stages": cup_stages, "league_rounds": league_rounds}


def get_stage_or_round_payload(team_name: str, round_number: int | None = None, cup_stage: str | None = None) -> dict:
    """
    Извлекает подробные данные матча(ей) для конкретного тура лиги или стадии кубка.
    """
    canon = resolve_team_name(team_name) or team_name
    base_payload = get_club_smm_payload(canon)

    matches_data = []
    with database.transaction() as conn:
        cursor = conn.cursor()
        if cup_stage:
            cursor.execute("""
                SELECT id, round_number, tournament_type, cup_stage, player1_team, player2_team,
                       player1_score, player2_score, status, mvp_player, match_date, match_time
                FROM matches
                WHERE (player1_team = ? OR player2_team = ?)
                  AND tournament_type = 'cup'
                  AND cup_stage = ?
                ORDER BY id ASC
            """, (canon, canon, str(cup_stage)))
        else:
            cursor.execute("""
                SELECT id, round_number, tournament_type, cup_stage, player1_team, player2_team,
                       player1_score, player2_score, status, mvp_player, match_date, match_time
                FROM matches
                WHERE (player1_team = ? OR player2_team = ?)
                  AND tournament_type = 'league'
                  AND round_number = ?
                ORDER BY id ASC
            """, (canon, canon, int(round_number or 1)))

        m_rows = cursor.fetchall()
        for r in m_rows:
            m_id = r["id"]
            is_p1 = teams_match(r["player1_team"], canon)
            my_score = r["player1_score"] if is_p1 else r["player2_score"]
            opp_score = r["player2_score"] if is_p1 else r["player1_score"]
            opponent = r["player2_team"] if is_p1 else r["player1_team"]

            cursor.execute("""
                SELECT team_name, player_name, event_type, count
                FROM match_events
                WHERE match_id = ?
            """, (m_id,))
            events = cursor.fetchall()
            my_goals, my_assists = [], []
            for ev in events:
                p_name = ev["player_name"]
                cnt = ev["count"] or 1
                if teams_match(ev["team_name"], canon):
                    if ev["event_type"] == "goal":
                        my_goals.append(f"{p_name} ({cnt})" if cnt > 1 else p_name)
                    elif ev["event_type"] == "assist":
                        my_assists.append(f"{p_name} ({cnt})" if cnt > 1 else p_name)

            matches_data.append({
                "match_id": m_id,
                "is_home": is_p1,
                "opponent": opponent,
                "my_score": my_score,
                "opp_score": opp_score,
                "status": r["status"],
                "mvp_player": r["mvp_player"],
                "club_goals": my_goals,
                "club_assists": my_assists,
                "date": r["match_date"],
                "time": r["match_time"],
            })

    target_type = "cup" if cup_stage else "league"
    target_name = f"Кубок, стадия {cup_stage}" if cup_stage else f"Тур {round_number}"

    return {
        "club": base_payload.get("club", {}),
        "manager": base_payload.get("manager"),
        "division": base_payload.get("division"),
        "target_type": target_type,
        "target_name": target_name,
        "round_number": round_number,
        "cup_stage": cup_stage,
        "matches": matches_data,
        "standings": base_payload.get("standings"),
    }


def generate_stage_post(
    team_name: str,
    round_number: int | None = None,
    cup_stage: str | None = None,
    for_caption: bool = False,
) -> str:
    """
    Генерирует пост строго в 1 абзац о конкретном туре лиги или стадии кубка.
    """
    canon = resolve_team_name(team_name) or team_name
    stage_payload = get_stage_or_round_payload(canon, round_number, cup_stage)
    system_text = _build_system_instruction(stage_payload)

    target_name = stage_payload["target_name"]
    matches = stage_payload["matches"]
    club_name = stage_payload["club"].get("name", "Бешикташ")
    emojis = stage_payload["club"].get("emojis", "🦅⚪⚫")
    hashtags = " ".join(stage_payload["club"].get("hashtags", ["#Besiktas", "#ЛоговоФифарей"]))

    if not matches:
        return f"{emojis} <b>{target_name.upper()}</b>\n\nМатчи {club_name} на этой стадии не найдены в расписании.\n\n{hashtags}"

    all_done = all(m["status"] in ("confirmed", "completed") for m in matches)
    any_done = any(m["status"] in ("confirmed", "completed") for m in matches)
    first_m = matches[0]
    opp = first_m["opponent"]

    if cup_stage:
        if all_done:
            my_wins = sum(1 for m in matches if (m["my_score"] or 0) > (m["opp_score"] or 0))
            opp_wins = sum(1 for m in matches if (m["opp_score"] or 0) > (m["my_score"] or 0))
            passed = my_wins > opp_wins
            outcome_str = f"Победа в серии {my_wins}:{opp_wins}! Выход в следующий раунд!" if passed else f"Итог серии {my_wins}:{opp_wins}."
            games_str = ", ".join(f"{m['my_score']}:{m['opp_score']}" for m in matches)
            all_scorers = []
            for m in matches:
                all_scorers.extend(m["club_goals"])
            scorers_str = ", ".join(dict.fromkeys(all_scorers)) or "команда"
            task_text = (
                f"ЗАДАЧА: Напиши КОРОТКИЙ победный/боевой обзор кубковой стадии {cup_stage} против {opp}!\n"
                f"Факты: серия завершена со счётом {my_wins}:{opp_wins} (игры: {games_str}). {outcome_str} Голы: {scorers_str}.\n"
                "ТРЕБОВАНИЕ К ФОРМАТУ: Заголовок (1 строка) -> 1 плотный абзац (3-4 предложения, до 350 символов) -> Хэштеги. Пиши сразу готовый текст поста."
            )
        elif any_done:
            task_text = (
                f"ЗАДАЧА: Напиши КОРОТКИЙ пост о ходе кубковой серии {cup_stage} против {opp}!\n"
                f"Факты: серия продолжается, сыграно матчей: {len(matches)}.\n"
                "ТРЕБОВАНИЕ К ФОРМАТУ: Заголовок (1 строка) -> 1 плотный абзац (3-4 предложения, до 350 символов) -> Хэштеги. Пиши сразу готовый текст поста."
            )
        else:
            task_text = (
                f"ЗАДАЧА: Напиши КОРОТКИЙ боевой анонс кубковой битвы стадии {cup_stage} против {opp}!\n"
                f"Факты: предстоит серия на вылет за кубковый трофей.\n"
                "ТРЕБОВАНИЕ К ФОРМАТУ: Заголовок (1 строка) -> 1 плотный абзац (3-4 предложения, до 350 символов) -> Хэштеги. Пиши сразу готовый текст поста."
            )
    else:
        # Тур чемпионата
        m = first_m
        if m["status"] in ("confirmed", "completed"):
            res = "победа" if (m["my_score"] or 0) > (m["opp_score"] or 0) else ("ничья" if m["my_score"] == m["opp_score"] else "поражение")
            scorers_str = ", ".join(m["club_goals"]) or "команда"
            mvp_str = f", MVP матча: {m['mvp_player']}" if m.get("mvp_player") else ""
            task_text = (
                f"ЗАДАЧА: Напиши КОРОТКИЙ обзор сыгранного Тура {round_number} против {opp}!\n"
                f"Факты: результат — {res}, счёт {m['my_score']}:{m['opp_score']}. Авторы голов: {scorers_str}{mvp_str}.\n"
                "ТРЕБОВАНИЕ К ФОРМАТУ: Заголовок (1 строка) -> 1 плотный абзац (3-4 предложения, до 350 символов) -> Хэштеги. Пиши сразу готовый текст поста."
            )
        else:
            task_text = (
                f"ЗАДАЧА: Напиши КОРОТКИЙ боевой анонс предстоящего Тура {round_number} против {opp}!\n"
                f"Факты: важнейшая встреча в борьбе за очки турнирной таблицы.\n"
                "ТРЕБОВАНИЕ К ФОРМАТУ: Заголовок (1 строка) -> 1 плотный абзац (3-4 предложения, до 350 символов) -> Хэштеги. Пиши сразу готовый текст поста."
            )

    user_text = f"{task_text}\n\nДАННЫЕ (JSON):\n{json.dumps(stage_payload, ensure_ascii=False)}"
    limit = CAPTION_MAX_CHARS if for_caption else POST_MAX_CHARS
    max_tokens = 220 if for_caption else 350

    # 1. OpenRouter
    text, model_name = _call_openrouter_text(system_text, user_text, max_tokens)
    if text:
        logger.info(f"Club SMM stage text generated via OpenRouter ({model_name})")
        return _fit_html(text, limit)

    # 2. Gemini
    text, model_name = _call_gemini_text(system_text, user_text, max_tokens)
    if text:
        logger.info(f"Club SMM stage text generated via Gemini ({model_name})")
        return _fit_html(text, limit)

    # 3. Fallback
    if cup_stage and all_done:
        return (
            f"{emojis} <b>КУБОК: ИТОГИ СТАДИИ {cup_stage}</b>\n\n"
            f"Кубковое противостояние против «{opp}» завершилось! Черно-белые сражались на каждом сантиметре поля "
            f"и показали несгибаемый характер орлов. Двигаемся дальше за трофеем!\n\n"
            f"{hashtags} #Кубок"
        )
    elif not cup_stage and first_m["status"] in ("confirmed", "completed"):
        m = first_m
        return (
            f"{emojis} <b>ИТОГИ ТУРА {round_number}</b>\n\n"
            f"Финальный свисток в матче против «{opp}» зафиксировал счёт {m['my_score']}:{m['opp_score']}. "
            f"Парни @sp1r1tVSA отдали все силы ради победы. Продолжаем сезон и готовимся к новым сражениям!\n\n"
            f"{hashtags} #Тур{round_number}"
        )
    else:
        return (
            f"{emojis} <b>{target_name.upper()}: ВРЕМЯ БИТВЫ!</b>\n\n"
            f"Готовимся к ответственному противостоянию против «{opp}»! Выходим на поле максимально заряженными "
            f"и нацеленными исключительно на положительный результат. Вперёд, Орлы!\n\n"
            f"{hashtags}"
        )
