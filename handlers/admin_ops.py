"""
handlers/admin_ops.py

Эксплуатация бота для глобальных админов: /health, /backup, /ocr_stats, /audit.
СТРОГО ТОЛЬКО В ЛИЧНЫХ СООБЩЕНИЯХ и СТРОГО ТОЛЬКО ДЛЯ is_global_admin —
в отчётах пути, состояние ключей и действия всех админов.

Здесь же джоба автобэкапа `job_auto_backup`.
"""

import asyncio
import html
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import database
from handlers.base import is_global_admin
from services import admin_journal, bot_health, db_backup, ocr_metrics

logger = logging.getLogger(__name__)

# Лимит Bot API на отправку файла.
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024
AUDIT_PAGE_SIZE = 10
OCR_PERIODS = (1, 7, 30)
BACKUP_LIST_SIZE = 5


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE, start_arg: str) -> bool:
    """Private chat + global admin, else answer and return False."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    query = update.callback_query

    if not is_global_admin(user.id):
        if query:
            await query.answer("⛔ Только для глобальных админов", show_alert=True)
        elif chat.type == "private":
            await update.effective_message.reply_text(
                "⛔ <b>Доступ запрещён</b>\n\nКоманда доступна только глобальным админам лиги.",
                parse_mode="HTML",
            )
        return False

    if chat.type != "private":
        bot_user = (context.bot.username or "").lower() if context and context.bot else ""
        pm_url = f"https://t.me/{bot_user}?start={start_arg}" if bot_user else "https://t.me"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Открыть в ЛС", url=pm_url)]])
        if query:
            await query.answer("Только в личных сообщениях", show_alert=True)
        else:
            await update.effective_message.reply_text(
                "🔒 <b>Команда доступна только в личных сообщениях</b>",
                reply_markup=kb,
                parse_mode="HTML",
            )
        return False
    return True


async def _show(update: Update, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit the message under a pressed button, else reply."""
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML",
                                          disable_web_page_preview=True)
            return
        except Exception as e:  # "message is not modified" or too old to edit
            if "not modified" in str(e).lower():
                return
            logger.debug("admin_ops edit failed, sending anew: %s", e)
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML",
                                              disable_web_page_preview=True)


async def _answer(update: Update, text: str | None = None) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer(text)
        except Exception:
            pass


# ─── /health ────────────────────────────────────────────────────────────────

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, "health"):
        return
    await _answer(update, "Обновляю…")
    report = await asyncio.to_thread(bot_health.collect)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="ops_health")],
        [
            InlineKeyboardButton("💾 Бэкапы", callback_data="ops_backup:list"),
            InlineKeyboardButton("🔍 OCR", callback_data="ops_ocr:7"),
            InlineKeyboardButton("📜 Журнал", callback_data="ops_audit:all:0:0"),
        ],
    ])
    await _show(update, bot_health.format_report(report), kb)


# ─── /backup ────────────────────────────────────────────────────────────────

def _backup_list_text(note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    backups = db_backup.list_backups()
    hs = db_backup.human_size
    lines = ["💾 <b>Бэкапы базы</b>", ""]
    if note:
        lines += [note, ""]
    if config.BACKUP_INTERVAL_HOURS > 0:
        lines.append(f"Авто: раз в {config.BACKUP_INTERVAL_HOURS:g} ч, хранится {config.BACKUP_KEEP}")
    else:
        lines.append("Авто: выключено (BACKUP_INTERVAL_HOURS=0)")
    lines.append(f"Папка: <code>{html.escape(db_backup.backup_dir())}</code>")
    lines.append("")
    rows: list[list[InlineKeyboardButton]] = []
    if backups:
        lines.append(f"Копий: {len(backups)}, всего {hs(sum(b.size_bytes for b in backups))}")
        for b in backups[:BACKUP_LIST_SIZE]:
            lines.append(f"• {b.created_at:%d.%m %H:%M} — {hs(b.size_bytes)}")
            if b.size_bytes <= TELEGRAM_FILE_LIMIT:
                rows.append([InlineKeyboardButton(
                    f"📎 {b.created_at:%d.%m %H:%M}", callback_data=f"ops_backup:send:{b.name}")])
    else:
        lines.append("Бэкапов пока нет.")
    rows.insert(0, [InlineKeyboardButton("➕ Сделать бэкап сейчас", callback_data="ops_backup:new")])
    rows.append([InlineKeyboardButton("🩺 Здоровье", callback_data="ops_health")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — create a backup now; /backup list — only the list."""
    if not await _guard(update, context, "backup"):
        return
    args = [a.lower() for a in (context.args or [])]
    if args and args[0] in ("list", "список", "ls"):
        text, kb = _backup_list_text()
        await _show(update, text, kb)
        return
    await _make_backup(update, context)


async def _make_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _answer(update, "Делаю бэкап…")
    try:
        result = await asyncio.to_thread(db_backup.create_backup)
    except Exception as e:
        logger.exception("Manual backup failed")
        text, kb = _backup_list_text(f"❌ Бэкап не удался: <code>{html.escape(str(e))}</code>")
        await _show(update, text, kb)
        return
    removed = f", удалено старых {len(result['removed'])}" if result["removed"] else ""
    note = (
        f"✅ Готово: <code>{html.escape(result['name'])}</code>\n"
        f"{db_backup.human_size(result['size_bytes'])} (без сжатия {db_backup.human_size(result['raw_bytes'])}), "
        f"проверка {html.escape(result['integrity'])}, {result['duration_s']:g} с{removed}"
    )
    await admin_journal.record(update.effective_user.id, "db_backup_created", "backup",
                               new=result["name"])
    text, kb = _backup_list_text(note)
    await _show(update, text, kb)


async def cb_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, "backup"):
        return
    parts = update.callback_query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else "list"
    if action == "new":
        await _make_backup(update, context)
        return
    if action == "send" and len(parts) == 3:
        await _send_backup_file(update, context, parts[2])
        return
    await _answer(update)
    text, kb = _backup_list_text()
    await _show(update, text, kb)


async def _send_backup_file(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    # Only a name from our own listing, so a forged callback cannot point outside the folder.
    info = next((b for b in db_backup.list_backups() if b.name == name), None)
    if info is None:
        await update.callback_query.answer("Такого бэкапа уже нет", show_alert=True)
        return
    if info.size_bytes > TELEGRAM_FILE_LIMIT:
        await update.callback_query.answer("Файл больше 50 МБ — Telegram его не примет", show_alert=True)
        return
    await _answer(update, "Отправляю файл…")
    with open(info.path, "rb") as fh:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=fh,
            filename=info.name,
            caption=f"💾 Бэкап базы от {info.created_at:%d.%m.%Y %H:%M} МСК",
        )
    await admin_journal.record(update.effective_user.id, "db_backup_downloaded", "backup",
                               new=info.name)


def _backup_chat_id():
    raw = str(config.BACKUP_TELEGRAM_CHAT_ID or "").strip()
    if not raw:
        return None
    return int(raw) if raw.lstrip("-").isdigit() else raw


async def job_auto_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Back up when the newest copy is older than BACKUP_INTERVAL_HOURS.

    Runs often and decides from the files, so a restart neither skips nor
    doubles a backup. Errors propagate to job_health, which alerts the admins.
    """
    if not await asyncio.to_thread(db_backup.backup_due):
        return
    result = await asyncio.to_thread(db_backup.create_backup)
    chat_id = _backup_chat_id()
    if chat_id is None:
        return
    if result["size_bytes"] > TELEGRAM_FILE_LIMIT:
        logger.warning("Backup %s is over 50 MB, not sent to Telegram", result["name"])
        return
    try:
        with open(result["path"], "rb") as fh:
            await context.bot.send_document(
                chat_id=chat_id,
                document=fh,
                filename=result["name"],
                caption=f"💾 Автобэкап базы {result['created_at']:%d.%m.%Y %H:%M} МСК",
                disable_notification=True,
            )
    except Exception as e:  # the backup itself is on disk; a Telegram hiccup is not a failure
        logger.warning("Could not send backup %s to Telegram: %s", result["name"], e)


# ─── /ocr_stats ─────────────────────────────────────────────────────────────

async def _render_ocr(update: Update, days: int) -> None:
    stats = await asyncio.to_thread(ocr_metrics.stats_for_days, days)
    row = [
        InlineKeyboardButton(("• " if d == days else "") + ("24 ч" if d == 1 else f"{d} дн."),
                             callback_data=f"ops_ocr:{d}")
        for d in OCR_PERIODS
    ]
    kb = InlineKeyboardMarkup([row, [InlineKeyboardButton("🩺 Здоровье", callback_data="ops_health")]])
    await _show(update, ocr_metrics.format_report(stats), kb)


async def cmd_ocr_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, "ocr_stats"):
        return
    days = 7
    if context.args and context.args[0].isdigit():
        days = max(1, min(365, int(context.args[0])))
    await _render_ocr(update, days)


async def cb_ocr_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, "ocr_stats"):
        return
    await _answer(update)
    raw = update.callback_query.data.split(":", 1)[-1]
    days = int(raw) if raw.isdigit() else 7
    await _render_ocr(update, max(1, min(365, days)))


# ─── /audit ─────────────────────────────────────────────────────────────────

async def _render_audit(update: Update, category: str, page: int, actor_id: int) -> None:
    cat = category if category in admin_journal.CATEGORIES else None
    filters_ = admin_journal.journal_filter(cat)
    rows, total = await asyncio.to_thread(
        database.get_admin_journal,
        AUDIT_PAGE_SIZE,
        page * AUDIT_PAGE_SIZE,
        actor_id=actor_id or None,
        **filters_,
    )
    pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
    page = min(page, pages - 1)

    title = admin_journal.CATEGORIES.get(cat, "Все действия") if cat else "Все действия"
    head = [f"📜 <b>Журнал админов</b> — {html.escape(title)}"]
    if actor_id:
        head.append(f"Админ: <code>{actor_id}</code>")
    head.append(f"Записей: {total}, стр. {page + 1}/{pages}")
    body = [admin_journal.format_entry(r) for r in rows] or ["Записей нет."]
    text = "\n".join(head) + "\n\n" + "\n\n".join(body)
    # Drop whole entries rather than cut the text: a cut can split an HTML tag.
    while len(text) > 4000 and len(body) > 1:
        body.pop()
        text = "\n".join(head) + "\n\n" + "\n\n".join(body) + "\n\n…"

    key = category if cat else "all"
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"ops_audit:{key}:{page - 1}:{actor_id}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"ops_audit:{key}:{page + 1}:{actor_id}"))
    cats = [("all", "Все")] + [(k, v) for k, v in admin_journal.CATEGORIES.items()]
    cat_buttons = [
        InlineKeyboardButton(("• " if k == key else "") + label,
                             callback_data=f"ops_audit:{k}:0:{actor_id}")
        for k, label in cats
    ]
    keyboard = [nav] if nav else []
    keyboard += [cat_buttons[i:i + 2] for i in range(0, len(cat_buttons), 2)]
    if actor_id:
        keyboard.append([InlineKeyboardButton("👥 Все админы", callback_data=f"ops_audit:{key}:0:0")])
    await _show(update, text, InlineKeyboardMarkup(keyboard))


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/audit [@username|telegram_id] [категория]."""
    if not await _guard(update, context, "audit"):
        return
    actor_id = 0
    category = "all"
    for arg in context.args or []:
        a = arg.strip()
        if a.lower() in admin_journal.CATEGORIES:
            category = a.lower()
        elif a.lstrip("-").isdigit():
            actor_id = int(a)
        elif a.startswith("@") or a.replace("_", "").isalnum():
            found = await asyncio.to_thread(database.find_telegram_id_by_username, a)
            if found is None:
                await update.effective_message.reply_text(
                    f"Не нашёл пользователя {html.escape(a)}.", parse_mode="HTML")
                return
            actor_id = found
    await _render_audit(update, category, 0, actor_id)


async def cb_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, "audit"):
        return
    await _answer(update)
    parts = update.callback_query.data.split(":")
    category = parts[1] if len(parts) > 1 else "all"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    actor_id = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0
    await _render_audit(update, category, page, actor_id)


def register_admin_ops_handlers(app) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CallbackQueryHandler(cmd_health, pattern=r"^ops_health$"))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CallbackQueryHandler(cb_backup, pattern=r"^ops_backup:"))
    app.add_handler(CommandHandler(["ocr_stats", "ocr"], cmd_ocr_stats))
    app.add_handler(CallbackQueryHandler(cb_ocr_stats, pattern=r"^ops_ocr:\d+$"))
    app.add_handler(CommandHandler(["audit", "admin_log"], cmd_audit))
    app.add_handler(CallbackQueryHandler(cb_audit, pattern=r"^ops_audit:"))
