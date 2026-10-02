"""Экран ответственного за трансферное окно в ЛС бота.

Всё здесь — только для `TRANSFER_MANAGER_ID` и только в личке (кроме
`/set_transfer_topic`, которую зовут внутри темы группы ТО). Заявки тренеры
подают в Mini App; здесь — окно, его настройки, бюджеты и привязка тем.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from services import admin_journal
from time_utils import MSK_LABEL, fmt_msk
from transfers import notify, repo, service
from transfers.engine import format_k

logger = logging.getLogger(__name__)

BUDGETS_PAGE_SIZE = 20

STATUS_LABELS = {"draft": "📝 черновик", "open": "🟢 открыто", "closed": "🔒 закрыто"}
TRANSFER_STATUS_LABELS = {
    "pending_counterparty": "ждут второй стороны", "pending_manager": "ждут вашего решения",
    "approved": "одобрено", "rejected": "отклонено", "withdrawn": "отозвано", "cancelled": "отменено",
}
SETTING_LABELS = {
    "title": "Название",
    "auto_close_at": "Автозакрытие",
    "fa_opens_at": "Свободные агенты с",
    "max_buys": "Покупок на клуб",
    "max_sells": "Продаж на клуб",
    "max_extra_slots": "Доп. слотов на клуб",
    "slot_price_coins": "Цена доп. слота, 🪙",
    "ovr_cap": "Потолок OVR",
    "min_core_players": "Минимум игроков ядра",
    "fa_ovr_cap": "Потолок OVR свободного агента",
    "fa_forbidden_clubs": "СА: запрещённые клубы",
    "fa_restricted_clubs": "СА: клубы лиги с ограничением",
    "urn_divisor_sellable": "Урна: делитель (продаваемый)",
    "urn_divisor_unsellable": "Урна: делитель (непродаваемый)",
    "urn_max_per_club": "Урна: покупок на клуб",
    "urn_restricted_clubs": "Урна: клубы с ограничением",
    "surcharge_min_ovr": "Доплата с OVR",
    "surcharge_table": "Таблица доплат (OVR = млн)",
}
TOPIC_ALIASES = {
    "requests": "requests", "заявки": "requests",
    "feed": "feed", "лента": "feed",
    "alerts": "alerts", "алерты": "alerts",
}


# ─── Доступ и вывод ──────────────────────────────────────────────────────────

async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Только ответственный за ТО и только в ЛС; иначе ответить и вернуть False."""
    chat, user, query = update.effective_chat, update.effective_user, update.callback_query
    if not chat or not user:
        return False
    if not service.is_transfer_manager(user.id):
        if query:
            await query.answer("⛔ Только для ответственного за ТО", show_alert=True)
        elif chat.type == "private":
            await update.effective_message.reply_text(
                "⛔ <b>Доступ запрещён</b>\n\nТрансферным окном управляет только ответственный.",
                parse_mode="HTML")
        return False
    if chat.type != "private":
        bot_user = (context.bot.username or "") if context and context.bot else ""
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "💬 Открыть в ЛС", url=f"https://t.me/{bot_user}" if bot_user else "https://t.me")]])
        if query:
            await query.answer("Только в личных сообщениях", show_alert=True)
        else:
            await update.effective_message.reply_text(
                "🔒 <b>Команда доступна только в личных сообщениях</b>", reply_markup=kb, parse_mode="HTML")
        return False
    return True


async def _show(update: Update, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Под нажатой кнопкой — правка сообщения, иначе новый ответ."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML",
                                          disable_web_page_preview=True)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            logger.debug("transfers: edit failed, sending anew: %s", e)
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML",
                                              disable_web_page_preview=True)


def _back(target: str = "tw:hub") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("⬅️ Назад", callback_data=target)]


def _active_or_none() -> dict | None:
    return repo.get_active_window()


async def _need_window(update: Update) -> dict | None:
    window = _active_or_none()
    if window is None:
        await _show(update, "Незакрытого окна нет. Создайте его в /to.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Трансферное окно", callback_data="tw:hub")]]))
    return window


def _window_name(window: dict) -> str:
    title = (window.get("title") or "").strip()
    return f"«{html.escape(title)}»" if title else f"№{window['id']}"


# ─── Хаб ─────────────────────────────────────────────────────────────────────

def _hub_view() -> tuple[str, InlineKeyboardMarkup]:
    window = _active_or_none()
    if window is None:
        latest = repo.get_latest_window()
        lines = ["🔁 <b>Трансферное окно</b>", "", "Незакрытого окна нет."]
        if latest:
            lines.append(f"Последнее: {_window_name(latest)}, закрыто {fmt_msk(latest.get('closed_at'))} {MSK_LABEL}.")
        return "\n".join(lines), InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Создать окно", callback_data="tw:create")]])

    info = service.overview(window["id"])
    status = window["status"]
    lines = [f"🔁 <b>Трансферное окно {_window_name(window)}</b> — {STATUS_LABELS.get(status, status)}", ""]
    if window.get("opened_at"):
        lines.append(f"Открыто: {fmt_msk(window['opened_at'])} {MSK_LABEL}")
    auto = window.get("auto_close_at")
    lines.append(f"Автозакрытие: {fmt_msk(auto)} {MSK_LABEL}" if auto else "Автозакрытие: не задано")
    lines.append(f"Бюджеты: {info['budgets']} из {info['clubs']} клубов (без бюджета — 0)")
    if status == "open":
        lines.append(f"Снимок составов: {info['snapshot']} из {info['clubs']} клубов")
    topics = info["topics"]
    missing = [notify.TOPIC_LABELS[t] for t in notify.TOPIC_LABELS if t not in topics]
    lines.append("Темы группы: все привязаны" if not missing
                 else "Темы группы: не привязаны — " + ", ".join(missing))
    if info["statuses"]:
        lines.append("")
        lines.append("Заявки: " + ", ".join(
            f"{TRANSFER_STATUS_LABELS.get(k, k)} — {v}" for k, v in sorted(info["statuses"].items())))

    rows = []
    if status == "draft":
        rows.append([InlineKeyboardButton("🔓 Открыть окно", callback_data="tw:open")])
    rows.append([InlineKeyboardButton("💰 Бюджеты", callback_data="tw:budgets:0"),
                 InlineKeyboardButton("⚙️ Настройки", callback_data="tw:settings")])
    if status == "open" and info["snapshot"] < info["clubs"]:
        rows.append([InlineKeyboardButton("📸 Дописать снимок составов", callback_data="tw:snap")])
    rows.append([InlineKeyboardButton("🔒 Закрыть окно", callback_data="tw:close"),
                 InlineKeyboardButton("🔄", callback_data="tw:hub")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    text, kb = await asyncio.to_thread(_hub_view)
    await _show(update, text, kb)


async def cb_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    actor = update.effective_user.id
    try:
        window_id = service.create_window(actor)
    except service.InputError as exc:
        await update.callback_query.answer(str(exc), show_alert=True)
        return
    await admin_journal.record(actor, "transfer_window_created", "transfer_window", window_id)
    text, kb = await asyncio.to_thread(_hub_view)
    await _show(update, "✅ Окно создано в черновике. Задайте настройки и бюджеты, затем откройте.\n\n" + text, kb)


# ─── Открытие и закрытие ─────────────────────────────────────────────────────

async def cb_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    if window["status"] != "draft":
        await _show(update, "Окно уже открыто.", InlineKeyboardMarkup([_back()]))
        return
    info = await asyncio.to_thread(service.overview, window["id"])
    text = (f"🔓 <b>Открыть окно {_window_name(window)}?</b>\n\n"
            f"Бюджеты заданы у {info['budgets']} из {info['clubs']} клубов — у остальных 0.\n"
            "При открытии запишется исходный состав каждого клуба (ядро), "
            "в ленту уйдёт объявление.")
    await _show(update, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Открыть", callback_data="tw:open_ok")], _back()]))


async def cb_open_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    actor = update.effective_user.id
    result = await asyncio.to_thread(service.open_window, window["id"], actor)
    if not result.opened:
        await _show(update, "Окно уже открыто.", InlineKeyboardMarkup([_back()]))
        return
    snap = result.snapshot
    await admin_journal.record(actor, "transfer_window_opened", "transfer_window", window["id"],
                               new={"clubs": snap.clubs_saved, "players": snap.players_added})
    posted = await notify.announce_open(context.bot, repo.get_window(window["id"]))
    lines = ["✅ <b>Окно открыто</b>",
             f"Снимок составов: {snap.clubs_saved} клубов, {snap.players_added} игроков."]
    if snap.without_squad:
        lines.append(f"Без состава ({len(snap.without_squad)}): "
                     + html.escape(", ".join(snap.without_squad))
                     + " — их снимок можно дописать позже, кнопкой в /to.")
    if not posted:
        lines.append("⚠️ Объявление в ленту не ушло — см. сообщение выше.")
    await _show(update, "\n".join(lines), InlineKeyboardMarkup([_back()]))


async def cb_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    pending = await asyncio.to_thread(repo.list_transfers, window["id"],
                                      statuses=("pending_counterparty", "pending_manager"))
    counter = sum(1 for t in pending if t["status"] == "pending_counterparty")
    manager = len(pending) - counter
    text = (f"🔒 <b>Закрыть окно {_window_name(window)}?</b>\n\n"
            f"Неподтверждённых второй стороной заявок: {counter} — они будут отклонены, "
            "тренеры получат уведомление.\n"
            f"Ждущих вашего решения: {manager} — останутся, их можно решить и после закрытия.\n\n"
            "Открыть окно заново нельзя.")
    await _show(update, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 Закрыть", callback_data="tw:close_ok")], _back()]))


async def cb_close_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    actor = update.effective_user.id
    result = await asyncio.to_thread(service.close_window, window["id"], actor)
    if not result.closed:
        await _show(update, "Окно уже закрыто.", InlineKeyboardMarkup([_back()]))
        return
    await admin_journal.record(actor, "transfer_window_closed", "transfer_window", window["id"],
                               new={"rejected": len(result.rejected)})
    await notify.announce_close(context.bot, window, result.rejected, auto=False)
    await _show(update, f"✅ Окно закрыто. Отклонено неподтверждённых заявок: {len(result.rejected)}.",
                InlineKeyboardMarkup([_back()]))


async def cb_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    if window["status"] != "open":
        await _show(update, "Снимок пишется при открытии окна.", InlineKeyboardMarkup([_back()]))
        return
    actor = update.effective_user.id
    snap = await asyncio.to_thread(service.snapshot_core, window["id"])
    if snap.clubs_saved:
        await admin_journal.record(actor, "transfer_core_snapshot", "transfer_window", window["id"],
                                   new={"clubs": snap.clubs_saved, "players": snap.players_added})
    lines = [f"📸 Дописано: {snap.clubs_saved} клубов, {snap.players_added} игроков."]
    if snap.without_squad:
        lines.append(f"Всё ещё без состава ({len(snap.without_squad)}): "
                     + html.escape(", ".join(snap.without_squad)))
    await _show(update, "\n".join(lines), InlineKeyboardMarkup([_back()]))


# ─── Бюджеты ─────────────────────────────────────────────────────────────────

async def cb_budgets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    page = int(update.callback_query.data.rsplit(":", 1)[1])
    rows = await asyncio.to_thread(service.budget_table, window["id"])
    pages = max(1, (len(rows) + BUDGETS_PAGE_SIZE - 1) // BUDGETS_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * BUDGETS_PAGE_SIZE:(page + 1) * BUDGETS_PAGE_SIZE]
    set_count = sum(1 for r in rows if r["budget_k"] is not None)
    lines = [f"💰 <b>Бюджеты</b> — задано {set_count} из {len(rows)}, стр. {page + 1}/{pages}", ""]
    for r in chunk:
        if r["budget_k"] is None:
            lines.append(f"▫️ {html.escape(r['club'])} — <i>не задан</i>")
        else:
            mark = "✍️" if r["source"] == "manual" else "📐"
            lines.append(f"{mark} {html.escape(r['club'])} — {format_k(r['budget_k'])}")
    lines += ["", "✍️ вручную · 📐 по правилам",
              "Задать: <code>/to_budget Клуб 120</code> (сумма в млн, можно <code>12,5</code>)"]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tw:budgets:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tw:budgets:{page + 1}"))
    kb = [nav] if nav else []
    kb.append([InlineKeyboardButton("📐 Выдать по правилам", callback_data="tw:rules")])
    kb.append(_back())
    await _show(update, "\n".join(lines), InlineKeyboardMarkup(kb))


async def cb_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    actor = update.effective_user.id
    result = await asyncio.to_thread(service.apply_default_budgets, window["id"], actor)
    if not (result.written or result.kept_manual or result.invalid):
        text = "📐 Правила выдачи бюджетов ещё не подключены — задайте бюджеты вручную: <code>/to_budget Клуб 120</code>."
    else:
        if result.written:
            await admin_journal.record(actor, "transfer_budgets_applied", "transfer_window", window["id"],
                                       new={"written": result.written})
        text = f"📐 Выдано по правилам: {result.written}."
        if result.kept_manual:
            text += f"\nОставлены ручные: {html.escape(', '.join(result.kept_manual))}."
        if result.invalid:
            text += f"\n⚠️ Не распознаны: {html.escape(', '.join(result.invalid))}."
    await _show(update, text, InlineKeyboardMarkup([_back("tw:budgets:0")]))


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/to_budget <клуб> <сумма>` — последнее слово сумма, всё до неё клуб."""
    if not await _guard(update, context):
        return
    args = context.args or []
    msg = update.effective_message
    if len(args) < 2:
        await msg.reply_text("Использование: <code>/to_budget Клуб 120</code> (млн)", parse_mode="HTML")
        return
    window = await _need_window(update)
    if window is None:
        return
    actor = update.effective_user.id
    try:
        club, amount, old = await asyncio.to_thread(
            service.set_budget, window["id"], " ".join(args[:-1]), args[-1], actor)
    except service.InputError as exc:
        await msg.reply_text(f"⚠️ {exc}", parse_mode="HTML")
        return
    await admin_journal.record(actor, "transfer_budget_set", "transfer_window", window["id"],
                               old={"club": club, "budget_k": old}, new={"club": club, "budget_k": amount})
    was = f" (было {format_k(old)})" if old is not None else ""
    await msg.reply_text(f"✅ {html.escape(club)}: бюджет {format_k(amount)}{was}.", parse_mode="HTML")


# ─── Настройки ───────────────────────────────────────────────────────────────

def _setting_value(window: dict, key: str) -> str:
    value = window.get(key)
    if value in (None, ""):
        return "—"
    if key in repo.DATETIME_SETTINGS:
        return f"{fmt_msk(value)} {MSK_LABEL}"
    if key in repo.LIST_SETTINGS:
        items = json.loads(value or "[]")
        return ", ".join(items) if items else "—"
    if key in repo.TABLE_SETTINGS:
        table = json.loads(value or "{}")
        return ", ".join(f"{ovr}={format_k(price)}" for ovr, price in table.items()) or "—"
    return str(value)


async def cb_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    lines = [f"⚙️ <b>Настройки окна {_window_name(window)}</b>", ""]
    for key, label in SETTING_LABELS.items():
        lines.append(f"{label}: <b>{html.escape(_setting_value(window, key))}</b> · <code>{key}</code>")
    lines += ["", "Изменить: <code>/to_set ключ значение</code>",
              "Списки — через запятую, <code>-</code> очищает. Время — <code>10.10 20:00</code>.",
              "Автозакрытие: <code>/to_autoclose 10.10 20:00</code> или <code>/to_autoclose off</code>."]
    await _show(update, "\n".join(lines), InlineKeyboardMarkup([_back()]))


async def _apply_setting(update: Update, key: str, text: str) -> None:
    msg = update.effective_message
    window = await _need_window(update)
    if window is None:
        return
    actor = update.effective_user.id
    old = _setting_value(window, key)
    try:
        updated = await asyncio.to_thread(service.update_setting, window["id"], key, text)
    except service.InputError as exc:
        await msg.reply_text(f"⚠️ {exc}", parse_mode="HTML")
        return
    new = _setting_value(updated, key)
    await admin_journal.record(actor, "transfer_window_settings", "transfer_window", window["id"],
                               old={key: old}, new={key: new})
    await msg.reply_text(f"✅ {html.escape(SETTING_LABELS.get(key, key))}: <b>{html.escape(new)}</b>",
                         parse_mode="HTML")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/to_set <ключ> <значение>`."""
    if not await _guard(update, context):
        return
    args = context.args or []
    if len(args) < 2 or args[0] not in SETTING_LABELS:
        await update.effective_message.reply_text(
            "Использование: <code>/to_set ключ значение</code>. Ключи — в /to → ⚙️ Настройки.",
            parse_mode="HTML")
        return
    await _apply_setting(update, args[0], " ".join(args[1:]))


async def cmd_autoclose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/to_autoclose <дата время>` или `/to_autoclose off`."""
    if not await _guard(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Использование: <code>/to_autoclose 10.10 20:00</code> или <code>/to_autoclose off</code>",
            parse_mode="HTML")
        return
    await _apply_setting(update, "auto_close_at", " ".join(context.args))


# ─── Темы группы ─────────────────────────────────────────────────────────────

async def cmd_set_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/set_transfer_topic <requests|feed|alerts>` — внутри темы группы ТО."""
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not user or not chat:
        return
    if not service.is_transfer_manager(user.id):
        await msg.reply_text("⛔ Темы ТО привязывает только ответственный за трансферное окно.")
        return
    topic_type = TOPIC_ALIASES.get((context.args[0] if context.args else "").lower())
    if topic_type is None:
        await msg.reply_text(
            "Использование: <code>/set_transfer_topic заявки|лента|алерты</code> внутри нужной темы.",
            parse_mode="HTML")
        return
    if chat.type not in ("supergroup", "group") or msg.message_thread_id is None:
        await msg.reply_text("⚠️ Вызовите команду внутри нужного форум-топика супергруппы!")
        return
    await asyncio.to_thread(repo.bind_topic, topic_type, chat.id, msg.message_thread_id, user.id)
    await admin_journal.record(user.id, "transfer_topic_bound", "transfer_topic", msg.message_thread_id,
                               new={"type": topic_type, "chat": chat.id})
    await msg.reply_text(f"✅ Эта тема — «{notify.TOPIC_LABELS[topic_type]}» трансферного окна.")


def register_handlers(app) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    app.add_handler(CommandHandler("to", cmd_hub))
    app.add_handler(CommandHandler("to_budget", cmd_budget))
    app.add_handler(CommandHandler("to_set", cmd_set))
    app.add_handler(CommandHandler("to_autoclose", cmd_autoclose))
    app.add_handler(CommandHandler("set_transfer_topic", cmd_set_topic))
    app.add_handler(CallbackQueryHandler(cmd_hub, pattern=r"^tw:hub$"))
    app.add_handler(CallbackQueryHandler(cb_create, pattern=r"^tw:create$"))
    app.add_handler(CallbackQueryHandler(cb_open, pattern=r"^tw:open$"))
    app.add_handler(CallbackQueryHandler(cb_open_ok, pattern=r"^tw:open_ok$"))
    app.add_handler(CallbackQueryHandler(cb_close, pattern=r"^tw:close$"))
    app.add_handler(CallbackQueryHandler(cb_close_ok, pattern=r"^tw:close_ok$"))
    app.add_handler(CallbackQueryHandler(cb_snapshot, pattern=r"^tw:snap$"))
    app.add_handler(CallbackQueryHandler(cb_budgets, pattern=r"^tw:budgets:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_rules, pattern=r"^tw:rules$"))
    app.add_handler(CallbackQueryHandler(cb_settings, pattern=r"^tw:settings$"))
