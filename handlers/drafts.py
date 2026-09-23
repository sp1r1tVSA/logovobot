import asyncio
import logging
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import database
import config
from services.topic_cache import topic_cache
from services.ai.ai_recognizer import recognize_match_screenshots_bytes
from handlers.cabinet import match_and_enrich_squad, build_formatted_match_post, resolve_mvp_player_name, has_shootout

logger = logging.getLogger(__name__)

# In-memory storage for collecting media groups
# { "buffer_key": { "photos": [...], "photo_file_ids": [...], "caption": "", "user_id": int, "message_ids": [...] } }
draft_media_groups = {}
draft_tasks = {}

async def handle_draft_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Listen for photos in the drafts topic and collect them."""
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        return
        
    msg = update.message
    if not msg:
        return
        
    if not msg.is_topic_message:
        return
        
    thread_id = msg.message_thread_id
    chat_id = update.effective_chat.id
    target_division_id = None

    # Привязка дивизиона имеет приоритет над легаси-конфигом: раньше глобальный
    # drafts_topic_id сравнивался с «голым» thread_id ДО поиска биндинга, и
    # совпадение обнуляло division_id — черновик дивизиона уходил в поиск матча
    # по всем дивизионам сразу.
    binding = topic_cache.get_by_topic(chat_id, thread_id)
    if binding and binding.get("topic_type") in ("draft", "drafts"):
        target_division_id = binding["division_id"]
    else:
        div = await asyncio.to_thread(database.get_division_by_topic, thread_id, "drafts", chat_id)
        if div:
            target_division_id = div["id"]
        else:
            # Легаси-инсталляции без привязок: только тот же топик в той же группе.
            legacy_topic_id = None
            drafts_topic_id_str = await asyncio.to_thread(database.get_config, "drafts_topic_id")
            if drafts_topic_id_str:
                try:
                    legacy_topic_id = int(drafts_topic_id_str)
                except ValueError:
                    legacy_topic_id = None
            if legacy_topic_id is None or thread_id != legacy_topic_id:
                return  # Message is in a topic not configured for drafts
            main_group_id = await asyncio.to_thread(database.get_group_id)
            if main_group_id and chat_id != int(main_group_id):
                return  # Legacy topic id from another group — не наш черновик
            target_division_id = None


    user_id = update.effective_user.id
    
    # Key by media_group_id or by user in topic for consecutive photos
    if msg.media_group_id:
        buffer_key = f"mg_{msg.media_group_id}"
    else:
        buffer_key = f"user_{update.effective_chat.id}_{msg.message_thread_id}_{user_id}"
        
    if buffer_key not in draft_media_groups:
        draft_media_groups[buffer_key] = {
            "photos": [],
            "photo_file_ids": [],
            "caption": "",
            "user_id": user_id,
            "message_ids": [],
            "division_id": target_division_id
        }
        
    group_data = draft_media_groups[buffer_key]
    group_data["message_ids"].append(msg.message_id)
    
    text = msg.caption or msg.text
    if text:
        if group_data["caption"]:
            if text not in group_data["caption"]:
                group_data["caption"] += f"\n{text}"
        else:
            group_data["caption"] = text
        
    if msg.photo:
        # get highest resolution
        photo = msg.photo[-1]
        group_data["photo_file_ids"].append(photo.file_id)
        f_obj = await context.bot.get_file(photo.file_id)
        f_bytes = await f_obj.download_as_bytearray()
        group_data["photos"].append(bytes(f_bytes))
        
    # Cancel previous timer if still waiting and restart debounce timer
    if buffer_key in draft_tasks:
        draft_tasks[buffer_key].cancel()
        
    draft_tasks[buffer_key] = asyncio.create_task(
        _process_draft_group_delayed(buffer_key, update, context)
    )

async def _process_draft_group_delayed(buffer_key: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Wait to let all media in the group or rapid consecutive photos arrive
    await asyncio.sleep(4.5)
    
    group_data = draft_media_groups.pop(buffer_key, None)
    draft_tasks.pop(buffer_key, None)
    
    if not group_data:
        return
        
    photos = group_data["photos"]
    caption = group_data["caption"]
    user_id = group_data["user_id"]
    msg_ids = group_data["message_ids"]
    photo_file_ids = group_data["photo_file_ids"]
    reply_to_id = msg_ids[0] if msg_ids else None
    
    if not photos:
        # Just text, ignore. Or prompt user for screenshots?
        # Let's just ignore to not spam.
        return
        
    status_msg = await update.effective_message.reply_text(
        "⏳ Обрабатываю результат через ИИ...", 
        reply_to_message_id=reply_to_id
    )
    
    try:
        ai_res = await asyncio.to_thread(
            recognize_match_screenshots_bytes,
            photos,
            caption=caption
        )
    except Exception as e:
        logger.exception("Error in draft AI processing")
        await status_msg.edit_text("❌ Ошибка при распознавании скриншота.")
        return
        
    if not ai_res:
        await status_msg.edit_text("🤖 ИИ не смог распознать результаты матча. Убедитесь, что скриншоты чёткие.")
        return
        
    matches_list = ai_res.get("matches") or [ai_res]

    if has_shootout(ai_res):
        # Голы серии пенальти EA FC пишет в колонку «Г» — черновик со скриншота
        # вышел бы с неверными авторами. Такой результат вносится вручную в ЛС.
        await status_msg.edit_text(
            "🥅 На скриншоте серия пенальти. Такой результат занесите вручную через "
            "личные сообщения бота: счёт основного времени и авторов голов с игры."
        )
        return
    
    # 1. Determine team names (by player names / squads first, then fallback to OCR / caption)
    s1_all_p = (matches_list[0].get("side1_goals") or matches_list[0].get("left_goals") or []) + \
               (matches_list[0].get("side1_assists") or matches_list[0].get("left_assists") or [])
    s2_all_p = (matches_list[0].get("side2_goals") or matches_list[0].get("right_goals") or []) + \
               (matches_list[0].get("side2_assists") or matches_list[0].get("right_assists") or [])

    detected_t1, detected_t2 = await asyncio.to_thread(database.detect_teams_from_players, s1_all_p, s2_all_p, caption)
    
    t1_raw = detected_t1 or matches_list[0].get("team1")
    t2_raw = detected_t2 or matches_list[0].get("team2")
    if not t1_raw or not t2_raw:
        await status_msg.edit_text("🤖 ИИ распознал счет, но не смог определить команды по составам игроков. Пожалуйста, укажите названия клубов текстом в описании к фото.")
        return
        
    t1 = database.resolve_team_name(t1_raw) or t1_raw
    t2 = database.resolve_team_name(t2_raw) or t2_raw
        
    # 2. Find active match and determine the round automatically
    division_id = group_data.get("division_id")
    first_match = await asyncio.to_thread(database.get_active_match_by_teams, t1, t2, caption, division_id=division_id)
    if not first_match:
        await status_msg.edit_text(f"❌ Не найден активный матч между командами {html.escape(t1)} и {html.escape(t2)}.\nВозможно, этот тур уже подтвержден или названия клубов не совпадают.")
        return

    prepared_games = []
    # Скоринг в get_active_match_by_teams детерминированный: без учёта уже занятых id
    # все игры серии сматчились бы на один матч, и N подтверждений переписали бы одну
    # строку, пока в РЕЗУЛЬТАТЫ уходило бы N постов.
    used_match_ids = set()

    for idx, m_info in enumerate(matches_list):
        if idx == 0:
            cur_match = first_match
        else:
            cur_match = database.get_active_match_by_teams(
                t1, t2, caption=caption, division_id=division_id, exclude_ids=used_match_ids
            )
        if not cur_match:
            logger.warning(
                f"Draft: only {idx} active match(es) found for {t1} vs {t2}, "
                f"but AI returned {len(matches_list)} games. Extra games dropped."
            )
            break
        used_match_ids.add(cur_match.get("id"))

        home_team = cur_match.get("player1_team") or cur_match.get("player1_nickname") or t1
        away_team = cur_match.get("player2_team") or cur_match.get("player2_nickname") or t2
        
        s1_goals = m_info.get("side1_goals") or m_info.get("home_goals") or []
        s2_goals = m_info.get("side2_goals") or m_info.get("away_goals") or []
        s1_assists = m_info.get("side1_assists") or m_info.get("home_assists") or []
        s2_assists = m_info.get("side2_assists") or m_info.get("away_assists") or []
        is_single_timeline = bool(m_info.get("is_single_timeline", False))
        
        try:
            h_goals, a_goals, h_assists, a_assists, is_side1_home = await asyncio.to_thread(
                match_and_enrich_squad,
                s1_goals, s2_goals, s1_assists, s2_assists,
                home_team, away_team,
                is_single_timeline=is_single_timeline,
            )
        except Exception as e:
            logger.exception(f"Error matching squad in draft: {e}")
            await status_msg.edit_text("❌ Ошибка при сопоставлении состава. Возможно, игроки не зарегистрированы.")
            return
            
        mvp_player = await asyncio.to_thread(
            resolve_mvp_player_name, m_info.get("mvp_player"), home_team, away_team
        )

        l_score = int(m_info.get("left_score", 0))
        r_score = int(m_info.get("right_score", 0))
        h_g_count = sum(h_goals.values())
        a_g_count = sum(a_goals.values())
        
        if is_side1_home:
            h_score = l_score if (l_score > 0 or r_score > 0) else h_g_count
            a_score = r_score if (l_score > 0 or r_score > 0) else a_g_count
        else:
            h_score = r_score if (l_score > 0 or r_score > 0) else h_g_count
            a_score = l_score if (l_score > 0 or r_score > 0) else a_g_count

        # SANITY CHECK: The team that scored more goals MUST have the higher score!
        if a_g_count > h_g_count and h_score > a_score:
            h_score, a_score = a_score, h_score
            is_side1_home = not is_side1_home
        elif h_g_count > a_g_count and a_score > h_score:
            h_score, a_score = a_score, h_score
            is_side1_home = not is_side1_home

        if h_score < h_g_count:
            h_score = h_g_count
        if a_score < a_g_count:
            a_score = a_g_count

        # Mathematical rule: assists cannot exceed score
        h_a_count = sum(h_assists.values())
        if h_a_count > h_score:
            excess = h_a_count - h_score
            for p in list(h_assists.keys()):
                if excess <= 0:
                    break
                if p in h_goals:
                    if h_assists[p] <= excess:
                        excess -= h_assists[p]
                        del h_assists[p]
                    else:
                        h_assists[p] -= excess
                        excess = 0

        a_a_count = sum(a_assists.values())
        if a_a_count > a_score:
            excess = a_a_count - a_score
            for p in list(a_assists.keys()):
                if excess <= 0:
                    break
                if p in a_goals:
                    if a_assists[p] <= excess:
                        excess -= a_assists[p]
                        del a_assists[p]
                    else:
                        a_assists[p] -= excess
                        excess = 0
                
        events = []
        for p, c in h_goals.items(): events.append((home_team, p, "goal", c))
        for p, c in a_goals.items(): events.append((away_team, p, "goal", c))
        for p, c in h_assists.items(): events.append((home_team, p, "assist", c))
        for p, c in a_assists.items(): events.append((away_team, p, "assist", c))

        p1_un = cur_match.get('player1_username')
        p2_un = cur_match.get('player2_username')
        p1_clean = html.escape(p1_un.lstrip('@')) if p1_un else ""
        p2_clean = html.escape(p2_un.lstrip('@')) if p2_un else ""
        p1_str = f" (@{p1_clean})" if p1_clean else ""
        p2_str = f" (@{p2_clean})" if p2_clean else ""

        prepared_games.append({
            "match_id": cur_match.get("id"),
            "round_number": cur_match.get("round_number"),
            "division_id": cur_match.get("division_id") or division_id,
            "game_num": cur_match.get("game_num_in_series", idx + 1),
            "home_team": home_team,
            "away_team": away_team,
            "h_score": h_score,
            "a_score": a_score,
            "p1_username": p1_un,
            "p2_username": p2_un,
            "p1_str": p1_str,
            "p2_str": p2_str,
            "h_goals": h_goals,
            "a_goals": a_goals,
            "h_assists": h_assists,
            "a_assists": a_assists,
            "is_single_timeline": is_single_timeline,
            "events": events,
            "mvp_player": mvp_player,
            "reporter_id": user_id,
            "photo_id": photo_file_ids[idx] if idx < len(photo_file_ids) else (photo_file_ids[0] if photo_file_ids else None),
            "division_id": cur_match.get("division_id") or division_id
        })

    import uuid
    draft_uuid = str(uuid.uuid4())[:8]

    is_multi = len(prepared_games) > 1
    # Одна игра — все присланные скрины её (счёт, голы, статистика), и в пост
    # уходят все. В серии каждой игре достаётся её собственный скрин.
    if not is_multi and prepared_games:
        prepared_games[0]["photo_ids"] = list(dict.fromkeys(p for p in photo_file_ids if p))
    
    if not is_multi:
        g = prepared_games[0]
        draft_data = {
            "is_multi": False,
            "match_id": g["match_id"],
            "round_number": g["round_number"],
            "home_team": g["home_team"],
            "away_team": g["away_team"],
            "h_score": g["h_score"],
            "a_score": g["a_score"],
            "p1_username": g["p1_username"],
            "p2_username": g["p2_username"],
            "h_goals": g["h_goals"],
            "a_goals": g["a_goals"],
            "h_assists": g["h_assists"],
            "a_assists": g["a_assists"],
            "is_single_timeline": g["is_single_timeline"],
            "events": g["events"],
            "mvp_player": g.get("mvp_player"),
            "reporter_id": g["reporter_id"],
            "photo_id": g["photo_id"],
            "division_id": g.get("division_id"),
            "games": prepared_games
        }
        group_text = build_formatted_match_post(
            round_number=g["round_number"],
            home_team=g["home_team"],
            away_team=g["away_team"],
            h_score=g["h_score"],
            a_score=g["a_score"],
            p1_username=g["p1_username"],
            p2_username=g["p2_username"],
            h_goals=g["h_goals"],
            a_goals=g["a_goals"],
            h_assists=g["h_assists"],
            a_assists=g["a_assists"],
            is_single_timeline=g["is_single_timeline"],
            is_pm=False,
            match_id=g["match_id"],
            is_draft=True,
            mvp_player=g.get("mvp_player")
        )
    else:
        draft_data = {
            "is_multi": True,
            "games": prepared_games
        }
        
        post_lines = ["📝 <b>ЧЕРНОВИК РЕЗУЛЬТАТОВ МАТЧЕЙ</b>\n"]

        def _fmt(data):
            if not data: return ""
            return ", ".join([f"{p} ({c})" if c > 1 else f"{p} (1)" for p, c in data.items() if c > 0])

        for g in prepared_games:
            h_team_esc = html.escape(g["home_team"])
            a_team_esc = html.escape(g["away_team"])
            
            post_lines.append(f"🏟 <b>Игра {g['game_num']}:</b>")
            post_lines.append(f"🏠 <b>{h_team_esc}</b>{g['p1_str']} <b>{g['h_score']} : {g['a_score']}</b> <b>{a_team_esc}</b>{g['p2_str']} ✈️")
            
            h_g_str = _fmt(g["h_goals"])
            a_g_str = _fmt(g["a_goals"])
            h_a_str = _fmt(g["h_assists"])
            a_a_str = _fmt(g["a_assists"])
            
            if g["h_score"] > 0:
                post_lines.append(f"⚽ <b>Голы ({h_team_esc}):</b> {html.escape(h_g_str) if h_g_str else 'не указаны'}")
                if not g["is_single_timeline"]:
                    post_lines.append(f"🎯 <b>Ассисты ({h_team_esc}):</b> {html.escape(h_a_str) if h_a_str else 'Нет'}")
            if g["a_score"] > 0:
                post_lines.append(f"⚽ <b>Голы ({a_team_esc}):</b> {html.escape(a_g_str) if a_g_str else 'не указаны'}")
                if not g["is_single_timeline"]:
                    post_lines.append(f"🎯 <b>Ассисты ({a_team_esc}):</b> {html.escape(a_a_str) if a_a_str else 'Нет'}")
            # 👑 Показываем только распознанную золотую корону; серую OCR отбрасывает сам.
            if g.get("mvp_player"):
                post_lines.append(f"👑 <b>MVP матча:</b> {html.escape(str(g['mvp_player']))}")
            post_lines.append("")

        post_lines.append("\n⏳ <i>Ожидает подтверждения администратором...</i>")
        group_text = "\n".join(post_lines)

    dropped_games = len(matches_list) - len(prepared_games)
    if dropped_games > 0:
        group_text += (
            f"\n\n⚠️ <i>ИИ распознал игр: {len(matches_list)}, "
            f"но свободных матчей в расписании только {len(prepared_games)}. "
            f"Лишние игры не занесены.</i>"
        )

    if "drafts" not in context.bot_data:
        context.bot_data["drafts"] = {}
    context.bot_data["drafts"][draft_uuid] = draft_data
    try:
        await asyncio.to_thread(database.save_draft, draft_uuid, draft_data)
    except Exception as e:
        logger.warning(f"Failed to persist draft {draft_uuid} to SQLite: {e}")

    btn_label = "✅ Подтвердить все игры" if is_multi else "✅ Подтвердить"
    keyboard = [
        [InlineKeyboardButton(btn_label, callback_data=f"draft_conf_{draft_uuid}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"draft_rej_{draft_uuid}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    
    await status_msg.delete()
    
    photo_id_to_send = photo_file_ids[0] if photo_file_ids else None
    try:
        kwargs = {"chat_id": update.effective_chat.id, "parse_mode": "HTML", "reply_markup": markup}
        if msg_ids:
            kwargs["reply_to_message_id"] = msg_ids[0]
            
        if photo_id_to_send and len(group_text) <= 1024:
            try:
                kwargs["photo"] = photo_id_to_send
                kwargs["caption"] = group_text
                await context.bot.send_photo(**kwargs)
            except Exception as e:
                logger.warning(f"Failed to send draft photo preview ({e}), falling back to text message")
                kwargs.pop("photo", None)
                kwargs.pop("caption", None)
                kwargs["text"] = group_text
                await context.bot.send_message(**kwargs)
        else:
            kwargs["text"] = group_text
            await context.bot.send_message(**kwargs)
    except Exception as e:
        logger.exception("Failed to send draft preview")

from telegram.ext import CallbackQueryHandler
from handlers.admin import is_admin
from handlers.base import is_global_admin, resolve_post_target, send_result_post, unique_photo_ids


def _draft_division_ids(draft: dict) -> set[int]:
    """Дивизионы всех игр черновика (пусто — легаси-черновик без дивизиона)."""
    games = draft.get("games") or [draft]
    return {int(g["division_id"]) for g in games if g.get("division_id")}


async def _can_manage_draft(user_id: int, draft: dict) -> bool:
    """
    Черновик дивизиона может подтвердить/отклонить только супер-админ или
    админ этого дивизиона: is_admin() истинен для админа ЛЮБОГО дивизиона.
    """
    if is_global_admin(user_id):
        return True
    div_ids = _draft_division_ids(draft)
    if not div_ids:
        return is_admin(user_id)  # легаси-черновик вне дивизионов
    for div_id in div_ids:
        if not await asyncio.to_thread(database.is_division_admin, user_id, div_id):
            return False
    return True


async def cb_draft_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("Только администратор может подтверждать черновики!", show_alert=True)
        return

    draft_uuid = query.data.replace("draft_conf_", "")
    drafts = context.bot_data.get("drafts", {})
    draft = drafts.get(draft_uuid)
    if not draft:
        draft = await asyncio.to_thread(database.get_draft, draft_uuid)
        if draft:
            if "drafts" not in context.bot_data:
                context.bot_data["drafts"] = {}
            context.bot_data["drafts"][draft_uuid] = draft
            drafts = context.bot_data["drafts"]

    if not draft:
        if query.message.photo: await query.edit_message_caption(caption="❌ Данные черновика устарели или не найдены.")
        else: await query.edit_message_text(text="❌ Данные черновика устарели или не найдены.")
        return
        
    # Черновик забираем только после успешного сохранения: иначе неудачное
    # подтверждение оставляло бы админа без данных и без возможности повторить.
    if not await _can_manage_draft(query.from_user.id, draft):
        await query.answer("⛔ У вас нет прав на дивизион этого черновика!", show_alert=True)
        return

    games = draft.get("games", [draft])
    # Снимаем до цикла: при частичном провале draft["games"] переписывается.
    draft_division_ids = _draft_division_ids(draft)

    failed_games = []

    for idx, g in enumerate(games):
        _m_row = None
        m_id = g.get("match_id")
        if not m_id:
            logger.error(f"Could not resolve match_id for game {idx+1}")
            failed_games.append((idx, g, "матч не найден в расписании"))
            continue

        try:
            await asyncio.to_thread(
                database.confirm_and_finalize_match,
                m_id, g["h_score"], g["a_score"], g["events"],
                reporter_id=g["reporter_id"], photo_id=g["photo_id"],
                mvp_player=g.get("mvp_player")
            )
        except Exception as e:
            # Пост в РЕЗУЛЬТАТЫ обязан следовать за записью в базу, а не идти
            # параллельно ей: иначе результат «есть» в топике и отсутствует в таблице.
            logger.exception(f"Failed to confirm match {m_id}")
            failed_games.append((idx, g, str(e) or e.__class__.__name__))
            continue

        try:
            # Reward players with -1 warn if this was an overdue debt match
            try:
                from handlers.cabinet import handle_debt_played_rewards
                await handle_debt_played_rewards(
                    context, m_id, g.get('round_number', 0), g.get('player1_id'), g.get('player2_id')
                )
            except Exception as e:
                logger.warning(f"Failed to handle debt rewards in draft confirm: {e}")

            # Ставки Logovo.bet уже рассчитаны внутри confirm_and_finalize_match,
            # а уведомления о выигрыше/возврате поставлены в очередь той же
            # транзакцией — их доставляет process_notification_queue_job.
        except Exception as e:
            # Матч уже сохранён — побочные эффекты не повод отменять публикацию.
            logger.warning(f"Post-confirm side effects failed for match {m_id}: {e}")

        official_text = build_formatted_match_post(
            round_number=g.get('round_number'),
            home_team=g.get('home_team'),
            away_team=g.get('away_team'),
            h_score=g.get('h_score'),
            a_score=g.get('a_score'),
            p1_username=g.get('p1_username'),
            p2_username=g.get('p2_username'),
            h_goals=g.get('h_goals'),
            a_goals=g.get('a_goals'),
            h_assists=g.get('h_assists'),
            a_assists=g.get('a_assists'),
            is_single_timeline=g.get('is_single_timeline', False),
            is_pm=False,
            match_id=m_id,
            is_draft=False,
            mvp_player=g.get('mvp_player')
        )

        # Append "debt closed" note when the match was an overdue debt
        try:
            from handlers.cabinet import build_debt_footer
            _m_row = await asyncio.to_thread(database.get_match, m_id)
            if _m_row:
                official_text += await build_debt_footer(_m_row)
        except Exception as e:
            logger.warning(f"Failed to build debt footer for match {m_id}: {e}")

        div_id = g.get("division_id")
        if not div_id and _m_row:
            div_id = _m_row.get("division_id")
        from constants import CUP_DIVISION_SENTINEL
        if (g.get("tournament_type") == "cup" or (_m_row and _m_row.get("tournament_type") == "cup")) and (div_id is None or div_id == CUP_DIVISION_SENTINEL):
            div_id = CUP_DIVISION_SENTINEL
        target = await resolve_post_target(
            div_id, "results", "reports",
            legacy_topic_keys=("results_topic_id", "reports_topic_id"),
        )

        if target:
            try:
                # Черновики, сохранённые до появления `photo_ids`, несут один `photo_id`.
                await send_result_post(
                    context.bot, target, official_text,
                    unique_photo_ids(g.get("photo_ids") or [], g.get("photo_id")),
                )
            except Exception as e:
                logger.error(f"Failed to send match post to group: {e}")

    admin_name = f"@{query.from_user.username}" if query.from_user.username else (query.from_user.first_name or "Администратор")
    original_text = query.message.caption if query.message.photo else query.message.text
    cleaned_text = (original_text or "").replace("⏳ <i>Ожидает подтверждения администратором...</i>", "").strip()
    # Отчёт о прошлой неудачной попытке не должен накапливаться при повторах.
    # message.text приходит без разметки, поэтому режем по «голому» маркеру.
    cleaned_text = cleaned_text.split("⚠️")[0].strip()

    if failed_games:
        # В черновике оставляем только несохранённые игры: повторное нажатие
        # доподтвердит их и не продублирует уже опубликованные.
        draft["games"] = [g for _, g, _ in failed_games]
        draft["is_multi"] = len(failed_games) > 1
        try:
            await asyncio.to_thread(database.save_draft, draft_uuid, draft)
        except Exception as e:
            logger.warning(f"Failed to update draft {draft_uuid} in SQLite: {e}")
        saved_count = len(games) - len(failed_games)
        fail_lines = "\n".join(
            f"• Игра {g.get('game_num', i + 1)} "
            f"({html.escape(str(g.get('home_team') or '?'))} — {html.escape(str(g.get('away_team') or '?'))}): "
            f"{html.escape(err)}"
            for i, g, err in failed_games
        )
        new_caption = (
            f"{cleaned_text}\n\n"
            f"⚠️ <b>Сохранено игр: {saved_count} из {len(games)}.</b>\n"
            f"Не занесены в базу:\n{fail_lines}\n\n"
            f"<i>Нажмите «Подтвердить» ещё раз или занесите результат вручную.</i>"
        )
        keep_markup = True
    else:
        drafts.pop(draft_uuid, None)
        try:
            await asyncio.to_thread(database.delete_draft, draft_uuid)
        except Exception as e:
            logger.warning(f"Failed to delete draft {draft_uuid} from SQLite: {e}")
        new_caption = f"{cleaned_text}\n\n✅ <b>Одобрено администратором {html.escape(admin_name)}.</b>"
        keep_markup = False

    if query.message.photo:
        try:
            if len(new_caption) <= 1024:
                await query.edit_message_caption(caption=new_caption, parse_mode="HTML", reply_markup=query.message.reply_markup if keep_markup else None)
            else:
                await query.edit_message_caption(caption=new_caption[:1015] + "...", parse_mode="HTML", reply_markup=query.message.reply_markup if keep_markup else None)
        except Exception:
            if not keep_markup:
                await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(new_caption, parse_mode="HTML")
    else:
        await query.edit_message_text(text=new_caption, parse_mode="HTML", reply_markup=query.message.reply_markup if keep_markup else None)


    from handlers.cabinet import refresh_league_table, refresh_debts_summary
    await refresh_debts_summary(context)
    for div_id in (draft_division_ids or {None}):
        await refresh_league_table(context, division_id=div_id)

async def cb_draft_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.answer("Только администратор может отклонять черновики!", show_alert=True)
        return
        
    draft_uuid = query.data.replace("draft_rej_", "")
    drafts = context.bot_data.get("drafts", {})
    draft = drafts.get(draft_uuid)
    if not draft:
        draft = await asyncio.to_thread(database.get_draft, draft_uuid)
        if draft:
            if "drafts" not in context.bot_data:
                context.bot_data["drafts"] = {}
            context.bot_data["drafts"][draft_uuid] = draft

    if draft is not None and not await _can_manage_draft(query.from_user.id, draft):
        await query.answer("⛔ У вас нет прав на дивизион этого черновика!", show_alert=True)
        return
    drafts.pop(draft_uuid, None)
    try:
        await asyncio.to_thread(database.delete_draft, draft_uuid)
    except Exception as e:
        logger.warning(f"Failed to delete rejected draft {draft_uuid} from SQLite: {e}")

    admin_name = f"@{query.from_user.username}" if query.from_user.username else (query.from_user.first_name or "Администратор")
    original_text = query.message.caption if query.message.photo else query.message.text
    cleaned_text = (original_text or "").replace("⏳ <i>Ожидает подтверждения администратором...</i>", "").strip()
    new_caption = f"{cleaned_text}\n\n❌ <b>Черновик отклонен администратором {html.escape(admin_name)}.</b>"
    if query.message.photo:
        try:
            if len(new_caption) <= 1024:
                await query.edit_message_caption(caption=new_caption, parse_mode="HTML")
            else:
                await query.edit_message_caption(caption=new_caption[:1015] + "...", parse_mode="HTML")
        except Exception:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(new_caption, parse_mode="HTML")
    else:
        await query.edit_message_text(text=new_caption, parse_mode="HTML")
