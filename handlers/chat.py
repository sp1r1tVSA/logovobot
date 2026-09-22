import re
import asyncio
import logging
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes
import database
from services.ai import ai_chat
from handlers.base import resolve_division_id
from handlers.text_commands import handle_temshik_command


logger = logging.getLogger(__name__)

# Сообщений истории (user + model), которые уходят в модель и хранятся в БД.
CHAT_HISTORY_KEEP = 6


async def _none():
    """Awaitable placeholder so asyncio.gather slots stay positional when a query is skipped."""
    return None


async def _empty_list():
    """Awaitable placeholder for skipped list-returning queries."""
    return []


async def _empty_dict():
    """Awaitable placeholder for skipped dict-returning queries."""
    return {}


# Резолв дивизиона переехал в handlers/base.py: он нужен и текстовым командам тоже,
# а chat импортирует text_commands, поэтому общее место может быть только ниже обоих.


async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик свободных сообщений и голосовых сообщений для ИИ Темшика.
    Подтягивает турнирную таблицу и информацию об игроке в качестве контекста.
    """
    if not update.message:
        return

    # Check if text is a tournament text command (e.g. "Темшик таблица", "Темшик состав")
    if update.message.text:
        handled = await handle_temshik_command(update, context)
        if handled:
            return

    # Мастер-выключатель из супер-админки: турнирные текстовые команды выше остаются
    # рабочими, глушится только генеративный диалог — ни Gemini, ни ответа в чат.
    if not await asyncio.to_thread(database.is_ai_chat_enabled):
        return

    is_voice_input = bool(update.message.voice)
    user_text = update.message.text.strip() if update.message.text else ""
    audio_input_bytes = None

    if is_voice_input:
        wants_voice = True
        try:
            vfile = await update.message.voice.get_file()
            audio_input_bytes = bytes(await vfile.download_as_bytearray())
            user_text = "(Голосовое сообщение)"
        except Exception as e:
            logger.error(f"Failed to download user voice message: {e}")
    else:
        if not user_text:
            return
        # Сообщение приходит как ответ (reply) на сообщение бота
        is_reply_to_bot = False
        if update.message.reply_to_message and update.message.reply_to_message.from_user:
            try:
                is_reply_to_bot = update.message.reply_to_message.from_user.id == context.bot.id
            except Exception:
                is_reply_to_bot = False
        # Если текстовое сообщение НЕ начинается с "темшик" и НЕ является ответом на бота
        if not user_text.lower().startswith("темшик") and not is_reply_to_bot:
            if re.match(r"^\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}$", user_text):
                await update.message.reply_text(
                    "⚠️ **Сессия ввода прервана из-за перезапуска бота.**\n\n"
                    "Пожалуйста, откройте админ-панель заново и повторите ввод дедлайна.",
                    parse_mode="Markdown"
                )
            return

        voice_keywords = ["голос", "озвучь", "проговори", "аудио", "скажи голосом", "поговори"]
        wants_voice = any(kw in user_text.lower() for kw in voice_keywords)

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Notify user that bot is "typing..."
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception as e:
        logger.warning(f"Failed to send typing action: {e}")

    # 1. Resolve the scope first: everything below is strictly one division + one season.
    user_data, active_season, chat_history, chat_mode = await asyncio.gather(
        asyncio.to_thread(database.get_user, user_id),
        asyncio.to_thread(database.get_active_season),
        asyncio.to_thread(database.get_chat_history, user_id, CHAT_HISTORY_KEEP),
        asyncio.to_thread(database.get_config, "chat_mode")
    )

    season_id = active_season["id"] if active_season else None
    season_name = (active_season["name"] if active_season else None) or "текущий сезон"
    division_id = await resolve_division_id(update, user_data)

    # 2. Gather division-scoped context concurrently
    (
        division,
        divisions,
        standings,
        top_scorers,
        top_assists,
        recent_matches,
        all_squads,
        division_rounds,
        recent_form_map,
        pending_matches,
        cup_series,
        division_players,
        season_rules
    ) = await asyncio.gather(
        asyncio.to_thread(database.get_division, division_id) if division_id else _none(),
        asyncio.to_thread(database.get_divisions, True),
        # Без дивизиона эти запросы ушли бы в legacy-ветку и смешали все дивизионы разом,
        # поэтому при неопределённом скоупе не отдаём турнирных данных вообще.
        asyncio.to_thread(database.get_standings, division_id, season_id) if division_id else _empty_list(),
        asyncio.to_thread(database.get_top_scorers, 5, division_id, season_id) if division_id else _empty_list(),
        asyncio.to_thread(database.get_top_assists, 5, division_id, season_id) if division_id else _empty_list(),
        asyncio.to_thread(database.get_recent_confirmed_matches, 6, division_id, season_id) if division_id else _empty_list(),
        asyncio.to_thread(database.get_all_squads) if division_id else _empty_dict(),
        asyncio.to_thread(database.get_division_rounds, division_id) if division_id else _empty_list(),
        asyncio.to_thread(database.get_teams_recent_form, 5, division_id, season_id) if division_id else _empty_dict(),
        asyncio.to_thread(database.get_open_pending_matches) if division_id else _empty_list(),
        asyncio.to_thread(database.get_all_cup_series) if division_id else _empty_list(),
        asyncio.to_thread(database.get_division_users, division_id) if division_id else _empty_list(),
        asyncio.to_thread(database.get_season_rules, season_id, division_id) if (season_id and division_id) else _none(),
    )

    division_name = (division or {}).get("name") if division else None
    if not division_name and division_id:
        division_name = f"Дивизион {division_id}"

    # Teams of this division — used to filter globally-stored data (squads, cup bracket).
    division_team_names = {
        (st.get("team_name") or "").lower() for st in standings if st.get("team_name")
    }
    for p in division_players:
        if p.get("team_name"):
            division_team_names.add(p["team_name"].lower())

    user_team = user_data["team_name"] if user_data else "Не зарегистрирован"
    username = user_data["username"] if user_data else update.effective_user.username or str(user_id)
    user_warn_count = user_data["warn_count"] if user_data and user_data["warn_count"] else 0
    
    # Promotion / relegation zones: division 1 has nothing above it, the last has nothing below.
    sorted_divs = sorted(divisions or [], key=lambda d: (d.get("sort_order") or 0, d.get("id") or 0))
    div_index = next((i for i, d in enumerate(sorted_divs) if d.get("id") == division_id), None)
    has_division_above = div_index is not None and div_index > 0
    has_division_below = div_index is not None and div_index < len(sorted_divs) - 1
    prom_slots = (season_rules or {}).get("promotion_slots", 3) if has_division_above else 0
    rel_slots = (season_rules or {}).get("relegation_slots", 3) if has_division_below else 0

    # Standings (division-scoped) with zone markers and recent form inline —
    # отдельный блок формы повторял бы все клубы таблицы второй раз.
    standings_text = (
        f"🏆 ТУРНИРНАЯ ТАБЛИЦА — {division_name or 'дивизион не определён'} ({season_name}):\n"
        "Формат: место. клуб (@тренер) О очки, И-В-Н-П, мячи, ФОРМА КОМАНД (последние игры, W/D/L)\n"
    )
    if standings:
        total_teams = len(standings)
        for i, st in enumerate(standings, 1):
            zone = ""
            if prom_slots and i <= prom_slots:
                zone = " 🚀"
            elif rel_slots and i > total_teams - rel_slots:
                zone = " 🔻"
            form_list = recent_form_map.get((st.get("team_name") or "").lower(), [])
            form_str = "".join(form_list) if form_list else "—"
            standings_text += (
                f"{i}. {st['team_name']} (@{st['username'] or '—'}) О{st['points']}, "
                f"{st['played']}-{st['wins']}-{st['draws']}-{st['losses']}, "
                f"{st['goals_scored']}:{st['goals_conceded']}, {form_str}{zone}\n"
            )
    else:
        standings_text += "Таблица пока пустая — сыгранных матчей в этом дивизионе нет.\n"

    # Top Scorers
    scorers_text = "⚽ ТОП БОМБАРДИРОВ ДИВИЗИОНА: "
    if top_scorers:
        scorers_text += "; ".join(
            f"{sc['player_name']} ({sc['team_name']}) {sc['total_goals']}" for sc in top_scorers
        ) + "\n"
    else:
        scorers_text += "голов пока нет.\n"

    # Top Assists
    assists_text = "🎯 ТОП АССИСТЕНТОВ ДИВИЗИОНА: "
    if top_assists:
        assists_text += "; ".join(
            f"{asst['player_name']} ({asst['team_name']}) {asst['total_assists']}" for asst in top_assists
        ) + "\n"
    else:
        assists_text += "ассистов пока нет.\n"

    # Recent Matches
    matches_text = "📊 ПОСЛЕДНИЕ СЫГРАННЫЕ МАТЧИ ДИВИЗИОНА:\n"
    if recent_matches:
        for m in recent_matches:
            matches_text += f"Тур {m['round_number']}: {m['team1']} {m['player1_score']} : {m['player2_score']} {m['team2']}\n"
    else:
        matches_text += "Сыгранных матчей пока нет.\n"

    # Rounds played in this division
    rounds_list = division_rounds or []
    total_rounds = max(rounds_list) if rounds_list else 0

    # Upcoming schedule — get_open_pending_matches is league-wide, so scope it here.
    # Весь хвост открытых туров модели не нужен: только матчи собеседника
    # и ближайший открытый тур дивизиона целиком.
    user_team_lc = (user_team or "").lower() if user_data else ""
    division_pending = [
        pm for pm in pending_matches
        if division_id is None or pm.get("division_id") == division_id
    ]
    nearest_round = min((pm.get("round_number") or 0 for pm in division_pending), default=None)
    own_opponents: list[str] = []
    schedule_by_round: dict[int, list[str]] = {}
    for pm in division_pending:
        team1 = pm.get("player1_team") or "?"
        team2 = pm.get("player2_team") or "?"
        is_own = bool(user_team_lc) and user_team_lc in (team1.lower(), team2.lower())
        if is_own:
            own_opponents.append(team2 if team1.lower() == user_team_lc else team1)
        rnd = pm.get("round_number") or 0
        if not is_own and rnd != nearest_round:
            continue
        line = f"{team1} (@{pm.get('player1_nickname') or '—'}) vs {team2} (@{pm.get('player2_nickname') or '—'})"
        if pm.get("deadline"):
            line += f" [дедлайн: {pm['deadline']}]"
        schedule_by_round.setdefault(rnd, []).append(line)

    schedule_text = "📅 РАСПИСАНИЕ ПРЕДСТОЯЩИХ МАТЧЕЙ (ближайший тур + матчи собеседника):\n"
    if schedule_by_round:
        for rnd in sorted(schedule_by_round.keys()):
            schedule_text += f"Тур {rnd}: " + "; ".join(schedule_by_round[rnd]) + "\n"
    else:
        schedule_text += "Открытых несыгранных матчей нет.\n"

    # Squads — stored globally by team name. Все 16 составов раздували промт сильнее всего,
    # поэтому отдаём только клуб собеседника, его ближайших соперников и клубы,
    # названные в самом сообщении.
    division_squads = {
        team: players for team, players in (all_squads or {}).items()
        if not division_team_names or (team or "").lower() in division_team_names
    }
    text_lc = user_text.lower()
    wanted = {user_team_lc} if user_team_lc else set()
    wanted.update(t.lower() for t in own_opponents[:2])
    for team in division_squads:
        tokens = [w for w in re.split(r"[\s\-]+", (team or "").lower()) if len(w) >= 5]
        if (team or "").lower() in text_lc or any(w in text_lc for w in tokens):
            wanted.add((team or "").lower())
    squads_text = "👥 СОСТАВЫ (клуб собеседника, его соперники и упомянутые клубы):\n"
    picked = [(t, p) for t, p in division_squads.items() if (t or "").lower() in wanted]
    if picked:
        for team, players in picked:
            squads_text += f"• {team}: {', '.join(players)}\n"
    else:
        squads_text += "Нужных составов нет в базе.\n"

    # History of past seasons — archive from the pre-division era.
    past_seasons_text = (
        "📜 АРХИВ ЕДИНОЙ ЛИГИ КПЛ (ЭПОХА ДО ДИВИЗИОНОВ, 16 клубов) — только для баек, НЕ текущее положение:\n"
        "• Прошлый сезон: 1. Расинг (@Vazya4mo666, двукратный чемпион), 2. Брага (@Saharokk8830), "
        "3. АЕК (@Snikers2121); Бенфика (@vtrrgyg) взяла Кубок КПЛ и Лигу Конференций; "
        "последний — Брюгге (@malenkihyi). Бомбардир Igor Paixao (50).\n"
        "• Позапрошлый: 1. Расинг (на очко выше Браги), 3. АЕК — Кубок КПЛ, Аякс (@LachesisQQQ) — Лига Европы.\n"
    )

    # Official League Rules & Info
    league_rules_text = (
        "📜 ОФИЦИАЛЬНЫЙ РЕГЛАМЕНТ ('Топ 7 лиг'):\n"
        "• Составы по Transfermarkt на 22.03.2026; игроки без клуба, Кумиры и Герои запрещены.\n"
        "• Максимум 111 OVR; ровно 6 спешл-карт; на поле минимум 5 игроков своей команды.\n"
        "• Прокачка: до 20 тренировок и 3 усилений (фиолетовый ранг), красный и золотой ранги запрещены; "
        "+1 тренировка за победу, +10 за активный канал клуба.\n"
        "• Запрещено: навесы (с игры, штрафных, угловых — угловые только «на балансе»), забросы с центра, "
        "в штрафную и «на ход», финты «пятка об пятку» и «переступ и выход», затягивание времени.\n"
        "• Ничьи не переигрываются; уйти с поста тренера до конца сезона нельзя (ЧС).\n"
        "• Судья: @onvamneVSAplayer. Правила и «Золотой Мяч»: @antonv2801.\n"
    )

    # Cup bracket — единая сетка на весь турнир, не имеет division_id.
    # Оставляем только серии, где участвует клуб из этого дивизиона.
    cup_info_text = "🏆 КУБОК (общий на весь турнир, сетка Best-of-3 — серии клубов ЭТОГО дивизиона):\n"
    cup_lines = []
    for cs in cup_series or []:
        t1, t2 = cs.get("team1_name", "?"), cs.get("team2_name", "?")
        if division_team_names and not (
            (t1 or "").lower() in division_team_names or (t2 or "").lower() in division_team_names
        ):
            continue
        w1, w2 = cs.get("team1_wins", 0) or 0, cs.get("team2_wins", 0) or 0
        stage = cs.get("stage") or "?"
        winner = cs.get("winner_name")
        line = f"• [{stage}] {t1} {w1}:{w2} {t2}"
        if winner:
            line += f" — прошёл дальше: {winner}"
        elif (cs.get("status") or "") == "active":
            line += " — серия ещё идёт"
        cup_lines.append(line)

    if cup_lines:
        cup_info_text += "\n".join(cup_lines) + "\n"
    else:
        cup_info_text += "Клубы этого дивизиона в кубковой сетке сейчас не представлены.\n"

    # Tournament structure — то, что модель обязана понимать про устройство турнира.
    if division_id:
        structure_lines = [
            f"• Турнир разбит на ДИВИЗИОНЫ (всего активных: {len(sorted_divs) or '—'}). Каждый дивизион — отдельная лига со своей таблицей, своими турами и своими дедлайнами.",
            f"• Ты сейчас работаешь СТРОГО в контексте: {division_name}, {season_name}.",
            f"• Клубов в этом дивизионе: {len(standings)}. Это полный список участников — "
            f"кого нет в таблице ниже, того нет и в дивизионе.",
            f"• Сыграно/заведено туров в этом дивизионе: {total_rounds if total_rounds else 'туры ещё не заведены'}.",
        ]
        if prom_slots:
            structure_lines.append(f"• Повышение: верхние {prom_slots} мест уходят дивизионом ВЫШЕ. 🚀")
        else:
            structure_lines.append("• Это ВЕРХНИЙ дивизион — выше подниматься некуда, тут играют за титул.")
        if rel_slots:
            structure_lines.append(f"• Вылет: нижние {rel_slots} мест падают дивизионом НИЖЕ. 🔻")
        else:
            structure_lines.append("• Это НИЖНИЙ дивизион — ниже падать некуда.")
        structure_lines.append("• Данных других дивизионов у тебя нет — про них цифры не выдумывай.")
        structure_text = "🗂 СТРУКТУРА ТУРНИРА:\n" + "\n".join(structure_lines) + "\n"
    else:
        structure_text = (
            "🗂 СТРУКТУРА ТУРНИРА:\n"
            "• Турнир разбит на дивизионы — каждый со своей таблицей, турами и дедлайнами.\n"
            "• ⚠️ ЭТОТ собеседник НЕ приписан ни к одному дивизиону, поэтому турнирных данных у тебя НЕТ.\n"
            "• Не выдумывай таблицу, места, очки и расписание. Скажи, что он не в дивизионе, "
            "и отправь к админу за распределением. Болтать на общие темы при этом можно.\n"
        )

    user_div_note = ""
    if division_id and user_data is not None:
        try:
            if user_data["division_id"] and user_data["division_id"] != division_id:
                user_div_note = (
                    f" ⚠️ Сам он приписан к другому дивизиону (#{user_data['division_id']}), "
                    f"а спрашивает в {division_name} — отвечай по данным {division_name}."
                )
        except (KeyError, IndexError):
            pass

    # Особые собеседники — строка попадает в промт, только когда это они.
    special_note = ""
    if (username or "").lower() == "snikers2121":
        special_note = ("Это сам @Snikers2121 (sniki) — великий! Относись к нему максимально "
                        "уважительно и по-братски, защищай его, называй великим.\n")
    elif (username or "").lower() == "sp1r1tvsa":
        special_note = "Это админ лиги @sp1r1tVSA — его не троллить, относись уважительно, по-дружески.\n"

    context_data = (
        f"Пользователь, который с тобой говорит: {username} (тренер команды '{user_team}').\n"
        f"Его дивизион в этом разговоре: {division_name or 'не определён'}.{user_div_note}\n"
        f"{special_note}"
        f"Предупреждения (варны) у этого пользователя: {user_warn_count}/4."
        f"{' ⚠️ ВНИМАНИЕ: у игрока 3/4 варна! Следующий варн (например, ещё один долг по матчу) приведёт к автоматическому лишению клуба и кику из группы!' if user_warn_count == 3 else ''}\n\n"
        f"{structure_text}\n"
        f"ВЛАДЕЛЕЦ ТУРНИРА: @antonv2801 — он хозяин и главный по правилам, но троллить и подкалывать его можно как любого другого.\n"
        f"{standings_text}\n"
        f"{schedule_text}\n"
        f"{scorers_text}\n"
        f"{assists_text}\n"
        f"{matches_text}\n"
        f"{squads_text}\n"
        f"{cup_info_text}\n\n"
        f"{past_seasons_text}\n"
        f"{league_rules_text}"
    )

    # 2. Call AI non-blocking via thread (history & mode were fetched above)
    chat_mode = chat_mode or "temshik"
    reply_text = await asyncio.to_thread(
        ai_chat.generate_chat_reply, 
        user_id, 
        user_text, 
        chat_history, 
        context_data,
        audio_input_bytes,
        "audio/ogg",
        chat_mode
    )

    # 3. Save to history
    await asyncio.to_thread(database.append_chat_history, user_id, "user", user_text)
    await asyncio.to_thread(database.append_chat_history, user_id, "model", reply_text)
    # Короткая память: длинные старые ответы в истории модель копирует как образец стиля.
    await asyncio.to_thread(database.trim_chat_history, user_id, keep=CHAT_HISTORY_KEEP)

    # 4. Send reply
    await update.message.reply_text(reply_text)




