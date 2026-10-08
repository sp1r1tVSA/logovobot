"""Модуль централизованной рассылки для глобального администратора.

Позволяет отправлять специальные брендированные сообщения о новостях лиги,
новой линии ставок Logovo.bet, старте туров/матчей и свободные объявления.
Поддерживает:
1. Выбор аудитории: ЛС игрокам, топики дивизионов или везде (ЛС + топики).
2. Пошаговый интерактивный мастер в ЛС с предпросмотром перед отправкой.
3. Быструю текстовую команду: «Темшик рассылка [ставки|новости|тур] <текст>».
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

import config
import database
from handlers.base import is_global_admin
from services import admin_journal

logger = logging.getLogger(__name__)

# Категории и шаблоны специальных рассылок
BROADCAST_CATEGORIES = {
    "betting": {
        "title": "🎰 Линия ставок (Логово Фифарей)",
        "header": "🎰 <b>ЛОГОВО ФИФАРЕЙ | НОВАЯ ЛИНИЯ СТАВОК</b>",
        "footer": "🔥 <i>Делайте ваши ставки и умножайте банк в турнирной линии!</i>",
        "btn_type": "betting",
    },
    "news": {
        "title": "📢 Новости лиги",
        "header": "📢 <b>НОВОСТИ ЛИГИ | ЛОГОВО ФИФАРЕЙ</b>",
        "footer": "⚡ <i>Следите за новостями и анонсами турнира!</i>",
        "btn_type": "cabinet",
    },
    "tour": {
        "title": "⚡ Старт тура / Матчи",
        "header": "⚡ <b>ТУРНИР | СТАРТ ТУРА И МАТЧИ</b>",
        "footer": "📅 <i>Не забывайте вовремя играть матчи и сдавать протоколы!</i>",
        "btn_type": "matches",
    },
    "free": {
        "title": "📝 Свободное объявление",
        "header": "📣 <b>ОБЪЯВЛЕНИЕ АДМИНИСТРАЦИИ</b>",
        "footer": "",
        "btn_type": "none",
    },
}

TARGET_TITLES = {
    "all": "🌐 Везде (ЛС игрокам + топики лиги)",
    "pm": "👤 Только в ЛС игрокам",
    "topics": "💬 Только в чаты и топики лиги",
}


def format_broadcast_content(category: str, raw_text: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Форматирует текст рассылки с разделителями и формирует инлайн-кнопки."""
    cfg = BROADCAST_CATEGORIES.get(category, BROADCAST_CATEGORIES["free"])
    bar = "━━━━━━━━━━━━━━━━━━━━━━"

    parts = [cfg["header"], bar, raw_text.strip()]
    if cfg.get("footer"):
        parts.extend([bar, cfg["footer"]])
    full_text = "\n".join(parts)

    btn_type = cfg.get("btn_type")
    buttons = []
    if btn_type == "betting":
        webapp_url = getattr(config, "WEBAPP_URL", "")
        if webapp_url and (webapp_url.startswith("https://") or "localhost" in webapp_url):
            buttons.append([InlineKeyboardButton("🎰 Сделать ставку", web_app=WebAppInfo(url=webapp_url))])
        elif webapp_url and webapp_url.startswith("http"):
            buttons.append([InlineKeyboardButton("🎰 Сделать ставку", url=webapp_url)])
        else:
            bot_user = getattr(config, "BOT_USERNAME", "") or "logovobot"
            buttons.append([InlineKeyboardButton("🎰 Сделать ставку", url=f"https://t.me/{bot_user}?start=miniapp")])
    elif btn_type == "cabinet":
        buttons.append([InlineKeyboardButton("👤 Личный кабинет", callback_data="menu_cabinet")])
    elif btn_type == "matches":
        buttons.append([InlineKeyboardButton("📋 Мои матчи", callback_data="cabinet_my_matches")])

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return full_text, markup


async def safe_send_broadcast(
    bot: Any,
    chat_id: int,
    thread_id: int | None,
    text: str,
    photo_id: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Безопасная отправка рассылочного сообщения с защитой от флуда и ошибок разметки."""
    if chat_id is None or chat_id == 0:
        return False
    try:
        if photo_id:
            await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=thread_id,
                photo=photo_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        return True
    except telegram.error.Forbidden:
        # Пользователь заблокировал бота
        return False
    except telegram.error.RetryAfter as e:
        wait_s = max(1, int(e.retry_after)) + 1
        logger.warning(f"Flood control in broadcast to {chat_id}: waiting {wait_s}s...")
        await asyncio.sleep(wait_s)
        return await safe_send_broadcast(bot, chat_id, thread_id, text, photo_id, reply_markup)
    except telegram.error.BadRequest as e:
        err_msg = str(e).lower()
        if "parse entities" in err_msg:
            # Сбой HTML-тегов, пробуем отправить без разметки
            try:
                if photo_id:
                    await bot.send_photo(chat_id=chat_id, message_thread_id=thread_id, photo=photo_id, caption=text, reply_markup=reply_markup)
                else:
                    await bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text, reply_markup=reply_markup)
                return True
            except Exception:
                return False
        logger.warning(f"BadRequest sending broadcast to {chat_id}: {e}")
        return False
    except Exception as e:
        logger.warning(f"Failed to send broadcast to {chat_id}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 📱 Интерактивный мастер в ЛС
# ─────────────────────────────────────────────────────────────────────────────

async def admin_broadcast_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Стартовый экран мастера рассылки: выбор категории."""
    query = update.callback_query
    user = update.effective_user
    if not user or not is_global_admin(user.id):
        if query:
            await query.answer("⛔ Доступно только главным администраторам лиги.", show_alert=True)
        return

    if query:
        await query.answer()

    # Очищаем временный стейт
    context.user_data["broadcast"] = {}

    keyboard = [
        [InlineKeyboardButton("🎰 Линия ставок (Логово Фифарей)", callback_data="admin_bcast_cat:betting")],
        [InlineKeyboardButton("📢 Новости лиги", callback_data="admin_bcast_cat:news")],
        [InlineKeyboardButton("⚡ Старт тура / Матчи", callback_data="admin_bcast_cat:tour")],
        [InlineKeyboardButton("📝 Свободное объявление", callback_data="admin_bcast_cat:free")],
        [InlineKeyboardButton("« Назад в админ-панель", callback_data="admin_main_menu")],
    ]

    text = (
        "📢 <b>Центр рассылки сообщений</b>\n\n"
        "Выберите тип сообщения, которое хотите разослать участникам:\n\n"
        "• <b>Линия ставок:</b> заголовок Логово Фифарей + кнопка быстрого перехода в Mini App\n"
        "• <b>Новости лиги:</b> официальное оформление новостей + кнопка кабинета\n"
        "• <b>Старт тура:</b> анонс расписания и дедлайнов + кнопка матчей\n"
        "• <b>Свободное:</b> кастомный текст без шаблона"
    )

    if query and query.message:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await query.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_broadcast_cat_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Категория выбрана: запрашиваем текст сообщения."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_global_admin(user.id):
        return

    category = query.data.split(":", 1)[1]
    cat_cfg = BROADCAST_CATEGORIES.get(category, BROADCAST_CATEGORIES["free"])

    context.user_data["broadcast"] = {
        "category": category,
        "state": "WAITING_TEXT",
    }

    text = (
        f"📝 <b>Выбрана категория: {cat_cfg['title']}</b>\n\n"
        "Отправьте следующим сообщением <b>текст рассылки</b>.\n"
        "• Поддерживается HTML (<b>жирный</b>, <i>курсив</i>, <code>код</code>, <a href=\"...\">ссылки</a>).\n"
        "• Можно отправить <b>фотографию с подписью</b> — рассылка уйдет с картинкой!\n\n"
        "<i>Для отмены нажмите кнопку ниже:</i>"
    )

    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="admin_bcast_cancel")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_broadcast_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Перехватывает текст или фото админа, когда включен стейт WAITING_TEXT.
    Возвращает True, если сообщение было обработано как шаг рассылки.
    """
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_global_admin(user.id):
        return False

    bcast = context.user_data.get("broadcast")
    if not bcast or bcast.get("state") != "WAITING_TEXT":
        return False

    raw_text = msg.text or msg.caption or ""
    if not raw_text.strip():
        await msg.reply_text("⚠️ Сообщение не содержит текста. Отправьте текст или фото с подписью.")
        return True

    photo_id = msg.photo[-1].file_id if msg.photo else None

    bcast["raw_text"] = raw_text
    bcast["photo_id"] = photo_id
    bcast["state"] = "WAITING_TARGET"

    # Считаем количество потенциальных получателей
    u_ids = await asyncio.to_thread(database.get_broadcast_user_ids)
    topics = await asyncio.to_thread(database.get_broadcast_chat_targets)

    keyboard = [
        [InlineKeyboardButton(f"🌐 Везде (ЛС: {len(u_ids)} + Топики: {len(topics)})", callback_data="admin_bcast_target:all")],
        [InlineKeyboardButton(f"👤 Только в ЛС игрокам ({len(u_ids)} чел.)", callback_data="admin_bcast_target:pm")],
        [InlineKeyboardButton(f"💬 Только в чаты/топики ({len(topics)} топиков)", callback_data="admin_bcast_target:topics")],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin_bcast_cancel")],
    ]

    prompt = (
        "🎯 <b>Куда отправить рассылку?</b>\n\n"
        f"• В базе зарегистрировано игроков: <b>{len(u_ids)}</b>\n"
        f"• Найдено активных чатов/топиков: <b>{len(topics)}</b>\n\n"
        "Выберите направление рассылки:"
    )

    await msg.reply_text(prompt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return True


async def admin_broadcast_target_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Аудитория выбрана: показываем ПРЕДПРОСМОТР и подтверждение."""
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_global_admin(user.id):
        return

    target = query.data.split(":", 1)[1]
    bcast = context.user_data.get("broadcast")
    if not bcast or "raw_text" not in bcast:
        await query.edit_message_text("⚠️ Данные рассылки устарели. Начните заново.")
        return

    bcast["target"] = target
    bcast["state"] = "CONFIRMING"

    category = bcast["category"]
    raw_text = bcast["raw_text"]
    photo_id = bcast.get("photo_id")

    full_text, content_markup = format_broadcast_content(category, raw_text)

    # 1. Отправляем предпросмотр сообщения
    await query.message.reply_text("👁‍🗨 <b>ПРЕДПРОСМОТР СООБЩЕНИЯ:</b>\n<i>(так его увидят получатели)</i>", parse_mode="HTML")

    if photo_id:
        await query.message.reply_photo(photo=photo_id, caption=full_text, parse_mode="HTML", reply_markup=content_markup)
    else:
        await query.message.reply_text(full_text, parse_mode="HTML", reply_markup=content_markup, disable_web_page_preview=True)

    # 2. Панель подтверждения отправки
    cat_title = BROADCAST_CATEGORIES.get(category, {}).get("title", category)
    target_title = TARGET_TITLES.get(target, target)

    confirm_text = (
        "⚙️ <b>Параметры рассылки:</b>\n\n"
        f"• Категория: <b>{cat_title}</b>\n"
        f"• Направление: <b>{target_title}</b>\n"
        f"• Медиафайл: <b>{'Фото прикреплено 📸' if photo_id else 'Только текст'}</b>\n\n"
        "Отправить рассылку прямо сейчас?"
    )

    confirm_keyboard = [
        [InlineKeyboardButton("🚀 Подтвердить и отправить", callback_data="admin_bcast_confirm")],
        [
            InlineKeyboardButton("✏️ Изменить текст", callback_data=f"admin_bcast_cat:{category}"),
            InlineKeyboardButton("🎯 Сменить цель", callback_data="admin_bcast_retarget"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin_bcast_cancel")],
    ]

    await query.message.reply_text(confirm_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(confirm_keyboard))


async def admin_broadcast_retarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Возврат к выбору цели рассылки."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    u_ids = await asyncio.to_thread(database.get_broadcast_user_ids)
    topics = await asyncio.to_thread(database.get_broadcast_chat_targets)

    keyboard = [
        [InlineKeyboardButton(f"🌐 Везде (ЛС: {len(u_ids)} + Топики: {len(topics)})", callback_data="admin_bcast_target:all")],
        [InlineKeyboardButton(f"👤 Только в ЛС игрокам ({len(u_ids)} чел.)", callback_data="admin_bcast_target:pm")],
        [InlineKeyboardButton(f"💬 Только в чаты/топики ({len(topics)} топиков)", callback_data="admin_bcast_target:topics")],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin_bcast_cancel")],
    ]

    await query.edit_message_text("🎯 Выберите направление рассылки:", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск отправки рассылки по выбранным получателям с отчетом."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    if not user or not is_global_admin(user.id):
        return

    bcast = context.user_data.get("broadcast")
    if not bcast or "raw_text" not in bcast:
        await query.edit_message_text("⚠️ Ошибка: сессия рассылки не найдена.")
        return

    category = bcast["category"]
    target = bcast["target"]
    raw_text = bcast["raw_text"]
    photo_id = bcast.get("photo_id")

    full_text, content_markup = format_broadcast_content(category, raw_text)

    status_msg = await query.edit_message_text("⏳ <i>Выполняю рассылку сообщений... Пожалуйста, подождите.</i>", parse_mode="HTML")

    sent_pm = 0
    forbidden_pm = 0
    sent_topics = 0
    failed_topics = 0

    bot = context.bot

    # 1. Отправка в ЛС игрокам
    if target in ("all", "pm"):
        user_ids = await asyncio.to_thread(database.get_broadcast_user_ids)
        for uid in user_ids:
            # Небольшая задержка, чтобы мягко обходить лимиты Telegram
            await asyncio.sleep(0.04)
            ok = await safe_send_broadcast(bot, chat_id=uid, thread_id=None, text=full_text, photo_id=photo_id, reply_markup=content_markup)
            if ok:
                sent_pm += 1
            else:
                forbidden_pm += 1

    # 2. Отправка в топики и беседы
    if target in ("all", "topics"):
        chat_targets = await asyncio.to_thread(database.get_broadcast_chat_targets)
        for ct in chat_targets:
            await asyncio.sleep(0.1)
            ok = await safe_send_broadcast(
                bot,
                chat_id=ct["chat_id"],
                thread_id=ct["thread_id"],
                text=full_text,
                photo_id=photo_id,
                reply_markup=content_markup,
            )
            if ok:
                sent_topics += 1
            else:
                failed_topics += 1

    # Запись в журнал аудита
    try:
        await admin_journal.record(
            user.id,
            "global_broadcast",
            "league",
            0,
            old=category,
            new=target,
            reason=raw_text[:100],
        )
    except Exception as e:
        logger.debug(f"Audit log failed for broadcast: {e}")

    # Очищаем сессию
    context.user_data.pop("broadcast", None)

    cat_title = BROADCAST_CATEGORIES.get(category, {}).get("title", category)
    target_title = TARGET_TITLES.get(target, target)

    report_text = (
        "✅ <b>Рассылка успешно выполнена!</b>\n\n"
        f"• Категория: <b>{cat_title}</b>\n"
        f"• Направление: <b>{target_title}</b>\n\n"
        "📊 <b>Итоги доставки:</b>\n"
        f"• Доставлено в ЛС игрокам: <b>{sent_pm}</b>\n"
        f"• Не доставлено в ЛС (блок/удален): <b>{forbidden_pm}</b>\n"
        f"• Отправлено в топики/чаты: <b>{sent_topics}</b>\n"
        f"• Ошибок в топиках: <b>{failed_topics}</b>"
    )

    back_kb = [[InlineKeyboardButton("👑 В админ-панель", callback_data="admin_main_menu")]]
    await status_msg.edit_text(report_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_kb))


async def admin_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена мастера рассылки."""
    query = update.callback_query
    if query:
        await query.answer("Рассылка отменена.")
    context.user_data.pop("broadcast", None)

    if query and query.message:
        await query.edit_message_text("❌ Рассылка отменена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« В админ-панель", callback_data="admin_main_menu")]]))


# ─────────────────────────────────────────────────────────────────────────────
# ⚡ Быстрая текстовая команда: «Темшик рассылка [тип] <текст>»
# ─────────────────────────────────────────────────────────────────────────────

async def run_quick_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, args_str: str) -> None:
    """Обработчик быстрой текстовой команды рассылки."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    if not is_global_admin(user.id):
        await msg.reply_text("⛔ Рассылка доступна только главным администраторам лиги.", parse_mode="HTML")
        return

    clean_args = (args_str or "").strip()
    if not clean_args:
        # Без аргументов открываем интерактивный мастер в ЛС
        await admin_broadcast_hub(update, context)
        return

    parts = clean_args.split(None, 1)
    first_word = parts[0].lower()

    cat_map = {
        "ставки": "betting",
        "ставка": "betting",
        "бет": "betting",
        "bet": "betting",
        "беттинг": "betting",
        "новости": "news",
        "новость": "news",
        "news": "news",
        "тур": "tour",
        "туры": "tour",
        "матчи": "tour",
        "матч": "tour",
    }

    if first_word in cat_map and len(parts) > 1:
        category = cat_map[first_word]
        text_body = parts[1].strip()
    else:
        category = "free"
        text_body = clean_args

    photo_id = msg.photo[-1].file_id if msg.photo else None

    # Записываем в user_data и сразу генерируем предпросмотр
    context.user_data["broadcast"] = {
        "category": category,
        "raw_text": text_body,
        "photo_id": photo_id,
        "target": "all",
        "state": "CONFIRMING",
    }

    full_text, content_markup = format_broadcast_content(category, text_body)

    await msg.reply_text("👁‍🗨 <b>ПРЕДПРОСМОТР БЫСТРОЙ РАССЫЛКИ:</b>", parse_mode="HTML")
    if photo_id:
        await msg.reply_photo(photo=photo_id, caption=full_text, parse_mode="HTML", reply_markup=content_markup)
    else:
        await msg.reply_text(full_text, parse_mode="HTML", reply_markup=content_markup, disable_web_page_preview=True)

    confirm_keyboard = [
        [InlineKeyboardButton("🚀 Подтвердить отправку везде (ЛС + чаты)", callback_data="admin_bcast_confirm")],
        [
            InlineKeyboardButton("👤 Только в ЛС", callback_data="admin_bcast_target:pm"),
            InlineKeyboardButton("💬 Только в чаты", callback_data="admin_bcast_target:topics"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin_bcast_cancel")],
    ]

    await msg.reply_text("Отправить сообщение получателям?", reply_markup=InlineKeyboardMarkup(confirm_keyboard))
