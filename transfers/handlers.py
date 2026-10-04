"""Панель ответственного за трансферное окно — `/to` в ЛС бота.

Всё управление — кнопками одной панели: окно, автозакрытие, бюджеты, темы
группы, правила окна. Где нужно значение (сумма, время, ссылка на тему), панель
спрашивает его и ждёт следующее сообщение — других команд нет. Только для
ответственного (`TRANSFER_MANAGER_ID`) и админов из `ADMIN_IDS`, только в личке.
Заявки тренеры подают в Mini App.

Ожидание ввода хранится в памяти процесса (`_pending`) и живёт
`INPUT_TTL_SECONDS`; любое нажатие в панели или `/to` его сбрасывает.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes, filters

from services import admin_journal
from time_utils import MSK_LABEL, fmt_msk
from transfers import approval, notify, repo, requests as req_mod, sanctions, service, squad
from transfers.engine import format_k

logger = logging.getLogger(__name__)

INPUT_TTL_SECONDS = 600
BUDGET_BUTTONS_PER_ROW = 2

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
TOPIC_PURPOSE = {
    "requests": "заявки, которые ждут вашего решения",
    "feed": "объявления для всей лиги: открытие, закрытие, сделки",
    "alerts": "сводки и проблемы: недоставленные ЛС, автоотклонения",
}

# user_id → {"kind": "budget"|"autoclose"|"topic", "expires": monotonic, ...}
_pending: dict[int, dict] = {}


def _set_pending(user_id: int, kind: str, **data) -> None:
    _pending[user_id] = {"kind": kind, "expires": time.monotonic() + INPUT_TTL_SECONDS, **data}


def _get_pending(user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    entry = _pending.get(user_id)
    if entry and entry["expires"] < time.monotonic():
        _pending.pop(user_id, None)
        return None
    return entry


class _AwaitingInput(filters.MessageFilter):
    """Сообщение в ЛС от того, кого панель сейчас ждёт."""

    def filter(self, message) -> bool:
        user = message.from_user
        return (message.chat is not None and message.chat.type == "private"
                and user is not None and _get_pending(user.id) is not None)


AWAITING_INPUT = _AwaitingInput(name="transfers.awaiting_input")


# ─── Доступ и вывод ──────────────────────────────────────────────────────────

async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Только ответственный за ТО или админ из `ADMIN_IDS` и только в ЛС; иначе ответить и вернуть False.

    Пропущенному сбрасывает ожидание ввода: нажал другую кнопку — передумал.
    """
    chat, user, query = update.effective_chat, update.effective_user, update.callback_query
    if not chat or not user:
        return False
    if not service.can_manage_window(user.id):
        if query:
            await query.answer("⛔ Только для ответственного за ТО и админов лиги", show_alert=True)
        elif chat.type == "private":
            await update.effective_message.reply_text(
                "⛔ <b>Доступ запрещён</b>\n\nТрансферным окном управляют ответственный и админы лиги.",
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
                "🔒 <b>Панель ТО доступна только в личных сообщениях</b>", reply_markup=kb, parse_mode="HTML")
        return False
    _pending.pop(user.id, None)
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


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _back(target: str = "tw:hub", label: str = "⬅️ Назад") -> list[InlineKeyboardButton]:
    return [_btn(label, target)]


def _window_name(window: dict) -> str:
    title = (window.get("title") or "").strip()
    return f"«{html.escape(title)}»" if title else f"№{window['id']}"


async def _need_window(update: Update) -> dict | None:
    window = repo.get_active_window()
    if window is None:
        await _show(update, "Незакрытого окна нет.", InlineKeyboardMarkup([_back(label="🔁 В панель")]))
    return window


# ─── Главный экран ───────────────────────────────────────────────────────────

def _hub_view() -> tuple[str, InlineKeyboardMarkup]:
    window = repo.get_active_window()
    if window is None:
        latest = repo.get_latest_window()
        lines = ["🔁 <b>Трансферное окно</b>", "", "Незакрытого окна нет."]
        if latest:
            lines.append(f"Последнее: {_window_name(latest)}, закрыто {fmt_msk(latest.get('closed_at'))} {MSK_LABEL}.")
        rows = [[_btn("➕ Создать окно", "tw:create")]]
        if _approved_items(latest):
            rows.append([_btn("📋 Одобренные заявки", "tw:appr:0")])
        rows.append([_btn("🧵 Темы группы", "tw:topics"), _btn("⛔ Санкции", "tw:sanc")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

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
    missing = [notify.TOPIC_LABELS[t] for t in notify.TOPIC_LABELS if t not in info["topics"]]
    lines.append("Темы группы: все привязаны" if not missing
                 else "Темы группы: не привязаны — " + ", ".join(missing))
    if info["statuses"]:
        lines.append("")
        lines.append("Заявки: " + ", ".join(
            f"{TRANSFER_STATUS_LABELS.get(k, k)} — {v}" for k, v in sorted(info["statuses"].items())))

    rows = []
    if status == "draft":
        rows.append([_btn("🔓 Открыть окно", "tw:open")])
    rows.append([_btn("💰 Бюджеты", "tw:budgets:0"), _btn("⏰ Автозакрытие", "tw:auto")])
    approved = _approved_items(window)
    if approved:
        unapplied = sum(1 for t in approved if squad.needs_apply(t))
        rows.append([_btn(f"📋 Одобренные заявки ({unapplied} без состава)" if unapplied
                          else "📋 Одобренные заявки", "tw:appr:0")])
    rows.append([_btn("🧵 Темы группы", "tw:topics"), _btn("📋 Правила окна", "tw:settings")])
    rows.append([_btn("⛔ Санкции", "tw:sanc")])
    if status == "open" and info["snapshot"] < info["clubs"]:
        rows.append([_btn("📸 Дописать снимок составов", "tw:snap")])
    rows.append([_btn("🔒 Закрыть окно", "tw:close"), _btn("🔄", "tw:hub")])
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
        window_id = await asyncio.to_thread(service.create_window, actor)
    except service.InputError as exc:
        await update.callback_query.answer(str(exc), show_alert=True)
        return
    await admin_journal.record(actor, "transfer_window_created", "transfer_window", window_id)
    text, kb = await asyncio.to_thread(_hub_view)
    await _show(update, "✅ Окно создано в черновике. Задайте бюджеты и темы, затем откройте.\n\n" + text, kb)


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
    await _show(update, text, InlineKeyboardMarkup([[_btn("✅ Открыть", "tw:open_ok")], _back()]))


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
                     + " — их снимок можно дописать позже кнопкой в панели.")
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
    await _show(update, text, InlineKeyboardMarkup([[_btn("🔒 Закрыть", "tw:close_ok")], _back()]))


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

def _budget_mark(row: dict) -> str:
    if row["budget_k"] is None:
        return "▫️"
    return "✍️" if row["source"] == "manual" else "📐"


def _budgets_view(window_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Страница — дивизион; клуб — кнопка, по нажатию панель спросит сумму."""
    pages = service.budget_pages(window_id)
    if not pages:
        return "💰 <b>Бюджеты</b>\n\nВ лиге нет клубов.", InlineKeyboardMarkup([_back()])
    page = max(0, min(page, len(pages) - 1))
    name, rows = pages[page]
    total = sum(len(r) for _, r in pages)
    set_count = sum(1 for _, r in pages for row in r if row["budget_k"] is not None)
    lines = [f"💰 <b>Бюджеты — {html.escape(name)}</b>",
             f"Задано {set_count} из {total} по лиге, без бюджета клуб считается с нулём.", ""]
    for row in rows:
        amount = "<i>не задан</i>" if row["budget_k"] is None else format_k(row["budget_k"])
        lines.append(f"{_budget_mark(row)} {html.escape(row['club'])} — {amount}")
    lines += ["", "✍️ вручную · 📐 по правилам · ▫️ не задан", "Нажмите клуб, чтобы задать бюджет."]

    kb: list[list[InlineKeyboardButton]] = []
    buttons = [_btn(f"{_budget_mark(row)} {row['club']}", f"tw:bclub:{page}:{i}") for i, row in enumerate(rows)]
    for i in range(0, len(buttons), BUDGET_BUTTONS_PER_ROW):
        kb.append(buttons[i:i + BUDGET_BUTTONS_PER_ROW])
    if len(pages) > 1:
        kb.append([_btn(f"• {i + 1} •" if i == page else str(i + 1), f"tw:budgets:{i}")
                   for i in range(len(pages))])
    kb.append([_btn("📐 Выдать по правилам", "tw:brules")])
    kb.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def cb_budgets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    page = int(update.callback_query.data.rsplit(":", 1)[1])
    text, kb = await asyncio.to_thread(_budgets_view, window["id"], page)
    await _show(update, text, kb)


async def cb_budget_club(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    _, _, page_s, idx_s = update.callback_query.data.split(":")
    page, idx = int(page_s), int(idx_s)
    pages = await asyncio.to_thread(service.budget_pages, window["id"])
    if page >= len(pages) or idx >= len(pages[page][1]):
        text, kb = await asyncio.to_thread(_budgets_view, window["id"], page)
        await _show(update, "Список клубов изменился, выберите заново.\n\n" + text, kb)
        return
    row = pages[page][1][idx]
    now = "не задан" if row["budget_k"] is None else (
        f"{format_k(row['budget_k'])} ({'вручную' if row['source'] == 'manual' else 'по правилам'})")
    _set_pending(update.effective_user.id, "budget", window_id=window["id"], club=row["club"], page=page)
    await _show(update,
                f"💰 <b>{html.escape(row['club'])}</b>\nСейчас: {now}\n\n"
                "Пришлите бюджет в млн: <code>120</code> или <code>12,5</code>.",
                InlineKeyboardMarkup([_back(f"tw:budgets:{page}", "✖️ Отмена")]))


async def cb_budget_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    actor = update.effective_user.id
    result = await asyncio.to_thread(service.apply_default_budgets, window["id"], actor)
    if not (result.written or result.kept_manual or result.invalid):
        text = "📐 Правила выдачи бюджетов ещё не подключены — задайте бюджеты вручную, нажимая на клубы."
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


async def _input_budget(update: Update, entry: dict, text: str) -> None:
    actor = update.effective_user.id
    club, amount, old = await asyncio.to_thread(service.set_budget, entry["window_id"], entry["club"], text, actor)
    await admin_journal.record(actor, "transfer_budget_set", "transfer_window", entry["window_id"],
                               old={"club": club, "budget_k": old}, new={"club": club, "budget_k": amount})
    was = f" (было {format_k(old)})" if old is not None else ""
    view, kb = await asyncio.to_thread(_budgets_view, entry["window_id"], entry["page"])
    await update.effective_message.reply_text(
        f"✅ {html.escape(club)}: бюджет {format_k(amount)}{was}.\n\n{view}", reply_markup=kb, parse_mode="HTML")


# ─── Автозакрытие ────────────────────────────────────────────────────────────

def _auto_view(window: dict) -> tuple[str, InlineKeyboardMarkup]:
    auto = window.get("auto_close_at")
    lines = ["⏰ <b>Автозакрытие</b>", "",
             f"Сейчас: <b>{fmt_msk(auto)} {MSK_LABEL}</b>" if auto else "Сейчас: не задано", "",
             "В это время окно закроется само — так же, как кнопкой «Закрыть окно»: "
             "неподтверждённые второй стороной заявки отклонятся, в ленту уйдёт объявление."]
    rows = [[_btn("✏️ Задать время", "tw:auto_set")]]
    if auto:
        rows[0].append(_btn("❌ Снять", "tw:auto_off"))
    rows.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cb_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    await _show(update, *_auto_view(window))


async def cb_auto_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    _set_pending(update.effective_user.id, "autoclose", window_id=window["id"])
    await _show(update,
                f"⏰ Пришлите дату и время закрытия ({MSK_LABEL}): <code>10.10 20:00</code> "
                "или <code>10.10.2026 20:00</code>.",
                InlineKeyboardMarkup([_back("tw:auto", "✖️ Отмена")]))


async def _set_auto_close(update: Update, window_id: int, text: str) -> dict:
    """Записать автозакрытие и журнал. InputError — пробросить вызывающему."""
    window = repo.get_window(window_id)
    old = window.get("auto_close_at") if window else None
    updated = await asyncio.to_thread(service.update_setting, window_id, "auto_close_at", text)
    await admin_journal.record(update.effective_user.id, "transfer_window_settings", "transfer_window",
                               window_id, old={"auto_close_at": old},
                               new={"auto_close_at": updated.get("auto_close_at")})
    return updated


async def cb_auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    window = await _need_window(update)
    if window is None:
        return
    updated = await _set_auto_close(update, window["id"], "off")
    text, kb = _auto_view(updated)
    await _show(update, "✅ Автозакрытие снято.\n\n" + text, kb)


async def _input_autoclose(update: Update, entry: dict, text: str) -> None:
    updated = await _set_auto_close(update, entry["window_id"], text)
    view, kb = _auto_view(updated)
    await update.effective_message.reply_text("✅ Автозакрытие задано.\n\n" + view,
                                              reply_markup=kb, parse_mode="HTML")


# ─── Темы группы ─────────────────────────────────────────────────────────────

def _topics_view() -> tuple[str, InlineKeyboardMarkup]:
    topics = repo.get_topics()
    lines = ["🧵 <b>Темы группы ТО</b>", ""]
    for key, label in notify.TOPIC_LABELS.items():
        mark = "✅" if key in topics else "▫️"
        lines.append(f"{mark} <b>{label}</b> — {TOPIC_PURPOSE[key]}")
    lines += ["", "Не привязанная тема не теряет сообщения — они приходят вам в ЛС.",
              "Нажмите тему, чтобы привязать или перепривязать её."]
    kb = [[_btn(("🔁 " if key in topics else "🔗 ") + label, f"tw:topic:{key}")
           for key, label in notify.TOPIC_LABELS.items()], _back()]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def cb_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _show(update, *await asyncio.to_thread(_topics_view))


async def cb_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    topic_type = update.callback_query.data.rsplit(":", 1)[1]
    if topic_type not in notify.TOPIC_LABELS:
        await _show(update, *await asyncio.to_thread(_topics_view))
        return
    _set_pending(update.effective_user.id, "topic", topic_type=topic_type)
    await _show(update,
                f"🔗 <b>Тема «{notify.TOPIC_LABELS[topic_type]}»</b>\n\n"
                "Откройте нужную тему в группе, нажмите ⋯ (или правой кнопкой по теме) → "
                "«Копировать ссылку» и пришлите ссылку сюда.\n"
                "Бот должен состоять в группе и иметь право писать в темы.",
                InlineKeyboardMarkup([_back("tw:topics", "✖️ Отмена")]))


async def _input_topic(update: Update, context: ContextTypes.DEFAULT_TYPE, entry: dict, text: str) -> None:
    topic_type = entry["topic_type"]
    label = notify.TOPIC_LABELS[topic_type]
    chat_ref, thread_id = service.parse_topic_link(text)
    bot = context.bot
    try:
        chat = await bot.get_chat(chat_ref)
    except TelegramError as exc:
        raise service.InputError(
            f"Не вижу эту группу ({html.escape(str(exc))}). Добавьте бота в группу и пришлите ссылку ещё раз.")
    if not getattr(chat, "is_forum", False):
        raise service.InputError("В этой группе нет тем — нужна группа с включёнными темами.")
    try:
        # Пробное сообщение заодно проверяет, что тема есть и бот может в неё писать.
        await bot.send_message(chat.id, f"✅ Эта тема — «{label}» трансферного окна.",
                               message_thread_id=thread_id)
    except TelegramError as exc:
        raise service.InputError(f"Не получилось написать в эту тему: {html.escape(str(exc))}.")
    actor = update.effective_user.id
    await asyncio.to_thread(repo.bind_topic, topic_type, chat.id, thread_id, actor)
    await admin_journal.record(actor, "transfer_topic_bound", "transfer_topic", thread_id,
                               new={"type": topic_type, "chat": chat.id})
    view, kb = await asyncio.to_thread(_topics_view)
    await update.effective_message.reply_text(f"✅ Тема «{label}» привязана.\n\n{view}",
                                              reply_markup=kb, parse_mode="HTML")


# ─── Правила окна ────────────────────────────────────────────────────────────

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
    lines = [f"📋 <b>Правила окна {_window_name(window)}</b>", ""]
    for key, label in SETTING_LABELS.items():
        lines.append(f"{label}: <b>{html.escape(_setting_value(window, key))}</b>")
    lines += ["", "Значения задаются правилами ТО."]
    await _show(update, "\n".join(lines), InlineKeyboardMarkup([_back()]))


# ─── Свободные агенты: приём комментариев от ответственного ────────────────

# draft_id -> {"draft": FaDraft, "preview": FaPreview, "user_id": int, "expires": float}
_fa_drafts: dict[str, dict] = {}


def _save_fa_draft(draft: req_mod.FaDraft, preview: req_mod.FaPreview, user_id: int) -> str:
    draft_id = uuid.uuid4().hex[:10]
    _fa_drafts[draft_id] = {
        "draft": draft,
        "preview": preview,
        "user_id": user_id,
        "expires": time.monotonic() + 1800,
    }
    return draft_id


def _get_fa_draft(draft_id: str) -> dict | None:
    entry = _fa_drafts.get(draft_id)
    if entry and entry["expires"] < time.monotonic():
        _fa_drafts.pop(draft_id, None)
        return None
    return entry


def _format_fa_preview(preview: req_mod.FaPreview, draft_id: str) -> tuple[str, InlineKeyboardMarkup]:
    draft = preview.draft
    ev = preview.evaluation
    lines = [
        "⚡️ <b>Свободный агент — проверка комментария</b>", "",
        f"Игрок: <b>{html.escape(draft.player_name)}</b>" + (f" (OVR {draft.ovr})" if draft.ovr else ""),
        f"Куда: <b>{html.escape(draft.to_club)}</b>" + (f" (тренер: {draft.to_user})" if draft.to_user else " (⚠️ нет тренера в боте)"),
        f"Откуда: {html.escape(draft.from_club or '—')}",
        f"Сумма: <b>{format_k(draft.price_k)}</b>",
    ]
    if draft.reported_budget_k is not None or ev.budget_remaining_after_k is not None:
        rep = format_k(draft.reported_budget_k) if draft.reported_budget_k is not None else "—"
        calc = format_k(ev.budget_remaining_after_k) if ev.budget_remaining_after_k is not None else "—"
        lines.append(f"Остаток бюджета: заявлен <b>{rep}</b> | расчётный <b>{calc}</b>")

    if draft.commented_at:
        lines.append(f"Время комментария: <b>{fmt_msk(draft.commented_at)} {MSK_LABEL}</b>")
    else:
        lines.append("Время комментария: ⚠️ <i>не определено (перешлите исходный комментарий)</i>")

    if preview.duplicate:
        lines += ["", f"❌ <b>Этот комментарий уже записан (заявка #{preview.duplicate['id']})</b>"]
    elif preview.conflict:
        lines += [
            "",
            f"⚠️ <b>Игрок уже записан за {html.escape(preview.conflict['to_club'])} (заявка #{preview.conflict['id']})</b>",
            f"Но этот комментарий оставлен РАНЬШЕ ({fmt_msk(draft.commented_at)} < {fmt_msk(preview.conflict['commented_at'])}).",
            "Вы можете переписать игрока на более ранний комментарий.",
        ]

    if ev.blocks:
        lines.append("")
        lines.append("⛔️ <b>Блокировки:</b>")
        for b in ev.blocks:
            lines.append(f"• {html.escape(b.message)}")

    if ev.warnings:
        lines.append("")
        lines.append("⚠️ <b>Предупреждения:</b>")
        for w in ev.warnings:
            lines.append(f"• {html.escape(w.message)}")

    if preview.notes:
        lines.append("")
        lines.append("ℹ️ <b>Заметки:</b>")
        for n in preview.notes:
            lines.append(f"• {html.escape(n.message)}")

    buttons = []
    if preview.can_record:
        buttons.append([InlineKeyboardButton("✅ Записать", callback_data=f"tw:fa:rec:{draft_id}")])
    elif preview.can_reassign and preview.conflict:
        buttons.append([InlineKeyboardButton("✅ Переписать игрока", callback_data=f"tw:fa:rea:{draft_id}:{preview.conflict['id']}")])

    buttons.append([
        InlineKeyboardButton("✏️ Исправить", callback_data=f"tw:fa:edit:{draft_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"tw:fa:rej:{draft_id}"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


class _FaCommentFilter(filters.MessageFilter):
    def filter(self, message) -> bool:
        if not message.chat or message.chat.type != "private":
            return False
        user = message.from_user
        if not user or not service.can_manage_window(user.id):
            return False
        if _get_pending(user.id) is not None:
            return False
        text = message.text or message.caption or ""
        return req_mod.looks_like_fa(text)


FA_COMMENT_FILTER = _FaCommentFilter(name="transfers.fa_comment")


async def on_fa_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приём пересланного комментария свободного агента из канала."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not service.can_manage_window(user.id):
        return

    text = msg.text or msg.caption or ""
    photo_file_id = msg.photo[-1].file_id if msg.photo else None

    moment = None
    if getattr(msg, "forward_origin", None) and hasattr(msg.forward_origin, "date"):
        moment = msg.forward_origin.date
    elif getattr(msg, "forward_date", None):
        moment = msg.forward_date
    commented_at = req_mod.commented_at_msk(moment)

    try:
        draft = req_mod.parse_fa_comment(text, commented_at=commented_at, photo_file_id=photo_file_id)
        preview = req_mod.fa_preview(draft)
    except service.InputError as exc:
        await msg.reply_text(f"⚠️ Не удалось разобрать заявку СА:\n\n{exc}", parse_mode="HTML")
        return
    except Exception as exc:
        logger.exception("transfers: fa parsing failed")
        await msg.reply_text(f"⚠️ Ошибка разбора комментария: {html.escape(str(exc))}", parse_mode="HTML")
        return

    draft_id = _save_fa_draft(draft, preview, user.id)
    card_text, kb = _format_fa_preview(preview, draft_id)
    if photo_file_id:
        try:
            await msg.reply_photo(photo=photo_file_id, caption=card_text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await msg.reply_text(card_text, reply_markup=kb, parse_mode="HTML")


async def cb_fa_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not await _guard(update, context):
        return
    await query.answer()
    import re
    match = re.match(r"^tw:fa:rec:([a-f0-9]+)$", query.data)
    if not match:
        return
    draft_id = match.group(1)
    entry = _get_fa_draft(draft_id)
    if not entry:
        await query.edit_message_text("Черновик устарел — перешлите комментарий заново.")
        return

    user = update.effective_user
    draft = entry["draft"]
    try:
        rec = await asyncio.to_thread(req_mod.record_free_agent, draft, user.id)
    except service.InputError as exc:
        await query.edit_message_text(f"⚠️ {exc}")
        return

    _fa_drafts.pop(draft_id, None)
    await admin_journal.record(user.id, "transfer_free_agent_recorded", "transfer", rec.transfer["id"],
                               new={"player": draft.player_name, "club": draft.to_club, "price_k": draft.price_k})
    _prefetch_portrait_later(rec.transfer)

    await notify.announce_free_agent(context.bot, rec.transfer)
    await notify.notify_free_agent_recorded(context.bot, rec.transfer)

    text = (f"✅ <b>Свободный агент #{rec.transfer['id']} записан и опубликован!</b>\n\n"
            f"Игрок: <b>{html.escape(rec.transfer['player_name'])}</b>\n"
            f"Клуб: <b>{html.escape(rec.transfer['to_club'])}</b>\n"
            f"Сумма: <b>{format_k(rec.transfer['price_k'])}</b>")
    await query.edit_message_text(text, parse_mode="HTML")


async def cb_fa_reassign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not await _guard(update, context):
        return
    await query.answer()
    import re
    match = re.match(r"^tw:fa:rea:([a-f0-9]+):(\d+)$", query.data)
    if not match:
        return
    draft_id, replace_id = match.group(1), int(match.group(2))
    entry = _get_fa_draft(draft_id)
    if not entry:
        await query.edit_message_text("Черновик устарел — перешлите комментарий заново.")
        return

    user = update.effective_user
    draft = entry["draft"]
    try:
        rec = await asyncio.to_thread(req_mod.record_free_agent, draft, user.id, replace_id=replace_id)
    except service.InputError as exc:
        await query.edit_message_text(f"⚠️ {exc}")
        return

    _fa_drafts.pop(draft_id, None)
    await admin_journal.record(user.id, "transfer_free_agent_reassigned", "transfer", rec.transfer["id"],
                               old={"replaced_id": replace_id},
                               new={"player": draft.player_name, "club": draft.to_club, "price_k": draft.price_k})
    _prefetch_portrait_later(rec.transfer)

    await notify.announce_free_agent(context.bot, rec.transfer)
    await notify.notify_free_agent_recorded(context.bot, rec.transfer)
    if rec.replaced:
        await notify.notify_free_agent_reassigned(context.bot, rec.transfer, rec.replaced)

    text = (f"✅ <b>Игрок переписан!</b>\n\n"
            f"Заявка #{rec.replaced['id'] if rec.replaced else replace_id} отменена.\n"
            f"Новая заявка #{rec.transfer['id']} одобрена:\n"
            f"• Игрок: <b>{html.escape(rec.transfer['player_name'])}</b>\n"
            f"• Клуб: <b>{html.escape(rec.transfer['to_club'])}</b>\n"
            f"• Сумма: <b>{format_k(rec.transfer['price_k'])}</b>")
    await query.edit_message_text(text, parse_mode="HTML")


async def cb_fa_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not await _guard(update, context):
        return
    await query.answer()
    import re
    match = re.match(r"^tw:fa:edit:([a-f0-9]+)$", query.data)
    if not match:
        return
    draft_id = match.group(1)
    entry = _get_fa_draft(draft_id)
    if not entry:
        await query.edit_message_text("Черновик устарел — перешлите комментарий заново.")
        return

    user = update.effective_user
    _set_pending(user.id, "fa_edit", draft_id=draft_id)
    draft = entry["draft"]
    prompt = (
        f"✏️ <b>Исправление заявки СА ({html.escape(draft.player_name)})</b>\n\n"
        f"Пришлите исправленный текст комментария (пункты 1–6).\n"
        f"Время исходного сообщения ({fmt_msk(draft.commented_at) if draft.commented_at else '—'} {MSK_LABEL}) "
        f"и прикреплённое фото сохранятся.\n\n"
        f"Или нажмите «Отмена»."
    )
    await query.message.reply_text(
        prompt,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data=f"tw:fa:can:{draft_id}")]]),
    )


async def cb_fa_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not await _guard(update, context):
        return
    await query.answer()
    import re
    match = re.match(r"^tw:fa:(rej|can):([a-f0-9]+)$", query.data)
    if not match:
        return
    draft_id = match.group(2)
    entry = _get_fa_draft(draft_id)
    if entry:
        draft = entry["draft"]
        await admin_journal.record(update.effective_user.id, "transfer_free_agent_rejected", "transfer", None,
                                   new={"player": draft.player_name, "club": draft.to_club})
        _fa_drafts.pop(draft_id, None)
        await query.edit_message_text(f"❌ <b>Заявка СА отклонена</b> ({html.escape(draft.player_name)} → {html.escape(draft.to_club)}).", parse_mode="HTML")
    else:
        await query.edit_message_text("Черновик уже закрыт.")


# ─── Решение по заявке: ✅/❌ на карточке ────────────────────────────────────

async def _alert(query, text: str) -> None:
    try:
        await query.answer(text[:190], show_alert=True)
    except Exception:
        pass


async def _close_card(bot, chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    """Снять кнопки с карточки и ответить под ней итогом (подпись фото править не нужно)."""
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception as exc:
        logger.debug("transfers: could not clear card buttons: %s", exc)
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
                               reply_to_message_id=message_id, allow_sending_without_reply=True)
    except Exception as exc:
        logger.debug("transfers: could not reply under card: %s", exc)


def _decision_summary(t: dict, word: str) -> str:
    return f"{word} <b>#{t['id']}</b> — {html.escape(t.get('player_name') or '')}"


async def cb_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✅ на карточке. Только ответственный; работает и в теме группы, и в ЛС."""
    query, user = update.callback_query, update.effective_user
    if not query or not user:
        return
    if not service.is_transfer_manager(user.id):
        await _alert(query, "⛔ Заявки решает только ответственный за трансферы")
        return
    transfer_id = int(query.data.rsplit(":", 1)[1])
    try:
        decision = await asyncio.to_thread(approval.approve, user.id, transfer_id)
    except service.InputError as exc:
        await _alert(query, str(exc))
        return
    t = decision.transfer
    await query.answer("✅ Одобрено")
    await admin_journal.record(
        user.id, "transfer_request_approved", "transfer", t["id"],
        old={"status": "pending_manager"},
        new={"status": "approved", "kind": t["kind"], "player": t["player_name"],
             "from": t["from_club"], "to": t["to_club"], "price_k": t["price_k"],
             "warnings": [w["code"] for w in decision.warnings]})
    warn = f"\n⚠️ Предупреждений: {len(decision.warnings)}" if decision.warnings else ""
    note = "\nСостав клуба пока не менялся — применить его можно кнопкой ниже." if squad.changes_squad(t) else ""
    await _close_card(context.bot, query.message.chat.id, query.message.message_id,
                      _decision_summary(t, "✅ Одобрена заявка") + warn + note,
                      _transfer_keyboard(t, back=False))
    await notify.notify_approved(context.bot, t)
    _prefetch_portrait_later(t)


_background: set = set()


def _prefetch_portrait_later(t: dict) -> None:
    """Портрет для истории качаем в фоне: сеть не должна держать ответ менеджеру."""
    task = asyncio.ensure_future(asyncio.to_thread(req_mod.prefetch_portrait, t))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def cb_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """❌: причину спрашиваем у ответственного в ЛС — в теме ждать текст негде."""
    query, user = update.callback_query, update.effective_user
    if not query or not user:
        return
    if not service.is_transfer_manager(user.id):
        await _alert(query, "⛔ Заявки решает только ответственный за трансферы")
        return
    transfer_id = int(query.data.rsplit(":", 1)[1])
    t = repo.get_transfer(transfer_id)
    if t is None or t["status"] != "pending_manager":
        await _alert(query, f"Заявка #{transfer_id} уже не ждёт решения.")
        return
    prompt = (f"❌ <b>Отклонить заявку #{t['id']}?</b>\n{notify.describe_transfer(t)}\n\n"
              "Пришлите причину — она уйдёт сторонам заявки. Или нажмите «Без причины».")
    kb = InlineKeyboardMarkup([[_btn("Без причины", f"tw:rjn:{t['id']}"), _btn("✖️ Отмена", "tw:rjc")]])
    if not await notify.dm_user(context.bot, user.id, prompt, kb):
        await _alert(query, "Откройте личный чат с ботом (/start) и нажмите ❌ ещё раз.")
        return
    _set_pending(user.id, "reject", transfer_id=t["id"], card_chat_id=query.message.chat.id,
                 card_message_id=query.message.message_id)
    await query.answer("Причину спросил в личных сообщениях")


async def _finish_reject(bot, user_id: int, entry: dict, reason: str | None) -> str:
    t = await asyncio.to_thread(approval.reject, user_id, entry["transfer_id"], reason)
    await admin_journal.record(
        user_id, "transfer_request_rejected", "transfer", t["id"],
        old={"status": "pending_manager"},
        new={"status": "rejected", "kind": t["kind"], "player": t["player_name"],
             "reason": t["decided_reason"]})
    why = f"\nПричина: {html.escape(t['decided_reason'])}" if t["decided_reason"] else ""
    await _close_card(bot, entry["card_chat_id"], entry["card_message_id"],
                      _decision_summary(t, "❌ Отклонена заявка") + why)
    await notify.notify_rejected(bot, t)
    return f"❌ Заявка #{t['id']} отклонена."


async def cb_reject_no_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query, user = update.callback_query, update.effective_user
    if not query or not user or not service.is_transfer_manager(user.id):
        return
    transfer_id = int(query.data.rsplit(":", 1)[1])
    entry = _get_pending(user.id)
    if not entry or entry["kind"] != "reject" or entry["transfer_id"] != transfer_id:
        await _alert(query, "Запрос устарел — нажмите ❌ на карточке заявки ещё раз.")
        return
    _pending.pop(user.id, None)
    try:
        text = await _finish_reject(context.bot, user.id, entry, None)
    except service.InputError as exc:
        text = f"⚠️ {exc}"
    await _show(update, text, None)


async def cb_reject_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query, user = update.callback_query, update.effective_user
    if not query or not user or not service.is_transfer_manager(user.id):
        return
    _pending.pop(user.id, None)
    await _show(update, "Отклонение отменено — заявка осталась без решения.", None)


# ─── Состав: применить, откатить, отменить одобренную ───────────────────────

APPROVED_PAGE = 8


def _transfer_keyboard(t: dict, *, back: bool) -> InlineKeyboardMarkup | None:
    rows = []
    if t["status"] == "approved":
        if squad.needs_apply(t):
            rows.append([_btn("📋 Применить к составу", f"tw:sq:{t['id']}")])
        elif t.get("squad_applied_at"):
            rows.append([_btn("↩️ Откатить состав", f"tw:sr:{t['id']}")])
        rows.append([_btn("🛑 Отменить заявку", f"tw:cx:{t['id']}")])
    if back:
        rows.append(_back("tw:appr:0", "⬅️ К одобренным"))
    return InlineKeyboardMarkup(rows) if rows else None


def _transfer_view(t: dict, *, back: bool) -> tuple[str, InlineKeyboardMarkup | None]:
    lines = [f"📋 {notify.describe_transfer(t)}",
             f"Статус: {TRANSFER_STATUS_LABELS.get(t['status'], t['status'])}"]
    if t.get("price_k"):
        lines.append(f"Сумма: {format_k(t['price_k'])}")
    if t["status"] == "approved":
        if not squad.changes_squad(t):
            lines.append("Состав: доплата состав не меняет")
        elif t.get("squad_applied_at"):
            lines.append(f"Состав: применён {fmt_msk(t['squad_applied_at'])} {MSK_LABEL}")
        else:
            lines.append("Состав: не применён")
            lines.extend(html.escape(step) for step in squad.preview(t))
    if t.get("decided_reason"):
        lines.append(f"Причина: {html.escape(t['decided_reason'])}")
    return "\n".join(lines), _transfer_keyboard(t, back=back)


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


async def _manager_press(update: Update) -> tuple[int, int] | None:
    """(user_id, transfer_id) из нажатия ответственного; чужому — отказ и None."""
    query, user = update.callback_query, update.effective_user
    if not query or not user:
        return None
    if not service.is_transfer_manager(user.id):
        await _alert(query, "⛔ Составом и отменой заявок занимается только ответственный за трансферы")
        return None
    return user.id, int(query.data.rsplit(":", 1)[1])


def _result_text(result: squad.SquadResult, head: str) -> str:
    lines = [head, ""]
    lines.extend(html.escape(line) for line in result.lines)
    lines.extend(f"ℹ️ {html.escape(note)}" for note in result.notes)
    return "\n".join(lines)


async def cb_squad_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    press = await _manager_press(update)
    if press is None:
        return
    user_id, transfer_id = press
    try:
        result = await asyncio.to_thread(squad.apply, user_id, transfer_id)
    except service.InputError as exc:
        await _alert(update.callback_query, str(exc))
        return
    t = result.transfer
    await admin_journal.record(
        user_id, "transfer_squad_applied", "transfer", t["id"],
        new={"player": t["player_name"], "changes": result.lines, "notes": result.notes})
    text, kb = _transfer_view(t, back=_is_private(update))
    await _show(update, _result_text(result, f"✅ <b>Состав обновлён по заявке #{t['id']}</b>") + "\n\n" + text, kb)
    await notify.notify_squad(context.bot, t, result.lines)


async def cb_squad_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    press = await _manager_press(update)
    if press is None:
        return
    user_id, transfer_id = press
    try:
        result = await asyncio.to_thread(squad.rollback, user_id, transfer_id)
    except service.InputError as exc:
        await _alert(update.callback_query, str(exc))
        return
    t = result.transfer
    await admin_journal.record(
        user_id, "transfer_squad_reverted", "transfer", t["id"],
        new={"player": t["player_name"], "changes": result.lines, "notes": result.notes})
    text, kb = _transfer_view(t, back=_is_private(update))
    await _show(update, _result_text(result, f"↩️ <b>Состав возвращён по заявке #{t['id']}</b>") + "\n\n" + text, kb)
    await notify.notify_squad(context.bot, t, result.lines, reverted=True)


async def cb_cancel_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    press = await _manager_press(update)
    if press is None:
        return
    _, transfer_id = press
    t = repo.get_transfer(transfer_id)
    if t is None or t["status"] != "approved":
        await _alert(update.callback_query, f"Заявка #{transfer_id} уже не одобрена — отменять нечего.")
        return
    rollback_note = ("\nСостав клубов будет возвращён." if t.get("squad_applied_at") else "")
    kb = InlineKeyboardMarkup([[_btn("🛑 Да, отменить", f"tw:cxy:{t['id']}"), _btn("Назад", f"tw:cxn:{t['id']}")]])
    await _show(update, f"🛑 <b>Отменить одобренную заявку?</b>\n{notify.describe_transfer(t)}\n\n"
                        f"Бюджет и слоты вернутся сторонам.{rollback_note}", kb)


async def cb_cancel_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    press = await _manager_press(update)
    if press is None:
        return
    t = repo.get_transfer(press[1])
    if t is None:
        await _alert(update.callback_query, "Заявка не найдена.")
        return
    text, kb = _transfer_view(t, back=_is_private(update))
    await _show(update, text, kb)


async def cb_cancel_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    press = await _manager_press(update)
    if press is None:
        return
    user_id, transfer_id = press
    try:
        t, rolled = await asyncio.to_thread(squad.cancel, user_id, transfer_id)
    except service.InputError as exc:
        await _alert(update.callback_query, str(exc))
        return
    lines = rolled.lines if rolled else []
    await admin_journal.record(
        user_id, "transfer_request_cancelled", "transfer", t["id"],
        old={"status": "approved"},
        new={"status": "cancelled", "kind": t["kind"], "player": t["player_name"],
             "from": t["from_club"], "to": t["to_club"], "price_k": t["price_k"], "squad_reverted": lines})
    summary = _decision_summary(t, "🛑 Отменена заявка") + ("\n" + "\n".join(html.escape(x) for x in lines) if lines else "")
    kb = InlineKeyboardMarkup([_back("tw:appr:0", "⬅️ К одобренным")]) if _is_private(update) else None
    await _show(update, summary, kb)
    await notify.notify_cancelled(context.bot, t, lines)


def _approved_window() -> dict | None:
    return repo.get_active_window() or repo.get_latest_window()


def _approved_items(window: dict | None) -> list[dict]:
    if window is None:
        return []
    items = repo.list_transfers(window["id"], statuses=("approved",))
    return sorted(items, key=lambda t: (not squad.needs_apply(t), t["id"]))


def _approved_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
    window = _approved_window()
    items = _approved_items(window)
    if not items:
        return "📋 Одобренных заявок нет.", InlineKeyboardMarkup([_back()])
    pending = sum(1 for t in items if squad.needs_apply(t))
    pages = (len(items) + APPROVED_PAGE - 1) // APPROVED_PAGE
    page = max(0, min(page, pages - 1))
    lines = [f"📋 <b>Одобренные заявки</b> — окно {_window_name(window)}", "",
             f"Всего: {len(items)}, не применено к составам: {pending}",
             "⏳ ждёт применения · ✔️ применено · ➖ состав не меняет"]
    rows = []
    for t in items[page * APPROVED_PAGE:(page + 1) * APPROVED_PAGE]:
        mark = "⏳" if squad.needs_apply(t) else ("✔️" if t.get("squad_applied_at") else "➖")
        label = f"{mark} #{t['id']} {t.get('player_name') or ''}"
        rows.append([_btn(label[:60], f"tw:tr:{t['id']}")])
    if pending:
        rows.append([_btn(f"📋 Применить все ({pending})", "tw:sqall")])
    rows.append([_btn("🖼 Подгрузить портреты", "tw:ports")])
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"tw:appr:{page - 1}"))
    if page < pages - 1:
        nav.append(_btn("▶️", f"tw:appr:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cb_approved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    page = int(update.callback_query.data.rsplit(":", 1)[1])
    text, kb = await asyncio.to_thread(_approved_view, page)
    await _show(update, text, kb)


async def cb_open_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    t = repo.get_transfer(int(update.callback_query.data.rsplit(":", 1)[1]))
    if t is None:
        await update.callback_query.answer("Заявка не найдена", show_alert=True)
        return
    text, kb = _transfer_view(t, back=True)
    await _show(update, text, kb)


async def cb_squad_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Применить к составам все одобренные заявки окна. Одна не прошла — остальные идут дальше."""
    query, user = update.callback_query, update.effective_user
    if not query or not user:
        return
    if not service.is_transfer_manager(user.id):
        await _alert(query, "⛔ Составом занимается только ответственный за трансферы")
        return
    window = _approved_window()
    done, failed = 0, []
    for t in [x for x in _approved_items(window) if squad.needs_apply(x)]:
        try:
            result = await asyncio.to_thread(squad.apply, user.id, t["id"])
        except service.InputError as exc:
            failed.append(f"#{t['id']} {t['player_name']}: {exc}")
            continue
        done += 1
        await admin_journal.record(
            user.id, "transfer_squad_applied", "transfer", t["id"],
            new={"player": t["player_name"], "changes": result.lines, "notes": result.notes})
        await notify.notify_squad(context.bot, result.transfer, result.lines)
    text, kb = await asyncio.to_thread(_approved_view, 0)
    head = f"✅ Применено к составам: {done}."
    if failed:
        head += "\n⚠️ Не применено:\n" + "\n".join(html.escape(f) for f in failed)
    await _show(update, head + "\n\n" + text, kb)


_portraits_running = False


async def cb_portraits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Догрузить портреты одобренных заявок окна. Качаем в фоне, итог — отдельным сообщением."""
    global _portraits_running
    if not await _guard(update, context):
        return
    query, user = update.callback_query, update.effective_user
    if _portraits_running:
        await _alert(query, "Портреты уже качаются — дождитесь итога")
        return
    items = _approved_items(_approved_window())
    if not items:
        await _alert(query, "Одобренных заявок нет")
        return
    _portraits_running = True
    await query.answer("Качаю портреты в фоне, пришлю итог")

    async def _work() -> None:
        global _portraits_running
        try:
            stats = await asyncio.to_thread(req_mod.backfill_portraits, items)
            text = (f"🖼 <b>Портреты</b>: игроков {stats['total']}\n"
                    f"Скачано: {stats['fetched']}, уже были: {stats['cached']}, "
                    f"не нашлось: {len(stats['missing'])}")
            if stats["missing"]:
                text += "\nБез портрета: " + html.escape(", ".join(stats["missing"][:30]))
        except Exception:
            logger.exception("transfers: portrait backfill failed")
            text = "⚠️ Не удалось догрузить портреты — подробности в логе."
        finally:
            _portraits_running = False
        try:
            await context.bot.send_message(chat_id=user.id, text=text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("transfers: portrait backfill report not delivered: %s", exc)

    task = asyncio.ensure_future(_work())
    _background.add(task)
    task.add_done_callback(_background.discard)


# ─── Санкции ─────────────────────────────────────────────────────────────────

SANCTION_SEASON_CHOICES = tuple(range(sanctions.MIN_SEASONS, sanctions.MAX_SEASONS + 1))
SANCTION_PAST_SHOWN = 5

# draft_id -> {"club_name": str|None, "user_id": int|None, "label": str, "expires": float}
_sanction_drafts: dict[str, dict] = {}


def _save_sanction_draft(club_name: str | None, user_id: int | None, label: str) -> str:
    draft_id = uuid.uuid4().hex[:10]
    _sanction_drafts[draft_id] = {"club_name": club_name, "user_id": user_id, "label": label,
                                  "expires": time.monotonic() + 1800}
    return draft_id


def _get_sanction_draft(draft_id: str) -> dict | None:
    entry = _sanction_drafts.get(draft_id)
    if entry and entry["expires"] < time.monotonic():
        _sanction_drafts.pop(draft_id, None)
        return None
    return entry


def _sanction_line(s: dict, names: dict[int, str]) -> str:
    reason = f" — {html.escape(s['reason'])}" if s.get("reason") else ""
    return f"{html.escape(sanctions.subject_label(s))}: {html.escape(sanctions.span_label(s, names))}{reason}"


def _sanctions_view() -> tuple[str, InlineKeyboardMarkup]:
    data = sanctions.overview()
    shown_past = data["past"][:SANCTION_PAST_SHOWN]
    names = repo.season_names(
        [i for s in data["active"] + shown_past for i in (s["from_season_id"], s["until_season_id"])])
    lines = ["⛔ <b>Санкции трансферного окна</b>",
             "Клуб или тренер под санкцией не подаёт заявки и не докупает слоты.", ""]
    kb: list[list[InlineKeyboardButton]] = []
    if data["active"]:
        lines.append("<b>Действуют:</b>")
        for s in data["active"]:
            lines.append("• " + _sanction_line(s, names))
            kb.append([_btn(f"🔓 Снять: {sanctions.subject_label(s)}", f"tw:sl:{s['id']}")])
    else:
        lines.append("Действующих санкций нет.")
    if shown_past:
        lines += ["", "<b>Снятые и истёкшие:</b>"]
        for s in shown_past:
            mark = "снята" if s["lifted_at"] else "истекла"
            lines.append(f"• {_sanction_line(s, names)} ({mark})")
    kb.append([_btn("➕ На клуб", "tw:sadd:club"), _btn("➕ На тренера", "tw:sadd:coach")])
    kb.append(_back())
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def cb_sanctions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    text, kb = await asyncio.to_thread(_sanctions_view)
    await _show(update, text, kb)


async def cb_sanction_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    target = update.callback_query.data.rsplit(":", 1)[1]
    _set_pending(update.effective_user.id, "sanc_target", target=target)
    ask = ("Пришлите название клуба: <code>Челси</code>." if target == "club"
           else "Пришлите тренера: <code>@username</code> или Telegram ID.")
    await _show(update, f"⛔ <b>Санкция на {'клуб' if target == 'club' else 'тренера'}</b>\n\n{ask}",
                InlineKeyboardMarkup([_back("tw:sanc", "✖️ Отмена")]))


async def _input_sanction_target(update: Update, entry: dict, text: str) -> None:
    if entry["target"] == "club":
        club = await asyncio.to_thread(sanctions.find_club, text)
        draft_id = _save_sanction_draft(club, None, f"клуб {club}")
        who = f"клуб <b>{html.escape(club)}</b>"
    else:
        coach = await asyncio.to_thread(sanctions.find_coach, text)
        name = f"@{coach['username']}" if coach["username"] else str(coach["user_id"])
        draft_id = _save_sanction_draft(None, coach["user_id"], f"тренер {name}")
        club = f" ({html.escape(coach['club'])})" if coach["club"] else ""
        who = f"тренер <b>{html.escape(name)}</b>{club}"
    kb = [[_btn(f"{n} {'сезон' if n == 1 else 'сезона' if n < 5 else 'сезонов'}", f"tw:sn:{draft_id}:{n}")
           for n in SANCTION_SEASON_CHOICES],
          _back("tw:sanc", "✖️ Отмена")]
    await update.effective_message.reply_text(
        f"⛔ Санкция: {who}.\n\nНа сколько сезонов, считая текущий?",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def cb_sanction_seasons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _, _, draft_id, seasons_s = update.callback_query.data.split(":")
    draft = _get_sanction_draft(draft_id)
    if draft is None:
        await _show(update, "Черновик санкции устарел — начните заново.",
                    InlineKeyboardMarkup([_back("tw:sanc")]))
        return
    _set_pending(update.effective_user.id, "sanc_reason", draft_id=draft_id, seasons=int(seasons_s))
    await _show(update, f"⛔ {html.escape(draft['label'])}, на {seasons_s} сез.\n\n"
                        "Пришлите причину одним сообщением или пропустите её.",
                InlineKeyboardMarkup([[_btn("Без причины", f"tw:snr:{draft_id}:{seasons_s}")],
                                      _back("tw:sanc", "✖️ Отмена")]))


async def _finish_sanction(bot, actor_id: int, draft_id: str, seasons: int, reason: str | None) -> str:
    draft = _get_sanction_draft(draft_id)
    if draft is None:
        raise service.InputError("Черновик санкции устарел — начните заново.")
    sanction = await asyncio.to_thread(
        lambda: sanctions.add(actor_id, club_name=draft["club_name"], user_id=draft["user_id"],
                              seasons=seasons, reason=reason))
    _sanction_drafts.pop(draft_id, None)
    await admin_journal.record(
        actor_id, "transfer_sanction_added", "transfer_sanction", sanction["id"],
        new={"club": sanction["club_name"], "user_id": sanction["user_id"],
             "from_season": sanction["from_season_id"], "until_season": sanction["until_season_id"]},
        reason=reason)
    recipients = await asyncio.to_thread(sanctions.coaches_to_notify, sanction)
    names = await asyncio.to_thread(
        repo.season_names, [sanction["from_season_id"], sanction["until_season_id"]])
    subject, span = sanctions.subject_label(sanction), sanctions.span_label(sanction, names)
    delivered = await notify.notify_sanction(bot, sanction, recipients, subject=subject, span=span)
    note = f"Уведомлено в ЛС: {delivered} из {len(recipients)}." if recipients else "Тренеров для уведомления нет."
    return f"✅ Санкция поставлена: {html.escape(subject)}, {html.escape(span)}.\n{note}"


async def cb_sanction_no_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _, _, draft_id, seasons_s = update.callback_query.data.split(":")
    try:
        head = await _finish_sanction(context.bot, update.effective_user.id, draft_id, int(seasons_s), None)
    except service.InputError as exc:
        head = f"⚠️ {exc}"
    text, kb = await asyncio.to_thread(_sanctions_view)
    await _show(update, head + "\n\n" + text, kb)


async def _input_sanction_reason(update: Update, context: ContextTypes.DEFAULT_TYPE, entry: dict, text: str) -> None:
    head = await _finish_sanction(context.bot, update.effective_user.id, entry["draft_id"],
                                  entry["seasons"], text)
    view, kb = await asyncio.to_thread(_sanctions_view)
    await update.effective_message.reply_text(head + "\n\n" + view, reply_markup=kb, parse_mode="HTML")


async def cb_sanction_lift_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    sanction_id = int(update.callback_query.data.rsplit(":", 1)[1])
    sanction = await asyncio.to_thread(repo.get_sanction, sanction_id)
    if sanction is None or sanction["lifted_at"]:
        text, kb = await asyncio.to_thread(_sanctions_view)
        await _show(update, "Эта санкция уже снята.\n\n" + text, kb)
        return
    names = await asyncio.to_thread(
        repo.season_names, [sanction["from_season_id"], sanction["until_season_id"]])
    await _show(update, f"🔓 <b>Снять санкцию?</b>\n\n{_sanction_line(sanction, names)}",
                InlineKeyboardMarkup([[_btn("✅ Снять", f"tw:sly:{sanction_id}"), _btn("✖️ Нет", "tw:sanc")]]))


async def cb_sanction_lift_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    actor = update.effective_user.id
    sanction_id = int(update.callback_query.data.rsplit(":", 1)[1])
    try:
        sanction = await asyncio.to_thread(sanctions.lift, sanction_id, actor)
    except service.InputError as exc:
        head = f"⚠️ {exc}"
    else:
        await admin_journal.record(
            actor, "transfer_sanction_lifted", "transfer_sanction", sanction_id,
            old={"club": sanction["club_name"], "user_id": sanction["user_id"],
                 "until_season": sanction["until_season_id"]})
        recipients = await asyncio.to_thread(sanctions.coaches_to_notify, sanction)
        subject = sanctions.subject_label(sanction)
        delivered = await notify.notify_sanction(context.bot, sanction, recipients, subject=subject,
                                                 span="", lifted=True)
        head = f"✅ Санкция снята: {html.escape(subject)}. Уведомлено в ЛС: {delivered} из {len(recipients)}."
    text, kb = await asyncio.to_thread(_sanctions_view)
    await _show(update, head + "\n\n" + text, kb)


# ─── Ввод значения ───────────────────────────────────────────────────────────

def _cancel_target(entry: dict) -> str:
    """Экран, с которого панель спросила значение."""
    if entry["kind"] == "budget":
        return f"tw:budgets:{entry['page']}"
    return {"autoclose": "tw:auto", "topic": "tw:topics", "sanc_target": "tw:sanc",
            "sanc_reason": "tw:sanc"}.get(entry["kind"], "tw:hub")


async def on_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сообщение, которого ждала панель: сумма, время или ссылка на тему."""
    user, msg = update.effective_user, update.effective_message
    entry = _get_pending(user.id if user else None)
    if entry is None or not msg:
        return
    if not service.can_manage_window(user.id):
        _pending.pop(user.id, None)
        return
    if entry["kind"] == "reject":
        _pending.pop(user.id, None)
        try:
            await msg.reply_text(await _finish_reject(context.bot, user.id, entry, (msg.text or "").strip()))
        except service.InputError as exc:
            await msg.reply_text(f"⚠️ {exc}")
        return
    if entry["kind"] in ("budget", "autoclose"):
        window = repo.get_active_window()
        if window is None or window["id"] != entry["window_id"]:
            _pending.pop(user.id, None)
            await msg.reply_text("Окно уже сменилось — откройте панель заново.",
                                 reply_markup=InlineKeyboardMarkup([_back(label="🔁 В панель")]))
            return
    text = (msg.text or "").strip()
    try:
        if entry["kind"] == "budget":
            await _input_budget(update, entry, text)
        elif entry["kind"] == "autoclose":
            await _input_autoclose(update, entry, text)
        elif entry["kind"] == "topic":
            await _input_topic(update, context, entry, text)
        elif entry["kind"] == "sanc_target":
            await _input_sanction_target(update, entry, text)
        elif entry["kind"] == "sanc_reason":
            await _input_sanction_reason(update, context, entry, text)
        elif entry["kind"] == "fa_edit":
            old_entry = _get_fa_draft(entry["draft_id"])
            if not old_entry:
                _pending.pop(user.id, None)
                await msg.reply_text("Черновик устарел — перешлите комментарий заново.")
                return
            draft = req_mod.edit_fa_draft(old_entry["draft"], text)
            preview = req_mod.fa_preview(draft)
            draft_id = _save_fa_draft(draft, preview, user.id)
            card_text, kb = _format_fa_preview(preview, draft_id)
            _pending.pop(user.id, None)
            await msg.reply_text(f"✅ Текст исправлен:\n\n{card_text}", reply_markup=kb, parse_mode="HTML")
            return
    except service.InputError as exc:
        # Ожидание остаётся: можно сразу прислать исправленное значение.
        _set_pending(user.id, **{k: v for k, v in entry.items() if k != "expires"})
        await msg.reply_text(f"⚠️ {exc}\n\nПришлите ещё раз или нажмите «Отмена».", parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup([_back(_cancel_target(entry), "✖️ Отмена")]))
        return
    _pending.pop(user.id, None)


def register_handlers(app) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

    app.add_handler(CommandHandler("to", cmd_hub))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & AWAITING_INPUT, on_input))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND & FA_COMMENT_FILTER, on_fa_comment))

    app.add_handler(CallbackQueryHandler(cmd_hub, pattern=r"^tw:hub$"))
    app.add_handler(CallbackQueryHandler(cb_create, pattern=r"^tw:create$"))
    app.add_handler(CallbackQueryHandler(cb_open, pattern=r"^tw:open$"))
    app.add_handler(CallbackQueryHandler(cb_open_ok, pattern=r"^tw:open_ok$"))
    app.add_handler(CallbackQueryHandler(cb_close, pattern=r"^tw:close$"))
    app.add_handler(CallbackQueryHandler(cb_close_ok, pattern=r"^tw:close_ok$"))
    app.add_handler(CallbackQueryHandler(cb_snapshot, pattern=r"^tw:snap$"))
    app.add_handler(CallbackQueryHandler(cb_budgets, pattern=r"^tw:budgets:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_budget_club, pattern=r"^tw:bclub:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_budget_rules, pattern=r"^tw:brules$"))
    app.add_handler(CallbackQueryHandler(cb_auto, pattern=r"^tw:auto$"))
    app.add_handler(CallbackQueryHandler(cb_auto_set, pattern=r"^tw:auto_set$"))
    app.add_handler(CallbackQueryHandler(cb_auto_off, pattern=r"^tw:auto_off$"))
    app.add_handler(CallbackQueryHandler(cb_topics, pattern=r"^tw:topics$"))
    app.add_handler(CallbackQueryHandler(cb_topic, pattern=r"^tw:topic:[a-z]+$"))
    app.add_handler(CallbackQueryHandler(cb_settings, pattern=r"^tw:settings$"))

    # Санкции
    app.add_handler(CallbackQueryHandler(cb_sanctions, pattern=r"^tw:sanc$"))
    app.add_handler(CallbackQueryHandler(cb_sanction_add, pattern=r"^tw:sadd:(club|coach)$"))
    app.add_handler(CallbackQueryHandler(cb_sanction_seasons, pattern=r"^tw:sn:[a-f0-9]+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sanction_no_reason, pattern=r"^tw:snr:[a-f0-9]+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sanction_lift_ask, pattern=r"^tw:sl:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_sanction_lift_yes, pattern=r"^tw:sly:\d+$"))

    # Решение по заявке
    app.add_handler(CallbackQueryHandler(cb_approve, pattern=r"^tw:ap:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reject, pattern=r"^tw:rj:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reject_no_reason, pattern=r"^tw:rjn:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_reject_cancel, pattern=r"^tw:rjc$"))

    # Состав и отмена одобренных
    app.add_handler(CallbackQueryHandler(cb_approved, pattern=r"^tw:appr:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_open_transfer, pattern=r"^tw:tr:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_squad_apply, pattern=r"^tw:sq:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_squad_rollback, pattern=r"^tw:sr:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_squad_all, pattern=r"^tw:sqall$"))
    app.add_handler(CallbackQueryHandler(cb_portraits, pattern=r"^tw:ports$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_ask, pattern=r"^tw:cx:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_yes, pattern=r"^tw:cxy:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cancel_back, pattern=r"^tw:cxn:\d+$"))

    # Свободные агенты
    app.add_handler(CallbackQueryHandler(cb_fa_record, pattern=r"^tw:fa:rec:[a-f0-9]+$"))
    app.add_handler(CallbackQueryHandler(cb_fa_reassign, pattern=r"^tw:fa:rea:[a-f0-9]+:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_fa_edit, pattern=r"^tw:fa:edit:[a-f0-9]+$"))
    app.add_handler(CallbackQueryHandler(cb_fa_reject, pattern=r"^tw:fa:(rej|can):[a-f0-9]+$"))

