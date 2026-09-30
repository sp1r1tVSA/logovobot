"""
handlers/club_smm.py

Персональный SMM-центр для владельца клуба (главного админа @sp1r1tVSA / ID 1642770076).
Генерация контента через Gemini (все доступные модели с ротацией), согласование черновика
в ЛС с ботом и публикация в привязанный Telegram-канал клуба (текстом или с инфографикой).
"""

import asyncio
import functools
import html
import logging
import re
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

import config
import database
from handlers.base import is_global_admin
from services import club_smm_service
from club_registry import resolve_team_name

logger = logging.getLogger(__name__)

# Владелец клуба: доступ не зависит от env-списка админов
SMM_OWNER_ID = 1642770076
DEFAULT_CLUB = "Бешикташ"

# Состояния FSM ConversationHandler
SMM_STATE_WAIT_PROMPT = 1
SMM_STATE_WAIT_EDIT = 2
SMM_STATE_WAIT_CHANNEL = 3
SMM_STATE_WAIT_STAGE = 4
SMM_STATE_WAIT_PHOTO = 5

CONFIG_CHANNEL_KEY = "my_club_channel"


def is_smm_allowed(user_id: int | None) -> bool:
    """Доступ для владельца клуба и глобальных администраторов (не админов дивизионов)."""
    if not user_id:
        return False
    if user_id == SMM_OWNER_ID:
        return True
    return is_global_admin(user_id)


async def _safe_answer(query, text: str | None = None, show_alert: bool = False) -> None:
    """answer() можно вызвать один раз на callback; повторный вызов не должен ронять хендлер."""
    if not query:
        return
    try:
        await query.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def _guard(update: Update, answer: bool = True) -> bool:
    """Проверка доступа для callback-хендлеров. Отвечает на callback ровно один раз."""
    user = update.effective_user
    query = update.callback_query
    if not user or not is_smm_allowed(user.id):
        await _safe_answer(query, "⛔ Нет доступа", show_alert=True)
        return False
    if answer:
        await _safe_answer(query)
    return True


async def _render(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с кнопкой; если нельзя (фото, старое сообщение) — шлёт новое."""
    query = update.callback_query
    if query and query.message:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    chat = update.effective_chat
    if chat:
        await chat.send_message(text, parse_mode="HTML", reply_markup=markup)


def _ends_conversation(fn):
    """Кнопочный маршрут внутри fallbacks диалога: выполняет хендлер и завершает диалог."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await fn(update, context)
        return ConversationHandler.END
    return wrapper


def normalize_telegram_channel(raw_input: str) -> str:
    """Нормализует ввод канала (URL, username, @username, ID) в формат для Telegram API."""
    text = (raw_input or "").strip().strip("\"'")
    if not text:
        return ""
    # 1. Если передан URL: https://t.me/username или t.me/username
    if "t.me/" in text:
        part = text.split("t.me/")[-1].split("?")[0].split("/")[0].strip()
        part = part.lstrip("@").lstrip("+")
        if part:
            return f"@{part}"
    # 2. Если это ID чата (-100... или просто цифры)
    if text.lstrip("-").isdigit():
        return text if text.startswith("-") else f"-100{text}"
    # 3. Юзернейм с @ или без
    return text if text.startswith("@") else f"@{text}"


def get_target_channel() -> str | None:
    """Возвращает сохраненный целевой канал из БД или config."""
    db_val = database.get_config(CONFIG_CHANNEL_KEY)
    if db_val and db_val.strip():
        return normalize_telegram_channel(db_val.strip())
    cfg_val = getattr(config, "MY_CLUB_CHANNEL", "")
    return normalize_telegram_channel(cfg_val.strip()) if cfg_val else None


async def _resolve_user_club(user_id: int) -> str:
    """Клуб пользователя из базы; иначе клуб канала (my_club_team); иначе «Бешикташ»."""
    team = await asyncio.to_thread(database.get_user_team, user_id)
    if not team:
        team = await asyncio.to_thread(database.get_config, "my_club_team")
    if team:
        return resolve_team_name(team) or team
    return DEFAULT_CLUB


# ─── Главное меню SMM-центра ────────────────────────────────────────────────

async def cmd_smm_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Точка входа по команде /club_post или /besiktas."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return

    team_name = await _resolve_user_club(user.id)
    channel = get_target_channel()
    channel_display = f"<code>{html.escape(channel)}</code>" if channel else "<i>Не настроен</i>"

    text = (
        f"🦅 <b>SMM-центр ФК «{html.escape(team_name.upper())}»</b>\n\n"
        f"Здесь вы можете генерировать посты для своего Telegram-канала с помощью ИИ "
        f"на основе реальной статистики из базы данных.\n\n"
        f"📢 <b>Канал публикации:</b> {channel_display}\n\n"
        f"Выберите тип поста для подготовки черновика:"
    )

    keyboard = [
        [
            InlineKeyboardButton("🔥 Анонс матча", callback_data="smm_gen:matchday"),
            InlineKeyboardButton("🏆 Итоги матча", callback_data="smm_gen:recap"),
        ],
        [
            InlineKeyboardButton("📊 Таблица и форма", callback_data="smm_gen:standings"),
            InlineKeyboardButton("🌟 Звезда клуба", callback_data="smm_gen:spotlight"),
        ],
        [
            InlineKeyboardButton("🗓 Тур / Кубок", callback_data="smm_choose_stage"),
            InlineKeyboardButton("✍️ Свой бриф / Голос", callback_data="smm_enter_prompt"),
        ],
        [
            InlineKeyboardButton("⚙️ Настроить канал", callback_data="smm_cfg_channel"),
            InlineKeyboardButton("« В кабинет", callback_data="menu_cabinet"),
        ]
    ]

    await _render(update, text, InlineKeyboardMarkup(keyboard))


async def cb_smm_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Коллбэк возврата в меню SMM-центра."""
    if not await _guard(update):
        return
    await cmd_smm_hub(update, context)


# ─── Генерация черновика ────────────────────────────────────────────────────

async def cb_smm_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерация поста выбранного типа и показ экрана предпросмотра."""
    query = update.callback_query
    if not query or not await _guard(update):
        return
    user = update.effective_user

    post_type = query.data.split(":")[1] if ":" in query.data else "matchday"
    team_name = await _resolve_user_club(user.id)

    # Статус-заглушка
    loading_text = "⏳ <b>ИИ анализирует турнирную статистику и пишет пост...</b>"
    try:
        await query.edit_message_text(loading_text, parse_mode="HTML")
    except Exception:
        pass

    # Генерация в фоновом потоке
    generated_text = await asyncio.to_thread(
        club_smm_service.generate_club_post,
        team_name=team_name,
        post_type=post_type,
    )

    match_photos = []
    if post_type == "recap":
        match_photos = await asyncio.to_thread(database.get_last_match_photos, team_name)

    context.user_data["smm_draft"] = {
        "text": generated_text,
        "team_name": team_name,
        "post_type": post_type,
        "custom_brief": "",
        "match_photos": match_photos,
        "photo_mode": "match" if match_photos else "none",
    }

    await _show_draft_preview(update, context, generated_text)


async def cb_smm_choose_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отображение меню выбора конкретного тура чемпионата или кубковой стадии клуба."""
    query = update.callback_query
    if not await _guard(update):
        return
    user = update.effective_user

    team_name = await _resolve_user_club(user.id)
    data = await asyncio.to_thread(club_smm_service.get_club_stages_and_rounds, team_name)
    cup_stages = data.get("cup_stages", [])
    league_rounds = data.get("league_rounds", [])

    keyboard = []

    # 1. Кубковые стадии (если есть)
    if cup_stages:
        cup_row = []
        # Если у клуба и общий кубок, и кубок дивизиона — различаем их в подписи
        mixed_cups = len({st.get("cup_scope", 0) for st in cup_stages}) > 1
        for st in cup_stages:
            stage_name = st["stage"]
            scope = st.get("cup_scope") or 0
            score = st.get("score_series")
            status = st.get("status")
            prefix = "🏆"
            if mixed_cups:
                prefix = "🏆 Общ." if scope == 0 else "🏅 Див."
            if status == "completed":
                btn_text = f"{prefix} {stage_name} ({score} ✅)"
            elif status == "in_progress":
                btn_text = f"{prefix} {stage_name} ({score} ⏳)"
            else:
                btn_text = f"{prefix} {stage_name} (⏳)"
            cup_row.append(InlineKeyboardButton(btn_text, callback_data=f"smm_stage:cup:{scope}:{stage_name}"))
            if len(cup_row) == 2:
                keyboard.append(cup_row)
                cup_row = []
        if cup_row:
            keyboard.append(cup_row)

    # 2. Туры чемпионата (по 2 в строке)
    rnd_row = []
    for r in league_rounds:
        rnd = r["round"]
        score = r.get("score")
        status = r.get("status")
        if status == "completed":
            btn_text = f"Тур {rnd} ({score} ✅)"
        else:
            btn_text = f"Тур {rnd} (⏳)"
        rnd_row.append(InlineKeyboardButton(btn_text, callback_data=f"smm_stage:league:{rnd}"))
        if len(rnd_row) == 2:
            keyboard.append(rnd_row)
            rnd_row = []
    if rnd_row:
        keyboard.append(rnd_row)

    # Кнопка ручного ввода тура/стадии
    keyboard.append([
        InlineKeyboardButton("✏️ Ввести номер тура / стадию", callback_data="smm_enter_stage")
    ])
    keyboard.append([
        InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")
    ])

    markup = InlineKeyboardMarkup(keyboard)
    text = (
        f"🗓 <b>Выбор тура или стадии кубка для ФК «{html.escape(team_name.upper())}»</b>\n\n"
        f"Выберите завершённый матч для победного обзора или предстоящий для анонса битвы. "
        f"ИИ автоматически подтянет счёт, авторов голов, MVP и создаст пост строго в один абзац.\n\n"
        f"Также вы можете ввести номер тура вручную."
    )

    await _render(update, text, markup)


def _parse_stage_callback(data: str) -> tuple[int | None, str | None, int | None]:
    """
    `smm_stage:league:<тур>` → (тур, None, None);
    `smm_stage:cup:<scope>:<стадия>` → (None, стадия, scope), где scope 0 — общий кубок, N — дивизион;
    старый формат `smm_stage:cup:<стадия>` → scope None (кубок с последним матчем этой стадии).
    """
    parts = data.split(":", 3)
    if len(parts) < 3:
        return None, None, None
    if parts[1] == "league":
        return (int(parts[2]) if parts[2].isdigit() else None), None, None
    if len(parts) == 4 and parts[2].isdigit():
        return None, parts[3], int(parts[2])
    return None, ":".join(parts[2:]), None


async def cb_smm_stage_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерация поста для выбранного тура лиги или стадии кубка."""
    query = update.callback_query
    if not query or not await _guard(update):
        return
    user = update.effective_user

    round_number, cup_stage, cup_division_id = _parse_stage_callback(query.data)
    if round_number is None and not cup_stage:
        return

    team_name = await _resolve_user_club(user.id)
    stage_label = f"стадии кубка {cup_stage}" if cup_stage else f"тура {round_number}"

    loading_text = f"⏳ <b>ИИ анализирует статистику {stage_label} и пишет пост...</b>"
    try:
        await query.edit_message_text(loading_text, parse_mode="HTML")
    except Exception:
        pass

    await _build_stage_draft(update, context, team_name, round_number, cup_stage, cup_division_id)


async def _build_stage_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    team_name: str,
    round_number: int | None,
    cup_stage: str | None,
    cup_division_id: int | None,
) -> None:
    """Генерирует пост тура/стадии, сохраняет черновик и показывает предпросмотр."""
    generated_text = await asyncio.to_thread(
        club_smm_service.generate_stage_post,
        team_name=team_name,
        round_number=round_number,
        cup_stage=cup_stage,
        cup_division_id=cup_division_id,
    )

    stage_photos = await asyncio.to_thread(
        database.get_stage_match_photos,
        team_name,
        round_number=round_number,
        cup_stage=cup_stage,
        cup_division_id=cup_division_id,
    )

    context.user_data["smm_draft"] = {
        "text": generated_text,
        "team_name": team_name,
        "post_type": "stage",
        "round_number": round_number,
        "cup_stage": cup_stage,
        "cup_division_id": cup_division_id,
        "custom_brief": f"Кубок {cup_stage}" if cup_stage else f"Тур {round_number}",
        "match_photos": stage_photos,
        "photo_mode": "match" if stage_photos else "none",
    }

    await _show_draft_preview(update, context, generated_text)


async def _show_draft_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, draft_text: str) -> None:
    """Показывает экран предпросмотра черновика с кнопками публикации и правки."""
    channel = get_target_channel()
    ch_label = f" ({html.escape(channel)})" if channel else " (⚠️ канал не задан)"

    draft = context.user_data.get("smm_draft") or {}
    match_photos = draft.get("match_photos") or []
    custom_photo_id = draft.get("custom_photo_id")
    photo_mode = draft.get("photo_mode")
    if not photo_mode:
        if custom_photo_id:
            photo_mode = "custom"
        elif match_photos:
            photo_mode = "match"
        else:
            photo_mode = "none"

    if photo_mode == "match" and match_photos:
        if len(match_photos) == 1:
            photo_line = "\n🖼 <b>Скрин матча:</b> прикреплён ✅"
            publish_photo_btn = InlineKeyboardButton("🚀 Опубликовать (со скрином матча)", callback_data="smm_publish:match_photos")
            remove_photo_btn = InlineKeyboardButton("❌ Убрать скриншот", callback_data="smm_remove_photo")
        else:
            photo_line = f"\n🖼 <b>Скрины матчей:</b> прикреплено ({len(match_photos)} шт.) ✅"
            publish_photo_btn = InlineKeyboardButton("🚀 Опубликовать (со скринами матчей)", callback_data="smm_publish:match_photos")
            remove_photo_btn = InlineKeyboardButton("❌ Убрать скрины", callback_data="smm_remove_photo")
    elif photo_mode == "custom" and custom_photo_id:
        photo_line = "\n🖼 <b>Своё фото:</b> прикреплено ✅"
        publish_photo_btn = InlineKeyboardButton("🚀 Опубликовать (со своим фото)", callback_data="smm_publish:custom_photo")
        remove_photo_btn = InlineKeyboardButton("❌ Убрать своё фото", callback_data="smm_remove_photo")
    else:
        photo_line = ""
        publish_photo_btn = None
        remove_photo_btn = None

    preview_message = (
        f"📝 <b>Черновик для публикации:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"{club_smm_service._fit_html(draft_text, 3500)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Канал:</b> {ch_label}"
        f"{photo_line}"
    )

    keyboard = []
    if photo_mode == "match" and match_photos:
        keyboard.append([publish_photo_btn])
        keyboard.append([
            InlineKeyboardButton("🎨 С ИИ-картинкой", callback_data="smm_publish:ai_photo"),
            InlineKeyboardButton("🏛 С карточкой клуба", callback_data="smm_publish:card"),
        ])
        keyboard.append([
            InlineKeyboardButton("📷 Своё фото", callback_data="smm_enter_photo"),
            remove_photo_btn,
        ])
        keyboard.append([
            InlineKeyboardButton("🚀 Опубликовать (Текст)", callback_data="smm_publish:text"),
        ])
    elif photo_mode == "custom" and custom_photo_id:
        keyboard.append([publish_photo_btn])
        keyboard.append([
            InlineKeyboardButton("🎨 С ИИ-картинкой", callback_data="smm_publish:ai_photo"),
            InlineKeyboardButton("🏛 С карточкой клуба", callback_data="smm_publish:card"),
        ])
        custom_row = []
        if match_photos:
            m_lbl = "📸 Скрин матча" if len(match_photos) == 1 else f"📸 Скрины матчей ({len(match_photos)})"
            custom_row.append(InlineKeyboardButton(m_lbl, callback_data="smm_attach_match_photos"))
        else:
            custom_row.append(InlineKeyboardButton("📷 Заменить фото", callback_data="smm_enter_photo"))
        custom_row.append(remove_photo_btn)
        keyboard.append(custom_row)
        keyboard.append([
            InlineKeyboardButton("🚀 Опубликовать (Текст)", callback_data="smm_publish:text"),
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("🚀 Опубликовать (Текст)", callback_data="smm_publish:text"),
        ])
        if match_photos:
            m_lbl = "📸 Прикрепить скрин матча" if len(match_photos) == 1 else f"📸 Прикрепить скрины матчей ({len(match_photos)})"
            keyboard.append([
                InlineKeyboardButton(m_lbl, callback_data="smm_attach_match_photos")
            ])
        keyboard.append([
            InlineKeyboardButton("🎨 С ИИ-картинкой", callback_data="smm_publish:ai_photo"),
            InlineKeyboardButton("🏛 С карточкой клуба", callback_data="smm_publish:card"),
        ])
        keyboard.append([
            InlineKeyboardButton("📷 Прикрепить своё фото", callback_data="smm_enter_photo"),
        ])

    keyboard.append([
        InlineKeyboardButton("🔄 Другой вариант текста", callback_data="smm_regen"),
        InlineKeyboardButton("✏️ Правка / Уточнить", callback_data="smm_enter_edit"),
    ])
    keyboard.append([
        InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")
    ])
    markup = InlineKeyboardMarkup(keyboard)

    target_chat = update.effective_chat
    query = update.callback_query

    if query and query.message:
        try:
            await query.edit_message_text(preview_message, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass

    if target_chat:
        await target_chat.send_message(preview_message, parse_mode="HTML", reply_markup=markup)


# ─── Регенерация и доработка ────────────────────────────────────────────────

async def cb_smm_regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерирует альтернативный вариант текущего поста."""
    query = update.callback_query
    if not await _guard(update):
        return

    draft = context.user_data.get("smm_draft")
    if not draft:
        await cmd_smm_hub(update, context)
        return

    try:
        await query.edit_message_text("🔄 <b>Генерирую новый вариант текста...</b>", parse_mode="HTML")
    except Exception:
        pass

    post_type = draft.get("post_type", "matchday")
    if post_type == "stage":
        generated_text = await asyncio.to_thread(
            club_smm_service.generate_stage_post,
            team_name=draft.get("team_name", DEFAULT_CLUB),
            round_number=draft.get("round_number"),
            cup_stage=draft.get("cup_stage"),
            cup_division_id=draft.get("cup_division_id"),
        )
    else:
        user = update.effective_user
        user_display = f"@{user.username}" if (user and user.username) else (user.first_name if user else "")
        generated_text = await asyncio.to_thread(
            club_smm_service.generate_club_post,
            team_name=draft.get("team_name", DEFAULT_CLUB),
            post_type=post_type,
            custom_brief=draft.get("custom_brief", ""),
            user_name=user_display,
        )

    draft["text"] = generated_text
    await _show_draft_preview(update, context, generated_text)


# ─── FSM: Свой бриф / Голосовое сообщение ───────────────────────────────────

async def start_custom_prompt_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос пользовательской темы или голосового сообщения."""
    if not await _guard(update):
        return ConversationHandler.END

    text = (
        "🎙 <b>Свой бриф / Тема для поста</b>\n\n"
        "Отправьте тему для поста текстом, голосовым сообщением или видеокружком — "
        "ИИ расшифрует речь и напишет пост.\n\n"
        "<i>Примеры:</i>\n"
        "• «Напиши бодрый пост, как мы разгромили соперника в кубке»\n"
        "• «Сделай акцент на сумасшедшем сейве вратаря и дерзкой игре Троссарда»\n"
        "• «Пост-настрой перед принципиальным дерби»\n\n"
        "Для отмены нажмите кнопку ниже."
    )
    keyboard = [[InlineKeyboardButton("« Отмена", callback_data="smm_hub")]]
    await _render(update, text, InlineKeyboardMarkup(keyboard))

    return SMM_STATE_WAIT_PROMPT


async def handle_custom_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка текста или голосового сообщения с брифом."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return ConversationHandler.END

    team_name = await _resolve_user_club(user.id)
    msg = update.message
    audio_bytes = None
    custom_brief = ""

    status_msg = await msg.reply_text("⏳ <b>ИИ слушает и создаёт клубный пост...</b>", parse_mode="HTML")

    audio_mime = "audio/ogg"
    voice_obj = msg.voice or msg.video_note
    if voice_obj:
        if msg.video_note:
            audio_mime = "video/mp4"
        try:
            voice_file = await voice_obj.get_file()
            audio_bytes = bytes(await voice_file.download_as_bytearray())
        except Exception as e:
            logger.exception(f"Club SMM: Failed to download voice file: {e}")
            await status_msg.edit_text("❌ Ошибка загрузки голосового сообщения. Попробуйте отправить текстом.")
            return SMM_STATE_WAIT_PROMPT
    elif msg.text:
        custom_brief = msg.text.strip()

    if audio_bytes:
        # Расшифровываем заранее: бриф сохраняется в черновике для «Другой вариант» и ИИ-картинки
        transcript = await asyncio.to_thread(club_smm_service.transcribe_audio, audio_bytes, audio_mime)
        if not transcript:
            await status_msg.edit_text(
                "❌ Не удалось расшифровать запись. Попробуйте ещё раз или отправьте тему текстом."
            )
            return SMM_STATE_WAIT_PROMPT
        custom_brief = transcript.strip()

    user_display = f"@{user.username}" if user.username else (user.first_name or "")
    generated_text = await asyncio.to_thread(
        club_smm_service.generate_club_post,
        team_name=team_name,
        post_type="custom",
        custom_brief=custom_brief,
        user_name=user_display,
    )

    try:
        await status_msg.delete()
    except Exception:
        pass

    context.user_data["smm_draft"] = {
        "text": generated_text,
        "team_name": team_name,
        "post_type": "custom",
        "custom_brief": custom_brief,
        "photo_mode": "none",
    }

    await _show_draft_preview(update, context, generated_text)
    return ConversationHandler.END


# ─── FSM: Ручной ввод тура / стадии кубка ───────────────────────────────────

async def start_stage_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос номера тура или стадии кубка вручную."""
    if not await _guard(update):
        return ConversationHandler.END

    text = (
        "✏️ <b>Ручной ввод тура или стадии кубка</b>\n\n"
        "Отправьте номер тура (например: <code>5</code>) "
        "или стадию кубка (например: <code>1/64</code>, <code>1/8</code>, <code>финал</code>).\n\n"
        "ИИ автоматически найдёт данные матча в базе и создаст ёмкий пост в один абзац."
    )
    keyboard = [[InlineKeyboardButton("« Назад к выбору", callback_data="smm_choose_stage")]]
    await _render(update, text, InlineKeyboardMarkup(keyboard))

    return SMM_STATE_WAIT_STAGE


async def handle_stage_input_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ручного ввода тура или стадии кубка."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return ConversationHandler.END

    team_name = await _resolve_user_club(user.id)
    msg = update.message
    raw = (msg.text or "").strip().lower()

    round_number = None
    cup_stage = None

    if any(k in raw for k in ("кубок", "cup", "финал", "1/")):
        if "1/64" in raw:
            cup_stage = "1/64"
        elif "1/32" in raw:
            cup_stage = "1/32"
        elif "1/16" in raw:
            cup_stage = "1/16"
        elif "1/8" in raw:
            cup_stage = "1/8"
        elif "1/4" in raw:
            cup_stage = "1/4"
        elif "1/2" in raw or "полуфинал" in raw:
            cup_stage = "1/2"
        elif "финал" in raw:
            cup_stage = "Финал"
        else:
            cup_stage = raw.replace("кубок", "").strip() or "1/64"
    else:
        digits = re.findall(r"\d+", raw)
        if digits:
            round_number = int(digits[0])
        else:
            await msg.reply_text(
                "⚠️ Не удалось распознать номер тура или стадию. Введите, например: <code>3</code> или <code>1/8</code>:",
                parse_mode="HTML"
            )
            return SMM_STATE_WAIT_STAGE

    status_msg = await msg.reply_text("⏳ <b>ИИ анализирует турнирные данные и создаёт пост...</b>", parse_mode="HTML")

    # Ручной ввод не различает кубки: None — берём кубок последнего матча этой стадии
    await _build_stage_draft(update, context, team_name, round_number, cup_stage, None)

    try:
        await status_msg.delete()
    except Exception:
        pass
    return ConversationHandler.END


# ─── FSM: Правка и доработка черновика ───────────────────────────────────────

async def start_edit_prompt_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос пожеланий по правке черновика."""
    if not await _guard(update):
        return ConversationHandler.END

    if not (context.user_data.get("smm_draft") or {}).get("text"):
        await cmd_smm_hub(update, context)
        return ConversationHandler.END

    text = (
        "✏️ <b>Доработка черновика</b>\n\n"
        "Напишите текстом или надиктуйте голосом, что именно нужно исправить или добавить "
        "(например: <i>«сделай короче»</i>, <i>«добавь больше огня и эмодзи»</i>, <i>«похвали тренера»</i>)."
    )
    keyboard = [[InlineKeyboardButton("« Назад к черновику", callback_data="smm_cancel_edit")]]
    await _render(update, text, InlineKeyboardMarkup(keyboard))
    return SMM_STATE_WAIT_EDIT


async def handle_edit_prompt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Применение правки к текущему черновику через Gemini."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return ConversationHandler.END

    draft = context.user_data.get("smm_draft")
    if not draft:
        await cmd_smm_hub(update, context)
        return ConversationHandler.END

    msg = update.message
    status_msg = await msg.reply_text("⏳ <b>Вношу правки в пост...</b>", parse_mode="HTML")

    voice_obj = msg.voice or msg.video_note
    if voice_obj:
        try:
            voice_file = await voice_obj.get_file()
            audio_bytes = bytes(await voice_file.download_as_bytearray())
        except Exception as e:
            logger.exception(f"Club SMM: Failed to download edit voice: {e}")
            await status_msg.edit_text("❌ Ошибка загрузки голосового сообщения. Попробуйте отправить текстом.")
            return SMM_STATE_WAIT_EDIT
        edit_brief = await asyncio.to_thread(
            club_smm_service.transcribe_audio, audio_bytes, "video/mp4" if msg.video_note else "audio/ogg"
        )
        if not edit_brief:
            await status_msg.edit_text(
                "❌ Не удалось расшифровать запись. Попробуйте ещё раз или напишите правку текстом."
            )
            return SMM_STATE_WAIT_EDIT
    else:
        edit_brief = (msg.text or "").strip() or "Улучши динамику и стиль."

    user_display = f"@{user.username}" if (user and user.username) else (user.first_name if user else "")
    edited_text = await asyncio.to_thread(
        club_smm_service.edit_club_post,
        draft.get("team_name", DEFAULT_CLUB),
        draft.get("text", ""),
        edit_brief,
        user_display,
    )

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not edited_text:
        # ИИ недоступен: черновик остаётся прежним, а не подменяется шаблоном с текстом инструкции
        await msg.reply_text(
            "⚠️ Не удалось внести правку — ИИ сейчас не отвечает. Черновик остался без изменений, "
            "попробуйте ещё раз чуть позже.",
        )
        await _show_draft_preview(update, context, draft.get("text", ""))
        return ConversationHandler.END

    draft["text"] = edited_text
    await _show_draft_preview(update, context, edited_text)
    return ConversationHandler.END


async def cancel_edit_and_return(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена правки и возврат к предпросмотру."""
    if not await _guard(update):
        return ConversationHandler.END
    draft = context.user_data.get("smm_draft")
    if draft and draft.get("text"):
        await _show_draft_preview(update, context, draft["text"])
    else:
        await cmd_smm_hub(update, context)
    return ConversationHandler.END


# ─── FSM: Прикрепление своего фото ──────────────────────────────────────────

async def start_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос пользовательского фото для публикации."""
    if not await _guard(update):
        return ConversationHandler.END

    if not (context.user_data.get("smm_draft") or {}).get("text"):
        await cmd_smm_hub(update, context)
        return ConversationHandler.END

    text = (
        "📷 <b>Прикрепление своего фото к посту</b>\n\n"
        "Отправьте фотографию или изображение сюда в чат.\n\n"
        "Она будет прикреплена к посту и опубликована в канале вместе с готовым текстом."
    )
    keyboard = [[InlineKeyboardButton("« Назад к черновику", callback_data="smm_cancel_photo")]]
    await _render(update, text, InlineKeyboardMarkup(keyboard))
    return SMM_STATE_WAIT_PHOTO


async def handle_custom_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка полученного фото от пользователя."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return ConversationHandler.END

    draft = context.user_data.get("smm_draft")
    if not draft:
        await cmd_smm_hub(update, context)
        return ConversationHandler.END

    msg = update.message
    file_id = None
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        file_id = msg.document.file_id

    if not file_id:
        await msg.reply_text("⚠️ Пожалуйста, отправьте именно фото или изображение.", parse_mode="HTML")
        return SMM_STATE_WAIT_PHOTO

    draft["custom_photo_id"] = file_id
    draft["photo_mode"] = "custom"

    await msg.reply_text("✅ <b>Фото успешно прикреплено к черновику!</b>", parse_mode="HTML")
    await _show_draft_preview(update, context, draft.get("text", ""))
    return ConversationHandler.END


async def handle_photo_input_invalid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь прислал текст вместо фото."""
    keyboard = [[InlineKeyboardButton("« Назад к черновику", callback_data="smm_cancel_photo")]]
    if update.message:
        await update.message.reply_text(
            "⚠️ Ожидается фото. Отправьте картинку или нажмите кнопку отмены:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return SMM_STATE_WAIT_PHOTO


async def cb_smm_remove_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет прикреплённое фото/скриншот из черновика."""
    if not await _guard(update, answer=False):
        return
    await _safe_answer(update.callback_query, "Фото откреплено от черновика")

    draft = context.user_data.get("smm_draft")
    if draft:
        draft["photo_mode"] = "none"
        draft.pop("custom_photo_id", None)

    if draft and draft.get("text"):
        await _show_draft_preview(update, context, draft["text"])
    else:
        await cmd_smm_hub(update, context)


async def cb_smm_attach_match_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Прикрепляет скриншоты матча из базы данных к черновику."""
    if not await _guard(update, answer=False):
        return

    draft = context.user_data.get("smm_draft")
    if draft:
        if not draft.get("match_photos"):
            team_name = draft.get("team_name")
            if team_name:
                post_type = draft.get("post_type")
                if post_type == "stage":
                    draft["match_photos"] = await asyncio.to_thread(
                        database.get_stage_match_photos,
                        team_name,
                        round_number=draft.get("round_number"),
                        cup_stage=draft.get("cup_stage"),
                        cup_division_id=draft.get("cup_division_id"),
                    )
                else:
                    draft["match_photos"] = await asyncio.to_thread(database.get_last_match_photos, team_name)

        if draft.get("match_photos"):
            draft["photo_mode"] = "match"
            await _safe_answer(update.callback_query, "Скриншот(ы) матча прикреплены к посту")
        else:
            await _safe_answer(update.callback_query, "Скриншотов этого матча в базе нет", show_alert=True)

        await _show_draft_preview(update, context, draft.get("text", ""))
    else:
        await _safe_answer(update.callback_query)
        await cmd_smm_hub(update, context)


# ─── FSM: Настройка канала назначения ───────────────────────────────────────

async def start_channel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос целевого Telegram-канала."""
    if not await _guard(update):
        return ConversationHandler.END

    curr = get_target_channel()
    curr_line = f"\nТекущий канал: <code>{html.escape(curr)}</code>\n" if curr else "\nКанал ещё не задан.\n"

    text = (
        f"⚙️ <b>Настройка канала для публикации</b>\n{curr_line}\n"
        f"Отправьте юзернейм канала (например: <code>@besiktas_tg</code>) или его цифровой ID "
        f"(например: <code>-1001234567890</code>).\n\n"
        f"⚠️ <b>Важно:</b> Бот должен быть предварительно добавлен в канал как <b>Администратор</b> "
        f"с правом публикации сообщений!"
    )
    keyboard = [[InlineKeyboardButton("« Отмена", callback_data="smm_hub")]]
    await _render(update, text, InlineKeyboardMarkup(keyboard))

    return SMM_STATE_WAIT_CHANNEL


async def _verify_channel(bot, target: str):
    """Проверяет канал и права бота. Возвращает (chat, None) или (None, текст_ошибки_HTML)."""
    try:
        chat = await bot.get_chat(target)
        bot_member = await chat.get_member(bot.id)
    except Exception as e:
        logger.warning(f"Club SMM: Failed to verify channel {target}: {e}")
        return None, (
            f"❌ Не удалось получить доступ к каналу <code>{html.escape(target)}</code>.\n\n"
            f"<b>Причина:</b> {html.escape(str(e))}\n\n"
            f"Убедитесь, что бот добавлен в канал и у него есть права администратора."
        )

    title = html.escape(chat.title or str(target))
    if bot_member.status not in ("administrator", "creator"):
        return None, (
            f"⚠️ Бот видит канал <b>{title}</b>, но ещё не назначен администратором!\n"
            f"Выдайте боту права администратора с разрешением на отправку сообщений и повторите попытку."
        )
    if getattr(bot_member, "can_post_messages", True) is False:
        return None, (
            f"⚠️ У бота нет права <b>публиковать сообщения</b> в канале <b>{title}</b>.\n"
            f"Включите это право в настройках администраторов канала и повторите попытку."
        )
    return chat, None


async def handle_channel_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохранение и проверка доступности канала."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return ConversationHandler.END

    target = normalize_telegram_channel((update.message.text or "").strip())
    if not target:
        await update.message.reply_text("⚠️ Отправьте @username канала или его числовой ID.")
        return SMM_STATE_WAIT_CHANNEL

    chat, error = await _verify_channel(context.bot, target)
    if error:
        await update.message.reply_text(
            f"{error}\n\nПопробуйте ещё раз или нажмите /cancel.", parse_mode="HTML"
        )
        return SMM_STATE_WAIT_CHANNEL

    await asyncio.to_thread(database.set_config, CONFIG_CHANNEL_KEY, str(target))
    await update.message.reply_text(
        f"✅ <b>Канал успешно привязан!</b>\n\n"
        f"• <b>Название:</b> {html.escape(chat.title or str(target))}\n"
        f"• <b>ID/Username:</b> <code>{html.escape(str(target))}</code>\n\n"
        f"Теперь посты из SMM-центра будут публиковаться напрямую сюда.",
        parse_mode="HTML"
    )
    await cmd_smm_hub(update, context)
    return ConversationHandler.END


async def cmd_set_club_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Быстрая команда /set_club_channel @channel_name."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: <code>/set_club_channel @username_канала</code> или <code>/set_club_channel -100xxxxxxxxxx</code>",
            parse_mode="HTML"
        )
        return

    target = normalize_telegram_channel(context.args[0].strip())
    chat, error = await _verify_channel(context.bot, target)
    if error:
        await update.message.reply_text(error, parse_mode="HTML")
        return

    await asyncio.to_thread(database.set_config, CONFIG_CHANNEL_KEY, str(target))
    await update.message.reply_text(
        f"✅ Канал <b>{html.escape(chat.title or str(target))}</b> (<code>{html.escape(str(target))}</code>) "
        f"привязан для публикации постов клуба!",
        parse_mode="HTML"
    )


# ─── Публикация в канал ─────────────────────────────────────────────────────

async def _send_with_photo(bot, channel, photo, text: str):
    """Фото + подпись. Подпись Telegram ограничена 1024 символами: длинный текст идёт вторым сообщением."""
    caption = club_smm_service._sanitize_html(text)
    if len(caption) <= club_smm_service.PUBLISH_CAPTION_MAX_CHARS:
        return await bot.send_photo(chat_id=channel, photo=photo, caption=caption, parse_mode="HTML")
    await bot.send_photo(chat_id=channel, photo=photo)
    return await _send_text(bot, channel, text)


async def _send_text(bot, channel, text: str):
    return await bot.send_message(
        chat_id=channel,
        text=club_smm_service._fit_html(text, 4000),
        parse_mode="HTML",
    )


async def _send_media_group(bot, channel, photos: list, text: str):
    from telegram import InputMediaPhoto

    caption = club_smm_service._sanitize_html(text)
    long_text = len(caption) > club_smm_service.PUBLISH_CAPTION_MAX_CHARS
    media = [
        InputMediaPhoto(
            media=p_id,
            caption=caption if (i == 0 and not long_text) else None,
            parse_mode="HTML" if (i == 0 and not long_text) else None,
        )
        for i, p_id in enumerate(photos[:10])
    ]
    sent = await bot.send_media_group(chat_id=channel, media=media)
    if long_text:
        return await _send_text(bot, channel, text)
    return sent[0] if sent else None


async def _publish_draft(bot, channel, mode: str, draft: dict) -> tuple:
    """Отправляет черновик в канал. Возвращает (сообщение, пояснение_если_режим_пришлось_заменить)."""
    text = draft["text"]
    team_name = draft.get("team_name", DEFAULT_CLUB)

    if mode == "ai_photo":
        post_type = draft.get("post_type", "matchday")
        brief = draft.get("custom_brief", "")
        if post_type == "stage":
            brief = brief or (f"Кубок {draft.get('cup_stage')}" if draft.get("cup_stage") else f"Тур {draft.get('round_number')}")
        buf = await asyncio.to_thread(club_smm_service.generate_club_ai_photo, team_name, post_type, brief)
        if buf:
            return await _send_with_photo(bot, channel, buf, text), None
        return await _send_text(bot, channel, text), "картинку создать не удалось — пост ушёл текстом"

    if mode == "match_photos":
        photos = draft.get("match_photos") or []
        if len(photos) == 1:
            return await _send_with_photo(bot, channel, photos[0], text), None
        if len(photos) > 1:
            return await _send_media_group(bot, channel, photos, text), None
        return await _send_text(bot, channel, text), "скриншотов матча нет — пост ушёл текстом"

    if mode == "custom_photo":
        photo_id = draft.get("custom_photo_id")
        if photo_id:
            return await _send_with_photo(bot, channel, photo_id, text), None
        return await _send_text(bot, channel, text), "своё фото не найдено — пост ушёл текстом"

    if mode in ("card", "media"):
        buf = await asyncio.to_thread(club_smm_service.generate_club_smm_media, team_name)
        if buf:
            return await _send_with_photo(bot, channel, buf, text), None
        return await _send_text(bot, channel, text), "карточку создать не удалось — пост ушёл текстом"

    return await _send_text(bot, channel, text), None


async def cb_smm_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправка поста в канал: текстом, со скринами матча, своим фото или картинкой клуба."""
    query = update.callback_query
    if not query or not await _guard(update, answer=False):
        return

    channel = get_target_channel()
    if not channel:
        await _safe_answer(query, "⚠️ Канал не настроен! Сначала привяжите канал через «⚙️ Настроить канал».", show_alert=True)
        return

    draft = context.user_data.get("smm_draft")
    if not draft or not draft.get("text"):
        await _safe_answer(query, "⚠️ Черновик не найден (возможно, уже опубликован). Сгенерируйте пост заново.", show_alert=True)
        return

    await _safe_answer(query)
    mode = query.data.split(":")[1] if ":" in query.data else "text"
    team_name = draft.get("team_name", DEFAULT_CLUB)

    try:
        await query.edit_message_text("🚀 <b>Публикую в канал...</b>", parse_mode="HTML")
    except Exception:
        pass

    try:
        sent_msg, note = await _publish_draft(context.bot, channel, mode, draft)

        if sent_msg is not None:
            try:
                await asyncio.to_thread(
                    database.save_published_club_smm_post,
                    team_name,
                    str(channel),
                    sent_msg.message_id,
                    draft.get("post_type", "custom"),
                    draft["text"],
                    mode,
                )
            except Exception as save_err:
                logger.warning(f"Club SMM: Failed to save post history: {save_err}")

        post_link = None
        if sent_msg is not None:
            if channel.startswith("@"):
                post_link = f"https://t.me/{channel.lstrip('@')}/{sent_msg.message_id}"
            elif str(channel).startswith("-100"):
                post_link = f"https://t.me/c/{str(channel)[4:]}/{sent_msg.message_id}"

        # Пост ушёл — черновик закрываем, чтобы повторный клик не задвоил публикацию
        context.user_data.pop("smm_draft", None)

        success_text = "✅ <b>Пост успешно опубликован в канале!</b>"
        if note:
            success_text += f"\n\nℹ️ {html.escape(note)}"
        keyboard = []
        if post_link:
            keyboard.append([InlineKeyboardButton("🔗 Открыть пост в Telegram", url=post_link)])
        keyboard.append([InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")])
        await _render(update, success_text, InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.exception(f"Club SMM: Failed to publish post to {channel}: {e}")
        err_text = (
            f"❌ <b>Ошибка при публикации в канал:</b>\n\n"
            f"<code>{html.escape(str(e))}</code>\n\n"
            f"Проверьте, что бот является администратором канала с правом отправки сообщений. "
            f"Черновик сохранён."
        )
        keyboard = [
            [InlineKeyboardButton("📝 К черновику", callback_data="smm_draft")],
            [InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")],
        ]
        await _render(update, err_text, InlineKeyboardMarkup(keyboard))


async def cancel_smm_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сброс FSM диалога (кнопка «Отмена» / команда /cancel)."""
    if update.callback_query and not await _guard(update):
        return ConversationHandler.END
    await cmd_smm_hub(update, context)
    return ConversationHandler.END


async def on_smm_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Диалог простаивал слишком долго (conversation_timeout) — сообщаем и завершаем."""
    chat = getattr(update, "effective_chat", None)
    if chat:
        try:
            await chat.send_message("⌛ Время ожидания ответа истекло. Откройте SMM-центр заново: /club_post")
        except Exception:
            pass
    return ConversationHandler.END


# Кнопочные маршруты SMM-центра. Регистрируются и как обычные хендлеры, и (через
# _ends_conversation) как fallbacks диалога — иначе кнопка, нажатая посреди ввода,
# отработала бы, но диалог остался бы в старом состоянии и съел следующее сообщение.
SMM_BUTTON_ROUTES = [
    (r"^smm_hub$", cb_smm_hub),
    (r"^smm_choose_stage$", cb_smm_choose_stage),
    (r"^smm_stage:(league|cup):.+$", cb_smm_stage_selected),
    (r"^smm_gen:[\w_]+$", cb_smm_generate),
    (r"^smm_regen$", cb_smm_regenerate),
    (r"^smm_publish:(text|media|card|ai_photo|custom_photo|match_photos)$", cb_smm_publish),
    (r"^smm_remove_photo$", cb_smm_remove_photo),
    (r"^smm_attach_match_photos$", cb_smm_attach_match_photos),
    (r"^(smm_cancel_edit|smm_cancel_photo|smm_draft)$", cancel_edit_and_return),
]


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Перехватывает сообщения, опубликованные напрямую в привязанном канале клуба
    (не через SMM-центр), и сохраняет их в историю публикаций club_smm_posts.
    Это позволяет ИИ при следующей генерации видеть хронологию всех постов в канале,
    включая написанные вручную.
    """
    if update.edited_channel_post:
        return
    msg = update.channel_post or update.message
    if not msg:
        return

    # Обрабатываем только посты из нашего привязанного канала
    channel = get_target_channel()
    if not channel:
        return

    chat = update.effective_chat
    if not chat:
        return

    # Сопоставляем канал: @username или -100... ID
    chat_match = False
    if channel.startswith("@") and chat.username and f"@{chat.username}" == channel:
        chat_match = True
    elif str(chat.id) == str(channel):
        chat_match = True

    if not chat_match:
        return

    text = msg.text or msg.caption or ""
    if not text.strip():
        return

    # Определяем команду клуба по каналу
    team_name = database.get_config("my_club_team") or DEFAULT_CLUB

    try:
        await asyncio.to_thread(
            database.save_published_club_smm_post,
            team_name,
            str(chat.id),
            msg.message_id,
            "channel_post",
            text,
            None,
        )
    except Exception as e:
        logger.debug(f"Club SMM on_channel_post: failed to save: {e}")
