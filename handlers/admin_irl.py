"""
handlers/admin_irl.py

Админка IRL-ставок для глобальных админов: панель матчей дня (`/irl` и кнопки под
превью из `services/irl_jobs`) и ручной расчёт `/irl_settle <id> <1|X|2|void>`.
СТРОГО ТОЛЬКО В ЛИЧНЫХ СООБЩЕНИЯХ и СТРОГО ТОЛЬКО ДЛЯ is_global_admin — внутри
деньги игроков.

Кнопки (все под префиксом `irl:`):
  irl:day:<день>               обновить панель
  irl:pub:<id> / irl:puball:<день>   опубликовать черновик(и)
  irl:rep:<id>                 заменить черновик — список других матчей дня
  irl:add:<день>               добавить матч — тот же список
  irl:pk:<fixture>:<день>:<id> выбрать матч из списка (id ≠ 0 — заменить им черновик)
  irl:can:<id> / irl:canok:<id>      отмена с подтверждением (ставки возвращаются)

Заменить можно только черновик: у него нет ставок. Опубликованный матч только
отменяется — с возвратом ставок. Новый матч из списка всегда создаётся черновиком
и публикуется отдельным нажатием (или автопубликацией).
"""

import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import database
from handlers.admin_ops import _answer, _guard, _show
from services import admin_journal, irl_betting, irl_jobs
from time_utils import fmt_msk, now_msk, parse_msk, today_msk_str

logger = logging.getLogger(__name__)

CANDIDATES_LIMIT = 10
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STATUS_ICON = {"draft": "📝", "open": "🟢", "closed": "🔒", "settled": "✅", "void": "↩️"}
_STATUS_NAME = {"draft": "черновик", "open": "открыт", "closed": "ставки закрыты",
                "settled": "рассчитан", "void": "аннулирован"}
_VOID_WORDS = ("void", "cancel", "аннул", "возврат")


def _get_provider():
    from services.sports import get_sports_provider
    return get_sports_provider()


# ─── Панель дня ──────────────────────────────────────────────────────────────

def day_keyboard(matches: list[dict], day: str) -> InlineKeyboardMarkup:
    """Кнопки под превью/панелью: по строке на матч + общие действия."""
    rows: list[list[InlineKeyboardButton]] = []
    for m in matches:
        mid = m["id"]
        if m["status"] == "draft":
            rows.append([
                InlineKeyboardButton(f"✅ #{mid}", callback_data=f"irl:pub:{mid}"),
                InlineKeyboardButton(f"🔁 #{mid}", callback_data=f"irl:rep:{mid}"),
                InlineKeyboardButton(f"🗑 #{mid}", callback_data=f"irl:can:{mid}"),
            ])
        elif m["status"] in ("open", "closed"):
            rows.append([InlineKeyboardButton(f"🗑 Отменить #{mid}", callback_data=f"irl:can:{mid}")])
    common = [InlineKeyboardButton("➕ Добавить матч", callback_data=f"irl:add:{day}")]
    if any(m["status"] == "draft" for m in matches):
        common.insert(0, InlineKeyboardButton("✅ Опубликовать все", callback_data=f"irl:puball:{day}"))
    rows.append(common)
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"irl:day:{day}")])
    return InlineKeyboardMarkup(rows)


def day_panel(day: str, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    matches = database.list_irl_matches(bet_day=day)
    lines = [f"⚽ <b>IRL-ставки на {html.escape(day)}</b>"]
    if note:
        lines += ["", note]
    if not matches:
        lines += ["", "Матчей на этот день нет."]
    for m in matches:
        stats = database.get_irl_match_bet_stats(m["id"])
        bets = f" · ставок {stats['count']} на {stats['total']:,} 🪙" if stats["count"] else ""
        lines += ["", f"{_STATUS_ICON.get(m['status'], '•')} {_STATUS_NAME.get(m['status'], m['status'])}"
                      f"{bets}\n{irl_jobs._match_line(m)}"]
    if not config.IRL_AUTO_PUBLISH:
        lines += ["", "Автопубликация выключена — открывайте матчи кнопкой."]
    return "\n".join(lines), day_keyboard(matches, day)


async def _show_day(update: Update, day: str, note: str | None = None) -> None:
    text, kb = day_panel(day, note)
    await _show(update, text, kb)


async def cmd_irl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/irl [ГГГГ-ММ-ДД] — панель матчей дня (по умолчанию сегодня, МСК)."""
    if not await _guard(update, context, "irl"):
        return
    day = today_msk_str()
    if context.args and _DAY_RE.match(context.args[0]):
        day = context.args[0]
    await _show_day(update, day)


# ─── Добавить / заменить ─────────────────────────────────────────────────────

async def _candidates(day: str):
    """Другие матчи дня из списка турниров, ещё не занесённые в базу. None — провайдер недоступен."""
    priority = list(config.IRL_COMPETITION_PRIORITY)
    fixtures = await _get_provider().get_prematch_fixtures(day, priority)
    if fixtures is None:
        return None
    now = now_msk()
    fresh = [f for f in fixtures
             if f.kickoff > now and not database.get_irl_match_by_fixture(f.fixture_id)]
    rank = {league: i for i, league in enumerate(priority)}
    fresh.sort(key=lambda f: (rank.get(f.league_id, len(rank)), f.kickoff))
    return fresh[:CANDIDATES_LIMIT]


async def _show_candidates(update: Update, day: str, replace_id: int) -> None:
    fixtures = await _candidates(day)
    back = InlineKeyboardButton("◀️ К матчам дня", callback_data=f"irl:day:{day}")
    if fixtures is None:
        await _show(update, "⚠️ Провайдер не отдал расписание. Попробуйте позже.",
                    InlineKeyboardMarkup([[back]]))
        return
    title = f"🔁 <b>Замена матча #{replace_id}</b>" if replace_id else "➕ <b>Добавить матч</b>"
    if not fixtures:
        await _show(update, f"{title}\n\nДругих матчей из списка турниров на {html.escape(day)} нет.",
                    InlineKeyboardMarkup([[back]]))
        return
    lines = [title, "", "Выберите матч — кэфы возьму у выбранного букмекера. "
                        "Матч появится черновиком, публикуется отдельно.", ""]
    rows = []
    for f in fixtures:
        lines.append(f"• {html.escape(f.league_name)}: {html.escape(f.home)} — {html.escape(f.away)}, "
                     f"{fmt_msk(f.kickoff, '%H:%M')}")
        label = f"{f.home} — {f.away} · {fmt_msk(f.kickoff, '%H:%M')}"
        rows.append([InlineKeyboardButton(label[:60],
                                          callback_data=f"irl:pk:{f.fixture_id}:{day}:{replace_id}")])
    rows.append([back])
    await _show(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _pick_fixture(update: Update, actor_id: int, fixture_id: str, day: str, replace_id: int) -> None:
    query = update.callback_query
    if not config.IRL_BOOKMAKER_ID:
        await query.answer("Не задан IRL_BOOKMAKER_ID", show_alert=True)
        return
    if database.get_irl_match_by_fixture(fixture_id):
        await query.answer("Этот матч уже есть в списке", show_alert=True)
        return
    provider = _get_provider()
    fx = await provider.get_prematch_fixture(fixture_id)
    if fx is None:
        await query.answer("Провайдер не отдал матч", show_alert=True)
        return
    if fx.kickoff <= now_msk():
        await query.answer("Матч уже начался", show_alert=True)
        return
    odds = await provider.get_match_winner_odds(fixture_id, int(config.IRL_BOOKMAKER_ID))
    if odds is None:
        await query.answer("У выбранного букмекера нет кэфов на этот матч", show_alert=True)
        return
    try:
        match_id, _ = database.create_irl_draft(
            fx.fixture_id, fx.league_id, fx.league_name, fx.home, fx.away, fx.kickoff,
            odds.home, odds.draw, odds.away, bet_day=fx.kickoff.date().isoformat(), picked_by="admin")
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return
    await _answer(update)
    label = f"{fx.home} — {fx.away}"
    note = f"➕ Добавлен черновик #{match_id}: {html.escape(label)}"
    if replace_id:
        old = database.get_irl_match(replace_id)
        if old and old["status"] == "draft":
            ok, _info = database.void_irl_match(replace_id, "Заменён админом", actor_id=actor_id)
            if ok:
                await admin_journal.record(actor_id, "irl_match_replaced", "irl_match", replace_id,
                                           old=f"{old['home']} — {old['away']}", new=label)
                note = f"🔁 Черновик #{replace_id} заменён на #{match_id}: {html.escape(label)}"
        else:
            note += f"\n⚠️ #{replace_id} уже не черновик — оставлен как есть."
    if note.startswith("➕"):
        await admin_journal.record(actor_id, "irl_match_added", "irl_match", match_id, new=label)
    await _show_day(update, day, note)


# ─── Опубликовать / отменить ─────────────────────────────────────────────────

async def _publish(update: Update, actor_id: int, match_id: int) -> str:
    ok, info = database.publish_irl_match(match_id)
    if ok:
        await admin_journal.record(actor_id, "irl_match_published", "irl_match", match_id)
        return f"✅ Матч #{match_id} опубликован."
    return f"⚠️ #{match_id}: {html.escape(str(info))}"


async def _confirm_cancel(update: Update, match_id: int) -> None:
    m = database.get_irl_match(match_id)
    if not m:
        await update.callback_query.answer("Матч не найден", show_alert=True)
        return
    if m["status"] in ("settled", "void"):
        await update.callback_query.answer("Матч уже рассчитан или аннулирован", show_alert=True)
        return
    await _answer(update)
    stats = database.get_irl_match_bet_stats(match_id)
    text = (f"🗑 <b>Отменить матч #{match_id}?</b>\n\n{irl_jobs._match_line(m)}\n\n"
            f"Ставок: {stats['count']} на {stats['total']:,} 🪙 — они вернутся игрокам целиком.")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Да, отменить", callback_data=f"irl:canok:{match_id}"),
        InlineKeyboardButton("◀️ Назад", callback_data=f"irl:day:{m['bet_day']}"),
    ]])
    await _show(update, text, kb)


async def _cancel(update: Update, actor_id: int, match_id: int) -> None:
    m = database.get_irl_match(match_id)
    day = m["bet_day"] if m else today_msk_str()
    ok, info = database.void_irl_match(match_id, "Отменён админом", actor_id=actor_id)
    if ok:
        await admin_journal.record(actor_id, "irl_match_cancelled", "irl_match", match_id,
                                   old=m["status"] if m else None,
                                   new=f"возвращено ставок: {info['refunded']}")
        note = f"🗑 Матч #{match_id} отменён, возвращено ставок: {info['refunded']}."
    else:
        note = f"⚠️ #{match_id}: {html.escape(str(info))}"
    await _show_day(update, day, note)


# ─── Диспетчер кнопок ────────────────────────────────────────────────────────

async def cb_irl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, "irl"):
        return
    parts = update.callback_query.data.split(":")
    action = parts[1] if len(parts) > 1 else "day"
    arg = parts[2] if len(parts) > 2 else ""
    actor_id = update.effective_user.id
    day_arg = arg if _DAY_RE.match(arg) else today_msk_str()

    if action in ("pub", "rep", "can", "canok") and not arg.isdigit():
        await update.callback_query.answer("Некорректная кнопка", show_alert=True)
        return

    if action == "day":
        await _answer(update)
        await _show_day(update, day_arg)
    elif action == "pub":
        await _answer(update)
        note = await _publish(update, actor_id, int(arg))
        m = database.get_irl_match(int(arg))
        await _show_day(update, m["bet_day"] if m else today_msk_str(), note)
    elif action == "puball":
        await _answer(update)
        notes = [await _publish(update, actor_id, m["id"])
                 for m in database.list_irl_matches(bet_day=day_arg, statuses=("draft",))]
        await _show_day(update, day_arg, "\n".join(notes) or "Черновиков нет.")
    elif action == "rep":
        await _answer(update)
        m = database.get_irl_match(int(arg))
        if not m or m["status"] != "draft":
            await _show_day(update, m["bet_day"] if m else today_msk_str(),
                            "⚠️ Заменить можно только черновик — опубликованный матч только отменяется.")
            return
        await _show_candidates(update, m["bet_day"], m["id"])
    elif action == "add":
        await _answer(update)
        await _show_candidates(update, day_arg, 0)
    elif action == "pk" and len(parts) == 5 and bool(parts[2]) and _DAY_RE.match(parts[3]) \
            and parts[4].isdigit():
        await _pick_fixture(update, actor_id, parts[2], parts[3], int(parts[4]))
    elif action == "can":
        await _confirm_cancel(update, int(arg))
    elif action == "canok":
        await _answer(update)
        await _cancel(update, actor_id, int(arg))
    else:
        await update.callback_query.answer("Некорректная кнопка", show_alert=True)


# ─── /irl_settle ─────────────────────────────────────────────────────────────

_USAGE = "<code>/irl_settle &lt;id&gt; 1|X|2|void</code>"


async def cmd_irl_settle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/irl_settle <id> <1|X|2|void> — ручной расчёт; без аргументов — что ждёт расчёта."""
    if not await _guard(update, context, "irl_settle"):
        return
    reply = update.effective_message.reply_text
    args = context.args or []
    if not args:
        waiting = [m for m in database.list_irl_matches(statuses=("open", "closed"), limit=50)
                   if _started(m)]
        lines = [f"Использование: {_USAGE}", ""]
        lines += [irl_jobs._match_line(m) for m in waiting] or ["Матчей, ждущих расчёта, нет."]
        await reply("\n".join(lines), parse_mode="HTML")
        return
    if len(args) != 2 or not args[0].isdigit():
        await reply(f"Формат: {_USAGE}", parse_mode="HTML")
        return

    match_id, verdict = int(args[0]), args[1].strip().casefold()
    m = database.get_irl_match(match_id)
    if not m:
        await reply(f"Матча #{match_id} нет.")
        return
    actor_id = update.effective_user.id
    label = f"{m['home']} — {m['away']}"
    if verdict.startswith(_VOID_WORDS):
        ok, info = database.void_irl_match(match_id, "Аннулирован админом", actor_id=actor_id)
        if not ok:
            await reply(f"⚠️ {html.escape(str(info))}", parse_mode="HTML")
            return
        await admin_journal.record(actor_id, "irl_match_settled", "irl_match", match_id,
                                   old=m["status"], new="void", reason=label)
        await reply(f"↩️ Матч #{match_id} аннулирован, возвращено ставок: {info['refunded']}.")
        return

    result = irl_betting.normalize_outcome(verdict)
    if result is None:
        await reply(f"Исход — 1, X, 2 или void. Формат: {_USAGE}", parse_mode="HTML")
        return
    ok, info = database.settle_irl_match(match_id, result, actor_id=actor_id)
    if not ok:
        await reply(f"⚠️ {html.escape(str(info))}", parse_mode="HTML")
        return
    await admin_journal.record(actor_id, "irl_match_settled", "irl_match", match_id,
                               old=m["status"], new=result, reason=label)
    await reply(f"✅ Матч #{match_id} рассчитан: <b>{irl_jobs._RESULT_LABEL[result]}</b>. "
                f"Выиграло {info['won']}, проиграло {info['lost']}, выплачено {info['paid']:,} 🪙.",
                parse_mode="HTML")


def _started(m: dict) -> bool:
    start = parse_msk(m["kickoff_at"])
    return start is not None and start <= now_msk()


def register_admin_irl_handlers(app) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    app.add_handler(CommandHandler("irl", cmd_irl))
    app.add_handler(CommandHandler("irl_settle", cmd_irl_settle))
    app.add_handler(CallbackQueryHandler(cb_irl, pattern=r"^irl:"))
