"""
handlers/cup_management.py

Управление кубками: общим и пятью кубками дивизионов — подготовка этапа, приём
прогнозов, старт.

Права: общий кубок — только глобальный админ. `is_admin` пропускает любого
дивизионного админа (это ловушка №5 из AGENTS.md), а общий кубок один на весь
сезон: в 1/64 играют Д4 и Д5 одновременно, и «свой дивизион» тут не защищает
ничего. Кубок дивизиона — глобальный админ или админ этого дивизиона
(`is_division_admin`); чужих кубков дивизионный админ не видит и не правит.
Права проверяются на каждом нажатии по кубку того этапа/серии, который правят, —
не по выбранному в переключателе.

Порядок действий админа совпадает с порядком кнопок: сетку заводит скрипт
(`scripts/seed_cup_bracket.py [--division N]`), панель заводит игры и заголовки
серий (`provision`), открывает приём прогнозов (линия видна в Mini App), и только
затем стартует этап. Старт этапа закрывает линию тем же переходом, что и тур
лиги, и вернуть её нельзя.

«⚔️ серии и матчи» — то же, что «Управление матчами» дивизиона: серии этапа →
игры серии → админская карточка матча (ввод счёта, сброс, скриншот).

Если у кубка привязана тема «Кубок» (`/set_div_topic <дивизион|общий> cup`), бот
ведёт её сам (`services.cup_broadcast`): закреп с сеткой, результаты игр,
объявления об открытии приёма прогнозов и старте этапа. «🖼 сетка в тему»
выкладывает и закрепляет сетку заново.
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
    "series": "⚔️ серии и матчи",
    "bracket": "🖼 сетка в тему",
    "refresh": "↻ обновить",
}

_DENIED = "⛔ Недостаточно прав."


def _manageable_scopes(user_id: int | None) -> list[int | None]:
    """Кубки, которыми правит пользователь: None — общий, число — дивизион.

    `is_global_admin` зовётся ровно один раз: права — по тому, КТО прислал апдейт
    (`update.effective_user`), а у нажатия кнопки `query.message.from_user` — это
    сам бот, автор панели: проверка по нему отказывала бы любому админу.
    """
    if not user_id:
        return []
    if is_global_admin(user_id):
        return [None] + [int(d["id"]) for d in database.get_divisions(is_active=True)]
    return [int(d["id"]) for d in database.get_admin_divisions(user_id)]


def _scope_code(division_id) -> int:
    """Кубок в callback_data: 0 — общий."""
    return database.cup_scope(division_id) or 0


def _current_scope(context, scopes: list[int | None]):
    scope = context.user_data.get("cup_scope")
    return scope if scope in scopes else scopes[0]


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
    rows.append([InlineKeyboardButton(_ACTIONS["series"], callback_data=f"cup_series_{stage_id}")])
    rows.append([InlineKeyboardButton(_ACTIONS["refresh"], callback_data="cup_refresh")])
    return rows


def _scope_switcher(scopes: list[int | None], current) -> list[list[InlineKeyboardButton]]:
    """Ряд «Общий · Д1 … Д5»; у админа одного дивизиона переключателя нет."""
    if len(scopes) < 2:
        return []
    buttons = []
    for scope in scopes:
        label = database.cup_scope_short(scope)
        if scope == current:
            label = f"• {label}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"cup_scope_{_scope_code(scope)}"))
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


def _series_icon(series: dict, disputed: bool) -> str:
    if series.get("winner_name"):
        return "✅"
    if disputed:
        return "⚠️"
    if (series.get("team1_wins") or 0) + (series.get("team2_wins") or 0):
        return "🟢"
    return "⚪"


def _game_label(game: dict) -> str:
    """«Игра 2: 1:1, пен. → Бавария» — как статус матча в списке тура у админа."""
    num = game.get("game_num_in_series") or "?"
    status = game.get("status")
    if status == "confirmed":
        s1, s2 = game.get("player1_score"), game.get("player2_score")
        label = f"{s1}:{s2}"
        if s1 == s2 and game.get("cup_winner_team"):
            label += f", пен. → {game['cup_winner_team']}"
    elif status == "disputed":
        label = "⚠️ спор"
    elif status == "cancelled":
        label = "снята"
    else:
        label = "⚔️"
    return f"Игра {num}: {label}"


async def _render_series_list(target, stage: dict) -> None:
    """Серии этапа — как сетка туров в «Управлении матчами» дивизиона."""
    stage_id = stage["id"]
    bracket = await asyncio.to_thread(
        database.get_cup_bracket, stage["stage"], season_id=stage.get("season_id"),
        division_id=stage.get("division_id"),
    )
    games = await asyncio.to_thread(database.get_cup_stage_games, stage_id)
    disputed = {g["series_id"] for g in games if g["status"] == "disputed"}

    keyboard = []
    for s in bracket:
        icon = _series_icon(s, s["id"] in disputed)
        label = (f"{icon} {s['series_num']}. {s['team1_name']} "
                 f"{s['team1_wins'] or 0}:{s['team2_wins'] or 0} {s['team2_name']}")
        keyboard.append([InlineKeyboardButton(label, callback_data=f"cup_ser_{s['id']}")])
    keyboard.append([InlineKeyboardButton("« К этапу", callback_data=f"cup_stage_{stage_id}")])

    stage_title = database.cup_stage_title(database.cup_stage_key(stage["stage"], stage.get("division_id")))
    title = f"⚔️ <b>Серии этапа {html.escape(stage_title)}</b>\n\n"
    if bracket:
        text = title + "⚪ не начата · 🟢 идёт · ⚠️ спор · ✅ решена\n\nВыберите серию:"
    else:
        text = title + "Серий нет — сетку заводит скрипт."
    await _edit_or_reply(target, text, InlineKeyboardMarkup(keyboard))


async def _render_series_card(target, series: dict) -> None:
    """Карточка серии: счёт и игры; игра открывает админскую карточку матча."""
    series_id = series["id"]
    games = await asyncio.to_thread(database.get_cup_series_games, series_id)

    stage_title = database.cup_stage_title(database.cup_stage_key(series["stage"], series.get("division_id")))
    t1, t2 = html.escape(series["team1_name"]), html.escape(series["team2_name"])
    lines = [
        f"🏆 <b>{html.escape(stage_title)} · серия {series['series_num']}</b>",
        "",
        f"<b>{t1}</b> — <b>{t2}</b>",
        f"Счёт серии: <code>{series['team1_wins'] or 0} : {series['team2_wins'] or 0}</code>",
    ]
    if series.get("winner_name"):
        lines.append(f"Прошёл дальше: ✅ <b>{html.escape(series['winner_name'])}</b>")
    lines.append("")
    lines.append("Выберите игру для ввода счёта или сброса:" if games
                 else "Игры не заведены — нажми «🧩 завести игры» на этапе.")

    keyboard = [[InlineKeyboardButton(_game_label(g), callback_data=f"admin_view_match_{g['match_id']}")]
                for g in games]
    if series.get("stage_id"):
        keyboard.append([InlineKeyboardButton("« К сериям", callback_data=f"cup_series_{series['stage_id']}")])
    await _edit_or_reply(target, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def _edit_or_reply(target, text: str, markup) -> None:
    try:
        await target.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await target.reply_text(text, parse_mode="HTML", reply_markup=markup)


def _seed_hint(scope) -> str:
    flag = "" if scope is None else f" --division {scope}"
    return f"<code>python scripts/seed_cup_bracket.py{flag} --apply</code>"


async def _render_panel(target, context, stage_id: int | None = None, note: str = "",
                        scopes: list[int | None] | None = None) -> None:
    scopes = scopes or [None]
    scope = _current_scope(context, scopes)
    stages = await asyncio.to_thread(database.list_cup_stages, division_id=scope)
    lines = [f"🏆 <b>{html.escape(database.cup_scope_label(scope))}</b>", ""]
    if not stages:
        lines.append(f"Этапов ещё нет — заведи сетку: {_seed_hint(scope)}")
    for stage in stages:
        sid = stage["id"]
        bracket = await asyncio.to_thread(
            database.get_cup_bracket, stage["stage"], season_id=stage.get("season_id"), division_id=scope,
        )
        decided = sum(1 for s in bracket if s["winner_name"])
        games = await asyncio.to_thread(database.count_cup_stage_matches, sid)
        lines.append(
            f"<b>{html.escape(stage['stage'])}</b> — {_stage_state_label(stage)}; "
            f"серий: {len(bracket)}, решено: {decided}, строк матчей: {games}"
        )
        if sid == stage_id and note:
            lines.append(f"↳ {html.escape(note)}")
    if note and stage_id not in {s["id"] for s in stages}:
        lines += ["", f"↳ {html.escape(note)}"]
    topic = await asyncio.to_thread(database.get_cup_topic, scope)
    lines.append("")
    lines.append("📌 Тема кубка привязана — сетка и результаты идут туда." if topic
                 else f"📌 Тема кубка не привязана: <code>/set_div_topic {_scope_code(scope) or 'общий'} cup</code> в нужной теме.")

    keyboard = [[InlineKeyboardButton(s["stage"], callback_data=f"cup_stage_{s['id']}")]
                for s in stages]
    if stage_id:
        selected = next((s for s in stages if s["id"] == stage_id), None)
        if selected:
            bracket = await asyncio.to_thread(
                database.get_cup_bracket, selected["stage"], season_id=selected.get("season_id"),
                division_id=scope,
            )
            decided = any(s["winner_name"] for s in bracket)
            keyboard[0:0] = _stage_keyboard(stage_id, decided=decided)
    else:
        keyboard.append([InlineKeyboardButton(_ACTIONS["refresh"], callback_data="cup_refresh")])
    if topic:
        keyboard.append([InlineKeyboardButton(_ACTIONS["bracket"], callback_data=f"cup_bracket_{_scope_code(scope)}")])
    keyboard += _scope_switcher(scopes, scope)
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await _edit_or_reply(target, "\n".join(lines), markup)


async def cmd_cup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cup — панель кубков (глобальный админ — все, админ дивизиона — кубок своего)."""
    message = update.effective_message
    if message is None:
        return
    user = update.effective_user
    scopes = await asyncio.to_thread(_manageable_scopes, user.id if user else None)
    if not scopes:
        await message.reply_text("⛔ Команда доступна глобальному админу и админам дивизионов.")
        return
    await _render_panel(message, context, scopes=scopes)


def _parse_tail(data: str, prefix: str) -> int | None:
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


async def cb_cup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    user_id = query.from_user.id if query.from_user else None
    scopes = await asyncio.to_thread(_manageable_scopes, user_id)
    # На callback отвечают ровно один раз: второй `answer` Telegram отклоняет.
    # Поэтому права проверяются до ответа — по кубку того объекта, что правят.
    if not scopes:
        await query.answer(_DENIED, show_alert=True)
        return

    if data == "cup_refresh":
        await query.answer()
        await _render_panel(query.message, context, stage_id=context.user_data.get("cup_stage_id"), scopes=scopes)
        return

    if data.startswith(("cup_scope_", "cup_bracket_")):
        is_scope = data.startswith("cup_scope_")
        code = _parse_tail(data, "cup_scope_" if is_scope else "cup_bracket_")
        scope = database.cup_scope(code) if code is not None else None
        if code is None or scope not in scopes:
            await query.answer(_DENIED, show_alert=True)
            return
        await query.answer()
        context.user_data["cup_scope"] = scope
        if is_scope:
            context.user_data.pop("cup_stage_id", None)
            await _render_panel(query.message, context, scopes=scopes)
            return
        from services.cup_broadcast import refresh_cup_bracket
        ok = await refresh_cup_bracket(context.bot, scope, republish=True)
        note = ("Сетка выложена в тему и закреплена." if ok
                else "Сетку выложить не удалось — проверь привязку темы и права бота.")
        await _render_panel(query.message, context, stage_id=context.user_data.get("cup_stage_id"),
                            note=note, scopes=scopes)
        return

    # Карточка серии ключуется номером серии, а не этапа.
    if data.startswith("cup_ser_"):
        series_id = _parse_tail(data, "cup_ser_")
        series = await asyncio.to_thread(database.get_cup_series, series_id) if series_id else None
        if series and database.cup_scope(series.get("division_id")) not in scopes:
            await query.answer(_DENIED, show_alert=True)
            return
        await query.answer()
        if not series:
            await _edit_or_reply(query, "❌ Серия не найдена.", InlineKeyboardMarkup(
                [[InlineKeyboardButton(_ACTIONS["refresh"], callback_data="cup_refresh")]]))
            return
        await _render_series_card(query, series)
        return

    # Номер этапа — последний сегмент: действие само может содержать «_».
    head, _, tail = data.rpartition("_")
    action = head.removeprefix("cup_")
    try:
        stage_id = int(tail)
    except ValueError:
        await query.answer()
        return
    stage = await asyncio.to_thread(database.get_cup_stage_by_id, stage_id)
    if stage and database.cup_scope(stage.get("division_id")) not in scopes:
        await query.answer(_DENIED, show_alert=True)
        return
    await query.answer()
    if not stage:
        await _render_panel(query.message, context, note="Этап не найден.", scopes=scopes)
        return
    scope = database.cup_scope(stage.get("division_id"))
    context.user_data["cup_stage_id"] = stage_id
    context.user_data["cup_scope"] = scope

    note = ""
    if action == "stage":
        await _render_panel(query.message, context, stage_id=stage_id, scopes=scopes)
        return
    if action == "series":
        await _render_series_list(query, stage)
        return

    announce = None
    if action == "provision":
        report = await asyncio.to_thread(
            database.provision_cup_stage_line, stage["stage"], season_id=stage.get("season_id"),
            division_id=scope,
        )
        note = (f"Игр заведено: {report['created_games']}, заголовков: {report['created_headers']} "
                f"(всего серий {report['series']})")
    elif action == "open":
        ok, message = await asyncio.to_thread(database.open_cup_stage_bets, stage_id, user_id)
        note = message
        if ok:
            priced = await _generate_stage_markets(stage["stage"], stage.get("season_id"), scope)
            note = f"{message} Выставлено объектов линии: {priced}."
            announce = "bets"
    elif action == "start":
        ok, message = await asyncio.to_thread(database.start_cup_stage, stage_id, user_id)
        note = message
        if ok:
            announce = "start"
    else:
        return

    await _render_panel(query.message, context, stage_id=stage_id, note=note, scopes=scopes)
    if announce:
        from services.cup_broadcast import announce_stage
        fresh = await asyncio.to_thread(database.get_cup_stage_by_id, stage_id)
        await announce_stage(context.bot, fresh or stage, announce)


async def _generate_stage_markets(stage: str, season_id, division_id=None) -> int:
    from services.betting_engine import generate_stage_markets

    kwargs = {"season_id": season_id}
    if division_id is not None:
        kwargs["division_id"] = division_id
    rows = await asyncio.to_thread(generate_stage_markets, stage, **kwargs)
    return len(rows)


def register_cup_handlers(app) -> None:
    """Регистрация идёт до catch-all группы 0: иначе нажатия утонут в AI-чате."""
    # Только латиница: PTB отвергает кириллические команды ValueError-ом, и
    # register_all_handlers падал бы вместе со всем ботом.
    app.add_handler(CommandHandler("cup", cmd_cup))
    app.add_handler(CallbackQueryHandler(
        cb_cup,
        pattern=("^cup_(refresh|stage_\\d+|provision_\\d+|open_\\d+|start_\\d+|series_\\d+|ser_\\d+"
                 "|scope_\\d+|bracket_\\d+)$"),
    ))
