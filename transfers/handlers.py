"""Панель ответственного за трансферное окно — `/to` в ЛС бота.

Всё управление — кнопками одной панели: окно, автозакрытие, бюджеты, темы
группы, правила окна. Где нужно значение (сумма, время, ссылка на тему), панель
спрашивает его и ждёт следующее сообщение — других команд нет. Только для
`TRANSFER_MANAGER_ID` и только в личке. Заявки тренеры подают в Mini App.

Ожидание ввода хранится в памяти процесса (`_pending`) и живёт
`INPUT_TTL_SECONDS`; любое нажатие в панели или `/to` его сбрасывает.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes, filters

from services import admin_journal
from time_utils import MSK_LABEL, fmt_msk
from transfers import notify, repo, service
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
    """Только ответственный за ТО и только в ЛС; иначе ответить и вернуть False.

    Пропущенному сбрасывает ожидание ввода: нажал другую кнопку — передумал.
    """
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
        return "\n".join(lines), InlineKeyboardMarkup([
            [_btn("➕ Создать окно", "tw:create")],
            [_btn("🧵 Темы группы", "tw:topics")]])

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
    rows.append([_btn("🧵 Темы группы", "tw:topics"), _btn("📋 Правила окна", "tw:settings")])
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


# ─── Ввод значения ───────────────────────────────────────────────────────────

def _cancel_target(entry: dict) -> str:
    """Экран, с которого панель спросила значение."""
    if entry["kind"] == "budget":
        return f"tw:budgets:{entry['page']}"
    return {"autoclose": "tw:auto", "topic": "tw:topics"}.get(entry["kind"], "tw:hub")


async def on_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сообщение, которого ждала панель: сумма, время или ссылка на тему."""
    user, msg = update.effective_user, update.effective_message
    entry = _get_pending(user.id if user else None)
    if entry is None or not msg:
        return
    if not service.is_transfer_manager(user.id):
        _pending.pop(user.id, None)
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
