"""
handlers/cup_management.py

Управление общим кубком: тема вещания, подготовка этапа, публикация линии и
результатов.

Права — только глобальный админ. `is_admin` пропускает любого дивизионного
админа (это ловушка №5 из AGENTS.md), а этап кубка один на весь сезон и правится
из любой точки: делить его по дивизионам нельзя — в 1/64 играют Д4 и Д5
одновременно, и «свой дивизион» тут не защищает ничего.

Порядок действий админа совпадает с порядком кнопок: сетку заводит скрипт,
панель заводит игры и заголовки серий (`provision`), открывает приём прогнозов,
потом — публикует линию в кубковую тему, и только затем стартует этап. Старт
этапа закрывает линию тем же переходом, что и тур лиги, и вернуть её нельзя.
"""

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

import database
from constants import CUP_TOPIC_TYPES
from handlers.base import is_global_admin

logger = logging.getLogger(__name__)

_ACTIONS = {
    "provision": "🧩 завести игры",
    "open": "🎟 открыть ставки",
    "post_line": "📣 линию в тему",
    "post_results": "🏆 результаты в тему",
    "start": "▶ начать этап",
    "refresh": "↻ обновить",
}


def _denied(user) -> bool:
    """Права — по тому, КТО прислал апдейт (`update.effective_user`).

    У нажатия кнопки `query.message.from_user` — это сам бот, автор панели:
    проверка по нему отказывала бы любому админу.
    """
    return not (user and is_global_admin(user.id))


async def cmd_cup_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cup_topic [line|reports] — привязать ТЕКУЩУЮ тему форума к кубковому вещанию.

    Команда вызывается внутри темы: `message_thread_id` берётся из сообщения, под
    которым её написали — тот же приём, что у `cmd_assign_topic` в дивизионах.
    """
    message = update.effective_message
    if message is None:
        return
    if _denied(update.effective_user):
        await message.reply_text("⛔ Команда доступна только глобальному админу.")
        return

    args = list(context.args or [])
    topic_type = (args[0] if args else "line").strip().lower()
    if topic_type not in CUP_TOPIC_TYPES:
        await message.reply_text(
            "Формат кубковой темы: " + ", ".join(f"<code>{t}</code>" for t in CUP_TOPIC_TYPES),
            parse_mode="HTML",
        )
        return

    thread_id = getattr(message, "message_thread_id", None)
    chat_id = getattr(message, "chat_id", None)
    if not thread_id or not chat_id:
        await message.reply_text(
            "Команду нужно писать ВНУТРИ темы форума: бот берёт идентификатор темы из "
            "сообщения, под которым её вызвали."
        )
        return

    result = await asyncio.to_thread(database.bind_cup_topic, topic_type, chat_id, int(thread_id))
    status = result.get("status")
    if status in ("bound", "already_bound"):
        await message.reply_text(
            f"✅ Тема <code>{chat_id}</code>/<code>{thread_id}</code> — "
            f"кубковый формат <b>{topic_type}</b>.",
            parse_mode="HTML",
        )
    elif status == "conflict_topic":
        await message.reply_text(
            f"⚠️ Эта тема уже назначена формату <b>{result.get('occupied_by')}</b>. "
            "Одна тема — один формат.", parse_mode="HTML"
        )
    else:
        await message.reply_text(f"❌ {result.get('error')}")


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
        rows.append([
            InlineKeyboardButton(_ACTIONS["post_line"], callback_data=f"cup_post_line_{stage_id}"),
            InlineKeyboardButton(_ACTIONS["start"], callback_data=f"cup_start_{stage_id}"),
        ])
    rows.append([
        InlineKeyboardButton(_ACTIONS["post_results"], callback_data=f"cup_post_results_{stage_id}"),
        InlineKeyboardButton(_ACTIONS["refresh"], callback_data="cup_refresh"),
    ])
    return rows


async def _render_panel(target, context, stage_id: int | None = None, note: str = "") -> None:
    stages = await asyncio.to_thread(database.list_cup_stages)
    topics = await asyncio.to_thread(database.list_cup_topics)
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
    if not topics:
        lines.append("")
        lines.append("⚠️ Кубковая тема не назначена: напиши <code>/cup_topic</code> внутри темы.")
    else:
        bound = ", ".join(f"{t['topic_type']}→<code>{t['message_thread_id']}</code>" for t in topics)
        lines.append("")
        lines.append(f"Темы вещания: {bound}")
    keyboard = [[InlineKeyboardButton(s["stage"], callback_data=f"cup_stage_{s['id']}")]
                for s in stages]
    if stage_id:
        selected = next((s for s in stages if s["id"] == stage_id), None)
        if selected:
            bracket = await asyncio.to_thread(
                database.get_cup_bracket, selected["stage"], season_id=selected.get("season_id")
            )
            decided = any(s["winner_name"] for s in bracket)
            keyboard.insert(0, _stage_keyboard(stage_id, decided=decided))
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

    # Номер этапа — последний сегмент: у `post_line` / `post_results` в самом
    # действии есть «_», и split по первому разделителю терял их целиком.
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
    elif action in ("post_line", "post_results"):
        note = await _publish(stage, context.bot, line=action == "post_line")
    else:
        return

    await _render_panel(query.message, context, stage_id=stage_id, note=note)


async def _generate_stage_markets(stage: str, season_id) -> int:
    from services.betting_engine import generate_stage_markets

    rows = await asyncio.to_thread(generate_stage_markets, stage, season_id=season_id)
    return len(rows)


async def _publish(stage: dict, bot, line: bool) -> str:
    topic_type = "line" if line else "reports"
    topic = await asyncio.to_thread(database.get_cup_topic, topic_type)
    if not topic:
        return f"Тема «{topic_type}» не назначена — вызови /cup_topic внутри темы."

    chunks = _format_stage_messages(stage, line=line)
    if not chunks:
        return "Публиковать нечего: сетка этапа пуста."
    for chunk in chunks:
        await bot.send_message(
            chat_id=topic["group_chat_id"],
            message_thread_id=topic["message_thread_id"],
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    return f"Опубликовано в теме «{topic_type}»: {len(chunks)} сообщ."


def _format_stage_messages(stage: dict, line: bool) -> list[str]:
    """Готовые к отправке куски текста этапа (линия или итоги серий)."""
    stage_name = stage["stage"]
    if line:
        data = database.get_cup_stage_line(stage_name)
        messages = [f"🏆 <b>ОБЩИЙ КУБОК · {html.escape(stage_name)}</b> · приём прогнозов открыт"]
        block: list[str] = []
        for series in data["series"]:
            head = series["header"] or {}
            head_odds = head.get("odds") or {}
            block.append(
                f"\n<b>{html.escape(series['team1_name'])} — {html.escape(series['team2_name'])}</b>"
            )
            if head_odds:
                block.append(
                    f"проход: <b>{head_odds.get('p1', '—')}</b> / <b>{head_odds.get('p2', '—')}</b>"
                    f" · третья игра: <b>{head_odds.get('tb25', '—')}</b>"
                )
            for game in series["games"]:
                odds = game.get("odds") or {}
                if not odds:
                    continue
                block.append(
                    f"  игра {game['game_num_in_series']}: П1 <b>{odds.get('p1', '—')}</b> · "
                    f"П2 <b>{odds.get('p2', '—')}</b> · ТБ2.5 <b>{odds.get('tb25', '—')}</b> · "
                    f"ОЗ да <b>{odds.get('btts_yes', '—')}</b>"
                )
            if sum(len(x) for x in block) > 3200:
                messages.append("\n".join(block))
                block = []
        if block:
            messages.append("\n".join(block))
        return messages

    bracket = database.get_cup_bracket(stage_name)
    if not bracket:
        return []
    messages = [f"🏆 <b>ОБЩИЙ КУБОК · {html.escape(stage_name)}</b> · результаты"]
    block = []
    for series in bracket:
        score = f"{series['team1_wins']}:{series['team2_wins']}"
        winner = series["winner_name"]
        mark = f"✅ <b>{html.escape(winner)}</b>" if winner else "⏳ играется"
        block.append(
            f"\n{html.escape(series['team1_name'])} — {html.escape(series['team2_name'])} "
            f"<b>{score}</b> · {mark}"
        )
        if sum(len(x) for x in block) > 3200:
            messages.append("\n".join(block))
            block = []
    if block:
        messages.append("\n".join(block))
    return messages


def register_cup_handlers(app) -> None:
    """Регистрация идёт до catch-all группы 0: иначе нажатия утонут в AI-чате."""
    # Только латиница: PTB отвергает кириллические команды ValueError-ом, и
    # register_all_handlers падал бы вместе со всем ботом.
    app.add_handler(CommandHandler("cup", cmd_cup))
    app.add_handler(CommandHandler("cup_topic", cmd_cup_topic))
    app.add_handler(CallbackQueryHandler(
        cb_cup,
        pattern="^cup_(refresh|stage_\\d+|provision_\\d+|open_\\d+|start_\\d+|post_line_\\d+|post_results_\\d+)$",
    ))
