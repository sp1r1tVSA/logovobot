"""
handlers/league_overview.py

`/overview` (он же `/obzor`, `/svodka`) — сводка по всем дивизионам сразу: какие
туры открыты и насколько сыграны, сколько долгов и кто близок к лимиту варнов.
Одно сообщение на все дивизионы, под ним кнопка на каждый дивизион с деталями
и «Обновить». Только по команде — фонового поста нет.

Права: глобальный админ видит все активные дивизионы, дивизионный — только
свои (`division_admins`). Проверяются заново на каждое нажатие: кнопку может
нажать не тот, кто вызвал команду, а права могли измениться с тех пор.

Считать здесь нечего — данные грузит `database`, собирает и форматирует
`services.league_overview`.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import config
import database
from handlers.base import is_global_admin
from services import league_overview
from time_utils import now_msk

logger = logging.getLogger(__name__)


def _visible_divisions(user_id: int) -> list[dict]:
    """Дивизионы, которые видит пользователь; пусто — доступа нет."""
    if is_global_admin(user_id):
        return database.get_active_divisions()
    return [
        {"id": d["id"], "name": d["name"], "code": d.get("code")}
        for d in database.get_admin_divisions(user_id)
        if d.get("is_active", 1)
    ]


def can_view_overview(user_id: int) -> bool:
    """Есть ли у пользователя хоть один дивизион в сводке — то же правило, что `_visible_divisions`."""
    return bool(is_global_admin(user_id) or database.get_admin_divisions(user_id))


def _load_snapshots(divisions: list[dict]) -> list[league_overview.DivisionSnapshot]:
    rows = database.get_league_overview_rows()
    debts = database.get_detailed_overdue_matches(season_id=rows["season_id"])
    return league_overview.build_snapshots(divisions, rows, debts, now_msk(), config.MAX_WARNS_LIMIT)


def _summary_keyboard(snapshots) -> InlineKeyboardMarkup:
    buttons = []
    for snap in snapshots:
        mark = " ⚠️" if snap.escalated or snap.at_limit or snap.overdue_rounds else ""
        buttons.append(InlineKeyboardButton(f"{snap.name}{mark}", callback_data=f"ovw_div:{snap.id}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([
        InlineKeyboardButton("🔄 Обновить", callback_data="ovw_home"),
        InlineKeyboardButton("🛡 Админка", callback_data="admin_main_menu"),
    ])
    return InlineKeyboardMarkup(rows)


def _division_keyboard(div_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📉 Долги дивизиона", callback_data=f"admin_div_overdue:{div_id}"),
            InlineKeyboardButton("🛡 Панель дивизиона", callback_data=f"admin_div_panel:{div_id}"),
        ],
        [
            InlineKeyboardButton("🔄 Обновить", callback_data=f"ovw_div:{div_id}"),
            InlineKeyboardButton("« К сводке", callback_data="ovw_home"),
        ],
    ])


async def _render_summary(user_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    divisions = await asyncio.to_thread(_visible_divisions, user_id)
    if not divisions:
        return None
    snapshots = await asyncio.to_thread(_load_snapshots, divisions)
    text = league_overview.render_summary(snapshots, now_msk(), config.MAX_WARNS_LIMIT)
    return text, _summary_keyboard(snapshots)


async def _edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup,
                                      disable_web_page_preview=True)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def overview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    rendered = await _render_summary(user.id)
    if rendered is None:
        await message.reply_text("⛔ Сводка доступна только админам лиги и дивизионов.")
        return
    text, markup = rendered
    await message.reply_text(text, parse_mode="HTML", reply_markup=markup,
                             disable_web_page_preview=True)


async def overview_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    rendered = await _render_summary(user.id)
    if rendered is None:
        await query.answer("⛔ Нет доступа", show_alert=True)
        return
    await query.answer("Обновлено")
    await _edit(query, *rendered)


async def overview_division_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    try:
        div_id = int(query.data.split(":", 1)[1])
    except (AttributeError, IndexError, ValueError):
        await query.answer()
        return

    divisions = await asyncio.to_thread(_visible_divisions, user.id)
    division = next((d for d in divisions if int(d["id"]) == div_id), None)
    if division is None:
        await query.answer("⛔ Нет доступа к этому дивизиону", show_alert=True)
        return
    await query.answer()

    snapshots = await asyncio.to_thread(_load_snapshots, [division])
    text = league_overview.render_division(snapshots[0], now_msk(), config.MAX_WARNS_LIMIT)
    await _edit(query, text, _division_keyboard(div_id))


def register_league_overview_handlers(app) -> None:
    app.add_handler(CommandHandler(["overview", "obzor", "svodka"], overview_command))
    app.add_handler(CallbackQueryHandler(overview_home_callback, pattern=r"^ovw_home$"))
    app.add_handler(CallbackQueryHandler(overview_division_callback, pattern=r"^ovw_div:\d+$"))
