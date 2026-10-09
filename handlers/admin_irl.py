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
  irl:rest:<id>                вернуть аннулированный матч в черновики

Заменить можно только черновик: у него нет ставок. Опубликованный матч только
отменяется — с возвратом ставок. Новый матч из списка всегда создаётся черновиком
и публикуется отдельным нажатием (или автопубликацией).
"""

import asyncio
from datetime import timedelta
import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

import config
import database
from handlers.admin_broadcast import safe_send_broadcast
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


def format_irl_broadcast(day: str, matches: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Форматирует сообщение рассылки об активных IRL-матчах и формирует кнопку в Mini App."""
    today = today_msk_str()
    tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    if day == today:
        day_label = "сегодня"
    elif day == tomorrow:
        day_label = "завтра"
    else:
        day_label = day

    header = "⚽ <b>ЛОГОВО ФИФАРЕЙ | IRL-СТАВКИ</b>"
    bar = "━━━━━━━━━━━━━━━━━━━━━━"
    subhead = f"🔥 <b>Открыты ставки на реальные футбольные матчи ({day_label})!</b>"

    match_lines = []
    for m in matches:
        league = f" <i>({html.escape(str(m['league_name']))})</i>" if m.get("league_name") else ""
        kickoff_time = fmt_msk(m["kickoff_at"], "%H:%M")
        odds = f"П1 <b>{m['odd_home']:.2f}</b> · Х <b>{m['odd_draw']:.2f}</b> · П2 <b>{m['odd_away']:.2f}</b>"
        match_lines.append(
            f"• <b>{html.escape(str(m['home']))} — {html.escape(str(m['away']))}</b>{league}\n"
            f"  🕒 <i>{kickoff_time} МСК</i> · {odds}"
        )

    matches_text = "\n\n".join(match_lines)
    footer = "💡 <i>Ставки принимаются до стартового свистка. Делайте ваши ставки и умножайте банк!</i>"

    full_text = "\n".join([header, bar, subhead, "", matches_text, "", bar, footer])

    webapp_url = getattr(config, "WEBAPP_URL", "")
    if webapp_url and (webapp_url.startswith("https://") or "localhost" in webapp_url or "127.0.0.1" in webapp_url):
        url = f"{webapp_url}?mode=irl" if "?" not in webapp_url else f"{webapp_url}&mode=irl"
        btn = InlineKeyboardButton("🎰 Сделать ставку", web_app=WebAppInfo(url=url))
    elif webapp_url and webapp_url.startswith("http"):
        url = f"{webapp_url}?mode=irl" if "?" not in webapp_url else f"{webapp_url}&mode=irl"
        btn = InlineKeyboardButton("🎰 Сделать ставку", url=url)
    else:
        bot_user = getattr(config, "BOT_USERNAME", "") or "logovobot"
        btn = InlineKeyboardButton("🎰 Сделать ставку", url=f"https://t.me/{bot_user}?start=miniapp")

    markup = InlineKeyboardMarkup([[btn]])
    return full_text, markup


# ─── Панель дня ──────────────────────────────────────────────────────────────

def day_keyboard(matches: list[dict], day: str) -> InlineKeyboardMarkup:
    """Кнопки под превью/панелью: по строке на матч + общие действия + навигация по дням."""
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
        elif m["status"] == "void":
            start = parse_msk(m["kickoff_at"])
            if start and start > now_msk():
                rows.append([InlineKeyboardButton(f"♻️ Вернуть #{mid}", callback_data=f"irl:rest:{mid}")])
    common = [InlineKeyboardButton("➕ Добавить матч", callback_data=f"irl:add:{day}")]
    if any(m["status"] == "draft" for m in matches):
        common.insert(0, InlineKeyboardButton("✅ Опубликовать все", callback_data=f"irl:puball:{day}"))
    rows.append(common)
    rows.append([InlineKeyboardButton("📢 Рассылка в ЛС", callback_data=f"irl:bcast:{day}")])

    now = now_msk()
    today = today_msk_str()
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    cur_d = parse_msk(day + " 00:00:00")
    if cur_d:
        prev_d = (cur_d - timedelta(days=1)).strftime("%Y-%m-%d")
        next_d = (cur_d + timedelta(days=1)).strftime("%Y-%m-%d")
        nav: list[InlineKeyboardButton] = []
        if day == today:
            nav.append(InlineKeyboardButton("◀️ Вчера", callback_data=f"irl:day:{prev_d}"))
            nav.append(InlineKeyboardButton("🔄 Обновить", callback_data=f"irl:day:{day}"))
            nav.append(InlineKeyboardButton("Завтра ▶️", callback_data=f"irl:day:{tomorrow_str}"))
        elif day == tomorrow_str:
            nav.append(InlineKeyboardButton("◀️ Сегодня", callback_data=f"irl:day:{today}"))
            nav.append(InlineKeyboardButton("🔄 Обновить", callback_data=f"irl:day:{day}"))
            nav.append(InlineKeyboardButton(f"{next_d[-5:]} ▶️", callback_data=f"irl:day:{next_d}"))
        else:
            nav.append(InlineKeyboardButton(f"◀️ {prev_d[-5:]}", callback_data=f"irl:day:{prev_d}"))
            nav.append(InlineKeyboardButton("Сегодня", callback_data=f"irl:day:{today}"))
            nav.append(InlineKeyboardButton(f"{next_d[-5:]} ▶️", callback_data=f"irl:day:{next_d}"))
        rows.append(nav)
    else:
        rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"irl:day:{day}")])

    return InlineKeyboardMarkup(rows)


def day_panel(day: str, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    matches = database.list_irl_matches(bet_day=day)
    today = today_msk_str()
    tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday = (now_msk() - timedelta(days=1)).strftime("%Y-%m-%d")

    day_label = day
    if day == today:
        day_label = f"{day} (Сегодня)"
    elif day == tomorrow:
        day_label = f"{day} (Завтра)"
    elif day == yesterday:
        day_label = f"{day} (Вчера)"

    lines = [f"⚽ <b>IRL-ставки на {html.escape(day_label)}</b>"]
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
    """Другие матчи дня из списка турниров, ещё не занесённые в базу (или аннулированные). None — провайдер недоступен."""
    priority = list(config.IRL_COMPETITION_PRIORITY)
    fixtures = await _get_provider().get_prematch_fixtures(day, priority)
    if fixtures is None:
        return None
    now = now_msk()
    fresh = []
    for f in fixtures:
        if f.kickoff <= now:
            continue
        existing = database.get_irl_match_by_fixture(f.fixture_id)
        if existing is None or existing["status"] == "void":
            fresh.append(f)
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
    today = today_msk_str()
    tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    day_name = "сегодня" if day == today else ("завтра" if day == tomorrow else day)
    title = f"🔁 <b>Замена матча #{replace_id}</b>" if replace_id else f"➕ <b>Добавить матч на {day_name}</b>"
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
    existing = database.get_irl_match_by_fixture(fixture_id)
    if existing and existing["status"] not in ("draft", "void"):
        await query.answer("Этот матч уже опубликован или завершён", show_alert=True)
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
            odds.home, odds.draw, odds.away, bet_day=day or fx.kickoff.date().isoformat(), picked_by="admin")
    except ValueError as e:
        await query.answer(str(e), show_alert=True)
        return
    await _answer(update)
    label = f"{fx.home} — {fx.away}"
    was_void = existing and existing["status"] == "void"
    action_word = "♻️ Черновик восстановлен" if was_void else "➕ Добавлен черновик"
    note = f"{action_word} #{match_id}: {html.escape(label)}"
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
    if not replace_id:
        event_name = "irl_match_restored" if was_void else "irl_match_added"
        await admin_journal.record(actor_id, event_name, "irl_match", match_id, new=label)
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
    if m["status"] == "draft":
        text = (f"🗑 <b>Отменить матч #{match_id}?</b> (черновик)\n\n{irl_jobs._match_line(m)}\n\n"
                f"Матч будет аннулирован (его можно вернуть кнопкой ♻️ или через «Добавить матч»).")
    else:
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


async def _restore(update: Update, actor_id: int, match_id: int) -> None:
    m = database.get_irl_match(match_id)
    if not m:
        await update.callback_query.answer("Матч не найден", show_alert=True)
        return
    if m["status"] != "void":
        await update.callback_query.answer("Матч не аннулирован", show_alert=True)
        return
    start = parse_msk(m["kickoff_at"])
    if start is None or start <= now_msk():
        await update.callback_query.answer("Матч уже начался или завершился", show_alert=True)
        return
    try:
        provider = _get_provider()
        if config.IRL_BOOKMAKER_ID:
            odds = await provider.get_match_winner_odds(m["provider_fixture_id"], int(config.IRL_BOOKMAKER_ID))
            if odds:
                database.update_irl_odds(match_id, odds.home, odds.draw, odds.away)
    except Exception as e:
        logger.warning("Could not refresh odds on restore for match #%s: %s", match_id, e)

    ok, info = database.restore_irl_match(match_id, actor_id=actor_id)
    label = f"{m['home']} — {m['away']}"
    if ok:
        await admin_journal.record(actor_id, "irl_match_restored", "irl_match", match_id, new=label)
        note = f"♻️ Матч #{match_id} возвращён в черновики: {html.escape(label)}."
    else:
        note = f"⚠️ #{match_id}: {html.escape(str(info))}"
    await _show_day(update, m["bet_day"] or today_msk_str(), note)


async def _confirm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, day: str) -> None:
    now = now_msk()
    matches = database.list_irl_matches(bet_day=day, statuses=("open",))
    active = [m for m in matches if not parse_msk(m["kickoff_at"]) or parse_msk(m["kickoff_at"]) > now]
    if not active:
        all_day_matches = database.list_irl_matches(bet_day=day)
        has_drafts = any(m["status"] == "draft" for m in all_day_matches)
        if has_drafts:
            msg = "⚠️ На этот день нет опубликованных матчей.\nСначала опубликуйте черновики (кнопка ✅)!"
        else:
            msg = f"⚠️ На {day} нет активных открытых матчей для ставок."
        await update.callback_query.answer(msg, show_alert=True)
        return

    await _answer(update)
    user_ids = await asyncio.to_thread(database.get_broadcast_user_ids)
    preview_text, _ = format_irl_broadcast(day, active)

    today = today_msk_str()
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    day_name = "сегодня" if day == today else ("завтра" if day == tomorrow else day)

    lines = [
        "📢 <b>Рассылка уведомления об активных матчах</b>",
        "",
        f"Будет отправлено уведомление всем игрокам (<b>{len(user_ids)}</b> чел.) в личные сообщения с анонсом открытых матчей на <b>{day_name}</b>.",
        "",
        "<b>Предпросмотр сообщения:</b>",
        preview_text,
        "",
        "Отправить рассылку сейчас?",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Да, отправить ({len(user_ids)} чел.)", callback_data=f"irl:bcastok:{day}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"irl:day:{day}")],
    ])
    await _show(update, "\n".join(lines), kb)


async def _run_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, actor_id: int, day: str) -> None:
    now = now_msk()
    matches = database.list_irl_matches(bet_day=day, statuses=("open",))
    active = [m for m in matches if not parse_msk(m["kickoff_at"]) or parse_msk(m["kickoff_at"]) > now]
    if not active:
        await update.callback_query.answer("Матчи уже начались или закрыты", show_alert=True)
        await _show_day(update, day)
        return

    await _answer(update)
    query = update.callback_query
    try:
        if query and query.message:
            await query.edit_message_text("⏳ <i>Выполняю рассылку сообщений... Пожалуйста, подождите.</i>", parse_mode="HTML")
    except Exception:
        pass

    user_ids = await asyncio.to_thread(database.get_broadcast_user_ids)
    text, markup = format_irl_broadcast(day, active)

    bot = context.bot
    sent = 0
    failed = 0
    for uid in user_ids:
        await asyncio.sleep(0.04)
        ok = await safe_send_broadcast(bot, chat_id=uid, thread_id=None, text=text, reply_markup=markup)
        if ok:
            sent += 1
        else:
            failed += 1

    await admin_journal.record(
        actor_id,
        "irl_broadcast_sent",
        "irl_match",
        len(active),
        new=f"отправлено: {sent}, ошибок: {failed}, день: {day}"
    )

    note = f"📢 Рассылка завершена!\nДоставлено: <b>{sent}</b> чел., не доставлено: <b>{failed}</b> чел."
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

    if action in ("pub", "rep", "can", "canok", "rest") and not arg.isdigit():
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
    elif action == "rest":
        await _answer(update)
        await _restore(update, actor_id, int(arg))
    elif action == "bcast":
        await _confirm_broadcast(update, context, day_arg)
    elif action == "bcastok":
        await _run_broadcast(update, context, actor_id, day_arg)
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
