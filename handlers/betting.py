"""
handlers/betting.py

Logovo.bet — Telegram Interactive UI & Betting Engine Handlers.
Manages user wallets, betting line navigation, single & express slips,
and leaderboard display.
"""

import html
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from handlers.base import is_admin
import database
from services.betting_limits import BettingLimitsService, DEFAULT_MAX_PAYOUT
from services.betting_engine import generate_round_markets

logger = logging.getLogger(__name__)

# Длина экспресса: от 2 событий (одно событие — ординар) до потолка, который
# главный админ задаёт в панели (database.get_max_express_events, по умолчанию 15).
# Серверная проверка живёт в database.place_user_bet; здесь — UI-зеркало.
MIN_EXPRESS_EVENTS = database.MIN_EXPRESS_EVENTS

# Human-readable outcome names
OUTCOME_TITLES = {
    "p1": "Победа 1",
    "x": "Ничья",
    "p2": "Победа 2",
    "tb25": "Тотал Б 2.5",
    "tm25": "Тотал М 2.5",
    "btts_yes": "Обе забьют: ДА",
    "btts_no": "Обе забьют: НЕТ"
}


def _check_betting_access(user_id: int) -> bool:
    """Check if Logovo.bet is accessible to the user (global admins only while in Lockdown)."""
    from handlers.base import is_logovo_access_allowed
    try:
        return is_logovo_access_allowed(user_id)
    except Exception:
        return False


def _get_slip(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Retrieve or initialize current user's bet coupon in session."""
    if "bet_slip" not in context.user_data:
        context.user_data["bet_slip"] = []
    return context.user_data["bet_slip"]


def _format_wallet_header(user_id: int, wallet: dict) -> str:
    """Render beautiful status block with coins and statistics."""
    bal = wallet.get("balance", 0)
    wagered = wallet.get("total_wagered", 0)
    won = wallet.get("total_won", 0)
    b_count = wallet.get("bets_count", 0)
    b_won = wallet.get("bets_won", 0)
    
    winrate = int((b_won / b_count * 100)) if b_count > 0 else 0
    profit = won - wagered
    profit_str = f"+{profit:,} 🪙" if profit >= 0 else f"{profit:,} 🪙"

    return (
        f"🎰 <b>Букмекерская Контора «Logovo.bet»</b>\n"
        f"<i>Управляющий: ИИ «Темшик»</i>\n\n"
        f"🪙 <b>Ваш баланс:</b> <code>{bal:,} 🪙</code>\n"
        f"📊 <b>Ставок:</b> {b_count} | <b>Побед:</b> {b_won} (<b>{winrate}%</b>)\n"
        f"📈 <b>Чистый профит:</b> <code>{profit_str}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )


async def cmd_bet_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entrypoint for /bet and /logovobet."""
    if not update.effective_user:
        return

    user_id = update.effective_user.id

    # Check access restriction
    if not _check_betting_access(user_id):
        text_restricted = (
            "🔒 <b>Logovo.bet временно недоступен</b>\n\n"
            "<i>Букмекерская контора ИИ «Темшик» в данный момент находится на техническом обслуживании.\n\n"
            "Следите за анонсами в канале лиги! 🎰</i>"
        )
        if update.callback_query:
            await update.callback_query.answer("🎰 Logovo.bet временно недоступен.", show_alert=True)
        elif update.message:
            await update.message.reply_text(text_restricted, parse_mode="HTML")
        return

    wallet = await asyncio.to_thread(database.get_or_create_wallet, user_id)
    slip = _get_slip(context)

    text = _format_wallet_header(user_id, wallet)
    text += "\nВыберите действие:"

    slip_count = len(slip)
    slip_btn_text = f"🎫 Купон ({slip_count})" if slip_count > 0 else "🎫 Мой Купон"

    kb = [
        [InlineKeyboardButton("📋 Линия на Тур", callback_data="bet_view_tours")],
        [
            InlineKeyboardButton(slip_btn_text, callback_data="bet_view_slip"),
            InlineKeyboardButton("📜 Мои Ставки", callback_data="bet_my_history")
        ],
        [
            InlineKeyboardButton("🏆 Топ Капперов", callback_data="bet_leaderboard")
        ]
    ]

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
            )
        except Exception:
            await update.effective_chat.send_message(
                text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
            )
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
        )


async def cb_bet_view_tours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available tours with open betting markets."""
    query = update.callback_query
    await query.answer()

    open_tours = await asyncio.to_thread(database.get_open_betting_tours)

    if not open_tours:
        text = (
            "🔒 <b>Линия ставок закрыта</b>\n\n"
            "<i>Все матчи текущих туров уже сыграны либо истёк дедлайн тура.</i>\n\n"
            "Темшик откроет новую линию ставок сразу после объявления следующего тура/туров администрацией чемпионата! 🎰"
        )
        kb = [[InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return

    if len(open_tours) == 1:
        # Single open tour -> display match list directly
        tour_num = open_tours[0]["round_number"]
        await _render_tour_matches(
            query, tour_num, open_tours[0].get("deadline"),
            context=context, division_id=open_tours[0].get("division_id")
        )
        return

    # Multiple open tours -> display interactive tour picker
    text = "📋 <b>Открытые Туры для Ставок</b>\n\nВыберите тур для просмотра линии коэффициентов:\n"
    kb = []
    for t in open_tours:
        r_num = t["round_number"]
        unplayed = t["unplayed_matches"]
        dl = t.get("deadline")
        dl_note = f"⏰ до {dl[:16]}" if dl else ""
        btn_text = f"⚽ Тур {r_num} ({unplayed} матчей) {dl_note}".strip()
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"bet_tour_{r_num}")])

    kb.append([
        InlineKeyboardButton("🎫 Мой Купон", callback_data="bet_view_slip"),
        InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")
    ])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def cb_bet_pick_tour(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show matches for a specific picked tour."""
    query = update.callback_query
    await query.answer()
    tour_num = int(query.data.replace("bet_tour_", ""))
    r_info = await asyncio.to_thread(database.get_round_info, tour_num)
    dl = r_info.get("deadline") if r_info else None
    div_id = r_info.get("division_id") if r_info else None
    await _render_tour_matches(query, tour_num, dl, context=context, division_id=div_id)


async def _render_tour_matches(
    query,
    tour_num: int,
    deadline: str | None = None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    division_id: int | None = None,
) -> None:
    """Render the tour line: the four central matches with inline odds buttons.

    В линию тура выставлены ровно четыре центральных матча — здесь показываются
    только они, по одной строке исходов на матч.
    """
    await asyncio.to_thread(generate_round_markets, tour_num, division_id)
    markets = await asyncio.to_thread(database.get_active_bet_markets, tour_num, division_id)

    if not markets:
        text = (
            f"📋 <b>Линия на Тур {tour_num}</b>\n\n"
            f"<i>Все матчи тура уже сыграны либо дедлайн истёк. Линия закрыта.</i>"
        )
        kb = [
            [InlineKeyboardButton("📋 Все Туры", callback_data="bet_view_tours")],
            [InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return

    slip = _get_slip(context) if context is not None else []
    picked = {s["match_id"]: s for s in slip}

    dl_text = f"\n⏰ <b>Дедлайн тура:</b> <code>{deadline[:16]}</code>" if deadline else ""
    lines = [
        f"📋 <b>Линия Logovo.bet • Тур {tour_num}</b>{dl_text}",
        f"<i>Центральные матчи тура — {len(markets)} из отобранных ИИ «Темшик».</i>",
        ""
    ]
    kb = []
    for idx, m in enumerate(markets, start=1):
        m_id = m.get("match_id")
        t1 = html.escape(str(m.get("team1_name", "Команда 1")))
        t2 = html.escape(str(m.get("team2_name", "Команда 2")))
        chosen = picked.get(m_id)
        mark = f" — <b>{OUTCOME_TITLES.get(chosen['outcome'], chosen['outcome'])}</b> ✅" if chosen else ""
        lines.append(f"<b>{idx}.</b> ⚽ {t1} — {t2}{mark}")

        kb.append([
            InlineKeyboardButton(f"П1 {m['odd_p1']:.2f}", callback_data=f"bet_add_{m_id}_p1"),
            InlineKeyboardButton(f"Х {m['odd_x']:.2f}", callback_data=f"bet_add_{m_id}_x"),
            InlineKeyboardButton(f"П2 {m['odd_p2']:.2f}", callback_data=f"bet_add_{m_id}_p2"),
        ])
        extra_row = [InlineKeyboardButton("⚽ Тоталы / ОЗ", callback_data=f"bet_match_{m_id}")]
        if chosen:
            extra_row.append(InlineKeyboardButton(f"❌ Матч {idx}", callback_data=f"bet_del_{m_id}"))
        kb.append(extra_row)

    kb.append([
        InlineKeyboardButton(f"🎫 Купон ({len(slip)})", callback_data="bet_view_slip"),
        InlineKeyboardButton("🗑 Очистить", callback_data="bet_clear_slip"),
    ])
    kb.append([InlineKeyboardButton("🔙 Все Туры", callback_data="bet_view_tours")])

    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
    )


async def cb_bet_match_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display detailed odds for a specific match."""
    query = update.callback_query
    await query.answer()

    match_id = int(query.data.replace("bet_match_", ""))
    market = await asyncio.to_thread(database.get_bet_market_by_match_id, match_id)

    if not market:
        await query.edit_message_text(
            "❌ Данный матч не найден или линия закрыта.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="bet_view_tours")]])
        )
        return

    t1 = html.escape(market["team1_name"])
    t2 = html.escape(market["team2_name"])
    tour = market["tour"]

    text = (
        f"⚽ <b>{t1} vs {t2}</b> (Тур {tour})\n"
        f"<i>Коэффициенты от ИИ «Темшик»</i>\n\n"
        f"<b>🏆 Основные исходы:</b>\n"
        f"• <b>П1 ({t1}):</b> <code>{market['odd_p1']:.2f}</code>\n"
        f"• <b>Ничья (Х):</b> <code>{market['odd_x']:.2f}</code>\n"
        f"• <b>П2 ({t2}):</b> <code>{market['odd_p2']:.2f}</code>\n\n"
        f"<b>🎯 Тоталы и Обе Забьют:</b>\n"
        f"• <b>ТБ 2.5:</b> <code>{market['odd_tb25']:.2f}</code> | <b>ТМ 2.5:</b> <code>{market['odd_tm25']:.2f}</code>\n"
        f"• <b>Обе забьют (Да):</b> <code>{market['odd_btts_yes']:.2f}</code> | <b>(Нет):</b> <code>{market['odd_btts_no']:.2f}</code>\n\n"
        f"<i>Нажмите на исход, чтобы добавить его в купон:</i>"
    )

    kb = [
        [
            InlineKeyboardButton(f"П1 ({market['odd_p1']:.2f})", callback_data=f"bet_add_{match_id}_p1"),
            InlineKeyboardButton(f"Х ({market['odd_x']:.2f})", callback_data=f"bet_add_{match_id}_x"),
            InlineKeyboardButton(f"П2 ({market['odd_p2']:.2f})", callback_data=f"bet_add_{match_id}_p2")
        ],
        [
            InlineKeyboardButton(f"ТБ 2.5 ({market['odd_tb25']:.2f})", callback_data=f"bet_add_{match_id}_tb25"),
            InlineKeyboardButton(f"ТМ 2.5 ({market['odd_tm25']:.2f})", callback_data=f"bet_add_{match_id}_tm25")
        ],
        [
            InlineKeyboardButton(f"ОЗ: ДА ({market['odd_btts_yes']:.2f})", callback_data=f"bet_add_{match_id}_btts_yes"),
            InlineKeyboardButton(f"ОЗ: НЕТ ({market['odd_btts_no']:.2f})", callback_data=f"bet_add_{match_id}_btts_no")
        ],
        [
            InlineKeyboardButton("🎫 В Купон", callback_data="bet_view_slip"),
            InlineKeyboardButton("🔙 К списку матчей", callback_data="bet_view_tours")
        ]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def cb_bet_add_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add or replace an outcome in the user's bet coupon."""
    query = update.callback_query
    parts = query.data.split("_")
    match_id = int(parts[2])
    outcome = parts[3]
    if len(parts) > 4:
        outcome = f"{parts[3]}_{parts[4]}"

    market = await asyncio.to_thread(database.get_bet_market_by_match_id, match_id)
    if not market:
        await query.answer("❌ Линия на этот матч уже закрыта.", show_alert=True)
        return

    slip = _get_slip(context)

    # Экспресс — от 2 событий до потолка. Лишнее событие в купон не добавляется;
    # замена исхода в уже выбранном матче ограничением не является.
    max_events = database.get_max_express_events()
    already_picked = any(s["match_id"] == match_id for s in slip)
    if not already_picked and len(slip) >= max_events:
        await query.answer(
            f"⚠️ В экспрессе может быть максимум {max_events} событий!",
            show_alert=True
        )
        return

    # Remove existing pick for this match if any
    context.user_data["bet_slip"] = [s for s in slip if s["match_id"] != match_id]

    odd_val = market.get(f"odd_{outcome}", 1.85)
    context.user_data["bet_slip"].append({
        "match_id": match_id,
        "team1": market["team1_name"],
        "team2": market["team2_name"],
        "outcome": outcome,
        "odd": odd_val
    })

    out_name = OUTCOME_TITLES.get(outcome, outcome)
    await query.answer(f"✅ Добавлено: {out_name} (Кэф {odd_val:.2f})!", show_alert=False)
    
    # Refresh to coupon
    await cb_bet_view_slip(update, context)


def _open_bets_state(user_id: int) -> tuple[int, int]:
    """(открыто сейчас, потолок) — для счётчика слотов в купоне."""
    limits = BettingLimitsService.get_user_effective_limits(user_id)
    return database.get_user_open_bets_count(user_id), int(limits["max_open_bets"])


async def cb_bet_view_slip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View coupon with current selections and bet placement buttons."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    wallet = await asyncio.to_thread(database.get_or_create_wallet, user_id)
    bal = wallet.get("balance", 0)
    slip = _get_slip(context)

    if not slip:
        text = (
            f"🎫 <b>Ваш Купон Ставок</b>\n\n"
            f"<i>Купон пуст. Перейдите в линию и выберите исходы матчей!</i>\n\n"
            f"🪙 <b>Ваш баланс:</b> <code>{bal:,} 🪙</code>"
        )
        kb = [
            [InlineKeyboardButton("📋 Открыть Линию", callback_data="bet_view_tours")],
            [InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")]
        ]
        if query:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return

    # Calculate total odd
    total_odd = 1.0
    lines = ["🎫 <b>Ваш Купон Ставок:</b>\n"]
    for i, s in enumerate(slip, 1):
        t1 = html.escape(s['team1'])
        t2 = html.escape(s['team2'])
        out_title = OUTCOME_TITLES.get(s['outcome'], s['outcome'])
        odd_v = s['odd']
        total_odd *= odd_v
        lines.append(f"{i}. <b>{t1} vs {t2}</b>\n   👉 <code>{out_title}</code> • Кэф: <b>{odd_v:.2f}</b>")

    total_odd = round(total_odd, 2)
    bet_type = "Ординар" if len(slip) == 1 else f"Экспресс ({len(slip)} события)"
    
    lines.append(f"\n🏷️ <b>Тип:</b> {bet_type}")
    lines.append(f"🔥 <b>Итоговый Коэффициент:</b> <code>{total_odd:.2f}</code>")
    lines.append(f"🪙 <b>Ваш баланс:</b> <code>{bal:,} 🪙</code>")

    # Счётчик слотов виден всегда, как и в Mini App: уже открытые купоны
    # занимают слоты, и человек должен понимать, почему их нет, ещё до отказа.
    open_bets, max_open_bets = await asyncio.to_thread(_open_bets_state, user_id)
    slots_line = f"🎫 <b>Открытых купонов:</b> <code>{open_bets} из {max_open_bets}</code>"
    if open_bets >= max_open_bets:
        slots_line += "\n⚠️ <i>Свободных слотов нет — дождитесь расчёта.</i>"
    lines.append(slots_line)

    lines.append("\n<b>Выберите сумму ставки:</b>")

    text = "\n".join(lines)

    kb = [
        [
            InlineKeyboardButton("50 🪙", callback_data="bet_place_50"),
            InlineKeyboardButton("100 🪙", callback_data="bet_place_100"),
            InlineKeyboardButton("250 🪙", callback_data="bet_place_250")
        ],
        [
            InlineKeyboardButton("500 🪙", callback_data="bet_place_500"),
            InlineKeyboardButton("1 000 🪙", callback_data="bet_place_1000"),
            InlineKeyboardButton("🔥 ВСЁ (All-In)", callback_data=f"bet_place_{bal}")
        ],
        [
            InlineKeyboardButton("🗑 Очистить купон", callback_data="bet_clear_slip"),
            InlineKeyboardButton("➕ Добавить событие", callback_data="bet_view_tours")
        ],
        [InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")]
    ]

    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def cb_bet_place_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute bet placement with selected amount."""
    query = update.callback_query
    if not query:
        return

    # In-flight guard to prevent duplicate concurrent placement on rapid double tap
    if context.user_data.get("_bet_in_flight"):
        await query.answer()
        return

    amount = int(query.data.replace("bet_place_", ""))
    user_id = update.effective_user.id

    if amount <= 0:
        await query.answer("❌ Баланс пуст!", show_alert=True)
        return

    slip = context.user_data.get("bet_slip", [])
    if not slip:
        await query.answer("❌ Купон пуст!", show_alert=True)
        return

    # Один исход — ординар, от 2 до потолка — экспресс. Лишние события
    # не принимаются (дублирует серверную проверку place_user_bet).
    max_events = database.get_max_express_events()
    if len(slip) > max_events:
        await query.answer(
            f"⚠️ В экспрессе может быть максимум {max_events} событий!",
            show_alert=True
        )
        return

    # Atomically extract coupon and lock placement in-flight
    context.user_data["bet_slip"] = []
    context.user_data["_bet_in_flight"] = True

    try:
        success, res = await asyncio.to_thread(database.place_user_bet, user_id, amount, slip)
        if not success:
            # Restore coupon if placement could not be completed
            context.user_data["bet_slip"] = slip
            err_msg = str(res)
            if isinstance(res, dict):
                err_code = res.get("error", "")
                if err_code == "INSUFFICIENT_FUNDS":
                    err_msg = "Недостаточно монет на балансе!"
                elif err_code == "MAX_BET_EXCEEDED":
                    err_msg = f"Превышена максимальная сумма ставки ({res.get('max_bet', 50000):,} 🪙)!"
                elif err_code == "MAX_PAYOUT_EXCEEDED":
                    err_msg = f"Максимальный выигрыш с купона — {res.get('max_payout', DEFAULT_MAX_PAYOUT):,} 🪙."
                    if res.get("max_allowed_stake"):
                        err_msg += f" Макс. ставка при этом кэфе: {res['max_allowed_stake']:,} 🪙."
                else:
                    err_msg = res.get("message", err_code)
            await query.answer(f"❌ {err_msg}", show_alert=True)
            return

        await query.answer()
        wallet = await asyncio.to_thread(database.get_or_create_wallet, user_id)

        text = (
            f"✅ <b>Ставка #{res} успешно принята!</b>\n\n"
            f"💵 <b>Сумма ставки:</b> <code>{amount:,} 🪙</code>\n"
            f"🪙 <b>Остаток на балансе:</b> <code>{wallet['balance']:,} 🪙</code>\n\n"
            f"<i>Темшик следит за матчами. Как только игра завершится, выигрыш будет зачислен автоматически!</i>"
        )

        kb = [
            [InlineKeyboardButton("📜 Мои Ставки", callback_data="bet_my_history")],
            [InlineKeyboardButton("📋 В Линию", callback_data="bet_view_tours")],
            [InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")]
        ]

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    finally:
        context.user_data["_bet_in_flight"] = False


async def cb_bet_clear_slip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear coupon."""
    context.user_data["bet_slip"] = []
    if update.callback_query:
        await update.callback_query.answer("Купон очищен")
    await cb_bet_view_slip(update, context)


async def cb_bet_remove_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove one match from the coupon (кнопка «❌ Матч N» в линии тура)."""
    query = update.callback_query
    match_id = int(query.data.replace("bet_del_", ""))

    slip = _get_slip(context)
    context.user_data["bet_slip"] = [s for s in slip if s["match_id"] != match_id]
    await query.answer("❌ Событие убрано из купона")

    market = await asyncio.to_thread(database.get_bet_market_by_match_id, match_id)
    if not market:
        await cb_bet_view_slip(update, context)
        return

    tour_num = market["tour"]
    r_info = await asyncio.to_thread(database.get_round_info, tour_num)
    await _render_tour_matches(
        query, tour_num,
        r_info.get("deadline") if r_info else None,
        context=context,
        division_id=r_info.get("division_id") if r_info else None,
    )


# Статусы купона и отдельной ноги. 'refunded' — возврат: так рассчитываются
# ставки на матч с техническим результатом (ТП/ТН), это не проигрыш.
_BET_STATUS_TITLES = {
    "pending": "⏳ В игре",
    "won": "✅ Выигрыш",
    "lost": "❌ Проигрыш",
    "refunded": "🔄 Возврат",
    "cancelled": "🔄 Возврат",
}
_ITEM_STATUS_EMOJI = {"pending": "⏳", "won": "✅", "lost": "❌", "refunded": "🔄"}


async def _build_bet_history_text(user_id: int) -> str:
    """Render the user's recent coupons as HTML (общий текст для /mybets и кнопки)."""
    bets = await asyncio.to_thread(database.get_user_bets, user_id, limit=8)
    if not bets:
        return "📜 <b>История Ставок</b>\n\n<i>У вас пока нет активных или рассчитанных ставок.</i>"

    lines = ["📜 <b>Ваши Последние Ставки:</b>\n"]
    for b in bets:
        status_title = _BET_STATUS_TITLES.get(b["status"], b["status"])
        b_type = "Ординар" if b["bet_type"] == "single" else "Экспресс"
        total_odd = float(b["total_odd"] or 1.0)
        lines.append(
            f"• <b>Ставка #{b['id']}</b> ({b_type}) — {status_title}\n"
            f"  Сумма: <code>{b['amount']:,} 🪙</code> | Кэф: <b>{total_odd:.2f}</b> | Выигрыш: <b>{b['potential_win']:,} 🪙</b>"
        )
        for item in b.get("items", []):
            t1 = html.escape(item.get("team1_name") or "Команда 1")
            t2 = html.escape(item.get("team2_name") or "Команда 2")
            out_name = OUTCOME_TITLES.get(item["outcome_type"], item["outcome_type"])
            item_emoji = _ITEM_STATUS_EMOJI.get(item["status"], "•")
            # Возвращённая нога идёт в экспрессе по коэффициенту 1.00 и купон не рушит.
            odd_note = " • кэф 1.00" if item["status"] == "refunded" else ""
            lines.append(f"    {item_emoji} {t1} vs {t2} (<code>{out_name}</code>){odd_note}")
        lines.append("")

    return "\n".join(lines)


async def cb_bet_my_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of recent bets for user."""
    query = update.callback_query
    await query.answer()

    text = await _build_bet_history_text(update.effective_user.id)
    kb = [
        [InlineKeyboardButton("📋 Линия на Тур", callback_data="bet_view_tours")],
        [InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def cmd_my_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct command /mybets — мои ставки (ординары и экспрессы)."""
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    if not _check_betting_access(user_id):
        await update.message.reply_text(
            "🔒 <b>Logovo.bet временно недоступен</b>\n\n"
            "<i>История ставок станет доступна после открытия букмекерки. 🎰</i>",
            parse_mode="HTML"
        )
        return

    text = await _build_bet_history_text(user_id)
    kb = [
        [InlineKeyboardButton("📋 Линия на Тур", callback_data="bet_view_tours")],
        [InlineKeyboardButton("🎰 Букмекерская Контора", callback_data="bet_menu_main")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")



async def cb_bet_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display top bettors leaderboard."""
    query = update.callback_query
    await query.answer()

    top_bettors = await asyncio.to_thread(database.get_top_bettors, 10)

    lines = ["🏆 <b>Топ Капперов • Рейтинг Logovo.bet</b>\n"]
    if not top_bettors:
        lines.append("<i>Рейтинг пока пуст. Сделайте первую ставку!</i>")
    else:
        for i, b in enumerate(top_bettors, 1):
            name = b.get("username") or b.get("team_name") or f"Игрок {b['user_id']}"
            name = html.escape(str(name))
            bal = b.get("balance", 0)
            won_cnt = b.get("bets_won", 0)
            total_cnt = b.get("bets_count", 0)
            medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"{i}."))
            lines.append(f"{medal} <b>{name}</b> — <code>{bal:,} 🪙</code> (Побед: {won_cnt}/{total_cnt})")

    kb = [[InlineKeyboardButton("🔙 Главное Меню", callback_data="bet_menu_main")]]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


LEGACY_BET_CALLBACK_PATTERN = (
    r"^(betting_main_menu|bet_menu_main|bet_view_tours|bet_tour_\d+|"
    r"bet_match_\d+|bet_add_\d+_.+|bet_view_slip|bet_place_\d+|"
    r"bet_clear_slip|bet_del_\d+|bet_my_history|bet_claim_bonus|bet_leaderboard)$"
)

BET_MOVED_TEXT = (
    "🎰 <b>Logovo.bet переехал в приложение!</b>\n\n"
    "Все ставки, купоны и статистика теперь доступны только в Telegram Mini App. "
    "Нажмите кнопку ниже для перехода:"
)


def _get_bet_redirect_markup(context: ContextTypes.DEFAULT_TYPE = None, is_private: bool = True) -> InlineKeyboardMarkup:
    import config
    from telegram import WebAppInfo
    webapp_url = getattr(config, "WEBAPP_URL", "")
    kb = []
    if is_private and webapp_url and (webapp_url.startswith("https://") or "localhost" in webapp_url):
        kb.append([InlineKeyboardButton("🎰 Открыть Logovo.bet", web_app=WebAppInfo(url=webapp_url))])
    elif webapp_url and webapp_url.startswith("http"):
        kb.append([InlineKeyboardButton("🎰 Открыть Logovo.bet", url=webapp_url)])
    else:
        bot_user = context.bot.username if context and context.bot else ""
        if bot_user:
            kb.append([InlineKeyboardButton("🎰 Открыть Logovo.bet", url=f"https://t.me/{bot_user}?start=miniapp")])
        else:
            kb.append([InlineKeyboardButton("🎰 Открыть Logovo.bet", url="https://t.me")])
    return InlineKeyboardMarkup(kb)


async def cmd_bet_moved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Redirect deprecated betting commands to Mini App."""
    if not update.effective_message:
        return
    is_private = bool(update.effective_chat and update.effective_chat.type == "private")
    markup = _get_bet_redirect_markup(context, is_private=is_private)
    await update.effective_message.reply_text(BET_MOVED_TEXT, reply_markup=markup, parse_mode="HTML")


async def cb_bet_moved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Redirect legacy inline betting buttons to Mini App."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    is_private = bool(update.effective_chat and update.effective_chat.type == "private")
    markup = _get_bet_redirect_markup(context, is_private=is_private)
    try:
        await query.edit_message_text(BET_MOVED_TEXT, reply_markup=markup, parse_mode="HTML")
    except Exception:
        try:
            if update.effective_chat:
                await update.effective_chat.send_message(BET_MOVED_TEXT, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass


def register_betting_handlers(app) -> None:
    """Register Logovo.bet handlers.
    In-chat betting UI is deprecated in favor of Telegram Mini App.
    Legacy commands and callback queries redirect the user to the Mini App.
    """
    # Deprecated in-chat commands -> redirect to Mini App
    app.add_handler(CommandHandler(["bet", "logovobet", "mybets", "bet_top", "top_bettors", "bonus"], cmd_bet_moved))

    # Deprecated callback buttons from previously sent messages -> redirect to Mini App
    app.add_handler(CallbackQueryHandler(cb_bet_moved, pattern=LEGACY_BET_CALLBACK_PATTERN))

