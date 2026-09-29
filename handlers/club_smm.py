"""
handlers/club_smm.py

Персональный SMM-центр для владельца клуба (главного админа @sp1r1tVSA / ID 1642770076).
Генерация контента через Gemini (все доступные модели с ротацией), согласование черновика
в ЛС с ботом и публикация в привязанный Telegram-канал клуба (текстом или с инфографикой).
"""

import asyncio
import html
import io
import logging
import re
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters

import config
import database
from handlers.base import is_admin
from services import club_smm_service
from club_registry import resolve_team_name

logger = logging.getLogger(__name__)

# Состояния FSM ConversationHandler
SMM_STATE_WAIT_PROMPT = 1
SMM_STATE_WAIT_EDIT = 2
SMM_STATE_WAIT_CHANNEL = 3
SMM_STATE_WAIT_STAGE = 4
SMM_STATE_WAIT_PHOTO = 5

CONFIG_CHANNEL_KEY = "my_club_channel"


def is_smm_allowed(user_id: int | None) -> bool:
    """Доступ строго для владельца клуба / главного администратора."""
    if not user_id:
        return False
    if user_id == 1642770076:
        return True
    if user_id in getattr(config, "ADMIN_IDS", []):
        return True
    return is_admin(user_id)


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
    """Определяет клуб пользователя из базы, по умолчанию «Бешикташ»."""
    team = await asyncio.to_thread(database.get_user_team, user_id)
    if team:
        return resolve_team_name(team) or team
    return "Бешикташ"


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

    markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=markup)


async def cb_smm_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Коллбэк возврата в меню SMM-центра."""
    query = update.callback_query
    if query:
        await query.answer()
    await cmd_smm_hub(update, context)
    return ConversationHandler.END


# ─── Генерация черновика ────────────────────────────────────────────────────

async def cb_smm_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерация поста выбранного типа и показ экрана предпросмотра."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return

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

    context.user_data["smm_draft"] = {
        "text": generated_text,
        "team_name": team_name,
        "post_type": post_type,
        "custom_brief": "",
    }

    await _show_draft_preview(update, context, generated_text)


async def cb_smm_choose_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отображение меню выбора конкретного тура чемпионата или кубковой стадии клуба."""
    query = update.callback_query
    if query:
        await query.answer()

    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return ConversationHandler.END

    team_name = await _resolve_user_club(user.id)
    data = await asyncio.to_thread(club_smm_service.get_club_stages_and_rounds, team_name)
    cup_stages = data.get("cup_stages", [])
    league_rounds = data.get("league_rounds", [])

    keyboard = []

    # 1. Кубковые стадии (если есть)
    if cup_stages:
        cup_row = []
        for st in cup_stages:
            stage_name = st["stage"]
            score = st.get("score_series")
            status = st.get("status")
            if status == "completed":
                btn_text = f"🏆 {stage_name} ({score} ✅)"
            elif status == "in_progress":
                btn_text = f"🏆 {stage_name} ({score} ⏳)"
            else:
                btn_text = f"🏆 {stage_name} (⏳)"
            cup_row.append(InlineKeyboardButton(btn_text, callback_data=f"smm_stage:cup:{stage_name}"))
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

    if query and query.message:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=markup)
    elif update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=markup)

    return ConversationHandler.END


async def cb_smm_stage_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Генерация поста для выбранного тура лиги или стадии кубка."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return

    parts = query.data.split(":")
    if len(parts) < 3:
        return
    stage_type = parts[1]  # 'league' or 'cup'
    stage_val = parts[2]

    team_name = await _resolve_user_club(user.id)
    round_number = int(stage_val) if stage_type == "league" and stage_val.isdigit() else None
    cup_stage = stage_val if stage_type == "cup" else None
    stage_label = f"стадии кубка {cup_stage}" if cup_stage else f"тура {round_number}"

    loading_text = f"⏳ <b>ИИ анализирует статистику {stage_label} и пишет пост...</b>"
    try:
        await query.edit_message_text(loading_text, parse_mode="HTML")
    except Exception:
        pass

    generated_text = await asyncio.to_thread(
        club_smm_service.generate_stage_post,
        team_name=team_name,
        round_number=round_number,
        cup_stage=cup_stage,
    )

    context.user_data["smm_draft"] = {
        "text": generated_text,
        "team_name": team_name,
        "post_type": "stage",
        "round_number": round_number,
        "cup_stage": cup_stage,
        "custom_brief": f"Кубок {cup_stage}" if cup_stage else f"Тур {round_number}",
    }

    await _show_draft_preview(update, context, generated_text)


async def _show_draft_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, draft_text: str) -> None:
    """Показывает экран предпросмотра черновика с кнопками публикации и правки."""
    channel = get_target_channel()
    ch_label = f" ({channel})" if channel else " (⚠️ канал не задан)"

    draft = context.user_data.get("smm_draft") or {}
    has_photo = bool(draft.get("custom_photo_id"))
    photo_line = "\n🖼 <b>Своё фото:</b> прикреплено ✅" if has_photo else ""

    preview_message = (
        f"📝 <b>Черновик для публикации:</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"{draft_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Канал:</b> {ch_label}"
        f"{photo_line}"
    )

    if has_photo:
        keyboard = [
            [
                InlineKeyboardButton("🚀 Опубликовать (со своим фото)", callback_data="smm_publish:custom_photo"),
            ],
            [
                InlineKeyboardButton("🎨 С ИИ-фото (Gemini)", callback_data="smm_publish:ai_photo"),
                InlineKeyboardButton("🏛 С карточкой клуба", callback_data="smm_publish:card"),
            ],
            [
                InlineKeyboardButton("📷 Заменить фото", callback_data="smm_enter_photo"),
                InlineKeyboardButton("❌ Убрать своё фото", callback_data="smm_remove_photo"),
            ],
            [
                InlineKeyboardButton("🚀 Опубликовать (Текст)", callback_data="smm_publish:text"),
            ],
            [
                InlineKeyboardButton("🔄 Другой вариант текста", callback_data="smm_regen"),
                InlineKeyboardButton("✏️ Правка / Уточнить", callback_data="smm_enter_edit"),
            ],
            [
                InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("🚀 Опубликовать (Текст)", callback_data="smm_publish:text"),
            ],
            [
                InlineKeyboardButton("🎨 С ИИ-фото (Gemini)", callback_data="smm_publish:ai_photo"),
                InlineKeyboardButton("🏛 С карточкой клуба", callback_data="smm_publish:card"),
            ],
            [
                InlineKeyboardButton("📷 Прикрепить своё фото", callback_data="smm_enter_photo"),
            ],
            [
                InlineKeyboardButton("🔄 Другой вариант текста", callback_data="smm_regen"),
                InlineKeyboardButton("✏️ Правка / Уточнить", callback_data="smm_enter_edit"),
            ],
            [
                InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")
            ]
        ]
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
    if query:
        await query.answer()

    draft = context.user_data.get("smm_draft")
    if not draft:
        await cb_smm_hub(update, context)
        return

    try:
        await query.edit_message_text("🔄 <b>Генерирую новый вариант через другую модель Gemini...</b>", parse_mode="HTML")
    except Exception:
        pass

    post_type = draft.get("post_type", "matchday")
    if post_type == "stage":
        generated_text = await asyncio.to_thread(
            club_smm_service.generate_stage_post,
            team_name=draft.get("team_name", "Бешикташ"),
            round_number=draft.get("round_number"),
            cup_stage=draft.get("cup_stage"),
        )
    else:
        user = update.effective_user
        user_display = f"@{user.username}" if (user and user.username) else (user.first_name if user else "")
        generated_text = await asyncio.to_thread(
            club_smm_service.generate_club_post,
            team_name=draft.get("team_name", "Бешикташ"),
            post_type=post_type,
            custom_brief=draft.get("custom_brief", ""),
            user_name=user_display,
        )

    draft["text"] = generated_text
    await _show_draft_preview(update, context, generated_text)


# ─── FSM: Свой бриф / Голосовое сообщение ───────────────────────────────────

async def start_custom_prompt_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос пользовательской темы или голосового сообщения."""
    query = update.callback_query
    if query:
        await query.answer()

    text = (
        "🎙 <b>Свой бриф / Тема для поста</b>\n\n"
        "Отправьте тему для поста текстом или запишите голосовое сообщение (аудиокружок/войс).\n\n"
        "<i>Примеры:</i>\n"
        "• «Напиши бодрый пост, как мы разгромили соперника в кубке»\n"
        "• «Сделай акцент на сумасшедшем сейве вратаря и дерзкой игре Троссарда»\n"
        "• «Пост-настрой перед принципиальным дерби»\n\n"
        "Для отмены нажмите кнопку ниже."
    )
    keyboard = [[InlineKeyboardButton("« Отмена", callback_data="smm_hub")]]
    markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=markup)

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

    if msg.voice:
        try:
            voice_file = await msg.voice.get_file()
            audio_bytes = bytes(await voice_file.download_as_bytearray())
        except Exception as e:
            logger.exception(f"Club SMM: Failed to download voice file: {e}")
            await status_msg.edit_text("❌ Ошибка загрузки голосового сообщения. Попробуйте отправить текстом.")
            return SMM_STATE_WAIT_PROMPT
    elif msg.text:
        custom_brief = msg.text.strip()

    user_display = f"@{user.username}" if user.username else (user.first_name or "")
    generated_text = await asyncio.to_thread(
        club_smm_service.generate_club_post,
        team_name=team_name,
        post_type="custom",
        custom_brief=custom_brief,
        audio_bytes=audio_bytes,
        audio_mime="audio/ogg" if audio_bytes else "audio/ogg",
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
    }

    await _show_draft_preview(update, context, generated_text)
    return ConversationHandler.END


# ─── FSM: Ручной ввод тура / стадии кубка ───────────────────────────────────

async def start_stage_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос номера тура или стадии кубка вручную."""
    query = update.callback_query
    if query:
        await query.answer()

    text = (
        "✏️ <b>Ручной ввод тура или стадии кубка</b>\n\n"
        "Отправьте номер тура (например: <code>5</code>) "
        "или стадию кубка (например: <code>1/64</code>, <code>1/8</code>, <code>финал</code>).\n\n"
        "ИИ автоматически найдёт данные матча в базе и создаст ёмкий пост в один абзац."
    )
    keyboard = [[InlineKeyboardButton("« Назад к выбору", callback_data="smm_choose_stage")]]
    markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=markup)

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

    generated_text = await asyncio.to_thread(
        club_smm_service.generate_stage_post,
        team_name=team_name,
        round_number=round_number,
        cup_stage=cup_stage,
    )

    try:
        await status_msg.delete()
    except Exception:
        pass

    context.user_data["smm_draft"] = {
        "text": generated_text,
        "team_name": team_name,
        "post_type": "stage",
        "round_number": round_number,
        "cup_stage": cup_stage,
        "custom_brief": f"Кубок {cup_stage}" if cup_stage else f"Тур {round_number}",
    }

    await _show_draft_preview(update, context, generated_text)
    return ConversationHandler.END


# ─── FSM: Правка и доработка черновика ───────────────────────────────────────

async def start_edit_prompt_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос пожеланий по правке черновика."""
    query = update.callback_query
    if query:
        await query.answer()

    text = (
        "✏️ <b>Доработка черновика</b>\n\n"
        "Напишите текстом или надиктуйте голосом, что именно нужно исправить или добавить "
        "(например: <i>«сделай короче»</i>, <i>«добавь больше огня и эмодзи»</i>, <i>«похвали тренера»</i>)."
    )
    keyboard = [[InlineKeyboardButton("« Назад к черновику", callback_data="smm_cancel_edit")]]
    markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
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
    edit_brief = msg.text.strip() if msg.text else "Улучши динамику и стиль."

    status_msg = await msg.reply_text("⏳ <b>Вношу правки в пост...</b>", parse_mode="HTML")

    combined_brief = (
        f"ТЕКУЩИЙ ЧЕРНОВИК:\n{draft.get('text', '')}\n\n"
        f"ИНСТРУКЦИЯ ПО ПРАВКЕ:\n{edit_brief}"
    )

    user_display = f"@{user.username}" if (user and user.username) else (user.first_name if user else "")
    generated_text = await asyncio.to_thread(
        club_smm_service.generate_club_post,
        team_name=draft.get("team_name", "Бешикташ"),
        post_type="custom",
        custom_brief=combined_brief,
        user_name=user_display,
    )

    try:
        await status_msg.delete()
    except Exception:
        pass

    draft["text"] = generated_text
    await _show_draft_preview(update, context, generated_text)
    return ConversationHandler.END


async def cancel_edit_and_return(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена правки и возврат к предпросмотру."""
    query = update.callback_query
    if query:
        await query.answer()
    draft = context.user_data.get("smm_draft")
    if draft and draft.get("text"):
        await _show_draft_preview(update, context, draft["text"])
    else:
        await cmd_smm_hub(update, context)
    return ConversationHandler.END


# ─── FSM: Прикрепление своего фото ──────────────────────────────────────────

async def start_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос пользовательского фото для публикации."""
    query = update.callback_query
    if query:
        await query.answer()

    text = (
        "📷 <b>Прикрепление своего фото к посту</b>\n\n"
        "Отправьте фотографию или изображение сюда в чат.\n\n"
        "Она будет прикреплена к посту и опубликована в канале вместе с готовым текстом."
    )
    keyboard = [[InlineKeyboardButton("« Назад к черновику", callback_data="smm_cancel_photo")]]
    markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=markup)
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
    """Удаляет прикреплённое фото из черновика."""
    query = update.callback_query
    if query:
        await query.answer("Фото откреплено от черновика")

    draft = context.user_data.get("smm_draft")
    if draft and "custom_photo_id" in draft:
        del draft["custom_photo_id"]

    if draft and draft.get("text"):
        await _show_draft_preview(update, context, draft["text"])
    else:
        await cb_smm_hub(update, context)


# ─── FSM: Настройка канала назначения ───────────────────────────────────────

async def start_channel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрос целевого Telegram-канала."""
    query = update.callback_query
    if query:
        await query.answer()

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
    markup = InlineKeyboardMarkup(keyboard)

    if query and query.message:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode="HTML", reply_markup=markup)

    return SMM_STATE_WAIT_CHANNEL


async def handle_channel_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохранение и проверка доступности канала."""
    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return ConversationHandler.END

    raw = update.message.text.strip()
    target = normalize_telegram_channel(raw)

    # Проверка прав бота в канале
    try:
        chat = await context.bot.get_chat(target)
        bot_member = await chat.get_member(context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await update.message.reply_text(
                f"⚠️ Бот видит канал <b>{html.escape(chat.title)}</b>, но ещё не назначен администратором!\n"
                f"Выдайте боту права администратора с разрешением на отправку сообщений и повторите попытку.",
                parse_mode="HTML"
            )
            return SMM_STATE_WAIT_CHANNEL

        # Успешная валидация
        await asyncio.to_thread(database.set_config, CONFIG_CHANNEL_KEY, str(target))

        await update.message.reply_text(
            f"✅ <b>Канал успешно привязан!</b>\n\n"
            f"• <b>Название:</b> {html.escape(chat.title)}\n"
            f"• <b>ID/Username:</b> <code>{html.escape(str(target))}</code>\n\n"
            f"Теперь посты из SMM-центра будут публиковаться напрямую сюда.",
            parse_mode="HTML"
        )
        await cmd_smm_hub(update, context)
        return ConversationHandler.END

    except Exception as e:
        logger.warning(f"Club SMM: Failed to verify channel {target}: {e}")
        await update.message.reply_text(
            f"❌ Не удалось получить доступ к каналу <code>{html.escape(target)}</code>.\n\n"
            f"<b>Причина:</b> {html.escape(str(e))}\n\n"
            f"Убедитесь, что бот добавлен в канал и у него есть права администратора. Попробуйте ещё раз или нажмите /cancel:",
            parse_mode="HTML"
        )
        return SMM_STATE_WAIT_CHANNEL


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

    raw = context.args[0].strip()
    target = normalize_telegram_channel(raw)

    try:
        chat = await context.bot.get_chat(target)
        await asyncio.to_thread(database.set_config, CONFIG_CHANNEL_KEY, str(target))
        await update.message.reply_text(
            f"✅ Канал <b>{html.escape(chat.title)}</b> (<code>{html.escape(str(target))}</code>) привязан для публикации постов клуба!",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка доступа к каналу: {html.escape(str(e))}", parse_mode="HTML")


# ─── Публикация в канал ─────────────────────────────────────────────────────

async def cb_smm_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправка поста в канал: текстом или с графической карточкой клуба."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_smm_allowed(user.id):
        return

    channel = get_target_channel()
    if not channel:
        await query.answer("⚠️ Канал не настроен! Сначала привяжите канал через меню «⚙️ Настроить канал».", show_alert=True)
        return

    draft = context.user_data.get("smm_draft")
    if not draft or not draft.get("text"):
        await query.answer("⚠️ Черновик не найден. Сгенерируйте пост заново.", show_alert=True)
        return

    mode = query.data.split(":")[1] if ":" in query.data else "text"
    text_content = draft["text"]
    team_name = draft.get("team_name", "Бешикташ")

    try:
        await query.edit_message_text("🚀 <b>Публикую в канал...</b>", parse_mode="HTML")
    except Exception:
        pass

    try:
        sent_msg = None
        if mode == "ai_photo":
            post_type = draft.get("post_type", "matchday")
            brief = draft.get("custom_brief", "")
            if post_type == "stage":
                brief = brief or (f"Кубок {draft.get('cup_stage')}" if draft.get("cup_stage") else f"Тур {draft.get('round_number')}")
            buf = await asyncio.to_thread(club_smm_service.generate_club_ai_photo, team_name, post_type, brief)
            caption = club_smm_service._fit_html(text_content, club_smm_service.CAPTION_MAX_CHARS)
            if buf:
                sent_msg = await context.bot.send_photo(
                    chat_id=channel,
                    photo=buf,
                    caption=caption,
                    parse_mode="HTML"
                )
            else:
                sent_msg = await context.bot.send_message(
                    chat_id=channel,
                    text=text_content,
                    parse_mode="HTML"
                )
        elif mode == "custom_photo":
            photo_id = draft.get("custom_photo_id")
            caption = club_smm_service._fit_html(text_content, club_smm_service.CAPTION_MAX_CHARS)
            if photo_id:
                sent_msg = await context.bot.send_photo(
                    chat_id=channel,
                    photo=photo_id,
                    caption=caption,
                    parse_mode="HTML"
                )
            else:
                sent_msg = await context.bot.send_message(
                    chat_id=channel,
                    text=text_content,
                    parse_mode="HTML"
                )
        elif mode in ("card", "media"):
            buf = await asyncio.to_thread(club_smm_service.generate_club_smm_media, team_name)
            caption = club_smm_service._fit_html(text_content, club_smm_service.CAPTION_MAX_CHARS)
            if buf:
                sent_msg = await context.bot.send_photo(
                    chat_id=channel,
                    photo=buf,
                    caption=caption,
                    parse_mode="HTML"
                )
            else:
                sent_msg = await context.bot.send_message(
                    chat_id=channel,
                    text=text_content,
                    parse_mode="HTML"
                )
        else:
            sent_msg = await context.bot.send_message(
                chat_id=channel,
                text=text_content,
                parse_mode="HTML"
            )

        # Формирование прямой ссылки на пост
        post_link = None
        if channel.startswith("@"):
            post_link = f"https://t.me/{channel.lstrip('@')}/{sent_msg.message_id}"
        elif str(channel).startswith("-100"):
            clean_id = str(channel)[4:]
            post_link = f"https://t.me/c/{clean_id}/{sent_msg.message_id}"

        success_text = f"✅ <b>Пост успешно опубликован в канале!</b>"
        keyboard = []
        if post_link:
            keyboard.append([InlineKeyboardButton("🔗 Открыть пост в Telegram", url=post_link)])
        keyboard.append([InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")])

        markup = InlineKeyboardMarkup(keyboard)
        if query.message:
            await query.edit_message_text(success_text, parse_mode="HTML", reply_markup=markup)

    except Exception as e:
        logger.exception(f"Club SMM: Failed to publish post to {channel}: {e}")
        err_text = (
            f"❌ <b>Ошибка при публикации в канал:</b>\n\n"
            f"<code>{html.escape(str(e))}</code>\n\n"
            f"Проверьте, что бот является администратором канала с правом отправки сообщений."
        )
        keyboard = [
            [InlineKeyboardButton("« В меню SMM", callback_data="smm_hub")]
        ]
        if query.message:
            await query.edit_message_text(err_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def cancel_smm_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сброс FSM диалога."""
    if update.callback_query:
        await update.callback_query.answer()
    await cmd_smm_hub(update, context)
    return ConversationHandler.END
