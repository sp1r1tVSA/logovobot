import os
import base64
import json
import logging
import threading
import urllib.request
import urllib.error
import config
import database
from services.ai import persona_base

logger = logging.getLogger(__name__)

GEMINI_CHAT_MODELS = getattr(config, "GEMINI_CHAT_MODELS", [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
])

_chat_key_index = 0
_chat_key_lock = threading.Lock()

def get_ordered_chat_keys(api_key: str | None = None) -> list[str]:
    """
    Возвращает список API-ключей Gemini для чата и аналитики с ротацией Round-Robin.
    Каждый следующий вызов сдвигает начальный ключ, распределяя запросы
    равномерно по пулу GEMINI_CHAT_API_KEYS.
    """
    if api_key:
        return [k.strip() for k in api_key.split(",") if k.strip()]

    keys = getattr(config, "GEMINI_CHAT_API_KEYS", [])
    if not keys:
        single = (getattr(config, "GEMINI_CHAT_API_KEY", "") or "").strip()
        keys = [k.strip() for k in single.split(",") if k.strip()]

    if not keys:
        return []
    if len(keys) == 1:
        return keys

    global _chat_key_index
    with _chat_key_lock:
        idx = _chat_key_index % len(keys)
        _chat_key_index += 1
        return keys[idx:] + keys[:idx]

_chat_model_index = 0
_chat_model_lock = threading.Lock()

def get_ordered_chat_models() -> list[str]:
    """
    Возвращает список моделей Gemini для чата и аналитики с ротацией Round-Robin.
    Каждый следующий вызов сдвигает начальную модель, балансируя нагрузку
    между всеми тремя моделями:
    gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-3.8-flash.
    """
    global _chat_model_index
    with _chat_model_lock:
        idx = _chat_model_index % len(GEMINI_CHAT_MODELS)
        _chat_model_index += 1
        return GEMINI_CHAT_MODELS[idx:] + GEMINI_CHAT_MODELS[:idx]

def generate_chat_reply(
    user_id: int, 
    user_text: str, 
    chat_history: list[dict], 
    context_data: str,
    audio_bytes: bytes = None,
    audio_mime: str = "audio/ogg",
    mode: str = "temshik"
) -> str:
    """
    Sends chat history and current user text or audio to Gemini for a conversational response.
    Returns the text reply from the AI.
    """
    keys_to_try = get_ordered_chat_keys()
    if not keys_to_try:
        logger.warning("GEMINI_CHAT_API_KEY is not set.")
        return "Ошибка: Не настроен ключ для чата (GEMINI_CHAT_API_KEY)."

    if mode == "persona2":
        system_instruction = _build_persona2_instruction(context_data, user_text or "")
    else:
        system_instruction = _build_temshik_instruction(context_data)

    contents = []
    for msg in chat_history:
        contents.append({
            "role": msg["role"],
            "parts": [{"text": msg["text"]}]
        })
    
    user_parts = []
    if audio_bytes:
        user_parts.append({
            "inline_data": {
                "mime_type": audio_mime,
                "data": base64.b64encode(audio_bytes).decode('utf-8')
            }
        })
        user_parts.append({"text": "Послушай это голосовое сообщение от пользователя и ответь ему."})
    else:
        user_parts.append({"text": user_text})

    contents.append({
        "role": "user",
        "parts": user_parts
    })

    payload = {
        "system_instruction": system_instruction,
        "contents": contents,
        "generationConfig": {
            "temperature": 0.8,
            # Потолок, а не цель: длину держит промт. 400 токенов хватает на «подробный»
            # ответ в 6-7 предложений; обрезанный хвост дочищает _trim_to_last_sentence.
            "maxOutputTokens": 400,
        }
    }

    
    payload_bytes = json.dumps(payload).encode('utf-8')

    from services.ai.ai_recognizer import _get_gemini_opener
    opener = _get_gemini_opener()

    for model_name in get_ordered_chat_models():
        base_url = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
        for api_key in keys_to_try:
            url = f"{base_url}/v1beta/models/{model_name}:generateContent?key={api_key}"
            req = urllib.request.Request(
                url,
                data=payload_bytes,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                }
            )
            try:
                with opener.open(req, timeout=25) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    
                    if "candidates" not in result or not result["candidates"]:
                        logger.warning(f"AI Chat: No candidates returned from model '{model_name}'. Response: {result}")
                        continue
                    
                    candidate = result["candidates"][0]
                    text_response = candidate["content"]["parts"][0]["text"]
                    if candidate.get("finishReason") == "MAX_TOKENS":
                        text_response = _trim_to_last_sentence(text_response)
                    clean_text = text_response.replace("**", "").replace("*", "")
                    cleaned_lines = [line.strip() for line in clean_text.split("\n")]
                    return "\n".join(cleaned_lines).strip()
                    
            except urllib.error.HTTPError as e:
                key_suffix = f"...{api_key[-4:]}" if len(api_key) > 4 else "***"
                if e.code == 404:
                    logger.warning(f"AI Chat: Model '{model_name}' HTTP 404 (model deprecated/not available). Skipping to next model...")
                    break
                if e.code in (400, 403, 429, 503):
                    logger.warning(f"AI Chat: Model '{model_name}' (key {key_suffix}) HTTP {e.code} (rate-limit / quota / unavailable). Trying next key/model fallback...")
                else:
                    logger.warning(f"AI Chat: Model '{model_name}' (key {key_suffix}) HTTP Error {e.code}: {e}")
                continue
            except Exception as e:
                logger.exception(f"AI Chat: Unexpected error generating reply with model '{model_name}'")
                continue
            
    return "Ох, что-то я сейчас не в форме (ошибка API или лимиты), попробуй написать попозже! ⚽"

def _trim_to_last_sentence(text: str) -> str:
    """Drop the half-sentence a MAX_TOKENS cut leaves at the end of a reply.

    Keeps everything up to the last sentence terminator; a reply without one
    (a single run-on sentence) is returned with an ellipsis instead of being lost.
    """
    cut = max(text.rfind(ch) for ch in (".", "!", "?", "…"))
    if cut >= len(text) // 3:
        return text[: cut + 1]
    return text.rstrip(" ,;:—-") + "…"


def _build_temshik_instruction(context_data: str) -> dict:
    return {
        "parts": [{
            "text": (
                "Ты — Темшик, аналитик турнира «Логово Фифарей» и душевный 30+ мужик (мудрый скуф со стажем): "
                "любишь расслабон, пенное, баньку, шашлык на даче, диван и футбол.\n\n"
                "СТИЛЬ:\n"
                "- Говори по-простому, по-братски, с добрым юмором и житейской мудростью.\n"
                "- На болтовню про жизнь отвечай в том же душевном стиле.\n"
                "- На вопросы про таблицу, матчи, кубок и шансы отвечай по данным ниже, с фирменной присказкой.\n\n"
                "ТУРНИР (НЕ ПУТАЙ):\n"
                "- Турнир разбит на дивизионы (~16 клубов в каждом), у каждого своя таблица, туры и дедлайны; "
                "интрига — титул, повышение 🚀 и вылет 🔻 (цифры — в блоке 'СТРУКТУРА ТУРНИРА').\n"
                "- Тебе передан ТОЛЬКО дивизион этого разговора. Про чужие дивизионы и всю лигу честно скажи, "
                "что не видишь, и не выдумывай цифры.\n"
                "- Архив единой лиги КПЛ — история для баек, а не текущее положение.\n\n"
                "ШАНСЫ: если спрашивают про шансы — назови вероятность в процентах "
                "(например, 'на повышение ~12%') и одним-двумя фактами из таблицы объясни почему "
                "(отставание, сколько туров осталось, форма). Считай только по текущим данным дивизиона.\n\n"
                f"=== ДАННЫЕ ЛИГИ И ИГРОКА ===\n{context_data}\n===========================\n\n"
                "ДЛИНА ОТВЕТА (СТРОГО):\n"
                "- 2-4 коротких предложения, как реплика в чате. Отвечай только на то, что спросили.\n"
                "- Никаких списков, нумерации, заголовков и пересказа таблицы — выбери главное.\n"
                "- Длиннее (до 6-7 предложений) — только если прямо попросили подробно, списком или весь состав.\n"
                "- Можно 1-2 эмодзи."
            )
        }]
    }

# Chat slang samples: the freshest few, minus one-word noise like «А» / «Го» / «…».
PERSONA2_SAMPLES = 10
PERSONA2_SAMPLE_MIN_LEN = 6


def _build_persona2_instruction(context_data: str, user_text: str = "") -> dict:
    samples = [s for s in database.get_style_samples(limit=40) if len(s.strip()) >= PERSONA2_SAMPLE_MIN_LEN]
    samples = samples[:PERSONA2_SAMPLES]
    samples_text = "\n".join(f"- {s}" for s in samples) if samples else "Пока нет примеров."
    archive_text = persona_base.get_coach_archive_text()
    styles_text = persona_base.get_participant_styles_text(user_text, context_data) or "—"
    return {
        "parts": [{
            "text": (
                "Ты — ЛЮТЫЙ БУЛЛИ, самый токсичный задира чата турнира «Логово Фифарей». Кумиров нет, "
                "ни за кого не болеешь: все вокруг — раки, клоуны и случайные пассажиры, и твоя цель — "
                "затроллить любого, кто подал голос.\n\n"
                "СТИЛЬ:\n"
                "- Нарочито небрежно, часто с маленькой буквы, чатовский сленг ('мдо', 'шо', 'пааан', 'терпи', "
                "'скули', 'кринж', 'отлетай', 'позорище').\n"
                "- Хвастается — обесцень ('чистый лак', 'соперник афк был'). Ноет — добей ('скилл ишью', "
                "'руки выпрями'). Спрашивает — ответь по делу, но с издёвкой. Дерзит — заткни ('ты кто вообще?').\n"
                "- В зоне вылета 🔻 — 'готовь чемодан'; из нижнего дивизиона — 'сначала повышение возьми'.\n"
                "- Никого не хвали. Исключение одно: админ @sp1r1tVSA — без мата, можно язвить.\n\n"
                "ТУРНИР (НЕ ПУТАЙ): пять дивизионов по ~16 клубов, единой лиги больше нет. У тебя данные ТОЛЬКО "
                "этого дивизиона — про чужие цифры не выдумывай, огрызнись ('не моя зона'). Текущий клуб и место "
                "тренера — только из таблицы ниже.\n\n"
                f"{archive_text}\n"
                "Это прошлое: кидай в лицо как подкол ('чемпион, ага, было дело'), но клубы из архива сейчас "
                "могут быть у ДРУГИХ тренеров — чужие титулы и позоры новому владельцу клуба не приписывай. "
                "Кого в архиве нет — тот новенький, бей по текущим данным.\n\n"
                f"КАК ПИШУТ УЧАСТНИКИ:\n{styles_text}\n\n"
                f"ПРИМЕРЫ ЧАТОВСКОГО СЛЕНГА:\n{samples_text}\n\n"
                f"=== ДАННЫЕ ЛИГИ И ИГРОКА ===\n{context_data}\n===========================\n\n"
                "ДЛИНА (СТРОГО): 1-2 хлёстких предложения, без списков."
            )
        }]
    }
