import asyncio
import logging
import html
import re
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

import database
from handlers.base import (
    is_admin,
    generate_league_table_image,
    resolve_division_id,
    resolve_division_target,
)

logger = logging.getLogger(__name__)

# Trigger pattern: matches messages starting with "темшик", "темщик", "temshik", or @bot_username
TRIGGER_REGEX = re.compile(r"^(?:темшик|темщик|temshik|@[\w_]+bot)\b[\s,:]*", re.IGNORECASE)

# «див 2», «дивизион: 3», «division 4», «div #1» — явное указание дивизиона.
# Голое число дивизионом НЕ считается: в «бомбардиры 10» и «открыть тур 5» число
# уже занято лимитом и номером тура, и угадывать тут нечего.
_DIV_KEYWORD_REGEX = re.compile(
    r"(?<!\w)(?:дивизион\w*|дива|див|division|div)(?!\w)\s*[:#№]?\s*([\w\-]+)?",
    re.IGNORECASE,
)


def _cut_span(text: str, start: int, end: int) -> str:
    """Вырезать кусок строки и схлопнуть оставшиеся пробелы."""
    return re.sub(r"\s+", " ", (text[:start] + " " + text[end:])).strip()


def _match_division_in_args(args_str: str, divisions: list[dict]) -> tuple[int | None, str]:
    """
    Выдернуть дивизион из аргументов команды.

    Возвращает (division_id | None, аргументы без куска, описывающего дивизион).
    Порядок разбора: полное название → код (DIV_2 / div2) → ключевое слово с
    номером или частью названия. Если ничего не совпало — аргументы возвращаются
    нетронутыми, и вызывающий резолвит дивизион по контексту.
    """
    if not args_str or not divisions:
        return None, (args_str or "").strip()

    # 1) Название дивизиона целиком: «Дивизион 2», «Высшая лига».
    #    Границы слова нужны, чтобы «Дивизион 1» не откусывался от «Дивизион 12».
    best: tuple[int, int, int, int] | None = None  # (start, end, division_id, len)
    for d in divisions:
        name = (d.get("name") or "").strip()
        if len(name) < 3:
            continue
        m = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", args_str, re.IGNORECASE)
        if m and (best is None or len(name) > best[3]):
            best = (m.start(), m.end(), d["id"], len(name))
    if best:
        return best[2], _cut_span(args_str, best[0], best[1])

    # 2) Код дивизиона отдельным токеном.
    for d in divisions:
        code = (d.get("code") or "").strip()
        if not code:
            continue
        for variant in (code, code.replace("_", ""), code.replace("_", " ")):
            m = re.search(rf"(?<!\w){re.escape(variant)}(?!\w)", args_str, re.IGNORECASE)
            if m:
                return d["id"], _cut_span(args_str, m.start(), m.end())

    # 3) Ключевое слово + номер либо часть названия: «див 2», «дивизион Альфа».
    m = _DIV_KEYWORD_REGEX.search(args_str)
    if m:
        token = (m.group(1) or "").strip()
        if token:
            if token.isdigit():
                target = int(token)
                for d in divisions:
                    if d["id"] == target:
                        return d["id"], _cut_span(args_str, m.start(), m.end())
            else:
                low = token.lower()
                hits = [d for d in divisions if low in (d.get("name") or "").lower()]
                if len(hits) == 1:
                    return hits[0]["id"], _cut_span(args_str, m.start(), m.end())

    return None, args_str.strip()


async def resolve_command_division(update: Update, args_str: str) -> tuple[int | None, str, list[dict]]:
    """
    Единая точка определения дивизиона для текстовых команд.

    Сначала смотрим в аргументы (явное указание всегда сильнее контекста),
    затем падаем в `resolve_division_id` — топик → группа → привязка тренера.
    Возвращает (division_id | None, очищенные аргументы, список активных дивизионов);
    список отдаётся наружу, чтобы подсказку об ошибке можно было собрать без
    повторного похода в базу.
    """
    divisions = await asyncio.to_thread(database.get_active_divisions)
    div_id, leftover = _match_division_in_args(args_str, divisions)
    if div_id is None:
        div_id = await resolve_division_id(update)
    return div_id, leftover, divisions


def _division_hint(divisions: list[dict], example: str) -> str:
    """Сообщение о том, что дивизион не определён, со списком доступных."""
    lines = [
        "🤷 <b>Не понял, о каком дивизионе речь.</b>\n",
        "Напишите команду в топике своего дивизиона, попросите админа привязать вас "
        "к дивизиону — или укажите дивизион прямо в команде:",
        f"<code>{html.escape(example)}</code>",
    ]
    if divisions:
        lines.append("\n📋 <b>Активные дивизионы:</b>")
        for d in divisions:
            lines.append(f"• <code>{d['id']}</code> — {html.escape(d.get('name') or '—')}")
    return "\n".join(lines)


async def _division_name(division_id: int) -> str:
    """Человекочитаемое название дивизиона с безопасным фолбэком."""
    division = await asyncio.to_thread(database.get_division, division_id)
    return (division or {}).get("name") or f"Дивизион {division_id}"




async def handle_temshik_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handle structured tournament text commands prefixed with 'Темшик' without slashes.
    Returns True if a tournament command was recognized and handled, False otherwise.
    """
    msg = update.effective_message
    if not msg or not msg.text:
        return False

    text = msg.text.strip()
    match = TRIGGER_REGEX.match(text)
    if not match:
        return False

    # Extract command part after the trigger
    cmd_text = text[match.end():].strip()
    if not cmd_text:
        await msg.reply_text(
            "👋 Привет! Я <b>Темшик</b> — турнирный бот.\n"
            "Напиши <code>Темшик помощь</code> или <code>Темшик команды</code>, чтобы посмотреть список доступных команд.",
            parse_mode="HTML"
        )
        return True

    user_id = update.effective_user.id if update.effective_user else 0
    is_adm = is_admin(user_id)

    parts = cmd_text.split(None, 1)
    action = parts[0].lower()
    args_str = parts[1].strip() if len(parts) > 1 else ""
    full_cmd = cmd_text.lower()

    # =========================================================================
    # 📊 СТАТИСТИКА, ТАБЛИЦЫ, ДОЛГИ, ПОМОЩЬ (Публичные)
    # =========================================================================

    if action in ("помощь", "help", "команды", "команда"):
        help_text = (
            "📋 <b>ТЕКСТОВЫЕ КОМАНДЫ БОТА:</b>\n\n"
            "🗂 <b>Как работают дивизионы</b>\n"
            "Лига разбита на дивизионы — у каждого свои туры, таблица, расписание, "
            "бомбардиры и линия Logovo.bet.\n"
            "Дивизион определяется автоматически:\n"
            "• в форумном топике — по самому топику;\n"
            "• в личке с ботом — по дивизиону вашего клуба.\n"
            "Либо укажите его явно последним аргументом: "
            "<code>Дивизион 2</code>, <code>див 2</code> или кодом <code>DIV_2</code>.\n"
            "Список — <code>Темшик дивизионы</code>.\n\n"
            "⚽ <b>Для всех участников:</b>\n"
            "• <code>Темшик таблица [дивизион]</code> — таблица дивизиона (графика + кнопка обновления)\n"
            "• <code>Темшик бомбардиры [число] [дивизион]</code> — топ бомбардиров (карточка или список)\n"
            "• <code>Темшик ассистенты [число] [дивизион]</code> — топ ассистентов\n"
            "• <code>Темшик долги [дивизион]</code> — несыгранные матчи дивизиона с тегами участников\n"
            "• <code>Темшик состав [клуб]</code> — фото и состав заявленного клуба\n"
            "• <code>Темшик карточка [клуб]</code> — инфокарточка клуба\n"
            "• <code>Темшик позвать [клуб]</code> — позвать тренера клуба на матч (тегнет тренера)\n"
            "• <code>Темшик дивизионы</code> — список активных дивизионов лиги\n"
        )
        if is_adm:
            help_text += (
                "\n👑 <b>Команды администратора:</b>\n"
                "<i>Туры — в рамках дивизиона:</i>\n"
                "• <code>Темшик закрыть тур [номер] [дивизион]</code>\n"
                "• <code>Темшик топики [дивизион]</code> — статус настройки форумных топиков\n\n"
                "<i>Составы и клубы:</i>\n"
                "• <code>Темшик +игрок [клуб] [имена]</code> — добавить в состав\n"
                "• <code>Темшик -игрок [клуб] [имя]</code> — удалить из состава\n"
                "• <code>Темшик позиция [клуб] [POS] [имя]</code> — сменить позицию (GK, CB, CM, ST…)\n"
                "• <code>Темшик переименовать игрока [клуб] [старое] -> [новое]</code>\n"
                "• <code>Темшик привязать клуб @username [клуб]</code>\n"
                "• <code>Темшик тег @username [клуб]</code> — выдать плашку клуба в чате\n"
                "• <code>Темшик обновить теги [дивизион]</code> — выдать плашки всем тренерам\n\n"
                "<i>Дисциплина:</i>\n"
                "• <code>Темшик варн @username [причина]</code> — выдать варн\n"
                "• <code>Темшик снять варн @username</code> — снять варн\n"
                "• <code>Темшик варны</code> — список игроков с варнами\n"
                "• <code>Темшик автоварны</code> — прогнать проверку долгов\n\n"
                "<i>Слеш-команды топиков:</i>\n"
                "• <code>/naznachit_topik &lt;div_id&gt;</code> — привязать текущий топик к дивизиону\n"
                "• <code>/topiki &lt;div_id&gt;</code> — статус топиков дивизиона\n"
                "• <code>/diviziony</code> — сводка по всем дивизионам"
            )
        await msg.reply_text(help_text, parse_mode="HTML")
        return True

    if action in ("дивизионы", "дивизион", "divisions", "divs"):
        divisions = await asyncio.to_thread(database.get_active_divisions)
        if not divisions:
            await msg.reply_text("📋 Активных дивизионов пока нет.", parse_mode="HTML")
            return True

        current_id = await resolve_division_id(update)
        lines = ["🏆 <b>АКТИВНЫЕ ДИВИЗИОНЫ ЛИГИ:</b>\n"]
        for d in divisions:
            mark = " 👈 <i>ваш</i>" if d["id"] == current_id else ""
            code = f" <code>{html.escape(d.get('code') or '')}</code>" if d.get("code") else ""
            lines.append(f"• <b>{html.escape(d.get('name') or '—')}</b>{code} — id <code>{d['id']}</code>{mark}")
        lines.append("\nℹ️ Дивизион можно указать в любой команде: <code>Темшик таблица Дивизион 2</code>")
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
        return True

    if action in ("таблица", "турнирка", "table", "standings"):
        # Без дивизиона запрос уходил в кросс-дивизионную ветку и рисовал таблицу
        # по 16 именам КПЛ — одну и ту же всем пяти дивизионам.
        division_id, _, divisions = await resolve_command_division(update, args_str)
        if division_id is None:
            await msg.reply_text(
                _division_hint(divisions, "Темшик таблица Дивизион 2"),
                parse_mode="HTML",
            )
            return True

        division_name = await _division_name(division_id)
        img_buf = await asyncio.to_thread(
            generate_league_table_image,
            None, None, division_name, division_id
        )
        caption = f"🏆 <b>Турнирная таблица — {html.escape(division_name)}</b>"
        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_div_table_{division_id}")]]
        await msg.reply_photo(photo=img_buf, caption=caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return True

    if action in ("бомбардиры", "голы", "топ_голы", "scorers"):
        from telegram import InputFile
        from services.graphics import top_stats_generator

        division_id, rest, divisions = await resolve_command_division(update, args_str)
        if division_id is None:
            await msg.reply_text(
                _division_hint(divisions, "Темшик бомбардиры 10 Дивизион 2"),
                parse_mode="HTML",
            )
            return True
        division_name = await _division_name(division_id)
        nums = re.findall(r"\d+", rest)

        if nums:
            # Text list mode if explicit number is given
            limit = min(30, max(3, int(nums[0])))
            top_list = await asyncio.to_thread(database.get_top_scorers, limit, division_id=division_id)
            if not top_list:
                await msg.reply_text(
                    f"⚽ Список бомбардиров дивизиона <b>{html.escape(division_name)}</b> пока пуст.",
                    parse_mode="HTML"
                )
                return True
            lines = [f"⚽ <b>ТОП-{len(top_list)} БОМБАРДИРОВ — {html.escape(division_name).upper()}:</b>\n"]
            for idx, p in enumerate(top_list, 1):
                badge = "🥇 " if idx == 1 else ("🥈 " if idx == 2 else ("🥉 " if idx == 3 else f"{idx}. "))
                team_str = f" ({p['team_name']})" if p.get('team_name') else ""
                goals_cnt = p.get('total_goals', p.get('goals', 0))
                lines.append(f"{badge}<b>{html.escape(p.get('player_name', '—'))}</b>{html.escape(team_str)} — <b>{goals_cnt}</b> ⚽")
            await msg.reply_text("\n".join(lines), parse_mode="HTML")
            return True
        else:
            # Graphic card mode!
            buf = await asyncio.to_thread(
                top_stats_generator.generate_top_stats_image,
                "goals", 10, "league", division_id, division_name
            )
            caption = f"<b>⚽ ТОП БОМБАРДИРОВ — {html.escape(division_name).upper()}</b>"
            filename = "top_scorers.png"
            await msg.reply_photo(photo=InputFile(buf, filename=filename), caption=caption, parse_mode="HTML")
            return True

    if action in ("ассистенты", "пасы", "топ_пас", "assists"):
        from telegram import InputFile
        from services.graphics import top_stats_generator

        division_id, rest, divisions = await resolve_command_division(update, args_str)
        if division_id is None:
            await msg.reply_text(
                _division_hint(divisions, "Темшик ассистенты 10 Дивизион 2"),
                parse_mode="HTML",
            )
            return True
        division_name = await _division_name(division_id)
        nums = re.findall(r"\d+", rest)

        if nums:
            # Text list mode if explicit number is given
            limit = min(30, max(3, int(nums[0])))
            top_list = await asyncio.to_thread(database.get_top_assists, limit, division_id=division_id)
            if not top_list:
                await msg.reply_text(
                    f"🎯 Список ассистентов дивизиона <b>{html.escape(division_name)}</b> пока пуст.",
                    parse_mode="HTML"
                )
                return True
            lines = [f"🎯 <b>ТОП-{len(top_list)} АССИСТЕНТОВ — {html.escape(division_name).upper()}:</b>\n"]
            for idx, p in enumerate(top_list, 1):
                badge = "🥇 " if idx == 1 else ("🥈 " if idx == 2 else ("🥉 " if idx == 3 else f"{idx}. "))
                team_str = f" ({p['team_name']})" if p.get('team_name') else ""
                assists_cnt = p.get('total_assists', p.get('assists', 0))
                lines.append(f"{badge}<b>{html.escape(p.get('player_name', '—'))}</b>{html.escape(team_str)} — <b>{assists_cnt}</b> 🎯")
            await msg.reply_text("\n".join(lines), parse_mode="HTML")
            return True
        else:
            # Graphic card mode!
            buf = await asyncio.to_thread(
                top_stats_generator.generate_top_stats_image,
                "assists", 10, "league", division_id, division_name
            )
            caption = f"<b>🎯 ТОП АССИСТЕНТОВ — {html.escape(division_name).upper()}</b>"
            filename = "top_assisters.png"
            await msg.reply_photo(photo=InputFile(buf, filename=filename), caption=caption, parse_mode="HTML")
            return True

    if action in ("долги", "debts", "должники"):
        division_id, _, divisions = await resolve_command_division(update, args_str)
        if division_id is None:
            await msg.reply_text(
                _division_hint(divisions, "Темшик долги Дивизион 2"),
                parse_mode="HTML",
            )
            return True
        division_name = await _division_name(division_id)

        debts = await asyncio.to_thread(database.get_all_unplayed_league_matches, division_id=division_id)
        if not debts:
            await msg.reply_text(
                f"✅ <b>В дивизионе {html.escape(division_name)} нет долгов!</b> Все матчи сыграны.",
                parse_mode="HTML"
            )
            return True
        lines = [f"⏳ <b>НЕЗАКРЫТЫЕ МАТЧИ — {html.escape(division_name).upper()}:</b>\n"]
        rounds_map = {}
        for d in debts:
            rn = d.get("round_number", 0)
            if rn not in rounds_map:
                rounds_map[rn] = []
            rounds_map[rn].append(d)

        for rn in sorted(rounds_map.keys()):
            lines.append(f"📌 <b>Тур {rn}:</b>")
            for m in rounds_map[rn]:
                t1 = html.escape(m.get("player1_team") or "—")
                t2 = html.escape(m.get("player2_team") or "—")
                p1_u = f"@{m['p1_username']}" if m.get("p1_username") else t1
                p2_u = f"@{m['p2_username']}" if m.get("p2_username") else t2
                lines.append(f"• {t1} ({p1_u}) 🆚 {t2} ({p2_u})")
            lines.append("")
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
        return True

    # =========================================================================
    # 👥 СОСТАВЫ КЛУБОВ (Фото состава)
    # =========================================================================

    if action in ("состав", "составы", "squad"):
        team_to_find = args_str.strip()
        if not team_to_find:
            team_to_find = await asyncio.to_thread(database.get_user_team, user_id)
            if not team_to_find:
                await msg.reply_text(
                    "ℹ️ Укажите название клуба, например: <code>Темшик состав Расинг</code>",
                    parse_mode="HTML"
                )
                return True

        photo_id = await asyncio.to_thread(database.get_team_squad_photo, team_to_find)

        if not photo_id:
            # Try searching team by partial match
            all_teams = await asyncio.to_thread(database.get_all_teams)
            matched_t = next((t for t in all_teams if team_to_find.lower() in t.lower() or t.lower() in team_to_find.lower()), None)
            if matched_t:
                team_to_find = matched_t
                photo_id = await asyncio.to_thread(database.get_team_squad_photo, team_to_find)

        if photo_id:
            caption = f"📸 <b>Состав клуба {html.escape(team_to_find)}</b>"
            await msg.reply_photo(photo=photo_id, caption=caption, parse_mode="HTML")
        else:
            await msg.reply_text(
                f"📸 У клуба <b>{html.escape(team_to_find)}</b> ещё не загружено фото состава.",
                parse_mode="HTML"
            )
        return True

    # =========================================================================
    # 👑 КОМАНДЫ АДМИНИСТРАТОРА (ТРЕБУЮТ ПРАВ ADMIN)
    # =========================================================================

    if (
        action in ("добавить", "добавь", "+игрок", "add_player") or
        full_cmd.startswith("добавить игрока") or
        full_cmd.startswith("добавь игрока")
    ):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        clean_args = re.sub(r"^(?:добавить|добавь)?\s*(?:игрока|игроков)?\s*", "", cmd_text, flags=re.IGNORECASE).strip()
        parts_s = clean_args.split(None, 1)
        if len(parts_s) < 2:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик добавить игрока [Клуб] [Имя игрока]</code>\n"
                "Пример: <code>Темшик добавить игрока Расинг Matías Zaracho</code>",
                parse_mode="HTML"
            )
            return True

        team_name, players_raw = parts_s[0], parts_s[1]
        player_names = [p.strip() for p in players_raw.split(",") if p.strip()]
        added_cnt = await asyncio.to_thread(database.add_squad, team_name, player_names)
        await msg.reply_text(
            f"✅ В состав клуба <b>{html.escape(team_name)}</b> успешно добавлено игроков: <b>{added_cnt}</b>.",
            parse_mode="HTML"
        )
        return True

    if (
        action in ("удалить", "удали", "-игрок", "del_player", "remove_player") or
        full_cmd.startswith("удалить игрока") or
        full_cmd.startswith("удали игрока")
    ):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        clean_args = re.sub(r"^(?:удалить|удали)?\s*(?:игрока|игроков)?\s*", "", cmd_text, flags=re.IGNORECASE).strip()
        parts_s = clean_args.split(None, 1)
        if len(parts_s) < 2:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик удалить игрока [Клуб] [Имя игрока]</code>\n"
                "Пример: <code>Темшик удалить игрока Расинг Colombo</code>",
                parse_mode="HTML"
            )
            return True

        team_name, player_name = parts_s[0], parts_s[1].strip()
        removed = await asyncio.to_thread(database.remove_player_from_squad, team_name, player_name)
        if removed:
            await msg.reply_text(
                f"🗑 Игрок <b>{html.escape(player_name)}</b> удалён из состава клуба <b>{html.escape(team_name)}</b>.",
                parse_mode="HTML"
            )
        else:
            await msg.reply_text(
                f"❌ Игрок <b>{html.escape(player_name)}</b> не найден в составе <b>{html.escape(team_name)}</b>.",
                parse_mode="HTML"
            )
        return True

    if (
        action in ("позиция", "позиция_игрока", "set_pos", "setpos", "position") or
        full_cmd.startswith("позиция игрока") or
        full_cmd.startswith("сменить позицию") or
        full_cmd.startswith("изменить позицию")
    ):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        clean_args = re.sub(r"^(?:позиция|сменить позицию|изменить позицию)?\s*(?:игрока)?\s*", "", cmd_text, flags=re.IGNORECASE).strip()
        parts_s = clean_args.split(None, 2)
        if len(parts_s) < 3:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик позиция [Клуб] [Позиция] [Имя игрока]</code>\n"
                "Пример: <code>Темшик позиция Спортинг ST Viktor Gyökeres</code>",
                parse_mode="HTML"
            )
            return True

        team_name, pos_val, player_name = parts_s[0], parts_s[1].strip().upper(), parts_s[2].strip()
        updated = await asyncio.to_thread(database.set_player_position, player_name, team_name, pos_val)
        norm_pos = await asyncio.to_thread(database.get_player_position, player_name, team_name)
        await msg.reply_text(
            f"✅ Позиция игрока <b>{html.escape(player_name)}</b> в клубе <b>{html.escape(team_name)}</b> установлена: <b>[{norm_pos}]</b>.",
            parse_mode="HTML"
        )
        return True

    if (
        action in ("переименовать", "rename_player") or
        full_cmd.startswith("переименовать игрока")
    ):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        clean_args = re.sub(r"^(?:переименовать)?\s*(?:игрока)?\s*", "", cmd_text, flags=re.IGNORECASE).strip()
        if "->" in clean_args:
            left_p, new_n = clean_args.split("->", 1)
            left_parts = left_p.strip().split(None, 1)
            if len(left_parts) == 2:
                team_n, old_n = left_parts[0], left_parts[1]
            else:
                team_n, old_n = None, left_parts[0]
            new_n = new_n.strip()
        else:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик переименовать игрока [Клуб] [Старое имя] -> [Новое имя]</code>\n"
                "Пример: <code>Темшик переименовать игрока Расинг Lang -> Noa Lang</code>",
                parse_mode="HTML"
            )
            return True

        ok, text_res = await asyncio.to_thread(database.rename_player, old_n, new_n, team_n)
        await msg.reply_text(f"{'✅' if ok else '❌'} {text_res}", parse_mode="HTML")
        return True


    if (
        action in ("закрыть", "закрой", "close_round") or
        full_cmd.startswith("закрыть тур") or
        full_cmd.startswith("закрой тур")
    ):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        division_id, rest, divisions = await resolve_command_division(update, args_str)
        if division_id is None:
            await msg.reply_text(
                _division_hint(divisions, "Темшик закрыть тур 17 Дивизион 2"),
                parse_mode="HTML",
            )
            return True

        nums = re.findall(r"\d+", rest)
        if not nums:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик закрыть тур [номер] [дивизион]</code>\n"
                "Пример: <code>Темшик закрыть тур 17 Дивизион 2</code>",
                parse_mode="HTML"
            )
            return True
        rn = int(nums[0])
        division_name = await _division_name(division_id)
        await asyncio.to_thread(database.update_round_status, rn, is_open=False, division_id=division_id)
        await msg.reply_text(
            f"🔒 <b>Тур {rn} — {html.escape(division_name)} закрыт.</b>",
            parse_mode="HTML"
        )
        return True


    if action in ("топики", "топик", "topics", "topiki"):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        division_id, _, divisions = await resolve_command_division(update, args_str)
        if division_id is None:
            await msg.reply_text(
                _division_hint(divisions, "Темшик топики Дивизион 2"),
                parse_mode="HTML",
            )
            return True

        from services.topic_cache import topic_cache

        division_name = await _division_name(division_id)
        summary = await asyncio.to_thread(topic_cache.get_division_topics_summary, division_id)
        if not summary:
            summary = await asyncio.to_thread(database.get_division_topics_map, division_id)

        lines = [f"🗂 <b>ТОПИКИ — {html.escape(division_name).upper()}</b>\n"]
        missing = 0
        for t in database.PRIMARY_DIVISION_TOPICS:
            display = database.TOPIC_DISPLAY_NAMES.get(t, t)
            bound = summary.get(t)
            if bound:
                lines.append(f"✅ {display} — <code>{bound.get('message_thread_id')}</code>")
            else:
                missing += 1
                lines.append(f"❌ {display} — не привязан")

        if missing:
            lines.append(
                f"\n⚠️ Не настроено топиков: <b>{missing}</b>.\n"
                f"Зайдите в нужный топик и выполните <code>/naznachit_topik {division_id}</code>."
            )
        else:
            lines.append("\n🎉 Все основные топики дивизиона настроены.")
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
        return True

    if action in ("автоварны", "проверить_долги", "чекер_долгов") or full_cmd.startswith("автоварны") or full_cmd.startswith("проверить долги") or full_cmd.startswith("проверка долгов"):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        from handlers.admin import job_debt_lifecycle_tracker
        await msg.reply_text("⏳ <b>Запуск проверки долгов и начисления авто-варнов...</b>", parse_mode="HTML")
        await job_debt_lifecycle_tracker(context)
        await msg.reply_text("✅ <b>Проверка долгов и авто-варнов успешно завершена!</b>", parse_mode="HTML")
        return True

    if (
        action in ("клуб", "карточка", "карточка_клуба", "клуб_инфо", "club") or
        full_cmd.startswith("клуб") or
        full_cmd.startswith("карточка")
    ):
        chat = update.effective_chat
        if chat and chat.type in ("group", "supergroup", "channel") and not is_adm:
            bot_me = await context.bot.get_me()
            bot_username = bot_me.username or "logovobot"
            await msg.reply_text(
                f"ℹ️ Просмотр карточек клубов доступен в личном кабинете бота: @{bot_username}\n"
                f"В общем чате эта команда доступна только администраторам.",
                parse_mode="HTML"
            )
            return True

        target_club_raw = re.sub(
            r"^(?:карточка(?:\s+клуба)?|клуб(?:\s+инфо)?)\s*", "", cmd_text, flags=re.IGNORECASE
        ).strip()
        if not target_club_raw:
            user = update.effective_user
            team = await asyncio.to_thread(database.get_user_team, user.id) if user else None
            target_club_raw = team or ""

        if not target_club_raw:
            from handlers.cabinet import show_clubs_catalog
            await show_clubs_catalog(update, context)
            return True

        canon = database.resolve_team_name(target_club_raw)
        if not canon:
            await msg.reply_text(
                f"❌ Клуб <b>{html.escape(target_club_raw)}</b> не найден. Напишите <code>/club</code>, чтобы посмотреть весь список.",
                parse_mode="HTML"
            )
            return True

        from handlers.cabinet import send_or_edit_club_card
        await send_or_edit_club_card(update, context, canon, back_cb="cb_clubs_catalog")
        return True

    if action in ("варн", "warn"):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        parts_w = args_str.split(None, 1)
        if not parts_w:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик варн @username [причина]</code>\n"
                "Пример: <code>Темшик варн @ch1lyx Срыв дедлайна</code>",
                parse_mode="HTML"
            )
            return True

        target_ref = parts_w[0]
        reason = parts_w[1].strip() if len(parts_w) > 1 else "Нарушение регламента турнира"

        target_user = await asyncio.to_thread(database.find_user_by_ref, target_ref)
        if not target_user:
            await msg.reply_text(f"❌ Пользователь <b>{html.escape(target_ref)}</b> не найден в базе данных.", parse_mode="HTML")
            return True

        target_user = dict(target_user)
        t_id = target_user["telegram_id"]
        new_cnt, exceeded = await asyncio.to_thread(database.add_warn, t_id, user_id, reason)
        from config import MAX_WARNS_LIMIT

        warn_msg = (
            f"⚠️ <b>ВЫДАНО ПРЕДУПРЕЖДЕНИЕ:</b>\n\n"
            f"👤 <b>Игрок:</b> @{html.escape(target_user.get('username') or str(t_id))}\n"
            f"🛡 <b>Клуб:</b> {html.escape(target_user.get('team_name') or '—')}\n"
            f"📊 <b>Текущие варны:</b> {new_cnt}/{MAX_WARNS_LIMIT}\n"
            f"📝 <b>Причина:</b> {html.escape(reason)}"
        )
        if exceeded:
            warn_msg += f"\n\n🚨 <b>ВНИМАНИЕ: Достигнут лимит варнов ({MAX_WARNS_LIMIT}/{MAX_WARNS_LIMIT})!</b>"

        await msg.reply_text(warn_msg, parse_mode="HTML")

        # Also forward to the ПРЕДЫ topic of the player's own division
        group_id, warns_topic_id = await resolve_division_target(
            target_user.get("division_id"), "warns", "previews",
            legacy_topic_keys=("warns_topic_id",),
        )
        if group_id and warns_topic_id and msg.chat_id != group_id:
            try:
                await context.bot.send_message(
                    chat_id=group_id,
                    text=warn_msg,
                    parse_mode="HTML",
                    message_thread_id=int(warns_topic_id)
                )
            except Exception as e:
                logger.warning(f"Failed to post warn to warns topic: {e}")

        try:
            from handlers.admin import _post_or_update_debts_in_warns
            await _post_or_update_debts_in_warns(context)
        except Exception as e:
            logger.warning(f"Failed to refresh debts in warns topic: {e}")

        return True

    if action in ("снять_варн", "unwarn", "разварн") or full_cmd.startswith("снять варн"):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        clean_ref = re.sub(r"^(?:снять|сними)?\s*(?:варн)?\s*", "", cmd_text, flags=re.IGNORECASE).strip()
        if not clean_ref:
            await msg.reply_text("ℹ️ Укажите игрока: <code>Темшик снять варн @username</code>", parse_mode="HTML")
            return True

        target_user = await asyncio.to_thread(database.find_user_by_ref, clean_ref)
        if not target_user:
            await msg.reply_text(f"❌ Пользователь <b>{html.escape(clean_ref)}</b> не найден в базе данных.", parse_mode="HTML")
            return True

        target_user = dict(target_user)
        t_id = target_user["telegram_id"]
        new_cnt, removed = await asyncio.to_thread(database.remove_warn, t_id, user_id, "Снято администратором")
        if removed:
            await msg.reply_text(
                f"✅ Предупреждение снято с @{html.escape(target_user.get('username') or str(t_id))}. "
                f"Текущие варны: <b>{new_cnt}</b>.",
                parse_mode="HTML"
            )
        else:
            await msg.reply_text(
                f"ℹ️ У игрока @{html.escape(target_user.get('username') or str(t_id))} нет активных варнов.",
                parse_mode="HTML"
            )

        try:
            from handlers.admin import _post_or_update_debts_in_warns
            await _post_or_update_debts_in_warns(context)
        except Exception as e:
            logger.warning(f"Failed to refresh debts in warns topic: {e}")

        return True

    if action in ("варны", "список_варнов", "warns"):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        warn_users = await asyncio.to_thread(database.get_all_active_warns)
        if not warn_users:
            await msg.reply_text("✨ <b>Участников с активными предупреждениями нет.</b>", parse_mode="HTML")
            return True

        from config import MAX_WARNS_LIMIT
        lines = ["⚠️ <b>СПИСОК ИГРОКОВ С ПРЕДУПРЕЖДЕНИЯМИ:</b>\n"]
        for u in warn_users:
            un = f"@{u['username']}" if u.get("username") else str(u['telegram_id'])
            tm = f" ({u['team_name']})" if u.get("team_name") else ""
            lines.append(f"• <b>{html.escape(un)}</b>{html.escape(tm)} — <b>{u['warn_count']}/{MAX_WARNS_LIMIT}</b>")
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
        return True

    if action in ("привязать_клуб", "привязать", "set_team") or full_cmd.startswith("привязать клуб"):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        clean_args = re.sub(r"^(?:привязать)?\s*(?:клуб)?\s*", "", cmd_text, flags=re.IGNORECASE).strip()
        parts_p = clean_args.split(None, 1)
        if len(parts_p) < 2:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик привязать клуб @username [Название клуба]</code>\n"
                "Пример: <code>Темшик привязать клуб @ch1lyx Расинг</code>",
                parse_mode="HTML"
            )
            return True

        user_ref, club_name = parts_p[0], parts_p[1].strip()
        ok, res_text = await asyncio.to_thread(database.set_player_club, user_ref, club_name)
        if ok:
            try:
                from handlers.admin import _post_or_update_debts_in_warns
                await _post_or_update_debts_in_warns(context)
            except Exception as e:
                logger.warning(f"Failed to update debts in warns: {e}")
        # Название клуба пришло из чата — в HTML-режиме его экранируем.
        await msg.reply_text(f"{'✅' if ok else '❌'} {html.escape(res_text)}", parse_mode="HTML")
        return True

    # =========================================================================
    # 📣 ВЫЗОВ ТРЕНЕРА КЛУБА НА МАТЧ («Темшик позвать Кельн»)
    # =========================================================================
    if (
        action in ("позвать", "позови", "вызвать", "вызови", "где", "summon", "call") or
        full_cmd.startswith(("позвать", "позови", "вызвать", "вызови", "где "))
    ):
        clean_target = re.sub(r"^(?:позвать|позови|вызвать|вызови|где|summon|call)\s*(?:тренера|клуб)?\s*", "", cmd_text, flags=re.IGNORECASE).strip()
        division_id, club_query, divisions = await resolve_command_division(update, clean_target)
        club_query = club_query.strip()
        if not club_query:
            await msg.reply_text(
                "ℹ️ Формат: <code>Темшик позвать [Название клуба] [дивизион]</code>\n"
                "Пример: <code>Темшик позвать Кельн</code> или <code>Темшик позвать Реал Дивизион 1</code>",
                parse_mode="HTML"
            )
            return True

        clean_club = re.sub(r"^(?:тренера|клуб|на\s+матч)\s*", "", club_query, flags=re.IGNORECASE).strip()
        target_club = clean_club or club_query
        coach = await asyncio.to_thread(database.find_coach_by_club, target_club, division_id)
        if not coach and clean_club != club_query:
            coach = await asyncio.to_thread(database.find_coach_by_club, club_query, division_id)

        if not coach:
            await msg.reply_text(
                f"❌ Тренер клуба «<b>{html.escape(target_club)}</b>» не найден среди участников турнира.",
                parse_mode="HTML"
            )
            return True

        u_name = coach.get("username")
        p_id = coach.get("telegram_id")
        t_name = coach.get("team_name") or target_club
        caller_name = (
            f"@{msg.from_user.username}"
            if (msg.from_user and msg.from_user.username)
            else (msg.from_user.first_name if msg.from_user else "Участник")
        )

        if u_name:
            clean_u = u_name.lstrip('@')
            mention = f"@{html.escape(clean_u)}"
        else:
            mention = f'<a href="tg://user?id={p_id}">Тренер {html.escape(t_name)}</a>'

        reply_text = (
            f"📣 <b>{html.escape(caller_name)}</b> вызывает тренера <b>{html.escape(t_name)}</b>!\n"
            f"👉 {mention}, вас ждут на матч! ⚽"
        )
        await msg.reply_text(reply_text, parse_mode="HTML")
        return True

    # =========================================================================
    # 🏷 УПРАВЛЕНИЕ ПЛАШКАМИ КЛУБОВ (Custom Titles)
    # =========================================================================
    if (
        action in ("тег", "теги", "звание", "плашка", "плашки", "set_title", "titles") or
        full_cmd.startswith(("обновить теги", "назначить теги", "теги обновить", "теги назначить"))
    ):
        if not is_adm:
            await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
            return True

        if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
            await msg.reply_text("⚠️ Установка плашек возможна только в супергруппе турнира.")
            return True

        from services.chat_titles import assign_club_title, sync_division_club_titles

        clean_args = re.sub(
            r"^(?:обновить|назначить|поставить)?\s*(?:теги|тег|звание|плашки|плашка)\s*",
            "", cmd_text, flags=re.IGNORECASE
        ).strip()

        # Массовое обновление тегов для дивизиона/группы
        is_bulk = (
            action in ("теги", "плашки", "titles") or
            full_cmd.startswith(("обновить теги", "назначить теги", "теги обновить", "теги назначить"))
        )

        if is_bulk and not (action in ("теги", "плашки", "titles") and clean_args.startswith("@")):
            clean_div = clean_args.strip()
            division_id, _, divisions = await resolve_command_division(update, clean_args)
            if clean_div.isdigit():
                target = int(clean_div)
                if any(d["id"] == target for d in divisions) or not divisions:
                    division_id = target
                else:
                    div_obj = await asyncio.to_thread(database.get_division, target)
                    if div_obj:
                        division_id = target
                    else:
                        await msg.reply_text(
                            f"❌ Дивизион с номером <code>{target}</code> не найден среди активных.\n\n"
                            + _division_hint(divisions, f"Темшик обновить теги {divisions[0]['id'] if divisions else 1}"),
                            parse_mode="HTML"
                        )
                        return True

            status_m = await msg.reply_text("⏳ <i>Обновляю плашки клубов для участников...</i>", parse_mode="HTML")
            stats = await sync_division_club_titles(context.bot, update.effective_chat.id, division_id)

            if stats.get("error") == "no_promote_rights" or (stats["failed"] > 0 and stats["success"] == 0 and any("can_promote_members" in d for d in stats["details"])):
                retry_arg = f" {division_id}" if division_id else ""
                await status_m.edit_text(
                    "⚠️ <b>Недостаточно прав у бота в группе!</b>\n\n"
                    "В Telegram плашки клубов (должности) технически привязаны к статусу администратора. "
                    "Бот делает тренеров администраторами с <i>минимальными правами</i> (только инвайт-ссылки, без права удалять сообщения или банить).\n\n"
                    "Чтобы бот мог автоматически выдавать плашки, ему требуется право <b>«Добавление администраторов»</b>.\n\n"
                    "👉 <b>Как настроить владельцу группы:</b>\n"
                    "1. Зайдите в <b>Настройки группы</b> → <b>Администраторы</b>.\n"
                    "2. Откройте профиль бота <b>ТЕМШИК</b>.\n"
                    "3. Включите пункт <b>«Добавление администраторов»</b> (или «Назначение администраторов»).\n"
                    "4. Сохраните и повторите команду:\n"
                    f"<code>Темшик обновить теги{retry_arg}</code>",
                    parse_mode="HTML"
                )
                return True

            div_label = f" (Дивизион {division_id})" if division_id else ""
            report_lines = [
                f"🏷 <b>ОБНОВЛЕНИЕ ПЛАШЕК КЛУБОВ ЗАВЕРШЕНО{html.escape(div_label)}:</b>\n",
                f"• Всего тренеров в базе: <b>{stats['total']}</b>",
                f"• ✅ Успешно установлено: <b>{stats['success']}</b>",
                f"• ⚠️ Пропущено (не в чате / владелец): <b>{stats['skipped']}</b>",
                f"• ❌ Ошибок (лимит 50 / нет прав): <b>{stats['failed']}</b>",
            ]
            if stats["details"] and stats["failed"] > 0:
                report_lines.append("\n<b>Ошибки:</b>")
                report_lines.extend(stats["details"][-5:])
            await status_m.edit_text("\n".join(report_lines), parse_mode="HTML")
            return True

        # Точечное назначение: «Темшик тег @username [клуб]»
        parts_p = clean_args.split(None, 1)
        if not parts_p:
            await msg.reply_text(
                "ℹ️ <b>Форматы управления плашками:</b>\n"
                "• <code>Темшик тег @username [Клуб]</code> — выдать плашку участнику\n"
                "• <code>Темшик обновить теги [дивизион]</code> — выдать плашки всем тренерам в чате",
                parse_mode="HTML"
            )
            return True

        target_ref = parts_p[0].strip().lstrip("@")
        override_club = parts_p[1].strip() if len(parts_p) > 1 else None

        user_row = await asyncio.to_thread(database.find_user_by_ref, target_ref)
        target_user_id = user_row["telegram_id"] if user_row else (int(target_ref) if target_ref.isdigit() and len(target_ref) >= 6 else None)
        club_to_assign = override_club or (user_row["team_name"] if user_row else None)

        if not target_user_id:
            await msg.reply_text(f"❌ Пользователь <code>@{html.escape(target_ref)}</code> не найден в базе данных.", parse_mode="HTML")
            return True

        if not club_to_assign:
            await msg.reply_text("❌ У пользователя не указан клуб в базе данных, и клуб не передан в команде.", parse_mode="HTML")
            return True

        ok, res_msg = await assign_club_title(context.bot, update.effective_chat.id, target_user_id, club_to_assign)
        await msg.reply_text(
            f"{'✅' if ok else '❌'} <b>@{html.escape(target_ref)}</b>: {html.escape(res_msg)}",
            parse_mode="HTML"
        )
        return True

    # Not a specific tournament command -> return False to allow conversational AI chat to handle it
    return False


async def cmd_summon_club(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Slash command /summon [club] [division]."""
    msg = update.effective_message
    if not msg:
        return

    args_str = " ".join(context.args) if context.args else ""
    if not args_str.strip():
        await msg.reply_text(
            "ℹ️ Формат: <code>/summon [Название клуба] [дивизион]</code>\n"
            "Пример: <code>/summon Кельн</code> или <code>/summon Реал Дивизион 1</code>",
            parse_mode="HTML"
        )
        return

    division_id, club_query, _ = await resolve_command_division(update, args_str)
    target_club = club_query.strip()
    coach = await asyncio.to_thread(database.find_coach_by_club, target_club, division_id)

    if not coach:
        await msg.reply_text(
            f"❌ Тренер клуба «<b>{html.escape(target_club)}</b>» не найден среди участников турнира.",
            parse_mode="HTML"
        )
        return

    u_name = coach.get("username")
    p_id = coach.get("telegram_id")
    t_name = coach.get("team_name") or target_club
    caller_name = (
        f"@{msg.from_user.username}"
        if (msg.from_user and msg.from_user.username)
        else (msg.from_user.first_name if msg.from_user else "Участник")
    )

    if u_name:
        clean_u = u_name.lstrip('@')
        mention = f"@{html.escape(clean_u)}"
    else:
        mention = f'<a href="tg://user?id={p_id}">Тренер {html.escape(t_name)}</a>'

    reply_text = (
        f"📣 <b>{html.escape(caller_name)}</b> вызывает тренера <b>{html.escape(t_name)}</b>!\n"
        f"👉 {mention}, вас ждут на матч! ⚽"
    )
    await msg.reply_text(reply_text, parse_mode="HTML")


async def cmd_sync_club_titles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Slash command /set_club_titles [division] (Admin only)."""
    msg = update.effective_message
    if not msg:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    if not is_admin(user_id):
        await msg.reply_text("⚠️ Эта команда доступна только администраторам турнира.")
        return

    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        await msg.reply_text("⚠️ Установка плашек возможна только в супергруппе турнира.")
        return

    from services.chat_titles import sync_division_club_titles

    args_str = " ".join(context.args) if context.args else ""
    division_id, _, divisions = await resolve_command_division(update, args_str)
    clean_div = args_str.strip()
    if clean_div.isdigit():
        target = int(clean_div)
        if any(d["id"] == target for d in divisions) or not divisions:
            division_id = target
        else:
            div_obj = await asyncio.to_thread(database.get_division, target)
            if div_obj:
                division_id = target
            else:
                await msg.reply_text(
                    f"❌ Дивизион с номером <code>{target}</code> не найден среди активных.\n\n"
                    + _division_hint(divisions, f"/set_club_titles {divisions[0]['id'] if divisions else 1}"),
                    parse_mode="HTML"
                )
                return

    status_m = await msg.reply_text("⏳ <i>Обновляю плашки клубов для участников...</i>", parse_mode="HTML")
    stats = await sync_division_club_titles(context.bot, update.effective_chat.id, division_id)

    if stats.get("error") == "no_promote_rights" or (stats["failed"] > 0 and stats["success"] == 0 and any("can_promote_members" in d for d in stats["details"])):
        retry_arg = f" {division_id}" if division_id else ""
        await status_m.edit_text(
            "⚠️ <b>Недостаточно прав у бота в группе!</b>\n\n"
            "В Telegram плашки клубов (должности) технически привязаны к статусу администратора. "
            "Чтобы бот мог автоматически выдавать плашки, ему требуется право <b>«Добавление администраторов»</b>.\n\n"
            "👉 <b>Как настроить:</b>\n"
            "1. Зайдите в <b>Настройки группы</b> → <b>Администраторы</b>.\n"
            "2. Откройте профиль бота <b>ТЕМШИК</b>.\n"
            "3. Включите право <b>«Добавление администраторов»</b>.\n"
            f"4. Сохраните и повторите: <code>/set_club_titles{retry_arg}</code>",
            parse_mode="HTML"
        )
        return

    div_label = f" (Дивизион {division_id})" if division_id else ""

    report_lines = [
        f"🏷 <b>ОБНОВЛЕНИЕ ПЛАШЕК КЛУБОВ ЗАВЕРШЕНО{html.escape(div_label)}:</b>\n",
        f"• Всего тренеров в базе: <b>{stats['total']}</b>",
        f"• ✅ Успешно установлено: <b>{stats['success']}</b>",
        f"• ⚠️ Пропущено (не в чате / владелец): <b>{stats['skipped']}</b>",
        f"• ❌ Ошибок (лимит 50 / нет прав): <b>{stats['failed']}</b>",
    ]
    if stats["details"] and stats["failed"] > 0:
        report_lines.append("\n<b>Ошибки:</b>")
        report_lines.extend(stats["details"][-5:])
    await status_m.edit_text("\n".join(report_lines), parse_mode="HTML")

