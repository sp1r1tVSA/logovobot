"""
handlers/cup_management.py

Управление общим кубком: подготовка этапа, приём прогнозов, старт.

Права — только глобальный админ. `is_admin` пропускает любого дивизионного
админа (это ловушка №5 из AGENTS.md), а этап кубка один на весь сезон и правится
из любой точки: делить его по дивизионам нельзя — в 1/64 играют Д4 и Д5
одновременно, и «свой дивизион» тут не защищает ничего.

Порядок действий админа совпадает с порядком кнопок: сетку заводит скрипт,
панель заводит игры и заголовки серий (`provision`), открывает приём прогнозов
(линия видна в Mini App), и только затем стартует этап. Старт этапа закрывает
линию тем же переходом, что и тур лиги, и вернуть её нельзя.

Результаты кубка бот в группу не публикует: участники сами сдают их боту в ЛС
и сами выкладывают под постом кубка.
"""

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import database
from handlers.base import is_global_admin

logger = logging.getLogger(__name__)

_ACTIONS = {
    "provision": "🧩 завести игры",
    "open": "🎟 открыть ставки",
    "start": "▶ начать этап",
    "refresh": "↻ обновить",
}


def _denied(user) -> bool:
    """Права — по тому, КТО прислал апдейт (`update.effective_user`).

    У нажатия кнопки `query.message.from_user` — это сам бот, автор панели:
    проверка по нему отказывала бы любому админу.
    """
    return not (user and is_global_admin(user.id))


def _stage_state_label(stage: dict) -> str:
    if stage.get("is_open"):
        return "▶ играют"
    if stage.get("bets_open"):
        return "🎟 ставки открыты"
    return "🔒 линия закрыта"


def _stage_keyboard(stage_id: int, decided: bool) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if not decided:
        rows.append([
            InlineKeyboardButton(_ACTIONS["provision"], callback_data=f"cup_provision_{stage_id}"),
            InlineKeyboardButton(_ACTIONS["open"], callback_data=f"cup_open_{stage_id}"),
        ])
        rows.append([InlineKeyboardButton(_ACTIONS["start"], callback_data=f"cup_start_{stage_id}")])
    rows.append([InlineKeyboardButton(_ACTIONS["refresh"], callback_data="cup_refresh")])
    return rows


async def _render_panel(target, context, stage_id: int | None = None, note: str = "") -> None:
    stages = await asyncio.to_thread(database.list_cup_stages)
    lines = ["🏆 <b>Общий кубок</b>", ""]
    if not stages:
        lines.append("Этапов ещё нет — заведи сетку: <code>python scripts/seed_cup_bracket.py --apply</code>")
    for stage in stages:
        sid = stage["id"]
        bracket = await asyncio.to_thread(database.get_cup_bracket, stage["stage"])
        decided = sum(1 for s in bracket if s["winner_name"])
        games = await asyncio.to_thread(database.count_cup_stage_matches, sid)
        lines.append(
            f"<b>{html.escape(stage['stage'])}</b> — {_stage_state_label(stage)}; "
            f"серий: {len(bracket)}, решено: {decided}, строк матчей: {games}"
        )
        if sid == stage_id and note:
            lines.append(f"↳ {html.escape(note)}")
    keyboard = [[InlineKeyboardButton(s["stage"], callback_data=f"cup_stage_{s['id']}")]
                for s in stages]
    if stage_id:
        selected = next((s for s in stages if s["id"] == stage_id), None)
        if selected:
            bracket = await asyncio.to_thread(
                database.get_cup_bracket, selected["stage"], season_id=selected.get("season_id")
            )
            decided = any(s["winner_name"] for s in bracket)
            keyboard[0:0] = _stage_keyboard(stage_id, decided=decided)
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    text = "\n".join(lines)
    try:
        await target.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await target.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_cup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cup — панель управления общим кубком (глобальный админ)."""
    message = update.effective_message
    if message is None:
        return
    if _denied(update.effective_user):
        await message.reply_text("⛔ Команда доступна только глобальному админу.")
        return
    await _render_panel(message, context)


async def cb_cup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    # На callback отвечают ровно один раз: второй `answer` Telegram отклоняет.
    if _denied(query.from_user):
        await query.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    await query.answer()

    if query.data == "cup_refresh":
        await _render_panel(query.message, context, stage_id=context.user_data.get("cup_stage_id"))
        return

    # Номер этапа — последний сегмент: действие само может содержать «_».
    head, _, tail = query.data.rpartition("_")
    action = head.removeprefix("cup_")
    try:
        stage_id = int(tail)
    except ValueError:
        return
    context.user_data["cup_stage_id"] = stage_id
    stage = await asyncio.to_thread(database.get_cup_stage_by_id, stage_id)
    if not stage:
        await _render_panel(query.message, context, note="Этап не найден.")
        return

    note = ""
    if action == "stage":
        await _render_panel(query.message, context, stage_id=stage_id)
        return

    if action == "provision":
        report = await asyncio.to_thread(database.provision_cup_stage_line, stage["stage"])
        note = (f"Игр заведено: {report['created_games']}, заголовков: {report['created_headers']} "
                f"(всего серий {report['series']})")
    elif action == "open":
        ok, message = await asyncio.to_thread(database.open_cup_stage_bets, stage_id, query.from_user.id)
        note = message
        if ok:
            priced = await _generate_stage_markets(stage["stage"], stage.get("season_id"))
            note = f"{message} Выставлено объектов линии: {priced}."
    elif action == "start":
        ok, message = await asyncio.to_thread(database.start_cup_stage, stage_id, query.from_user.id)
        note = message
    else:
        return

    await _render_panel(query.message, context, stage_id=stage_id, note=note)


async def _generate_stage_markets(stage: str, season_id) -> int:
    from services.betting_engine import generate_stage_markets

    rows = await asyncio.to_thread(generate_stage_markets, stage, season_id=season_id)
    return len(rows)


def register_cup_handlers(app) -> None:
    """Регистрация идёт до catch-all группы 0: иначе нажатия утонут в AI-чате."""
    # Только латиница: PTB отвергает кириллические команды ValueError-ом, и
    # register_all_handlers падал бы вместе со всем ботом.
    app.add_handler(CommandHandler("cup", cmd_cup))
    app.add_handler(CallbackQueryHandler(
        cb_cup,
        pattern="^cup_(refresh|stage_\\d+|provision_\\d+|open_\\d+|start_\\d+)$",
    ))
