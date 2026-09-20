"""
handlers/admin_bets.py

Мониторинг и управление ставками для супер-администраторов (Logovo.bet).
СТРОГО ТОЛЬКО В ЛИЧНЫХ СООБЩЕНИЯХ (Private chat only).
СТРОГО ТОЛЬКО ДЛЯ СУПЕР-АДМИНИСТРАТОРОВ (is_global_admin).
"""

import asyncio
import html
import json
import logging
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import config
import database
from handlers.base import is_global_admin
from handlers.cabinet import safe_send_notification
from services.bet_outcome_text import (
    FINISHED_MATCH_STATUSES,
    describe_selection,
    explain_result,
)
from services.integrity_engine import CASE_MIN_SCORE

logger = logging.getLogger(__name__)

# Маппинг статусов купона
BET_STATUS_TITLES = {
    "pending": "⏳ В игре",
    "won": "✅ Выигрыш",
    "lost": "❌ Проигрыш",
    "refunded": "🔄 Возврат",
    "cancelled": "🔄 Отмена",
    "cashed_out": "💵 Кэшаут",
}

ITEM_STATUS_EMOJI = {
    "pending": "⏳",
    "won": "✅",
    "lost": "❌",
    "refunded": "🔄",
}

ITEM_RESULT_TITLES = {
    "won": "✅ зашло",
    "lost": "❌ не зашло",
    "refunded": "🔄 возврат",
}

PAGE_SIZE = 5

MSK = timezone(timedelta(hours=3), "МСК")


def _ensure_private_chat_and_super_admin(update: Update) -> tuple[bool, int | None]:
    """Проверка ограничений: строго ЛС и строго глобальный супер-админ."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False, None

    if chat.type != "private":
        return False, user.id

    if not is_global_admin(user.id):
        return False, None

    return True, user.id


def _build_overview_header(stats: dict, filter_status: str | None = None, filter_user_id: int | None = None) -> str:
    """Генерация сводки метрик банка и конторы."""
    profit = stats.get("bookmaker_profit", 0)
    profit_sign = "+" if profit > 0 else ""
    profit_emoji = "🟢" if profit >= 0 else "🔴"

    filter_note = ""
    if filter_user_id:
        u = database.get_user(filter_user_id)
        u_name = f"@{u['username']}" if u and u.get("username") else f"ID {filter_user_id}"
        filter_note = f"\n🎯 <i>Фильтр по игроку: <b>{html.escape(u_name)}</b></i>"
    elif filter_status and filter_status != "all":
        status_label = BET_STATUS_TITLES.get(filter_status, filter_status)
        filter_note = f"\n🎯 <i>Фильтр по статусу: <b>{status_label}</b></i>"

    return (
        f"🎰 <b>МОНИТОРИНГ СТАВОК (Super-Admin)</b>{filter_note}\n"
        f"──────────────────────────────\n"
        f"📊 <b>Сводка по ставкам:</b>\n"
        f"• Всего ставок: <b>{stats['total_bets']:,}</b>\n"
        f"• ⏳ В игре: <b>{stats['count_pending']:,}</b> | ✅ Выигрышей: <b>{stats['count_won']:,}</b>\n"
        f"• ❌ Проигрышей: <b>{stats['count_lost']:,}</b> | 🔄 Возвратов: <b>{stats['count_refunded']:,}</b>\n"
        f"• 💵 Кэшаутов: <b>{stats['count_cashed_out']:,}</b>\n\n"
        f"💰 <b>Банк и Риск Конторы:</b>\n"
        f"• Оборот (Wagered): <code>{stats['total_wagered']:,} 🪙</code>\n"
        f"• В игре (Exposure): <code>{stats['pending_exposure']:,} 🪙</code>\n"
        f"• Макс. выплата по активным: <code>{stats['pending_potential_liability']:,} 🪙</code>\n"
        f"• Выплачено игрокам: <code>{stats['total_paid_out']:,} 🪙</code>\n"
        f"• Профит конторы: {profit_emoji} <b>{profit_sign}{profit:,} 🪙</b>\n"
        f"──────────────────────────────\n"
    )


def _fmt_dt(value) -> str:
    """UTC из SQLite (CURRENT_TIMESTAMP) → московское время: '2026-09-19 08:42:11' → '19.09 11:42'.

    Москва живёт в UTC+3 без перехода на летнее время, поэтому фиксированный сдвиг
    точен и не требует tzdata. Непонятный формат отдаём как есть.
    """
    raw = str(value or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw or "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


def _fmt_coins(n: int) -> str:
    return f"{int(n):,} 🪙"


def _fmt_net(n: int) -> str:
    if n > 0:
        return f"🟢 <b>+{n:,} 🪙</b>"
    if n < 0:
        return f"🔴 <b>−{abs(n):,} 🪙</b>"
    return "⚪ <b>0 🪙</b>"


def _bet_payout(bet: dict) -> int:
    """Фактическая выплата; у старых выигрышей actual_payout мог остаться 0."""
    payout = int(bet.get("actual_payout") or 0)
    if bet.get("status") == "won" and payout == 0:
        payout = int(bet.get("potential_win") or 0)
    return payout


def _player_net(bet: dict) -> int | None:
    """Чистый итог ставки для игрока; None — ставка ещё не рассчитана."""
    status = bet.get("status")
    amount = int(bet.get("amount") or 0)
    if status in ("won", "cashed_out"):
        return _bet_payout(bet) - amount
    if status == "lost":
        return -amount
    if status in ("refunded", "cancelled"):
        return 0
    return None


def _money_line(bet: dict) -> str:
    """Строка «сколько поставил → что получил» в зависимости от статуса."""
    status = bet.get("status")
    amount = int(bet.get("amount") or 0)
    odd = float(bet.get("total_odd") or 1.0)
    potential = int(bet.get("potential_win") or 0)
    stake = f"Ставка <code>{_fmt_coins(amount)}</code> × {odd:.2f}"

    if status == "won":
        return f"{stake} → выплачено <b>{_fmt_coins(_bet_payout(bet))}</b>"
    if status == "lost":
        return f"{stake} → не сыграла (могла дать {_fmt_coins(potential)})"
    if status == "cashed_out":
        return (f"Ставка <code>{_fmt_coins(amount)}</code> → кэшаут <b>{_fmt_coins(_bet_payout(bet))}</b> "
                f"(полная выплата была бы {_fmt_coins(potential)})")
    if status in ("refunded", "cancelled"):
        return f"Ставка <code>{_fmt_coins(amount)}</code> → возвращена игроку"
    return f"{stake} → возможный выигрыш <b>{_fmt_coins(potential)}</b>"


def _settled_time(bet: dict):
    if bet.get("status") == "cashed_out":
        return bet.get("cashout_at") or bet.get("settled_at")
    return bet.get("settled_at")


def _time_line(bet: dict) -> str:
    line = f"Поставлена {_fmt_dt(bet.get('created_at'))}"
    settled = _settled_time(bet)
    if settled:
        verb = "кэшаут" if bet.get("status") == "cashed_out" else "рассчитана"
        line += f" · {verb} {_fmt_dt(settled)}"
    return line + " (МСК)"


def _leg_teams(item: dict) -> tuple[str, str]:
    return item.get("team1_name") or "Хозяева", item.get("team2_name") or "Гости"


def _match_line(item: dict) -> str:
    """«Кельн 1:3 Айнтрахт», «Кельн — Айнтрахт · не сыгран», «🔴 67' Кельн 1:0 Айнтрахт»."""
    t1, t2 = (html.escape(t) for t in _leg_teams(item))
    s1, s2 = item.get("player1_score"), item.get("player2_score")
    match_status = item.get("match_status")
    minute = item.get("live_minute")

    if s1 is not None and s2 is not None:
        score_line = f"<b>{t1} {s1}:{s2} {t2}</b>"
        if match_status in FINISHED_MATCH_STATUSES:
            return score_line
        if minute:
            return f"🔴 {minute}' {score_line}"
        return f"{score_line} <i>(счёт на проверке)</i>"
    if minute:
        return f"🔴 {minute}' <b>{t1} — {t2}</b>"
    return f"<b>{t1} — {t2}</b> <i>· не сыгран</i>"


def _selection_text(item: dict) -> str:
    t1, t2 = _leg_teams(item)
    return html.escape(describe_selection(
        item.get("outcome_type"), t1, t2,
        market_key=item.get("market_key"),
        selection_name=item.get("selection_name"),
    ))


def _format_bet_snippet(bet: dict) -> str:
    """Форматирование одной ставки для ленты."""
    status = bet["status"]
    status_title = BET_STATUS_TITLES.get(status, status)
    items = bet.get("items", [])
    b_type = "Ординар" if bet.get("bet_type") == "single" else f"Экспресс из {len(items)}"

    user_name = f"@{bet['username']}" if bet.get("username") else f"ID {bet['user_id']}"
    team_name = bet.get("user_team")
    team_info = f" ({html.escape(team_name)})" if team_name else ""

    lines = [
        f"• <b>#{bet['id']}</b> · {b_type} · <b>{status_title}</b>",
        f"  👤 <b>{html.escape(user_name)}</b>{team_info}",
    ]

    for item in items[:3]:
        item_emoji = ITEM_STATUS_EMOJI.get(item.get("status"), "•")
        item_odd = float(item.get("odd") or 1.0)
        lines.append(f"  ⚽ {_match_line(item)}")
        lines.append(f"     {item_emoji} {_selection_text(item)} · <b>@{item_odd:.2f}</b>")
    if len(items) > 3:
        lines.append(f"     <i>…и ещё {len(items) - 3} событ. — в карточке 🔍</i>")

    lines.append(f"  💵 {_money_line(bet)}")
    net = _player_net(bet)
    lines.append(f"  📈 Итог игрока: {_fmt_net(net)}" if net is not None else "  📈 Итог игрока: <i>ждёт расчёта</i>")
    lines.append(f"  🕒 <i>{_time_line(bet)}</i>")

    return "\n".join(lines)


def _build_monitor_keyboard(
    status: str,
    page: int,
    total_count: int,
    bets: list[dict],
    admin_id: int,
    user_id_filter: int | None = None
) -> InlineKeyboardMarkup:
    """Генерация клавиатуры фильтров, пагинации, быстрых кнопок и переключателя оповещений."""
    keyboard = []

    # Строка 1: Фильтры статусов
    def flt_btn(code: str, label: str) -> InlineKeyboardButton:
        is_active = (status == code)
        text = f"• {label} •" if is_active else label
        uid_str = str(user_id_filter) if user_id_filter else "0"
        return InlineKeyboardButton(text, callback_data=f"admin_bets_flt:{code}:0:{uid_str}")

    keyboard.append([
        flt_btn("all", "Все"),
        flt_btn("pending", "⏳ В игре"),
        flt_btn("won", "✅ Выигрыш"),
        flt_btn("lost", "❌ Проигрыш"),
    ])

    # Строка 2: Пагинация
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    uid_str = str(user_id_filter) if user_id_filter else "0"
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Пред", callback_data=f"admin_bets_page:{status}:{page - 1}:{uid_str}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"admin_bets_refresh:{status}:{page}:{uid_str}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("След ▶️", callback_data=f"admin_bets_page:{status}:{page + 1}:{uid_str}"))
    keyboard.append(nav_row)

    # Строка 3: Быстрые кнопки просмотра деталей по каждой ставке на странице
    if bets:
        detail_buttons = [
            InlineKeyboardButton(f"🔍 #{b['id']}", callback_data=f"admin_bet_view:{b['id']}")
            for b in bets
        ]
        # Разбиваем по 3 в ряд
        for i in range(0, len(detail_buttons), 3):
            keyboard.append(detail_buttons[i:i + 3])

    # Вход в детектор договорных матчей. Счётчик — только неразобранные дела
    # выше порога; служебные строки-заглушки движка в него не попадают.
    if config.INTEGRITY_ENABLED:
        try:
            pending_cases = database.count_integrity_cases(
                status="open", min_score=CASE_MIN_SCORE
            )
        except Exception as e:
            logger.debug(f"Failed to count integrity cases: {e}")
            pending_cases = 0
        label = f"🕵️ Подозрения ({pending_cases})" if pending_cases else "🕵️ Подозрения"
        keyboard.append([InlineKeyboardButton(label, callback_data="admin_integrity_hub")])

    # Строка 4: Переключатель Live-оповещений + Сброс фильтра игрока (если включен)
    alerts_on = database.is_live_bet_alerts_enabled(admin_id)
    alert_label = "🔔 Оповещения: ВКЛ" if alerts_on else "🔕 Оповещения: ВЫКЛ"
    ctrl_row = [
        InlineKeyboardButton(alert_label, callback_data=f"admin_bets_alerts_toggle:{status}:{page}:{uid_str}"),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"admin_bets_refresh:{status}:{page}:{uid_str}"),
    ]
    keyboard.append(ctrl_row)

    bottom_row = []
    if user_id_filter:
        bottom_row.append(InlineKeyboardButton("👥 Сбросить фильтр игрока", callback_data=f"admin_bets_flt:{status}:0:0"))
    bottom_row.append(InlineKeyboardButton("« Админ-панель", callback_data="admin_main_menu"))
    keyboard.append(bottom_row)

    return InlineKeyboardMarkup(keyboard)


async def cmd_admin_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /admin_bets (алиасы: /all_bets, /track_bets, /ставки_админ).
    Работает строго в ЛС и только для супер-админа.
    """
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    if chat.type != "private":
        bot_user = (context.bot.username or "").lower() if context and context.bot else ""
        pm_url = f"https://t.me/{bot_user}?start=admin_bets" if bot_user else "https://t.me"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Открыть в ЛС", url=pm_url)]])
        await update.effective_message.reply_text(
            "🔒 <b>Команда доступна только в личных сообщениях</b>\n\n"
            "Мониторинг ставок содержит конфиденциальную финансовую статистику "
            "и информацию об игроках. Пожалуйста, откройте бота в ЛС.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return

    if not is_global_admin(user.id):
        await update.effective_message.reply_text(
            "⛔ <b>Доступ запрещён</b>\n\n"
            "Данная команда доступна исключительно супер-администраторам лиги.",
            parse_mode="HTML"
        )
        return

    # Разбор аргументов
    status_filter = "all"
    user_id_filter = None

    if context and context.args:
        arg = context.args[0].strip().lower()
        if arg in ("pending", "active", "в_игре", "игра"):
            status_filter = "pending"
        elif arg in ("won", "win", "выигрыш"):
            status_filter = "won"
        elif arg in ("lost", "проигрыш"):
            status_filter = "lost"
        elif arg in ("refunded", "cancelled", "возврат"):
            status_filter = "refunded"
        elif arg.startswith("@"):
            raw_user = arg[1:]
            with database.transaction() as conn:
                cur = conn.cursor()
                cur.execute("SELECT telegram_id FROM users WHERE LOWER(username) = ?", (raw_user,))
                row = cur.fetchone()
                if row:
                    user_id_filter = row["telegram_id"]
        elif arg.isdigit():
            user_id_filter = int(arg)

    await _render_bets_monitor(update, context, status=status_filter, page=0, user_id_filter=user_id_filter)


async def _render_bets_monitor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status: str = "all",
    page: int = 0,
    user_id_filter: int | None = None,
    edit: bool = False
) -> None:
    """Отрисовка главного экрана мониторинга ставок."""
    user = update.effective_user
    if not user:
        return

    offset = page * PAGE_SIZE
    bets, total_count = await asyncio.to_thread(
        database.get_all_bets,
        status=status,
        user_id=user_id_filter,
        limit=PAGE_SIZE,
        offset=offset
    )
    stats = await asyncio.to_thread(database.get_bets_summary_stats)

    header = _build_overview_header(stats, filter_status=status, filter_user_id=user_id_filter)

    if not bets:
        body = "\n<i>Ставок с выбранными критериями не найдено.</i>\n"
    else:
        items_text = "\n\n".join(_format_bet_snippet(b) for b in bets)
        body = f"\n📋 <b>Лента ставок ({page * PAGE_SIZE + 1}-{min((page + 1) * PAGE_SIZE, total_count)} из {total_count}):</b>\n\n{items_text}\n"

    full_text = header + body
    reply_markup = _build_monitor_keyboard(status, page, total_count, bets, user.id, user_id_filter)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(full_text, reply_markup=reply_markup, parse_mode="HTML")
            return
        except Exception as e:
            logger.debug(f"Failed edit_message_text in _render_bets_monitor: {e}")

    if update.effective_message:
        await update.effective_message.reply_text(full_text, reply_markup=reply_markup, parse_mode="HTML")


async def cb_admin_bets_navigate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик навигации, смены фильтра и обновления списка."""
    query = update.callback_query
    if not query:
        return

    ok, admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён или чат не является приватным.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    action = parts[0]
    status = parts[1] if len(parts) > 1 else "all"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    raw_uid = parts[3] if len(parts) > 3 else "0"
    user_id_filter = int(raw_uid) if raw_uid.isdigit() and int(raw_uid) > 0 else None

    await _render_bets_monitor(update, context, status=status, page=page, user_id_filter=user_id_filter, edit=True)


async def cb_admin_bets_toggle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переключение подписки супер-админа на Live-оповещения о ставках."""
    query = update.callback_query
    if not query:
        return

    ok, admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    current = await asyncio.to_thread(database.is_live_bet_alerts_enabled, admin_id)
    new_state = not current
    await asyncio.to_thread(database.set_live_bet_alerts_enabled, admin_id, new_state)

    msg = "🔔 Live-оповещения о ставках включены!" if new_state else "🔕 Live-оповещения о ставках выключены."
    await query.answer(msg, show_alert=True)

    data = query.data or ""
    parts = data.split(":")
    status = parts[1] if len(parts) > 1 else "all"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    raw_uid = parts[3] if len(parts) > 3 else "0"
    user_id_filter = int(raw_uid) if raw_uid.isdigit() and int(raw_uid) > 0 else None

    await _render_bets_monitor(update, context, status=status, page=page, user_id_filter=user_id_filter, edit=True)


def _format_player_stats(stats: dict) -> list[str]:
    """Блок «как этот игрок ставит вообще» для карточки ставки."""
    win_rate = stats.get("win_rate")
    win_rate_str = f"{win_rate:.1f}%" if win_rate is not None else "—"
    return [
        "📊 <b>Игрок в ставках (за всё время):</b>",
        f"• Ставок: <b>{stats['total_bets']:,}</b> (⏳ в игре: {stats['count_pending']:,} на {_fmt_coins(stats['pending_amount'])})",
        f"• ✅ {stats['count_won']:,} · ❌ {stats['count_lost']:,} · 💵 кэшаутов {stats['count_cashed_out']:,} · 🔄 возвратов {stats['count_refunded']:,}",
        f"• Процент побед: <b>{win_rate_str}</b>",
        f"• Поставлено: {_fmt_coins(stats['total_wagered'])} · получено: {_fmt_coins(stats['total_paid_out'])}",
        f"• Итог по рассчитанным ставкам: {_fmt_net(int(stats['net_profit']))}",
    ]


def _format_cashout_block(bet: dict) -> list[str]:
    """Сколько игрок забрал кэшаутом и чем в итоге обернулся бы купон."""
    payout = _bet_payout(bet)
    potential = int(bet.get("potential_win") or 0)
    lines = [
        "💵 <b>Кэшаут:</b>",
        f"• Забрал: <b>{_fmt_coins(payout)}</b> из {_fmt_coins(potential)} возможных",
    ]
    leg_statuses = [it.get("status") for it in bet.get("items", [])]
    if leg_statuses and "lost" in leg_statuses:
        lines.append(f"• Купон в итоге <b>не сыграл</b> — кэшаут спас игроку {_fmt_coins(payout)}")
    elif leg_statuses and all(s in ("won", "refunded") for s in leg_statuses):
        lines.append(f"• Купон в итоге <b>сыграл</b> — игрок недополучил {_fmt_coins(max(potential - payout, 0))}")
    else:
        lines.append(f"• Если купон сыграет, недополучит {_fmt_coins(max(potential - payout, 0))}")
    return lines


def _format_bet_card(bet: dict, player_stats: dict) -> str:
    """Подробная карточка одной ставки."""
    bet_id = bet["id"]
    status = bet["status"]
    items = bet.get("items", [])
    b_type = "Ординар" if bet.get("bet_type") == "single" else f"Экспресс из {len(items)} событий"
    amount = int(bet.get("amount") or 0)
    odd = float(bet.get("total_odd") or 1.0)
    potential_win = int(bet.get("potential_win") or 0)

    u_name = f"@{bet['username']}" if bet.get("username") else f"ID {bet['user_id']}"
    club = html.escape(bet.get("user_team") or "—")
    league = bet.get("user_league")
    league_part = f" · Лига: {html.escape(league)}" if league else ""
    wallet_bal = int(bet.get("user_wallet_balance") or 0)

    lines = [
        f"🔍 <b>КАРТОЧКА СТАВКИ #{bet_id}</b>",
        "──────────────────────────────",
        f"👤 <b>Игрок:</b> {html.escape(u_name)} (ID: <code>{bet['user_id']}</code>)",
        f"🛡 <b>Клуб:</b> {club}{league_part}",
        f"🪙 <b>Баланс сейчас:</b> <code>{_fmt_coins(wallet_bal)}</code>",
        "",
        *_format_player_stats(player_stats),
        "──────────────────────────────",
        f"📋 <b>Тип:</b> {b_type}",
        f"📌 <b>Статус:</b> <b>{BET_STATUS_TITLES.get(status, status)}</b>",
        f"💵 <b>Сумма ставки:</b> <code>{_fmt_coins(amount)}</code>",
        f"📊 <b>Коэффициент:</b> <b>{odd:.2f}</b>",
        f"🎯 <b>Возможный выигрыш:</b> <code>{_fmt_coins(potential_win)}</code>",
    ]
    if status in ("won", "cashed_out"):
        lines.append(f"💰 <b>Выплачено:</b> <code>{_fmt_coins(_bet_payout(bet))}</code>")
    net = _player_net(bet)
    lines.append(f"📈 <b>Итог для игрока:</b> {_fmt_net(net)}" if net is not None else "📈 <b>Итог для игрока:</b> <i>ждёт расчёта</i>")
    lines.append(f"🕒 <b>Поставлена:</b> {_fmt_dt(bet.get('created_at'))} МСК")
    settled = _settled_time(bet)
    if settled:
        label = "Кэшаут сделан" if status == "cashed_out" else "Рассчитана"
        lines.append(f"🏁 <b>{label}:</b> {_fmt_dt(settled)} МСК")

    if status == "cashed_out":
        lines.append("")
        lines.extend(_format_cashout_block(bet))

    lines.append("\n⚽ <b>События в купоне:</b>")
    for idx, it in enumerate(items, 1):
        t1, t2 = _leg_teams(it)
        it_status = it.get("status", "pending")
        it_emoji = ITEM_STATUS_EMOJI.get(it_status, "•")
        it_odd = float(it.get("odd") or 1.0)
        div_name = it.get("division_name") or "Дивизион не указан"

        leg = [
            f"{idx}. {it_emoji} {_match_line(it)}",
            f"   🏆 {html.escape(div_name)} · Тур {it.get('tour', 1)}",
            f"   🎯 Выбор: <b>{_selection_text(it)}</b> · @<b>{it_odd:.2f}</b>",
        ]
        reason = explain_result(
            it.get("outcome_type"), t1, t2,
            it.get("player1_score"), it.get("player2_score"),
            market_key=it.get("market_key"),
            ht_score1=it.get("ht_score1"), ht_score2=it.get("ht_score2"),
        )
        if reason:
            verdict = ITEM_RESULT_TITLES.get(it_status)
            suffix = f" → {verdict}" if verdict else ""
            leg.append(f"   🧾 <i>{html.escape(reason)}</i>{suffix}")
        lines.append("\n".join(leg))

    return "\n".join(lines)


async def cb_admin_bet_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отображение подробной карточки конкретной ставки."""
    query = update.callback_query
    if not query:
        return

    ok, admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""
    try:
        bet_id = int(data.split(":")[1])
    except (IndexError, ValueError):
        await query.answer("Неверный ID ставки.", show_alert=True)
        return

    bet = await asyncio.to_thread(database.get_bet_by_id, bet_id)
    if not bet:
        await query.answer("Ставка не найдена.", show_alert=True)
        return

    player_stats = await asyncio.to_thread(database.get_user_bet_summary, bet["user_id"])
    text = _format_bet_card(bet, player_stats)

    kb = []
    # Если ставка в игре, суперадмин может ее аннулировать (Void)
    if bet["status"] == "pending":
        kb.append([InlineKeyboardButton("⚠️ Аннулировать ставку (Void / Возврат)", callback_data=f"admin_bet_void_ask:{bet_id}")])

    kb.append([
        InlineKeyboardButton("👤 Все ставки игрока", callback_data=f"admin_bets_flt:all:0:{bet['user_id']}"),
        InlineKeyboardButton("« К списку ставок", callback_data="admin_bets_page:all:0:0")
    ])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def cb_admin_bet_void_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждение аннулирования ставки."""
    query = update.callback_query
    if not query:
        return

    ok, admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    await query.answer()
    data = query.data or ""
    bet_id = int(data.split(":")[1])

    bet = await asyncio.to_thread(database.get_bet_by_id, bet_id)
    if not bet:
        await query.answer("Ставка не найдена.", show_alert=True)
        return

    u_name = f"@{bet['username']}" if bet.get("username") else f"ID {bet['user_id']}"
    text = (
        f"⚠️ <b>Подтверждение аннулирования ставки #{bet_id}</b>\n\n"
        f"• Игрок: <b>{html.escape(u_name)}</b>\n"
        f"• Сумма возврата: <code>{bet['amount']:,} 🪙</code>\n"
        f"• Текущий статус: <b>{BET_STATUS_TITLES.get(bet['status'], bet['status'])}</b>\n\n"
        f"<i>При подтверждении ставка получит статус «refunded», а {bet['amount']:,} монет будут мгновенно возвращены на кошелёк пользователя.</i>"
    )

    kb = [
        [
            InlineKeyboardButton("✅ Да, аннулировать", callback_data=f"admin_bet_void_do:{bet_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"admin_bet_view:{bet_id}")
        ]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def cb_admin_bet_void_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выполнение аннулирования ставки."""
    query = update.callback_query
    if not query:
        return

    ok, admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    data = query.data or ""
    bet_id = int(data.split(":")[1])

    try:
        res = await asyncio.to_thread(database.void_user_bet, bet_id=bet_id, actor_id=admin_id)
        await query.answer("Ставка успешно аннулирована!", show_alert=True)

        text = (
            f"✅ <b>Ставка #{bet_id} успешно аннулирована!</b>\n\n"
            f"💵 <b>Сумма возврата:</b> <code>{res['refunded_amount']:,} 🪙</code>\n"
            f"👤 <b>Пользователь ID:</b> <code>{res['user_id']}</code>\n\n"
            f"<i>Средства зачислены обратно на баланс игрока. Запись внесена в аудит-лог.</i>"
        )
        kb = [[InlineKeyboardButton("« К списку ставок", callback_data="admin_bets_page:all:0:0")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    except ValueError as e:
        await query.answer(f"Ошибка: {e}", show_alert=True)
    except Exception as e:
        logger.exception("Error in cb_admin_bet_void_execute")
        await query.answer(f"Непредвиденная ошибка: {e}", show_alert=True)


async def notify_super_admins_new_bet(bot=None, bet_id: int = 0) -> None:
    """
    Отправка уведомления подписанным супер-администраторам в ЛС о новой ставке.
    Вызывается после размещения ставки.
    """
    try:
        subscribers = await asyncio.to_thread(database.get_live_bet_alert_subscribers)
        if not subscribers:
            return

        if bot is None:
            import config
            from telegram import Bot
            if not getattr(config, "TOKEN", None):
                return
            bot = Bot(config.TOKEN)

        bet = await asyncio.to_thread(database.get_bet_by_id, bet_id)
        if not bet:
            return

        u_name = f"@{bet['username']}" if bet.get("username") else f"ID {bet['user_id']}"
        team_name = bet.get("user_team")
        team_str = f" ({html.escape(team_name)})" if team_name else ""
        b_type = "Ординар" if bet.get("bet_type") == "single" else "Экспресс"
        amount = bet.get("amount", 0)
        odd = float(bet.get("total_odd") or 1.0)
        potential_win = bet.get("potential_win", 0)

        lines = [
            f"🎰 <b>Новая ставка #{bet_id}!</b> ({b_type})",
            f"👤 <b>Игрок:</b> {html.escape(u_name)}{team_str}",
            f"💵 <b>Сумма:</b> <code>{amount:,} 🪙</code> | Кэф: <b>{odd:.2f}</b>",
            f"🎯 <b>Потенц. выигрыш:</b> <code>{potential_win:,} 🪙</code>",
            f"🕒 <b>Поставлена:</b> {_fmt_dt(bet.get('created_at'))} МСК",
            "",
            "⚽ <b>События:</b>"
        ]
        for it in bet.get("items", [])[:3]:
            t1, t2 = (html.escape(t) for t in _leg_teams(it))
            it_odd = float(it.get("odd") or 1.0)
            lines.append(f"• <b>{t1} — {t2}</b>\n   {_selection_text(it)} · @{it_odd:.2f}")

        text = "\n".join(lines)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Открыть карточку", callback_data=f"admin_bet_view:{bet_id}")]])

        for admin_id in subscribers:
            asyncio.create_task(safe_send_notification(bot, admin_id, text, reply_markup=kb))

    except Exception as e:
        logger.debug(f"Failed to notify super admins about new bet #{bet_id}: {e}")


# ─── Экран «Подозрения»: дела о возможных договорных матчах ──────────────────
# Только супер-админ и только здесь: детектор не пишет никому в личку.

SEVERITY_EMOJI = {
    "critical": "⛔",
    "high": "🔴",
    "medium": "🟠",
    "low": "🟡",
}

CASE_STATUS_TITLES = {
    "open": "🆕 Не разобрано",
    "acknowledged": "👁 Разобрано",
    "dismissed": "🚫 Ложное",
    "confirmed": "⛔ Сговор подтверждён",
}

# Фильтры ленты: (подпись, kwargs для get_integrity_cases/count_integrity_cases).
# Порог CASE_MIN_SCORE отсекает служебные строки-заглушки: движок пишет дело на
# каждую оценённую ногу, чтобы не пересчитывать её в каждом прогоне джобы.
INTEGRITY_FILTERS = {
    "open": ("🆕 Новые", {"status": "open", "min_score": CASE_MIN_SCORE}),
    "high": ("🔴 Тяжёлые", {"min_score": 70.0}),
    "all": ("Все", {"min_score": CASE_MIN_SCORE}),
}


def _integrity_filter(code: str) -> tuple[str, dict]:
    label, kwargs = INTEGRITY_FILTERS.get(code, INTEGRITY_FILTERS["open"])
    return label, dict(kwargs)


def _case_teams(case: dict) -> str:
    t1 = html.escape(str(case.get("player1_team") or "?"))
    t2 = html.escape(str(case.get("player2_team") or "?"))
    s1, s2 = case.get("player1_score"), case.get("player2_score")
    if s1 is not None and s2 is not None:
        return f"{t1} {s1}:{s2} {t2}"
    return f"{t1} — {t2}"


def _case_player(case: dict) -> str:
    team = case.get("user_team")
    username = case.get("username")
    if team and username:
        label = f"{team} (@{username})"
    elif team:
        label = str(team)
    elif username:
        label = f"@{username}"
    else:
        label = f"ID {case.get('user_id')}"
    return html.escape(label)


def _case_pick(case: dict) -> str:
    pick = case.get("selection_name") or case.get("outcome_type") or "?"
    return html.escape(str(pick))


def _case_features(case: dict) -> dict:
    raw = case.get("features")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_integrity_row(case: dict) -> str:
    """Одна строка ленты дел."""
    emoji = SEVERITY_EMOJI.get(case.get("severity"), "🟡")
    score = float(case.get("total_score") or 0)
    flag = " ⚠️ мало истории" if case.get("low_confidence") else ""
    odd = float(case.get("odds_at_placement") or case.get("odd") or 0)
    status = CASE_STATUS_TITLES.get(case.get("status"), case.get("status") or "")
    return (
        f"{emoji} <b>#{case['id']} · {score:.0f}/100</b>{flag}\n"
        f"👤 {_case_player(case)} · {_fmt_coins(case.get('amount') or 0)}\n"
        f"⚽ {_case_teams(case)}\n"
        f"🎯 {_case_pick(case)} @ {odd:.2f} · {status}"
    )


def _format_integrity_card(case: dict) -> str:
    """Карточка дела: чем ставка не похожа на обычную, построчно."""
    emoji = SEVERITY_EMOJI.get(case.get("severity"), "🟡")
    score = float(case.get("total_score") or 0)
    odd = float(case.get("odds_at_placement") or case.get("odd") or 0)
    features = _case_features(case)

    lines = [
        f"{emoji} <b>Дело #{case['id']} · индекс {score:.0f}/100</b>",
        f"<i>{CASE_STATUS_TITLES.get(case.get('status'), case.get('status') or '')}</i>",
        "",
        f"👤 <b>Игрок:</b> {_case_player(case)}",
        f"⚽ <b>Матч:</b> {_case_teams(case)}",
        f"🎯 <b>Выбор:</b> {_case_pick(case)} @ {odd:.2f}",
        f"💰 <b>Ставка:</b> {_fmt_coins(case.get('amount') or 0)}"
        f" · выплата {_fmt_coins(case.get('actual_payout') or 0)}",
        f"🕒 <b>Размещена:</b> {_fmt_dt(case.get('placed_at'))}",
    ]

    model_p = features.get("model_probability")
    if isinstance(model_p, (int, float)):
        lines.append(f"📉 <b>Модель давала исходу:</b> {model_p * 100:.1f}%")

    if case.get("low_confidence"):
        lines.append("")
        lines.append("⚠️ <i>У игрока мало истории ставок — оценка ориентировочная.</i>")

    gate = features.get("gate")
    if gate == "families":
        lines.append("")
        lines.append("<i>Сработало только одно семейство признаков — балл обнулён.</i>")
    elif gate == "lost":
        lines.append("")
        lines.append("<i>Нога не зашла — знать результат заранее было нечего.</i>")

    rows = [
        r for r in (features.get("online") or []) + (features.get("post") or [])
        if isinstance(r, dict) and (r.get("points") or 0) > 0
    ]
    rows.sort(key=lambda r: r.get("points") or 0, reverse=True)
    if rows:
        lines.append("")
        lines.append("🔍 <b>Что сработало:</b>")
        for r in rows:
            label = html.escape(str(r.get("label") or r.get("name") or "?"))
            lines.append(f"• {label}: <b>+{float(r['points']):.1f}</b>")

    online = float(case.get("online_score") or 0)
    post = float(case.get("post_score") or 0)
    lines.append("")
    lines.append(f"<i>Онлайн {online:.0f} (в зачёт до 60) + после матча {post:.0f}</i>")

    if case.get("reviewed_by"):
        lines.append(
            f"<i>Разобрал ID {case['reviewed_by']} · {_fmt_dt(case.get('reviewed_at'))}</i>"
        )

    lines.append("")
    lines.append("<i>Индекс — повод посмотреть вручную, а не доказательство.</i>")
    return "\n".join(lines)


def _build_integrity_keyboard(
    code: str, page: int, total: int, cases: list
) -> InlineKeyboardMarkup:
    keyboard = []

    def flt_btn(c: str) -> InlineKeyboardButton:
        label = INTEGRITY_FILTERS[c][0]
        text = f"• {label} •" if c == code else label
        return InlineKeyboardButton(text, callback_data=f"admin_integrity_flt:{c}:0")

    keyboard.append([flt_btn(c) for c in ("open", "high", "all")])

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            "◀️ Пред", callback_data=f"admin_integrity_page:{code}:{page - 1}"
        ))
    nav_row.append(InlineKeyboardButton(
        f"{page + 1}/{total_pages}", callback_data=f"admin_integrity_refresh:{code}:{page}"
    ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            "След ▶️", callback_data=f"admin_integrity_page:{code}:{page + 1}"
        ))
    keyboard.append(nav_row)

    buttons = [
        InlineKeyboardButton(
            f"🔍 #{c['id']}",
            callback_data=f"admin_integrity_case:{c['id']}:{code}:{page}"
        )
        for c in cases
    ]
    for i in range(0, len(buttons), 3):
        keyboard.append(buttons[i:i + 3])

    keyboard.append([
        InlineKeyboardButton("🔄 Обновить", callback_data=f"admin_integrity_refresh:{code}:{page}"),
        InlineKeyboardButton("💰 Ставки", callback_data="admin_bets_refresh:all:0:0"),
    ])
    keyboard.append([InlineKeyboardButton("« Админ-панель", callback_data="admin_main_menu")])
    return InlineKeyboardMarkup(keyboard)


async def _render_integrity_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    code: str = "open",
    page: int = 0,
    edit: bool = False
) -> None:
    """Лента дел о возможных договорных матчах."""
    label, kwargs = _integrity_filter(code)
    offset = page * PAGE_SIZE

    cases = await asyncio.to_thread(
        database.get_integrity_cases,
        kwargs.get("status"), kwargs.get("severity"), kwargs.get("min_score"),
        PAGE_SIZE, offset
    )
    total = await asyncio.to_thread(
        database.count_integrity_cases,
        kwargs.get("status"), kwargs.get("severity"), kwargs.get("min_score")
    )

    header = (
        "🕵️ <b>Подозрительные ставки</b>\n"
        f"<i>Фильтр: {label} · найдено: {total}</i>\n\n"
        "Индекс считается по отклонению исхода от нашей модели и по тому, насколько "
        "ставка не похожа на обычное поведение этого игрока. Ставки при этом не "
        "блокируются — это повод посмотреть вручную.\n"
    )

    if not cases:
        body = "\n<i>По этому фильтру дел нет.</i>\n"
    else:
        body = "\n" + "\n\n".join(_format_integrity_row(c) for c in cases) + "\n"

    markup = _build_integrity_keyboard(code, page, total, cases)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                header + body, reply_markup=markup, parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.debug(f"Failed to edit integrity list message: {e}")

    if update.effective_message:
        await update.effective_message.reply_text(
            header + body, reply_markup=markup, parse_mode="HTML"
        )


async def cmd_admin_integrity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /integrity и вход с экрана мониторинга ставок."""
    query = update.callback_query
    ok, _admin_id = _ensure_private_chat_and_super_admin(update)

    if not ok:
        if query:
            await query.answer("⛔ Доступ запрещён или чат не является приватным.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                "⛔ <b>Доступ запрещён</b>\n\n"
                "Экран доступен исключительно супер-администраторам лиги и только в ЛС.",
                parse_mode="HTML"
            )
        return

    if query:
        await query.answer()

    await _render_integrity_list(update, context, code="open", page=0, edit=bool(query))


async def cb_admin_integrity_navigate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пагинация, смена фильтра и обновление ленты дел."""
    query = update.callback_query
    if not query:
        return

    ok, _admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён или чат не является приватным.", show_alert=True)
        return

    await query.answer()
    parts = (query.data or "").split(":")
    code = parts[1] if len(parts) > 1 else "open"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    await _render_integrity_list(update, context, code=code, page=page, edit=True)


async def cb_admin_integrity_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Карточка одного дела с разбором признаков."""
    query = update.callback_query
    if not query:
        return

    ok, _admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён или чат не является приватным.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    case_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    code = parts[2] if len(parts) > 2 else "open"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    case = await asyncio.to_thread(database.get_integrity_case, case_id)
    if not case:
        await query.answer("Дело не найдено.", show_alert=True)
        return

    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Разобрано", callback_data=f"admin_integrity_ack:{case_id}:{code}:{page}"),
            InlineKeyboardButton("🚫 Ложное", callback_data=f"admin_integrity_dismiss:{case_id}:{code}:{page}"),
        ],
        [InlineKeyboardButton(
            "⛔ Подтверждаю сговор",
            callback_data=f"admin_integrity_confirm:{case_id}:{code}:{page}"
        )],
        [
            InlineKeyboardButton(
                f"🧾 Купон #{case['bet_id']}", callback_data=f"admin_bet_view:{case['bet_id']}"
            ),
            InlineKeyboardButton("« К списку", callback_data=f"admin_integrity_flt:{code}:{page}"),
        ],
    ])

    try:
        await query.edit_message_text(
            _format_integrity_card(case), reply_markup=keyboard, parse_mode="HTML"
        )
    except Exception as e:
        logger.debug(f"Failed to edit integrity card message: {e}")


async def cb_admin_integrity_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вердикт супер-админа: разобрано / ложное / сговор подтверждён."""
    query = update.callback_query
    if not query:
        return

    ok, admin_id = _ensure_private_chat_and_super_admin(update)
    if not ok:
        await query.answer("⛔ Доступ запрещён или чат не является приватным.", show_alert=True)
        return

    parts = (query.data or "").split(":")
    action = parts[0].replace("admin_integrity_", "")
    case_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    code = parts[2] if len(parts) > 2 else "open"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    new_status = {"ack": "acknowledged", "dismiss": "dismissed", "confirm": "confirmed"}.get(action)
    if not new_status:
        await query.answer()
        return

    changed = await asyncio.to_thread(
        database.set_integrity_case_status, case_id, new_status, admin_id, None
    )
    if changed:
        await query.answer(CASE_STATUS_TITLES.get(new_status, "Готово"))
    else:
        await query.answer("Дело не найдено.", show_alert=True)

    await _render_integrity_list(update, context, code=code, page=page, edit=True)
