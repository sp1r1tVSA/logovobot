import os
import io
import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from telegram.ext import ContextTypes, ConversationHandler
import html
import database
from handlers.base import is_admin, resolve_division_target

import logging
logger = logging.getLogger(__name__)

import asyncio
import telegram.error
from telegram.error import Forbidden
from services.ai import ai_recognizer
import config
from config import MAX_WARNS_LIMIT
from services.graphics import player_card_generator
from services.graphics import club_card_generator
from services.graphics import club_schedule_generator

def check_group_card_access(update: Update) -> bool:
    """
    Check if the user is permitted to interact with club cards/catalogs in the current chat.
    In groups/supergroups/channels, only admins are permitted.
    In private chats, all users are permitted.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type in ("group", "supergroup", "channel"):
        if not user or not is_admin(user.id):
            return False
    return True

def match_squad_player_names(raw_players: list[str], squad_list: list[str]) -> dict[str, int]:
    counts = {}
    for raw in raw_players:
        matched_name = raw
        raw_lower = raw.lower().strip()
        for squad_player in squad_list:
            sp_lower = squad_player.lower().strip()
            if raw_lower in sp_lower or sp_lower in raw_lower:
                matched_name = squad_player
                break
        counts[matched_name] = counts.get(matched_name, 0) + 1
    return counts

def _normalize_name_translit(text: str) -> str:
    CYR_LAT_MAP = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
        'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
        'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'c',
        'ч': 'ch', 'ш': 'sh', 'щ': 'sh', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu',
        'я': 'ya', 'á': 'a', 'à': 'a', 'ã': 'a', 'â': 'a', 'é': 'e', 'ê': 'e', 'í': 'i',
        'ó': 'o', 'õ': 'o', 'ô': 'o', 'ú': 'u', 'ç': 'c', 'ø': 'o', 'æ': 'ae', 'ñ': 'n'
    }
    t = text.lower().strip()
    res = []
    for ch in t:
        res.append(CYR_LAT_MAP.get(ch, ch))
    return "".join(res)

def find_squad_match(raw_name, squad_list):
    """Resolve an OCR-read player name to the squad's own spelling (or None)."""
    if not raw_name:
        return None
    raw_lower = raw_name.lower().strip()
    raw_norm = _normalize_name_translit(raw_lower)
    for squad_p in squad_list:
        sp_lower = squad_p.lower().strip()
        sp_norm = _normalize_name_translit(sp_lower)
        # 1. Exact match (case / translit)
        if raw_lower == sp_lower or raw_norm == sp_norm:
            return squad_p
        # 2. Substring match only for meaningful length (>=4) to prevent false matches on short words
        if len(raw_lower) >= 4 and (raw_lower in sp_lower or sp_lower in raw_lower):
            return squad_p
        if len(raw_norm) >= 4 and (raw_norm in sp_norm or sp_norm in raw_norm):
            return squad_p
        # 3. Token-level matching
        raw_parts = [p for p in raw_norm.split() if len(p) >= 3]
        sp_parts = [p for p in sp_norm.split() if len(p) >= 3]
        if raw_parts and sp_parts:
            if any(p in sp_parts or any(p == spp for spp in sp_parts) for p in raw_parts):
                return squad_p
    return None


def resolve_mvp_player_name(raw_mvp: str | None, home_team: str | None, away_team: str | None) -> str | None:
    """Bring the crown name to the squad's spelling, the way goals and assists are.

    `matches.mvp_player` stores a bare name, so the MVP tables join it by name:
    an OCR form that differs from the declared squad («H. Kane» vs «Harry Kane»)
    splits one player's awards in two and leaves the club column empty. Both
    squads are searched — the crown may belong to either side — and a name that
    fits both is left as read, because guessing the club is worse than a raw name.
    """
    raw = (raw_mvp or "").strip()
    if not raw:
        return None
    home_match = find_squad_match(raw, database.get_squad(home_team) or []) if home_team else None
    away_match = find_squad_match(raw, database.get_squad(away_team) or []) if away_team else None
    if home_match and away_match and home_match != away_match:
        return raw
    return home_match or away_match or raw


def match_and_enrich_squad(raw_side1_goals: list[str], raw_side2_goals: list[str], raw_side1_assists: list[str], raw_side2_assists: list[str], home_team: str, away_team: str, is_single_timeline: bool = False):
    """
    Handles both screenshot formats:
    - Format A (Standard 2-column stats): side1 is left team, side2 is right team.
    - Format B (Vertical timeline list): all goals are pooled together and assigned to home_team vs away_team by matching each goalscorer against home_squad vs away_squad in DB!
    """
    home_squad = database.get_squad(home_team) or []
    away_squad = database.get_squad(away_team) or []

    # If single timeline had everything dumped into one list and second list is empty
    if is_single_timeline and (not raw_side1_goals or not raw_side2_goals) and (raw_side1_goals or raw_side2_goals):
        all_raw = raw_side1_goals + raw_side2_goals
        home_g = {}
        away_g = {}
        for raw in all_raw:
            raw_clean = raw.strip()
            if not raw_clean:
                continue
            h_match = find_squad_match(raw_clean, home_squad)
            a_match = find_squad_match(raw_clean, away_squad)
            if h_match and not a_match:
                home_g[h_match] = home_g.get(h_match, 0) + 1
            elif a_match and not h_match:
                away_g[a_match] = away_g.get(a_match, 0) + 1
            elif not raw_side1_goals and raw_side2_goals:
                use_name = a_match or raw_clean
                away_g[use_name] = away_g.get(use_name, 0) + 1
            else:
                use_name = h_match or raw_clean
                home_g[use_name] = home_g.get(use_name, 0) + 1
        return home_g, away_g, {}, {}, True

    side1_all = raw_side1_goals + raw_side1_assists
    side2_all = raw_side2_goals + raw_side2_assists

    side1_home_matches = sum(1 for p in side1_all if find_squad_match(p, home_squad))
    side1_away_matches = sum(1 for p in side1_all if find_squad_match(p, away_squad))
    side2_home_matches = sum(1 for p in side2_all if find_squad_match(p, home_squad))
    side2_away_matches = sum(1 for p in side2_all if find_squad_match(p, away_squad))

    # Detect if side1 is Away and side2 is Home
    if (side1_away_matches > side1_home_matches) or (side2_home_matches > side2_away_matches):
        is_side1_home = False
        side1_team, side2_team = away_team, home_team
        side1_squad, side2_squad = away_squad, home_squad
    elif (side1_home_matches > side1_away_matches) or (side2_away_matches > side2_home_matches):
        is_side1_home = True
        side1_team, side2_team = home_team, away_team
        side1_squad, side2_squad = home_squad, away_squad
    else:
        is_side1_home = True
        side1_team, side2_team = home_team, away_team
        side1_squad, side2_squad = home_squad, away_squad

    def process_side_events(raw_list, this_squad, opp_squad):
        counts = {}
        for raw in raw_list:
            raw_clean = raw.strip()
            if not raw_clean:
                continue
            matched_name = find_squad_match(raw_clean, this_squad)
            if matched_name:
                counts[matched_name] = counts.get(matched_name, 0) + 1
            else:
                # If player does NOT belong to this team's squad, check if they belong to opponent squad
                if opp_squad and find_squad_match(raw_clean, opp_squad):
                    # Cross-column OCR bleed detected! Do not assign opponent player to this team
                    continue
                # If player is in neither squad (e.g. unregistered player or bench sub), keep raw name
                counts[raw_clean] = counts.get(raw_clean, 0) + 1
        return counts

    side1_goals = process_side_events(raw_side1_goals, side1_squad, side2_squad)
    side2_goals = process_side_events(raw_side2_goals, side2_squad, side1_squad)
    side1_assists = process_side_events(raw_side1_assists, side1_squad, side2_squad)
    side2_assists = process_side_events(raw_side2_assists, side2_squad, side1_squad)

    if is_side1_home:
        return side1_goals, side2_goals, side1_assists, side2_assists, True
    else:
        return side2_goals, side1_goals, side2_assists, side1_assists, False

def safe_escape(val: str | None, default: str = "") -> str:
    """Safe HTML escaping for strings that may be None."""
    if val is None:
        return html.escape(default)
    return html.escape(str(val))

async def safe_query_answer(query, text: str | None = None, show_alert: bool = False) -> None:
    if not query:
        return
    try:
        if text:
            await query.answer(text, show_alert=show_alert)
        else:
            await query.answer()
    except Exception as e:
        logger.debug(f"Ignored expired query answer: {e}")

async def safe_send_notification(bot, chat_id: int, text: str, reply_markup=None, parse_mode: str = "HTML") -> bool:
    """
    Safely send messages with Telegram rate limit (RetryAfter) and Forbidden handling.
    A deactivated account also arrives as Forbidden — PTB has no separate class for it.
    If the markup fails to parse, the message is resent as plain text so it is not lost.

    Personal messages only: a non-positive chat_id is a pre-registered coach's
    temporary id (see database.pre_register_player) with no private chat yet, so
    it is skipped instead of failing with "Chat not found".
    """
    if chat_id is None or chat_id <= 0:
        logger.info(f"Skipping personal notification to pre-registered user {chat_id}")
        return False
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
        return True
    except telegram.error.Forbidden:
        logger.warning(f"User {chat_id} has blocked the bot (Forbidden).")
        return False
    except telegram.error.RetryAfter as e:
        logger.warning(f"Rate limited by Telegram API. Waiting {e.retry_after}s...")
        await asyncio.sleep(e.retry_after)
        return await safe_send_notification(bot, chat_id, text, reply_markup, parse_mode)
    except telegram.error.BadRequest as e:
        if parse_mode and "parse entities" in str(e).lower():
            logger.warning(f"Bad {parse_mode} markup in notification to {chat_id}, resending as plain text: {e}")
            return await safe_send_notification(bot, chat_id, text, reply_markup, parse_mode=None)
        logger.exception(f"Telegram error sending to {chat_id}")
        return False
    except telegram.error.TelegramError as e:
        logger.exception(f"Telegram error sending to {chat_id}")
        return False
    except Exception as e:
        logger.exception(f"Unexpected error sending to {chat_id}")
        return False

# Conversation states
GAME_NICKNAME, TEAM_NAME, LEAGUE_NAME, EDITING_FIELD = range(4)
WAITING_FOR_SCORE = 100
SQUAD_PHOTO = 101
REPORT_SCORE_PHOTO = 102
MATCH_CUSTOM_TIME = 104

async def show_cabinet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user profile info with stats, or access denied if not registered."""
    query = update.callback_query
    if query:
        await query.answer()

    if not check_group_card_access(update):
        if query:
            await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    user = update.effective_user
    if not user:
        return

    # Check if user has a club assigned
    team = await asyncio.to_thread(database.get_user_team, user.id)

    if not team:
        if is_admin(user.id):
            team = "Админ-Клуб (Тест)"
        else:
            # Not registered
            text = "⚠️ <b>Доступ ограничен</b>\n\nВы еще не зарегистрированы в системе лиги."
            keyboard = [[InlineKeyboardButton("« Назад в меню", callback_data="main_menu")]]
            markup = InlineKeyboardMarkup(keyboard)
            target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
            thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None
            if query and query.message and (query.message.photo or query.message.document):
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
            elif query:
                try:
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                except Exception:
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                    await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
            elif update.message:
                await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
            return

    # Get stats
    stats = await asyncio.to_thread(database.get_player_stats, user.id)
    username_display = f"@{html.escape(user.username)}" if user.username else html.escape(user.first_name)

    # Get warn count
    user_record = await asyncio.to_thread(database.get_user, user.id)
    warn_count = user_record['warn_count'] if user_record and user_record['warn_count'] else 0

    warn_line = f"\n⚠️ <b>Предупреждения:</b> {warn_count} / {MAX_WARNS_LIMIT}"

    div_line = ""
    if user_record and user_record['division_id']:
        div_row = await asyncio.to_thread(database.get_division, user_record['division_id'])
        if div_row:
            div_line = f"• <b>Дивизион:</b> {html.escape(div_row['name'])}\n"

    text = (
        f"👤 <b>Личный кабинет участника</b>\n\n"
        f"• <b>Telegram:</b> {username_display}\n"
        f"• <b>Игровой клуб:</b> {html.escape(team)}\n"
        f"{div_line}\n"
        f"📊 <b>Ваша статистика:</b>\n"
        f"• <b>Сыграно матчей:</b> {stats['played']}\n"
        f"• <b>Победы:</b> {stats['wins']} | <b>Ничьи:</b> {stats['draws']} | <b>Поражения:</b> {stats['losses']}\n"
        f"• <b>Забито/Пропущено:</b> {stats['goals_scored']} / {stats['goals_conceded']}\n"
        f"• <b>Очки:</b> {stats['points']}"
        f"{warn_line}"
    )

    keyboard = [
        [
            InlineKeyboardButton("🏛 Карточка клуба", callback_data="cb_my_club_card"),
            InlineKeyboardButton("📸 Состав", callback_data="cabinet_my_squad"),
        ],
        [
            InlineKeyboardButton("📋 Мои матчи", callback_data="cabinet_my_matches"),
            InlineKeyboardButton("📜 История игр", callback_data="cabinet_game_history"),
        ],
        [
            InlineKeyboardButton("⚽ Топ клуба", callback_data="cabinet_club_stats"),
            InlineKeyboardButton("🌍 Все клубы", callback_data="cb_clubs_catalog"),
        ],
        [InlineKeyboardButton("« Назад в меню", callback_data="main_menu")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query and query.message and (query.message.photo or query.message.document):
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
    elif query:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def show_club_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show top scorers and assistants for the user's club."""
    query = update.callback_query
    if query:
        await query.answer()

    if not check_group_card_access(update):
        if query:
            await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    user = update.effective_user
    if not user:
        return

    team = await asyncio.to_thread(database.get_user_team, user.id)
    keyboard = [[InlineKeyboardButton("« Назад в кабинет", callback_data="menu_cabinet")]]
    markup = InlineKeyboardMarkup(keyboard)

    if not team:
        text = "⚠️ Вы не привязаны к клубу."
        if query:
            await query.edit_message_text(text, reply_markup=markup)
        return

    scorers = await asyncio.to_thread(database.get_club_top_scorers, team)
    assisters = await asyncio.to_thread(database.get_club_top_assisters, team)

    text = f"⚽ <b>Статистика игроков клуба {html.escape(team)}:</b>\n"

    # Collect unique player names to add card buttons
    all_players: list[str] = []
    seen: set[str] = set()

    if scorers:
        text += "\n<b>🔥 Бомбардиры:</b>\n"
        for i, s in enumerate(scorers, 1):
            text += f"{i}. {html.escape(s['player_name'])} — {s['total']} ⚽\n"
            if s['player_name'] not in seen:
                all_players.append(s['player_name'])
                seen.add(s['player_name'])
    else:
        text += "\n<i>Пока нет данных о голах.</i>\n"

    if assisters:
        text += "\n<b>👟 Ассистенты:</b>\n"
        for i, a in enumerate(assisters, 1):
            text += f"{i}. {html.escape(a['player_name'])} — {a['total']} 🅰️\n"
            if a['player_name'] not in seen:
                all_players.append(a['player_name'])
                seen.add(a['player_name'])
    else:
        text += "\n<i>Пока нет данных о передачах.</i>\n"

    context.user_data["club_stats_team"] = team
    context.user_data["club_stats_players"] = all_players

    # Build inline keyboard: safe short callback_data (pcard_{idx}) to avoid Telegram 64-byte limit
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, pname in enumerate(all_players):
        cb = f"pcard_{idx}"
        btn = InlineKeyboardButton(f"👤 {pname}", callback_data=cb)
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if all_players:
        text += "\n<i>Нажми на игрока, чтобы открыть его карточку:</i>"

    buttons.append([InlineKeyboardButton("« Назад в кабинет", callback_data="menu_cabinet")])
    markup = InlineKeyboardMarkup(buttons)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query and query.message and (query.message.photo or query.message.caption):
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
    elif query:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def show_player_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a player stats card image for the given player."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not check_group_card_access(update):
        await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    data = query.data or ""
    player_name = None
    team_name = context.user_data.get("club_stats_team")

    if data.startswith("pcard_"):
        try:
            idx = int(data.replace("pcard_", ""))
            players_list = context.user_data.get("club_stats_players", [])
            if 0 <= idx < len(players_list):
                player_name = players_list[idx]
        except ValueError:
            pass
    elif data.startswith("player_card_"):
        payload = data[len("player_card_"):]
        if "|" in payload:
            try:
                team_name, player_name = payload.split("|", 1)
            except ValueError:
                pass
        else:
            player_name = payload

    if not player_name:
        await query.answer("Ошибка: не удалось распознать игрока.", show_alert=True)
        return

    if not team_name:
        user = await asyncio.to_thread(database.get_user, query.from_user.id)
        if user and user.get("team_name"):
            team_name = user["team_name"]
        else:
            team_name = "—"

    back_cb = context.user_data.get("club_stats_back_cb") or (f"clsquad_{team_name}" if team_name and team_name != "—" else "cabinet_club_stats")
    keyboard = [[InlineKeyboardButton("« Назад", callback_data=back_cb)]]
    markup = InlineKeyboardMarkup(keyboard)

    # Fetch stats from DB in a thread
    stats = await asyncio.to_thread(database.get_player_card_stats, player_name, team_name)

    # Generate image in a thread
    buf = await asyncio.to_thread(player_card_generator.generate_player_card, stats)

    pts = stats.get("total_points", stats["total_goals"] + stats["total_assists"])
    caption = (
        f"🃏 <b>{html.escape(player_name)}</b> · {html.escape(team_name)}\n"
        f"⚽ <b>{stats['total_goals']}</b> голов\n"
        f"🅰️ <b>{stats['total_assists']}</b> ассистов\n"
        f"🔥 <b>{pts}</b> очков (Г+П)"
    )

    target_chat_id = query.message.chat_id if query.message else (update.effective_chat.id if update.effective_chat else query.from_user.id)
    thread_id = query.message.message_thread_id if query.message and query.message.is_topic_message else None

    if query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    await context.bot.send_photo(
        chat_id=target_chat_id,
        message_thread_id=thread_id,
        photo=buf,
        caption=caption,
        parse_mode="HTML",
        reply_markup=markup,
    )


async def send_or_edit_club_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, team_name: str, back_cb: str | None = None) -> None:
    """Send or edit high-res graphic schedule and results card for a club."""
    query = update.callback_query
    msg = update.effective_message
    chat = update.effective_chat

    canon = database.resolve_team_name(team_name) or team_name
    schedule_data = await asyncio.to_thread(database.get_club_schedule_and_results, canon, 12)
    buf = await asyncio.to_thread(club_schedule_generator.generate_club_schedule, schedule_data)

    target_back = back_cb or f"view_club_{canon}"
    keyboard = [
        [
            InlineKeyboardButton("🏛 Карточка", callback_data=f"view_club_{canon}"),
            InlineKeyboardButton("👥 Состав", callback_data=f"clsquad_{canon}"),
        ],
        [
            InlineKeyboardButton("🌍 Все клубы", callback_data="cb_clubs_catalog"),
            InlineKeyboardButton("« В кабинет", callback_data="menu_cabinet"),
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    caption = f"📅 <b>МАТЧИ И РАСПИСАНИЕ: {html.escape(canon.upper())}</b>"

    if query:
        if query.message:
            try:
                await query.message.delete()
            except Exception:
                pass
        target_chat_id = query.message.chat_id if query.message else (chat.id if chat else update.effective_user.id)
        thread_id = query.message.message_thread_id if query.message and query.message.is_topic_message else None
        await context.bot.send_photo(
            chat_id=target_chat_id,
            message_thread_id=thread_id,
            photo=buf,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup
        )
    elif msg:
        await msg.reply_photo(
            photo=buf,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup
        )
    elif chat:
        await context.bot.send_photo(
            chat_id=chat.id,
            photo=buf,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup
        )


async def show_my_matches_stub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show schedule/matches for user's club."""
    await show_game_history_stub(update, context)


AVATARS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "avatars")
_user_avatar_file_ids: dict[int, str] = {}


async def get_cached_or_fetch_user_avatar(bot, user_id: int | None) -> str | None:
    """
    Fetch the latest user avatar from Telegram API every time.
    Downloads and updates local cache if file_id changed or file missing.
    Returns path to avatar image or None if user has no avatar.
    """
    if not user_id or not bot:
        return None
    try:
        os.makedirs(AVATARS_DIR, exist_ok=True)
        local_path = os.path.join(AVATARS_DIR, f"{user_id}.jpg")

        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if photos and photos.total_count > 0 and len(photos.photos) > 0 and len(photos.photos[0]) > 0:
            best_photo = photos.photos[0][-1]
            cur_file_id = best_photo.file_id

            # If already downloaded and file exists on disk, reuse fast
            if _user_avatar_file_ids.get(user_id) == cur_file_id and os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                return local_path

            # Download fresh avatar from Telegram
            file_obj = await bot.get_file(cur_file_id)
            buf = io.BytesIO()
            await file_obj.download_to_memory(buf)
            buf.seek(0)
            with open(local_path, "wb") as f:
                f.write(buf.getvalue())
            _user_avatar_file_ids[user_id] = cur_file_id
            return local_path
        else:
            # User has no avatar on TG (or removed it)
            _user_avatar_file_ids.pop(user_id, None)
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            return None
    except Exception as e:
        logger.debug(f"Could not fetch avatar for user {user_id}: {e}")
        # In case of network/rate-limit error, fallback to existing local avatar if present
        local_path = os.path.join(AVATARS_DIR, f"{user_id}.jpg")
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return local_path
    return None


async def send_or_edit_club_card(update: Update, context: ContextTypes.DEFAULT_TYPE, team_name: str, back_cb: str = "cb_clubs_catalog") -> None:
    """Send or edit the high-res graphic club card with compact inline keyboard and no wall of text."""
    query = update.callback_query
    msg = update.effective_message
    chat = update.effective_chat

    canon = database.resolve_team_name(team_name) or team_name
    card_data = await asyncio.to_thread(database.get_club_card_data, canon)
    
    # Fetch owner avatar if manager is assigned
    mgr = card_data.get("manager")
    mgr_id = mgr.get("telegram_id") if mgr else None
    avatar_path = await get_cached_or_fetch_user_avatar(context.bot, mgr_id) if mgr_id else None

    buf = await asyncio.to_thread(club_card_generator.generate_club_card, card_data, avatar_path)

    # Compact inline keyboard (2 buttons per row, minimal labels)
    keyboard = [
        [
            InlineKeyboardButton("👥 Состав", callback_data=f"clsquad_{canon}"),
            InlineKeyboardButton("📅 Матчи", callback_data=f"clhist_{canon}"),
        ],
        [
            InlineKeyboardButton("🌍 Все клубы", callback_data="cb_clubs_catalog"),
            InlineKeyboardButton("« Назад", callback_data=back_cb),
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    caption = f"🏛 <b>{html.escape(canon.upper())}</b>"

    if query:
        if query.message:
            try:
                await query.message.delete()
            except Exception:
                pass
        target_chat_id = query.message.chat_id if query.message else (chat.id if chat else update.effective_user.id)
        thread_id = query.message.message_thread_id if query.message and query.message.is_topic_message else None
        await context.bot.send_photo(
            chat_id=target_chat_id,
            message_thread_id=thread_id,
            photo=buf,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup
        )
    elif msg:
        await msg.reply_photo(
            photo=buf,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup
        )
    elif chat:
        await context.bot.send_photo(
            chat_id=chat.id,
            photo=buf,
            caption=caption,
            parse_mode="HTML",
            reply_markup=markup
        )


async def show_my_club_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the graphic club card of the current user."""
    query = update.callback_query
    if query:
        await query.answer()

    if not check_group_card_access(update):
        if query:
            await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    user = update.effective_user
    if not user:
        return

    team = await asyncio.to_thread(database.get_user_team, user.id)
    if not team:
        if is_admin(user.id):
            await show_clubs_catalog(update, context)
            return
        text = "⚠️ Вы не привязаны ни к одному клубу лиги."
        keyboard = [[InlineKeyboardButton("« В кабинет", callback_data="menu_cabinet")]]
        if query and query.message:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        elif update.message:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await send_or_edit_club_card(update, context, team, back_cb="menu_cabinet")


async def show_specific_club_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show specific club card by callback (view_club_<name>)."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not check_group_card_access(update):
        await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    raw_team = query.data.replace("view_club_", "")
    canon = database.resolve_team_name(raw_team) or raw_team
    div_id = None
    with database.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT division_id FROM users WHERE LOWER(team_name) = LOWER(?) AND division_id IS NOT NULL LIMIT 1", (canon.strip(),))
        row = c.fetchone()
        if row:
            div_id = row["division_id"]
    back_cb = f"clubs_catalog_div:{div_id}" if div_id else "cb_clubs_catalog"
    await send_or_edit_club_card(update, context, canon, back_cb=back_cb)


async def show_club_graphic_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias for viewing club card."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not check_group_card_access(update):
        await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    team_name = query.data.replace("img_club_", "")
    await send_or_edit_club_card(update, context, team_name, back_cb="cb_clubs_catalog")


async def show_club_squad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display full squad with individual goals & assists and interactive player card buttons."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not check_group_card_access(update):
        await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    team_name = query.data.replace("clsquad_", "")
    canon = database.resolve_team_name(team_name) or team_name
    squad_stats = await asyncio.to_thread(database.get_club_squad_stats, canon)

    text = (
        f"👥 <b>СОСТАВ И СТАТИСТИКА ИГРОКОВ: {html.escape(canon.upper())}</b>\n\n"
    )

    if not squad_stats:
        text += "<i>В клубе пока нет зарегистрированных игроков.</i>\n"
    else:
        for idx, p in enumerate(squad_stats, 1):
            p_name = html.escape(p["player_name"])
            g = p["goals"]
            a = p["assists"]
            reg_badge = "" if p["is_registered"] else " <i>(вне заявки)</i>"
            text += f"<b>{idx}.</b> <b>{p_name}</b> — <b>{g}</b> ⚽ · <b>{a}</b> 🅰️{reg_badge}\n"

    # Player card buttons
    context.user_data["club_stats_team"] = canon
    context.user_data["club_stats_players"] = [p["player_name"] for p in squad_stats]

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, p in enumerate(squad_stats[:18]):
        btn = InlineKeyboardButton(f"👤 {p['player_name']}", callback_data=f"pcard_{idx}")
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("« К карточке клуба", callback_data=f"view_club_{canon}")])
    markup = InlineKeyboardMarkup(buttons)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else query.from_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
    else:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)


async def show_club_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display visual match schedule and history for a specific club."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not check_group_card_access(update):
        await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    team_name = query.data.replace("clhist_", "")
    await send_or_edit_club_schedule(update, context, team_name, back_cb=f"view_club_{team_name}")


async def show_game_history_stub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show game history for the user's club."""
    query = update.callback_query
    if query:
        await query.answer()

    if not check_group_card_access(update):
        if query:
            await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    user = update.effective_user
    if not user:
        return
    team = await asyncio.to_thread(database.get_user_team, user.id)
    if not team:
        await show_clubs_catalog(update, context)
        return
    await send_or_edit_club_schedule(update, context, team, back_cb="menu_cabinet")


async def show_clubs_catalog_divisions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 1: Display list of active divisions to select for clubs catalog."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    if not check_group_card_access(update):
        if query:
            await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    divisions = await asyncio.to_thread(database.get_active_divisions)

    text = (
        "🌍 <b>КАТАЛОГ КЛУБОВ ПО ДИВИЗИОНАМ</b>\n\n"
        "Выберите дивизион для просмотра клубов, их статистики, составов и формы:\n"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    if divisions:
        for d in divisions:
            d_name = d.get("name") or f"Дивизион {d['id']}"
            buttons.append([InlineKeyboardButton(f"🏆 {d_name}", callback_data=f"clubs_catalog_div:{d['id']}")])
    else:
        text = "🌍 <b>КАТАЛОГ КЛУБОВ</b>\n\n<i>В текущем сезоне пока нет активных дивизионов.</i>"

    buttons.append([InlineKeyboardButton("« Назад в меню", callback_data="main_menu")])
    markup = InlineKeyboardMarkup(buttons)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query:
        if query.message and query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
        else:
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

# Backward-compatible alias
show_clubs_catalog = show_clubs_catalog_divisions


async def show_clubs_catalog_for_division(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 2: Display interactive grid of clubs belonging to the chosen division."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    if not check_group_card_access(update):
        if query:
            await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    data = query.data if query else ""
    div_id = int(data.split(":")[1]) if ":" in data else 1

    div_info = await asyncio.to_thread(database.get_division, div_id)
    div_name = div_info.get("name") if div_info else f"Дивизион {div_id}"

    clubs = await asyncio.to_thread(database.get_clubs_summary_for_division, div_id)

    text = (
        f"🌍 <b>КАТАЛОГ КЛУБОВ — {html.escape(div_name.upper())}</b>\n\n"
        "Выберите клуб для просмотра полной клубной карточки, статистики, формы и состава:\n"
    )
    if not clubs:
        text += "\n<i>В этом дивизионе пока нет зарегистрированных клубов.</i>\n"

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for c in clubs:
        t_name = c["team_name"]
        rank_p = f"#{c['rank']} " if c["rank"] > 0 else ""
        btn_title = f"{rank_p}{t_name}"
        btn = InlineKeyboardButton(btn_title, callback_data=f"view_club_{t_name}")
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("« Назад к дивизионам", callback_data="cb_clubs_catalog")])
    markup = InlineKeyboardMarkup(buttons)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query:
        if query.message and query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
        else:
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def club_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /club [team_name] command."""
    chat = update.effective_chat
    user = update.effective_user
    user_id = user.id if user else 0
    if chat and chat.type in ("group", "supergroup", "channel") and not is_admin(user_id):
        bot_me = await context.bot.get_me()
        bot_username = bot_me.username or "logovobot"
        if update.message:
            await update.message.reply_text(
                f"ℹ️ Просмотр карточек клубов доступен в личном кабинете бота: @{bot_username}\n"
                f"В общем чате эта команда доступна только администраторам.",
                parse_mode="HTML"
            )
        return

    args = context.args or []
    if not args:
        team = await asyncio.to_thread(database.get_user_team, user_id) if user else None
        if team:
            await send_or_edit_club_card(update, context, team, back_cb="cb_clubs_catalog")
        else:
            await show_clubs_catalog(update, context)
        return

    req_team = " ".join(args).strip()
    canon = database.resolve_team_name(req_team)
    if not canon:
        if update.message:
            await update.message.reply_text(
                f"❌ Клуб «{html.escape(req_team)}» не найден.\n"
                f"Используйте команду <code>/club</code> без параметров, чтобы открыть каталог всех клубов.",
                parse_mode="HTML"
            )
        return

    await send_or_edit_club_card(update, context, canon, back_cb="cb_clubs_catalog")

def is_valid_name(text: str) -> bool:
    """Validate entered text."""
    system_buttons = ["👤 Мой кабинет", "📊 Рейтинги", "💬 Поддержка", "⚙️ Админ-панель", "Отмена", "/cancel", "/start"]
    if text in system_buttons:
        return False
    if len(text) < 2 or len(text) > 50:
        return False
    if text.startswith("/"):
        return False
    return True

async def show_edit_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder."""
    pass

# --- Full Registration Flow (Only asks for Club) ---

async def start_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inform players that self-registration is disabled and only admin can register them."""
    query = update.callback_query
    text = (
        "ℹ️ **Регистрация участников**\n\n"
        "В нашей лиге регистрация участников производится только администратором.\n"
        "Обратитесь к организатору турнира для привязки вашего клуба."
    )
    keyboard = [[InlineKeyboardButton("« Назад в меню", callback_data="main_menu")]]
    markup = InlineKeyboardMarkup(keyboard)
    if query:
        await query.answer()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    return ConversationHandler.END

async def reg_team_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture team name, save profile to DB and finish."""
    text = update.message.text.strip()
    if not is_valid_name(text):
        await update.message.reply_text("❌ Пожалуйста, введите корректное название команды (без команд и системных кнопок):")
        return TEAM_NAME

    user = update.effective_user
    if not user:
        return ConversationHandler.END

    # Update DB profile
    await asyncio.to_thread(database.update_profile, user.id, text, "Основная")

    await update.message.reply_text(
        "🎉 **Регистрация успешно завершена!**",
        parse_mode="Markdown"
    )
    
    # Send updated profile view
    await show_cabinet(update, context)
    return ConversationHandler.END

# --- Selective Field Editing Flow ---

async def start_selective_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initiate selective edit for the club name."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
        
    context.user_data["edit_field"] = "team_name"

    await query.edit_message_text(
        "✏️ Введите новое название вашего **клуба / команды**:\n\n*(Отправить /cancel для отмены)*",
        parse_mode="Markdown"
    )
    return EDITING_FIELD

async def save_selective_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the updated club name to the database."""
    if not update.message or (update.effective_chat and update.effective_chat.type != "private"):
        return ConversationHandler.END
    text = update.message.text.strip()
    user = update.effective_user

    if not is_valid_name(text):
        await update.message.reply_text("❌ Недопустимое значение. Пожалуйста, введите корректный текст:")
        return EDITING_FIELD

    # Update DB
    await asyncio.to_thread(database.update_single_field, user.id, "team_name", text)
    context.user_data.pop("edit_field", None)

    await update.message.reply_text("✅ Клуб успешно изменен!")
    
    # Show updated cabinet view
    await show_cabinet(update, context)
    return ConversationHandler.END

async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Abort registration or selective edit conversation."""
    context.user_data.pop("edit_field", None)
    
    msg_text = "Регистрация / редактирование отменено."
    if update.message:
        await update.message.reply_text(msg_text)
        await show_cabinet(update, context)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg_text)
        await show_cabinet(update, context)
        
    return ConversationHandler.END

async def safe_edit_or_reply(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode: str = "HTML") -> None:
    """Safely edit a message whether it's a text message or photo message, preserving group/topic context."""
    if not query:
        return
    
    target_chat_id = query.message.chat_id if query.message else query.from_user.id
    thread_id = query.message.message_thread_id if query.message and query.message.is_topic_message else None

    # 1. Проверяем, содержит ли исходное сообщение медиафайл (фотография)
    has_photo = bool(query.message and (query.message.photo or query.message.caption or query.message.document))

    if has_photo:
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug(f"Could not delete photo message: {e}")
        await context.bot.send_message(
            chat_id=target_chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    else:
        try:
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            err_msg = str(e).lower()
            if "there is no text in the message to edit" in err_msg or "message is not modified" in err_msg:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup
                )
            else:
                logger.warning(f"safe_edit_or_reply BadRequest: {e}")
        except Exception as e:
            logger.warning(f"edit_message_text failed ({e}). Falling back to delete & send_message...")
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=target_chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )

# --- Placeholders ---

async def show_my_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not check_group_card_access(update):
        await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return

    user_id = query.from_user.id
    matches = await asyncio.to_thread(database.get_pending_matches, user_id)
    
    text = "📋 **Ваши открытые матчи:**\n\n"
    keyboard = []
    
    if not matches:
        text += "У вас нет несыгранных матчей на данный момент."
    else:
        text += "Выберите матч для просмотра и ввода результата:"
        for m in matches:
            opp = m['opponent_team'] or m['opponent_username'] or "Соперник"
            btn_text = f"⚽ Тур {m.get('round_number', '?')}: 🆚 {opp}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"cabinet_view_match_{m['id']}")])
            
    keyboard.append([InlineKeyboardButton("« Назад в кабинет", callback_data="menu_cabinet")])
    markup = InlineKeyboardMarkup(keyboard)
    await safe_edit_or_reply(query, context, text, parse_mode="Markdown", reply_markup=markup)

async def cabinet_view_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    
    match_id = int(query.data.replace("cabinet_view_match_", ""))
    user_id = query.from_user.id
    
    m = await asyncio.to_thread(database.get_match, match_id)
    if not m:
        await safe_edit_or_reply(query, context, "Матч не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="cabinet_my_matches")]]))
        return
        
    round_info = await asyncio.to_thread(database.get_round_info, m['round_number'])
    deadline_text = round_info.get("deadline") if round_info else None
    
    is_overdue = False
    if deadline_text:
        dt = database.parse_flexible_datetime(deadline_text)
        if dt and datetime.datetime.now() > dt:
            is_overdue = True
            
    if m['player1_id'] == user_id:
        opp_id = m['player2_id']
        my_team = m['player1_team'] or m['player1_nickname']
        opp_team = m['player2_team'] or m['player2_nickname']
        score_str = f"{m['player1_score']}:{m['player2_score']}" if m['player1_score'] is not None else "-:-"
    else:
        opp_id = m['player1_id']
        my_team = m['player2_team'] or m['player2_nickname']
        opp_team = m['player1_team'] or m['player1_nickname']
        score_str = f"{m['player2_score']}:{m['player1_score']}" if m['player1_score'] is not None else "-:-"
        
    status_text = "⏳ Ожидает ввода результата"
    if m['status'] == 'disputed':
        status_text = "⚠️ Спорный"
    elif m['status'] == 'confirmed':
        status_text = "✅ Завершен"
        
    proposed_time = m.get('proposed_time')
    proposed_by = m.get('proposed_by')
    time_status = m.get('time_status') or 'none'

    time_info_text = ""
    if time_status == 'accepted' and proposed_time:
        time_info_text = f"⏰ **Согласованное время:** {html.escape(proposed_time)} ✅\n"
    elif time_status == 'proposed' and proposed_time:
        if proposed_by == user_id:
            time_info_text = f"⏰ **Предложено вами:** {html.escape(proposed_time)} *(ожидание ответа)*\n"
        else:
            time_info_text = f"⏰ **Соперник предлагает время:** {html.escape(proposed_time)}\n"
    else:
        time_info_text = "⏰ **Время матча:** не согласовано\n"

    text = f"🏟 <b>МАТЧ #{m['id']} | Тур {m['round_number']}</b>\n"
    text += f"Статус: {status_text}\n"
    if deadline_text:
        text += f"⏳ Дедлайн: {html.escape(deadline_text)}\n"
    text += time_info_text
        
    text += f"\n🏠 <b>Вы</b> {score_str} <b>{html.escape(opp_team)}</b> ✈️\n"
    text += "────────────────────────\n\n"
    
    if m['status'] == 'pending':
        if is_overdue and not m.get('is_extended'):
            text += "⏳ <b>Дедлайн истек.</b> Результат принимает администратор.\n\nВы можете отправить запрос администратору — он внесёт результат за вас."
        else:
            if m['player1_id'] == user_id:
                text += "🏠 <b>Вы играете Дома.</b>\n\n"
            else:
                text += "✈️ <b>Вы играете в Гостях.</b>\n\n"
            text += (
                "📌 <b>Инструкция по внесению результата:</b>\n"
                "1. Нажмите кнопку <b>📝 Ввести результат</b>.\n"
                "2. Выберите <b>⚡ Автоматический ввод (по фото)</b>.\n"
                "3. Отправьте боту от 1 до 3 скриншотов статистики из игры.\n"
                "4. ИИ автоматически распознает счёт, авторов голов и ассистов.\n"
                "5. Проверьте данные и нажмите <b>✅ Всё верно</b> — результат сразу автоматически подтверждается и заносится в турнирную таблицу лиги!"
            )
    elif m['status'] == 'disputed':
        text += "⚠️ <b>Матч оспорен.</b> Ожидайте решения администратора."
    elif m['status'] == 'confirmed':
        text += "✅ <b>Матч сыгран и результат подтвержден.</b>"
        
    keyboard = []
    
    keyboard.append([
        InlineKeyboardButton("👀 Состав соперника", callback_data=f"cabinet_view_squad_{opp_id}")
    ])

    if m['status'] == 'pending':
        if time_status == 'proposed' and proposed_by != user_id:
            keyboard.append([
                InlineKeyboardButton("✅ Согласовать время", callback_data=f"cb_accept_time_{match_id}"),
                InlineKeyboardButton("⏰ Предложить другое", callback_data=f"cb_propose_time_prompt_{match_id}")
            ])
        elif time_status == 'proposed' and proposed_by == user_id:
            keyboard.append([
                InlineKeyboardButton("✏️ Изменить предложенное время", callback_data=f"cb_propose_time_prompt_{match_id}")
            ])
        elif time_status == 'accepted':
            keyboard.append([
                InlineKeyboardButton("⏰ Изменить время", callback_data=f"cb_propose_time_prompt_{match_id}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("⏰ Предложить время матча", callback_data=f"cb_propose_time_prompt_{match_id}")
            ])
    
    if m['status'] == 'pending' and user_id in (m['player1_id'], m['player2_id']) and (not is_overdue or m.get('is_extended')):
        keyboard.append([InlineKeyboardButton("📝 Ввести результат", callback_data=f"cabinet_report_score_{match_id}")])

    # Overdue: show request to admin button
    if m['status'] == 'pending' and is_overdue and not m.get('is_extended') and user_id in (m['player1_id'], m['player2_id']):
        keyboard.append([InlineKeyboardButton("📨 Запросить ввод через админа", callback_data=f"cb_request_admin_result_{match_id}")])
        
    keyboard.append([InlineKeyboardButton("📜 Правила турнира", url="https://t.me/fifulatyrniru/3405")])
    keyboard.append([InlineKeyboardButton("🔙 К списку матчей", callback_data="cabinet_my_matches")])
    
    await safe_edit_or_reply(query, context, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cancel_score_report_and_navigate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """FSM fallback: clears all score-reporting state and routes user to the appropriate screen.

    Triggered when a user presses a navigation button (e.g. «К списку матчей», «Главное меню»)
    while a score-reporting ConversationHandler is active. Without this fallback the reporting
    keys survive in context.user_data and corrupt the next reporting session.
    """
    # ── Wipe every key that the reporting flow may have written ──────────────────
    for key in (
        "reporting_match_id",
        "report_photo_id",
        "awaiting_report_photo",
        "home_goals",
        "away_goals",
        "home_assists",
        "away_assists",
        "home_goal_players",
        "away_goal_players",
        "home_assist_players",
        "away_assist_players",
        "is_admin_reporting",
        "ai_photos_list",
        "processed_media_groups",
        "report_mvp_player",
    ):
        context.user_data.pop(key, None)

    query = update.callback_query
    if query:
        await query.answer()
        dest = query.data  # "main_menu" | "cabinet_my_matches" | anything else
    else:
        dest = ""

    from telegram.ext import ConversationHandler
    if dest == "main_menu":
        from handlers.base import show_main_menu
        await show_main_menu(update, context)
    else:
        await show_my_matches(update, context)

    return ConversationHandler.END


async def cb_request_admin_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Player requests permission from admin to enter result for an overdue match."""
    query = update.callback_query
    if not query:
        return

    match_id = int(query.data.replace("cb_request_admin_result_", ""))
    user_id = query.from_user.id
    
    # Anti-spam check: See if we already changed the button on this message
    if query.message and query.message.reply_markup:
        is_already_sent = False
        for row in query.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == "ignore" and "Запрос отправлен" in btn.text:
                    is_already_sent = True
        if is_already_sent:
            await query.answer("⏳ Запрос уже отправлен!", show_alert=False)
            return
            
    await query.answer()  # Single answer() to dismiss loading spinner

    m = await asyncio.to_thread(database.get_match, match_id)
    if not m:
        await context.bot.send_message(chat_id=user_id, text="❌ Матч не найден.")
        return

    if user_id not in (m['player1_id'], m['player2_id']):
        await context.bot.send_message(chat_id=user_id, text="⛔ Вы не являетесь участником этого матча.")
        return

    if m.get('is_extended'):
        await context.bot.send_message(chat_id=user_id, text="✅ Разрешение уже выдано! Вы можете вносить результат.")
        return

    team1 = m.get('player1_team') or m.get('player1_nickname') or '?'
    team2 = m.get('player2_team') or m.get('player2_nickname') or '?'
    nick1 = m.get('player1_nickname') or '?'
    nick2 = m.get('player2_nickname') or '?'
    rnd = m.get('round_number', '?')
    deadline = m.get('deadline', '—')

    requester_name = query.from_user.full_name or query.from_user.username or str(user_id)
    requester_username = f"@{query.from_user.username}" if query.from_user.username else f"id{user_id}"

    admin_text = (
        f"📨 <b>Запрос на разрешение внесения результата</b>\n\n"
        f"🏟 Матч #{match_id} | Тур {rnd}\n"
        f"🏠 {html.escape(team1)} (@{html.escape(nick1)}) vs {html.escape(team2)} (@{html.escape(nick2)}) ✈️\n"
        f"⏳ Дедлайн истёк: {html.escape(str(deadline))}\n\n"
        f"👤 Запрос отправил: {html.escape(requester_name)} ({requester_username})\n\n"
        f"Нажмите <b>✅ Разрешить</b>, чтобы игрок мог сам внести результат."
    )
    # callback carries both match_id and requesting player_id
    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Разрешить внесение", callback_data=f"cb_admin_approve_{match_id}_{user_id}")]
    ])

    sent_count = 0
    from config import ADMIN_IDS
    # Filter unique ADMIN_IDS in case of duplicates
    unique_admins = list(set(ADMIN_IDS))
    
    for admin_id in unique_admins:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=admin_keyboard
            )
            sent_count += 1
        except Exception as e:
            logger.warning(f"Could not notify admin {admin_id}: {e}")

    if sent_count > 0:
        await query.answer("✅ Запрос отправлен администратору!\nКак только он одобрит — бот пришлёт вам уведомление.", show_alert=True)
        # Update button to prevent multiple clicks
        if query.message and query.message.reply_markup:
            keyboard = query.message.reply_markup.inline_keyboard
            new_keyboard = []
            for row in keyboard:
                new_row = []
                for btn in row:
                    if btn.callback_data == query.data:
                        new_row.append(InlineKeyboardButton("⏳ Запрос отправлен", callback_data="noop"))
                    else:
                        new_row.append(btn)
                new_keyboard.append(new_row)
            try:
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
            except Exception as e:
                pass
    else:
        await query.answer("⚠️ Не удалось уведомить администраторов. Напишите напрямую @antonv2801.", show_alert=True)


async def cb_admin_approve_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin approves the player's request — unlocks the match and sends player a match card to enter result."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    admin_id = query.from_user.id
    from config import ADMIN_IDS
    if admin_id not in ADMIN_IDS:
        await query.answer("⛔ Только администратор может одобрять запросы.", show_alert=True)
        return

    # parse match_id and player_id from callback data: cb_admin_approve_{match_id}_{player_id}
    parts = query.data.replace("cb_admin_approve_", "").split("_")
    if len(parts) < 2:
        await query.answer("Ошибка данных.", show_alert=True)
        return
    match_id = int(parts[0])
    player_id = int(parts[1])

    m = await asyncio.to_thread(database.get_match, match_id)
    if not m:
        await query.answer("Матч не найден.", show_alert=True)
        return

    if m['status'] != 'pending':
        await query.answer(f"Матч уже имеет статус: {m['status']}.", show_alert=True)
        return

    # Unlock the match — set is_extended = 1 (explicit set, NOT a toggle:
    # a toggle could accidentally unfreeze a match an admin froze earlier)
    await asyncio.to_thread(database.set_match_extended, match_id, 1)

    team1 = m.get('player1_team') or m.get('player1_nickname') or '?'
    team2 = m.get('player2_team') or m.get('player2_nickname') or '?'
    rnd = m.get('round_number', '?')
    admin_username = query.from_user.username or str(admin_id)

    # Notify player with match card and direct "Enter result" button
    player_text = (
        f"✅ <b>Разрешение получено!</b>\n\n"
        f"Администратор @{html.escape(admin_username)} разрешил вам внести результат матча:\n\n"
        f"🏟 Матч #{match_id} | Тур {rnd}\n"
        f"🏠 {html.escape(team1)} vs {html.escape(team2)} ✈️\n\n"
        f"Нажмите кнопку ниже, чтобы сразу внести результат:"
    )
    player_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Ввести результат", callback_data=f"cabinet_report_score_{match_id}")]
    ])

    try:
        await context.bot.send_message(
            chat_id=player_id,
            text=player_text,
            parse_mode="HTML",
            reply_markup=player_keyboard
        )
    except Exception as e:
        logger.exception(f"Failed to notify player {player_id} about admin approval")

    # Update admin's message to show it's been approved
    try:
        await query.edit_message_text(
            text=(
                f"✅ <b>Разрешение выдано!</b>\n\n"
                f"Игрок получил уведомление и может сам внести результат матча #{match_id}."
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass


async def cb_propose_time_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user with quick time choices or custom text entry."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    match_id = int(query.data.replace("cb_propose_time_prompt_", ""))
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        return

    text = (
        f"⏰ **Предложение времени матча #{match_id}**\n\n"
        f"Выберите удобный вариант из списка ниже или введите своё время текстом:"
    )

    keyboard = [
        [
            InlineKeyboardButton("Сегодня в 19:00", callback_data=f"cb_quick_time_{match_id}_Сегодня в 19:00"),
            InlineKeyboardButton("Сегодня в 20:00", callback_data=f"cb_quick_time_{match_id}_Сегодня в 20:00")
        ],
        [
            InlineKeyboardButton("Сегодня в 21:00", callback_data=f"cb_quick_time_{match_id}_Сегодня в 21:00"),
            InlineKeyboardButton("Сегодня в 22:00", callback_data=f"cb_quick_time_{match_id}_Сегодня в 22:00")
        ],
        [
            InlineKeyboardButton("Завтра в 19:00", callback_data=f"cb_quick_time_{match_id}_Завтра в 19:00"),
            InlineKeyboardButton("Завтра в 20:00", callback_data=f"cb_quick_time_{match_id}_Завтра в 20:00")
        ],
        [
            InlineKeyboardButton("Завтра в 21:00", callback_data=f"cb_quick_time_{match_id}_Завтра в 21:00"),
            InlineKeyboardButton("Завтра в 22:00", callback_data=f"cb_quick_time_{match_id}_Завтра в 22:00")
        ],
        [
            InlineKeyboardButton("✍️ Ввести своё время", callback_data=f"cb_custom_time_prompt_{match_id}")
        ],
        [
            InlineKeyboardButton("« Назад к матчу", callback_data=f"cabinet_view_match_{match_id}")
        ]
    ]

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def cb_quick_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save quick selected time and notify opponent."""
    query = update.callback_query
    if not query:
        return
    await query.answer("✅ Предложение времени отправлено!")

    # Pattern: cb_quick_time_{match_id}_{time_str}
    raw = query.data.replace("cb_quick_time_", "")
    parts = raw.split("_", 1)
    match_id = int(parts[0])
    time_str = parts[1]
    user_id = query.from_user.id

    await asyncio.to_thread(database.propose_match_time, match_id, user_id, time_str)
    match = await asyncio.to_thread(database.get_match, match_id)

    # Notify opponent
    if match:
        opp_id = match['player2_id'] if match['player1_id'] == user_id else match['player1_id']
        sender_team = match['player1_team'] if match['player1_id'] == user_id else match['player2_team']
        sender_team = sender_team or "Соперник"

        if opp_id:
            pm_text = (
                f"⏰ <b>ПРЕДЛОЖЕНИЕ ВРЕМЕНИ МАТЧА!</b>\n\n"
                f"Соперник <b>{html.escape(sender_team)}</b> предлагает сыграть <b>Матч #{match_id} (Тур {match['round_number']})</b>:\n"
                f"🕒 <b>{html.escape(time_str)}</b>\n\n"
                f"<i>Перейдите в карточку матча для подтверждения или ответа!</i>"
            )
            kb = [[InlineKeyboardButton("🏟 Открыть карточку матча", callback_data=f"cabinet_view_match_{match_id}")]]
            await safe_send_notification(context.bot, opp_id, pm_text, InlineKeyboardMarkup(kb))

    # Return user to match card view
    await cabinet_view_match(update, context)

async def cb_accept_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept the proposed match time."""
    query = update.callback_query
    if not query:
        return
    await query.answer("🎉 Время матча успешно согласовано!", show_alert=True)

    match_id = int(query.data.replace("cb_accept_time_", ""))
    user_id = query.from_user.id

    await asyncio.to_thread(database.accept_match_time, match_id)
    match = await asyncio.to_thread(database.get_match, match_id)

    if match:
        proposer_id = match.get("proposed_by")
        accepted_time = match.get("proposed_time") or "неизвестно"
        acceptor_team = match['player1_team'] if match['player1_id'] == user_id else match['player2_team']
        acceptor_team = acceptor_team or "Соперник"

        if proposer_id and proposer_id != user_id:
            pm_text = (
                f"✅ <b>ВРЕМЯ МАТЧА СОГЛАСОВАНО!</b>\n\n"
                f"Соперник <b>{html.escape(acceptor_team)}</b> подтвердил время проведения <b>Матча #{match_id} (Тур {match['round_number']})</b>:\n"
                f"🕒 <b>{html.escape(accepted_time)}</b>\n\n"
                f"Удачной игры!"
            )
            kb = [[InlineKeyboardButton("🏟 Открыть карточку матча", callback_data=f"cabinet_view_match_{match_id}")]]
            await safe_send_notification(context.bot, proposer_id, pm_text, InlineKeyboardMarkup(kb))

    await cabinet_view_match(update, context)

async def start_custom_time_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt user to type custom time."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    match_id = int(query.data.replace("cb_custom_time_prompt_", ""))
    context.user_data["custom_time_match_id"] = match_id

    text = (
        f"✍️ **Ввод своего времени для матча #{match_id}**\n\n"
        f"Напишите удобные дату и время сообщением (например: `Сегодня в 22:30` или `23.07 в 18:00`):\n\n"
        f"*(Отправьте /cancel для отмены)*"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"cabinet_view_match_{match_id}")]]
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return MATCH_CUSTOM_TIME

async def save_custom_match_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save custom match time entered by text."""
    if not update.message or not update.message.text or (update.effective_chat and update.effective_chat.type != "private"):
        return MATCH_CUSTOM_TIME

    time_str = update.message.text.strip()
    if len(time_str) > 100:
        await update.message.reply_text("⚠️ Введенное время слишком длинное (максимум 100 символов). Попробуйте еще раз:")
        return MATCH_CUSTOM_TIME

    match_id = context.user_data.get("custom_time_match_id")
    user_id = update.effective_user.id

    if not match_id:
        await update.message.reply_text("❌ Ошибка: матч не найден.")
        return ConversationHandler.END

    await asyncio.to_thread(database.propose_match_time, match_id, user_id, time_str)
    match = await asyncio.to_thread(database.get_match, match_id)

    if match:
        opp_id = match['player2_id'] if match['player1_id'] == user_id else match['player1_id']
        sender_team = match['player1_team'] if match['player1_id'] == user_id else match['player2_team']
        sender_team = sender_team or "Соперник"

        if opp_id:
            pm_text = (
                f"⏰ <b>ПРЕДЛОЖЕНИЕ ВРЕМЕНИ МАТЧА!</b>\n\n"
                f"Соперник <b>{html.escape(sender_team)}</b> предлагает сыграть <b>Матч #{match_id} (Тур {match['round_number']})</b>:\n"
                f"🕒 <b>{html.escape(time_str)}</b>\n\n"
                f"<i>Перейдите в карточку матча для подтверждения или ответа!</i>"
            )
            kb = [[InlineKeyboardButton("🏟 Открыть карточку матча", callback_data=f"cabinet_view_match_{match_id}")]]
            await safe_send_notification(context.bot, opp_id, pm_text, InlineKeyboardMarkup(kb))

    await update.message.reply_text(f"✅ Время матча #{match_id} успешно предложено: <b>{html.escape(time_str)}</b>", parse_mode="HTML")
    
    # Render updated match card
    m_info = await asyncio.to_thread(database.get_match, match_id)
    if m_info:
        round_info = await asyncio.to_thread(database.get_round_info, m_info['round_number'])
        deadline_text = round_info.get("deadline") if round_info else None
        
        if m_info['player1_id'] == user_id:
            opp_team = m_info['player2_team'] or m_info['player2_nickname']
            score_str = f"{m_info['player1_score']}:{m_info['player2_score']}" if m_info['player1_score'] is not None else "-:-"
        else:
            opp_team = m_info['player1_team'] or m_info['player1_nickname']
            score_str = f"{m_info['player2_score']}:{m_info['player1_score']}" if m_info['player1_score'] is not None else "-:-"

        opp_id = m_info['player2_id'] if m_info['player1_id'] == user_id else m_info['player1_id']
        kb_match = [
            [InlineKeyboardButton("👀 Состав соперника", callback_data=f"cabinet_view_squad_{opp_id}")],
            [InlineKeyboardButton("✏️ Изменить предложенное время", callback_data=f"cb_propose_time_prompt_{match_id}")],
            [InlineKeyboardButton("🔙 К списку матчей", callback_data="cabinet_my_matches")]
        ]
        if m_info['status'] == 'pending' and m_info['player1_id'] == user_id:
            kb_match.insert(1, [InlineKeyboardButton("📝 Ввести результат", callback_data=f"cabinet_report_score_{match_id}")])

        card_text = (
            f"🏟 <b>МАТЧ #{match_id} | Тур {m_info['round_number']}</b>\n"
            f"Статус: ⏳ Ожидает ввода результата\n"
        )
        if deadline_text:
            card_text += f"⏳ Дедлайн: {html.escape(str(deadline_text))}\n"
        card_text += f"⏰ <b>Предложено вами:</b> {html.escape(time_str)} <i>(ожидание ответа)</i>\n"
        card_text += f"\n🏠 <b>Вы</b> {score_str} <b>{html.escape(str(opp_team or 'Соперник'))}</b> ✈️\n────────────────────────\n\n"
        card_text += "⏳ Предложение времени отправлено сопернику."

        await update.message.reply_text(card_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb_match))

    return ConversationHandler.END

async def show_game_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id
    matches = await asyncio.to_thread(database.get_match_history, user_id)

    text = "📜 <b>Ваша история игр:</b>\n\n"
    if not matches:
        text += "Вы еще не сыграли ни одного матча."
    else:
        for m in matches:
            opp = m['opponent_team'] or m['opponent_username']
            user_score = m['player1_score'] if m['player1_id'] == user_id else m['player2_score']
            opp_score = m['player2_score'] if m['player1_id'] == user_id else m['player1_score']
            text += f"Тур {m['round_number']}: <b>Вы</b> {user_score} : {opp_score} <b>{html.escape(str(opp or 'Соперник'))}</b>\n"

    keyboard = [[InlineKeyboardButton("« Назад в кабинет", callback_data="menu_cabinet")]]
    markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


# ==========================================
# ВВОД И ПОДТВЕРЖДЕНИЕ РЕЗУЛЬТАТОВ МАТЧА
# ==========================================

def get_match_cancel_cb(context: ContextTypes.DEFAULT_TYPE, user_id: int, match_id: int) -> str:
    """Return appropriate cancel callback data depending on whether user is admin or player."""
    is_admin_user = is_admin(user_id) or context.user_data.get("is_admin_reporting", False)
    if is_admin_user:
        return f"admin_view_match_{match_id}"
    return f"cabinet_view_match_{match_id}"

async def start_score_reporting(update: Update, context: ContextTypes.DEFAULT_TYPE, match_id: int | None = None) -> None:
    query = update.callback_query
    if query:
        await query.answer()
        if match_id is None:
            match_id = int(query.data.replace("cabinet_report_score_", ""))

    context.user_data["reporting_match_id"] = match_id
    context.user_data.pop("report_mvp_player", None)

    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        if query:
            await query.answer("❌ Матч не найден.", show_alert=True)
        return

    if match['status'] == 'confirmed':
        if query:
            await query.answer("⛔ Результат этого матча уже занесён в таблицу!", show_alert=True)
        return

    user_id = query.from_user.id if query else update.effective_user.id

    home_team = match['player1_team'] or match['player1_nickname']
    away_team = match['player2_team'] or match['player2_nickname']

    context.user_data["report_home_team"] = home_team
    context.user_data["report_away_team"] = away_team

    user_id = query.from_user.id if query else update.effective_user.id
    cancel_cb = get_match_cancel_cb(context, user_id, match_id)

    text = (
        f"📝 <b>Ввод результата матча #{match_id}</b>\n\n"
        f"⚔️ <b>{safe_escape(home_team)}</b> 🆚 <b>{safe_escape(away_team)}</b>\n\n"
        f"Выберите способ ввода результата:"
    )

    keyboard = [
        [InlineKeyboardButton("⚡ Автоматический ввод (по фото)", callback_data=f"cb_report_choice_auto_{match_id}")],
        [InlineKeyboardButton("✍️ Ручной ввод", callback_data=f"cb_report_choice_manual_{match_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if query:
        await safe_edit_or_reply(query, context, text, parse_mode="HTML", reply_markup=markup)

async def cb_report_choice_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    match_id = int(query.data.replace("cb_report_choice_auto_", ""))

    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await query.answer("❌ Матч не найден.", show_alert=True)
        return

    if match['status'] == 'confirmed':
        await query.answer("⛔ Результат этого матча уже занесён в таблицу!", show_alert=True)
        return

    user_id = query.from_user.id
    context.user_data["reporting_match_id"] = match_id
    context.user_data["reporting_mode"] = "auto"
    context.user_data.pop("report_mvp_player", None)
    context.user_data["awaiting_report_photo"] = True
    context.user_data["ai_photos_list"] = []

    text = (
        "📸 <b>Автоматический ввод по фото</b>\n\n"
        "Пожалуйста, отправьте <b>от 1 до 3 скриншотов</b> матча строго с статистикой(голы и ассисты).\n\n"
        "💡 <i>Вы можете отправить от 1 до 3 фото сразу альбомом.</i>"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"cabinet_view_match_{match_id}")]]
    await safe_edit_or_reply(query, context, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def cb_report_choice_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    try:
        match_id = int(query.data.replace("cb_report_choice_manual_", ""))
    except (ValueError, TypeError):
        await query.answer(
            "⚠️ Сессия ввода устарела (бот перезапускался). Откройте матч и начните ввод результата заново.",
            show_alert=True
        )
        return

    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await query.answer("❌ Матч не найден.", show_alert=True)
        return

    if match['status'] == 'confirmed':
        await query.answer("⛔ Результат этого матча уже занесён в таблицу!", show_alert=True)
        return

    user_id = query.from_user.id
    context.user_data["reporting_match_id"] = match_id
    context.user_data["reporting_mode"] = "manual"
    # Ручной ввод не редактирует MVP, а к нему часто переходят как раз потому,
    # что ИИ ошибся — поэтому корону из распознавания не переносим.
    context.user_data.pop("report_mvp_player", None)

    home_team = match['player1_team'] or match['player1_nickname']
    away_team = match['player2_team'] or match['player2_nickname']
    context.user_data["report_home_team"] = home_team
    context.user_data["report_away_team"] = away_team

    text = (
        f"⚽ <b>Ввод результата матча #{match_id}</b>\n"
        f"🏠 <b>{safe_escape(home_team)}</b> vs <b>{safe_escape(away_team)}</b> ✈️\n\n"
        f"Выберите, сколько забила домашняя команда (<b>{safe_escape(home_team)}</b>):"
    )

    keyboard = []
    row = []
    for i in range(16):
        row.append(InlineKeyboardButton(str(i), callback_data=f"cb_report_hg_{i}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
        
    user_id = query.from_user.id
    cancel_cb = get_match_cancel_cb(context, user_id, match_id)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)])

    # Используем safe_edit_or_reply, чтобы корректно удалить карточку с картинкой!
    await safe_edit_or_reply(query, context, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def cb_report_home_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    hg = int(query.data.replace("cb_report_hg_", ""))
    context.user_data["report_home_goals"] = hg

    match_id = context.user_data.get("reporting_match_id")
    home_team = context.user_data.get("report_home_team")
    away_team = context.user_data.get("report_away_team")

    text = (
        f"⚽ <b>Ввод результата матча #{match_id}</b>\n"
        f"🏠 <b>{safe_escape(home_team)}</b> ({hg}) vs <b>{safe_escape(away_team)}</b> (?)\n\n"
        f"Теперь выберите, сколько забил соперник (<b>{safe_escape(away_team)}</b>):"
    )

    keyboard = []
    row = []
    for i in range(16):
        row.append(InlineKeyboardButton(str(i), callback_data=f"cb_report_ag_{i}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
        
    user_id = query.from_user.id
    cancel_cb = get_match_cancel_cb(context, user_id, match_id)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)])

    await safe_edit_or_reply(query, context, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def cb_report_away_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    ag = int(query.data.replace("cb_report_ag_", ""))
    context.user_data["report_away_goals"] = ag

    hg = context.user_data.get("report_home_goals", 0)
    home_team = context.user_data.get("report_home_team")

    # Clear previous selections
    context.user_data["home_goals_count"] = {}
    context.user_data["home_assists_count"] = {}
    context.user_data["away_goals_count"] = {}
    context.user_data["away_assists_count"] = {}

    if hg > 0:
        context.user_data["current_picking_phase"] = "home_goals"
        context.user_data["goals_to_pick"] = hg
        await render_squad_goals_picker(update, context, home_team)
    else:
        await start_home_assists_picker(update, context)

async def start_home_assists_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hg = context.user_data.get("report_home_goals", 0)
    home_team = context.user_data.get("report_home_team")
    if hg > 0:
        context.user_data["current_picking_phase"] = "home_assists"
        context.user_data["assists_to_pick"] = hg
        await render_squad_assists_picker(update, context, home_team)
    else:
        await start_away_goals_picker(update, context)

async def start_away_goals_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ag = context.user_data.get("report_away_goals", 0)
    away_team = context.user_data.get("report_away_team")
    if ag > 0:
        context.user_data["current_picking_phase"] = "away_goals"
        context.user_data["goals_to_pick"] = ag
        await render_squad_goals_picker(update, context, away_team)
    else:
        await start_away_assists_picker(update, context)

async def start_away_assists_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ag = context.user_data.get("report_away_goals", 0)
    away_team = context.user_data.get("report_away_team")
    if ag > 0:
        context.user_data["current_picking_phase"] = "away_assists"
        context.user_data["assists_to_pick"] = ag
        await render_squad_assists_picker(update, context, away_team)
    else:
        await prompt_photo_upload(update, context)

async def render_squad_goals_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, team_name: str) -> None:
    query = update.callback_query
    phase = context.user_data.get("current_picking_phase", "home_goals")
    left = context.user_data.get("goals_to_pick", 0)

    dict_key = "home_goals_count" if "home" in phase else "away_goals_count"
    picked_dict = context.user_data.get(dict_key, {})

    squad = await asyncio.to_thread(database.get_squad, team_name)

    summary_str = ""
    if picked_dict:
        summary_str = "\n\n⚽ **Уже выбрано:**\n" + "\n".join([f"• {p}: {c}" for p, c in picked_dict.items()])

    text = (
        f"⚽ <b>Авторы голов команды ({html.escape(team_name)})</b>\n"
        f"Осталось распределить голов: <b>{left}</b>{summary_str}\n\n"
        f"Нажимайте на кнопки с игроками состава:"
    )

    keyboard = []
    context.user_data["temp_active_squad_goals"] = squad
    if not squad:
        text += "\n\n⚠️ <i>Состав команды пока не добавлен в систему.</i>"
        keyboard.append([InlineKeyboardButton("⏩ Пропустить ввод авторов", callback_data="cb_skip_goals")])
    else:
        row = []
        for idx, player in enumerate(squad):
            row.append(InlineKeyboardButton(f"🏃‍♂️ {player}", callback_data=f"cb_pick_goal_idx_{idx}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("⏩ Пропустить остаток (пенальти/автоголы)", callback_data="cb_skip_goals")])

    match_id = context.user_data.get("reporting_match_id")
    user_id = query.from_user.id if query else update.effective_user.id
    cancel_cb = get_match_cancel_cb(context, user_id, match_id)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)])

    markup = InlineKeyboardMarkup(keyboard)
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

async def cb_pick_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    idx = int(query.data.replace("cb_pick_goal_idx_", ""))
    squad = context.user_data.get("temp_active_squad_goals", [])
    player = squad[idx] if idx < len(squad) else "Unknown"

    phase = context.user_data.get("current_picking_phase", "home_goals")
    dict_key = "home_goals_count" if "home" in phase else "away_goals_count"
    dict_goals = context.user_data.get(dict_key, {})

    dict_goals[player] = dict_goals.get(player, 0) + 1
    left = context.user_data.get("goals_to_pick", 0) - 1
    context.user_data["goals_to_pick"] = left
    context.user_data[dict_key] = dict_goals

    team_name = context.user_data.get("report_home_team") if "home" in phase else context.user_data.get("report_away_team")

    if left > 0:
        await render_squad_goals_picker(update, context, team_name)
    else:
        if phase == "home_goals":
            await start_home_assists_picker(update, context)
        else:
            await start_away_assists_picker(update, context)

async def cb_skip_goals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    phase = context.user_data.get("current_picking_phase", "home_goals")
    if phase == "home_goals":
        await start_home_assists_picker(update, context)
    else:
        await start_away_assists_picker(update, context)

async def render_squad_assists_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, team_name: str) -> None:
    query = update.callback_query
    phase = context.user_data.get("current_picking_phase", "home_assists")
    left = context.user_data.get("assists_to_pick", 0)

    dict_key = "home_assists_count" if "home" in phase else "away_assists_count"
    goals_key = "report_home_goals" if "home" in phase else "report_away_goals"
    max_assists = context.user_data.get(goals_key, 0)

    picked_dict = context.user_data.get(dict_key, {})

    squad = await asyncio.to_thread(database.get_squad, team_name)

    summary_str = ""
    if picked_dict:
        summary_str = "\n\n🎯 **Уже выбрано:**\n" + "\n".join([f"• {p}: {c}" for p, c in picked_dict.items()])

    text = (
        f"🎯 <b>Авторы ассистов команды ({html.escape(team_name)})</b>\n"
        f"Осталось ассистов (макс {max_assists}): <b>{left}</b>{summary_str}\n\n"
        f"Нажимайте на кнопки с игроками состава или пропустите:"
    )

    keyboard = []
    context.user_data["temp_active_squad_assists"] = squad
    if squad and left > 0:
        row = []
        for idx, player in enumerate(squad):
            row.append(InlineKeyboardButton(f"🎯 {player}", callback_data=f"cb_pick_assist_idx_{idx}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("⏩ Пропустить остаток ассистов", callback_data="cb_skip_assists")])
    match_id = context.user_data.get("reporting_match_id")
    user_id = query.from_user.id if query else update.effective_user.id
    cancel_cb = get_match_cancel_cb(context, user_id, match_id)
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)])

    markup = InlineKeyboardMarkup(keyboard)
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

async def cb_pick_assist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    idx = int(query.data.replace("cb_pick_assist_idx_", ""))
    squad = context.user_data.get("temp_active_squad_assists", [])
    player = squad[idx] if idx < len(squad) else "Unknown"

    phase = context.user_data.get("current_picking_phase", "home_assists")
    dict_key = "home_assists_count" if "home" in phase else "away_assists_count"
    dict_assists = context.user_data.get(dict_key, {})

    dict_assists[player] = dict_assists.get(player, 0) + 1
    left = context.user_data.get("assists_to_pick", 0) - 1
    context.user_data["assists_to_pick"] = left
    context.user_data[dict_key] = dict_assists

    team_name = context.user_data.get("report_home_team") if "home" in phase else context.user_data.get("report_away_team")

    if left > 0:
        await render_squad_assists_picker(update, context, team_name)
    else:
        if phase == "home_assists":
            await start_away_goals_picker(update, context)
        else:
            await prompt_photo_upload(update, context)

async def cb_skip_assists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    phase = context.user_data.get("current_picking_phase", "home_assists")
    if phase == "home_assists":
        await start_away_goals_picker(update, context)
    else:
        await prompt_photo_upload(update, context)

async def prompt_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data["awaiting_report_photo"] = True
    match_id = context.user_data.get('reporting_match_id')
    user_id = query.from_user.id if query else update.effective_user.id

    # user_data is in-memory and is lost on bot restart — stale buttons from a
    # previous session must not produce broken callbacks (e.g. "_None").
    if not match_id:
        expired_text = (
            "⚠️ <b>Сессия ввода результата устарела</b> (бот перезапускался).\n\n"
            "Откройте матч в кабинете и начните ввод результата заново."
        )
        if query:
            await query.answer("⚠️ Сессия устарела. Откройте матч заново.", show_alert=True)
            try:
                await query.edit_message_text(expired_text, parse_mode="HTML")
            except Exception:
                pass
        else:
            await update.effective_message.reply_text(expired_text, parse_mode="HTML")
        return ConversationHandler.END

    cancel_cb = get_match_cancel_cb(context, user_id, match_id)

    text = (
        "📸 **Прикрепление скриншота результата**\n\n"
        "Пожалуйста, отправьте **от 1 до 3 скриншотов** результата сыгранной игры."
    )
    reporting_mode = context.user_data.get("reporting_mode", "auto")
    if reporting_mode == "manual":
        # Score/goals already entered — allow finishing without a screenshot
        skip_btn = InlineKeyboardButton("⏭ Продолжить без скриншота", callback_data="cb_skip_report_photo")
    else:
        # Auto mode with nothing entered yet — jump to manual entry instead
        skip_btn = InlineKeyboardButton("⌨️ Ввести вручную (без скриншота)", callback_data=f"cb_report_choice_manual_{match_id}")
    keyboard = [[skip_btn], [InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)]]
    markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

    return REPORT_SCORE_PHOTO

async def _show_manual_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_id: str | None) -> int:
    """Build and send the final 'check your data' card of the manual flow.
    Works with or without an attached screenshot."""
    match_id = context.user_data.get("reporting_match_id")
    match = await asyncio.to_thread(database.get_match, match_id) if match_id else None
    home_team = match.get("player1_team") if match else "Хозяева"
    away_team = match.get("player2_team") if match else "Гости"

    h_goals = context.user_data.get("home_goals_count", {})
    a_goals = context.user_data.get("away_goals_count", {})
    h_assists = context.user_data.get("home_assists_count", {})
    a_assists = context.user_data.get("away_assists_count", {})

    h_goals_summary = ", ".join([f"{p} ({c})" for p, c in h_goals.items()]) if h_goals else "Нет"
    a_goals_summary = ", ".join([f"{p} ({c})" for p, c in a_goals.items()]) if a_goals else "Нет"
    h_assists_summary = ", ".join([f"{p} ({c})" for p, c in h_assists.items()]) if h_assists else "Нет"
    a_assists_summary = ", ".join([f"{p} ({c})" for p, c in a_assists.items()]) if a_assists else "Нет"

    h_score = context.user_data.get("report_home_goals", 0)
    a_score = context.user_data.get("report_away_goals", 0)

    photo_line = "📸 <i>Скриншот(ы) прикреплены.</i>" if photo_id else "🚫 <i>Скриншот не прикреплён (ввод без скрина).</i>"

    text = (
        f"📊 <b>Проверьте данные перед сохранением:</b>\n\n"
        f"🏟 <b>Матч #{match_id}</b>\n"
        f"🏠 <b>{safe_escape(home_team)}</b> {h_score} : {a_score} <b>{safe_escape(away_team)}</b> ✈️\n\n"
        f"⚽ <b>Голы ({safe_escape(home_team)}):</b> {safe_escape(h_goals_summary)}\n"
        f"🎯 <b>Ассисты ({safe_escape(home_team)}):</b> {safe_escape(h_assists_summary)}\n\n"
        f"⚽ <b>Голы ({safe_escape(away_team)}):</b> {safe_escape(a_goals_summary)}\n"
        f"🎯 <b>Ассисты ({safe_escape(away_team)}):</b> {safe_escape(a_assists_summary)}\n\n"
        f"{photo_line}"
    )

    is_admin_user = is_admin(update.effective_user.id) or context.user_data.get("is_admin_reporting", False)
    cancel_cb = f"admin_view_match_{match_id}" if is_admin_user else f"cabinet_view_match_{match_id}"
    confirm_text = "✅ Сохранить и занести результат" if is_admin_user else "✅ Подтвердить и занести результат"

    keyboard = [
        [InlineKeyboardButton(confirm_text, callback_data=f"cb_submit_report_to_guest_{match_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    chat_id = update.effective_user.id
    try:
        if photo_id:
            await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        logger.warning(f"Failed to send manual confirmation card: {e}")
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
    return ConversationHandler.END


async def cb_skip_report_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manual flow: continue to the confirmation card WITHOUT a screenshot."""
    query = update.callback_query
    if query:
        await query.answer()
    context.user_data.pop("awaiting_report_photo", None)

    if not context.user_data.get("reporting_match_id"):
        alert = "⚠️ Сессия ввода устарела (бот перезапускался). Откройте матч и начните ввод результата заново."
        if query:
            await query.answer(alert, show_alert=True)
        else:
            await update.effective_message.reply_text(alert)
        return ConversationHandler.END

    return await _show_manual_confirmation(update, context, photo_id=None)


async def save_report_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or (update.effective_chat and update.effective_chat.type != "private"):
        return REPORT_SCORE_PHOTO

    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif update.message.document:
        photo_id = update.message.document.file_id

    if not photo_id:
        await update.message.reply_text("❌ Пожалуйста, отправьте фото скриншота результата.")
        return REPORT_SCORE_PHOTO

    media_group_id = update.message.media_group_id
    if media_group_id:
        processed_groups = context.user_data.setdefault("processed_media_groups", set())
        photos_list = context.user_data.get("ai_photos_list", [])
        if photo_id not in photos_list:
            photos_list.append(photo_id)
        context.user_data["ai_photos_list"] = photos_list

        if media_group_id in processed_groups:
            return REPORT_SCORE_PHOTO
        processed_groups.add(media_group_id)
        await asyncio.sleep(0.6)

    match_id = context.user_data.get("reporting_match_id")
    match = await asyncio.to_thread(database.get_match, match_id) if match_id else None
    home_team = match.get("player1_team") if match else "Хозяева"
    away_team = match.get("player2_team") if match else "Гости"
    context.user_data["report_home_team"] = home_team
    context.user_data["report_away_team"] = away_team

    reporting_mode = context.user_data.get("reporting_mode", "auto")

    if reporting_mode == "auto" and config.GEMINI_API_KEY:
        photos_list = context.user_data.get("ai_photos_list", [])
        if photo_id not in photos_list:
            photos_list.append(photo_id)
        context.user_data["ai_photos_list"] = photos_list
        context.user_data["report_photo_id"] = photos_list[0]

        match_id = context.user_data.get("reporting_match_id")
        n_photos = len(photos_list)
        collected_text = (
            f"📸 <b>Скриншот {n_photos}/3 принят.</b>\n\n"
            f"Отправьте остальные скриншоты (вертикальная колонка голов и/или таблица статистики), "
            f"затем нажмите кнопку распознавания."
        )
        keyboard = [
            [InlineKeyboardButton(f"🔍 Распознать результат ({n_photos})", callback_data=f"ai_recognize_now_{match_id}")],
            [InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"cb_report_choice_manual_{match_id}")],
        ]
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=collected_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return REPORT_SCORE_PHOTO

    context.user_data["report_photo_id"] = photo_id
    context.user_data.pop("awaiting_report_photo", None)

    return await _show_manual_confirmation(update, context, photo_id=photo_id)

async def ai_recognize_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run AI recognition on all collected screenshots and show the result for confirmation."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id

    match_id = None
    try:
        match_id = int(query.data.replace("ai_recognize_now_", ""))
    except (ValueError, TypeError):
        match_id = context.user_data.get("reporting_match_id")

    photos_list = context.user_data.get("ai_photos_list", [])
    if not photos_list:
        await query.message.reply_text("⚠️ Не найдено скриншотов. Отправьте фото заново.")
        return

    status_msg = None
    try:
        status_msg = await query.message.reply_text("🤖 <i>ИИ распознаёт результат со скриншотов...</i>", parse_mode="HTML")

        downloaded_bytes = []
        for p_id in photos_list[:3]:
            f_obj = await context.bot.get_file(p_id)
            img_b = await f_obj.download_as_bytearray()
            downloaded_bytes.append(bytes(img_b))

        ai_res = await asyncio.to_thread(ai_recognizer.recognize_match_screenshots_bytes, downloaded_bytes)

        match = await asyncio.to_thread(database.get_match, match_id) if match_id else None
        home_team = match.get("player1_team") if match else "Хозяева"
        away_team = match.get("player2_team") if match else "Гости"
        context.user_data["report_home_team"] = home_team
        context.user_data["report_away_team"] = away_team

        if ai_res and ("home_score" in ai_res) and ("away_score" in ai_res):
            s1_goals = ai_res.get("side1_goals") or ai_res.get("home_goals") or []
            s2_goals = ai_res.get("side2_goals") or ai_res.get("away_goals") or []
            s1_assists = ai_res.get("side1_assists") or ai_res.get("home_assists") or []
            s2_assists = ai_res.get("side2_assists") or ai_res.get("away_assists") or []

            is_single_timeline = bool(ai_res.get("is_single_timeline", False))

            h_goals, a_goals, h_assists, a_assists, is_side1_home = await asyncio.to_thread(
                match_and_enrich_squad,
                s1_goals, s2_goals, s1_assists, s2_assists,
                home_team, away_team,
                is_single_timeline=is_single_timeline,
            )

            if is_side1_home:
                h_score = int(ai_res.get("left_score", ai_res.get("home_score", sum(h_goals.values()))))
                a_score = int(ai_res.get("right_score", ai_res.get("away_score", sum(a_goals.values()))))
            else:
                h_score = int(ai_res.get("right_score", ai_res.get("away_score", sum(h_goals.values()))))
                a_score = int(ai_res.get("left_score", ai_res.get("home_score", sum(a_goals.values()))))

            context.user_data["report_home_goals"] = h_score
            context.user_data["report_away_goals"] = a_score
            context.user_data["home_goals_count"] = h_goals
            context.user_data["away_goals_count"] = a_goals
            context.user_data["home_assists_count"] = h_assists
            context.user_data["away_assists_count"] = a_assists
            context.user_data["is_single_timeline"] = bool(is_single_timeline)
            context.user_data["report_photo_id"] = photos_list[0] if photos_list else context.user_data.get("report_photo_id")
            mvp_player = await asyncio.to_thread(
                resolve_mvp_player_name,
                ai_recognizer.clean_mvp_name(ai_res.get("mvp_player")),
                home_team, away_team,
            )
            if mvp_player:
                context.user_data["report_mvp_player"] = mvp_player
            else:
                context.user_data.pop("report_mvp_player", None)
            mvp_line = f"👑 <b>Игрок матча (MVP):</b> {safe_escape(mvp_player)}\n\n" if mvp_player else ""

            h_goals_summary = ", ".join([f"{p} ({c})" for p, c in h_goals.items()]) if h_goals else "Нет"
            a_goals_summary = ", ".join([f"{p} ({c})" for p, c in a_goals.items()]) if a_goals else "Нет"
            h_assists_summary = ", ".join([f"{p} ({c})" for p, c in h_assists.items()]) if h_assists else "Нет"
            a_assists_summary = ", ".join([f"{p} ({c})" for p, c in a_assists.items()]) if a_assists else "Нет"

            h_assists_str = safe_escape(h_assists_summary) if not is_single_timeline else "<i>не отображаются в данном формате скриншота</i>"
            a_assists_str = safe_escape(a_assists_summary) if not is_single_timeline else "<i>не отображаются в данном формате скриншота</i>"

            text = (
                f"🤖 <b>ИИ автоматически распознал результат со скриншота:</b>\n\n"
                f"🏟 <b>Матч #{match_id}</b>\n"
                f"🏠 <b>{safe_escape(home_team)}</b> {h_score} : {a_score} <b>{safe_escape(away_team)}</b> ✈️\n\n"
                f"⚽ <b>Голы ({safe_escape(home_team)}):</b> {safe_escape(h_goals_summary)}\n"
                f"🎯 <b>Ассисты ({safe_escape(home_team)}):</b> {h_assists_str}\n\n"
                f"⚽ <b>Голы ({safe_escape(away_team)}):</b> {safe_escape(a_goals_summary)}\n"
                f"🎯 <b>Ассисты ({safe_escape(away_team)}):</b> {a_assists_str}\n\n"
                f"{mvp_line}"
                f"📸 <i>Скриншот(ы) прикреплены.</i>"
            )

            is_admin_user = is_admin(user_id) or context.user_data.get("is_admin_reporting", False)
            cancel_cb = f"admin_view_match_{match_id}" if is_admin_user else f"cabinet_view_match_{match_id}"
            manual_cb = f"cb_report_choice_manual_{match_id}"

            keyboard = [
                [InlineKeyboardButton("✅ Всё верно (Сохранить и занести результат)", callback_data=f"cb_confirm_ai_final_{match_id}")],
                [InlineKeyboardButton("✏️ Изменить вручную", callback_data=manual_cb)],
                [InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)]
            ]
            markup = InlineKeyboardMarkup(keyboard)

            photo_to_show = photos_list[0] if photos_list else context.user_data.get("report_photo_id")
            await context.bot.send_photo(chat_id=user_id, photo=photo_to_show, caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            context.user_data["report_photo_id"] = photos_list[0] if photos_list else context.user_data.get("report_photo_id")
            context.user_data.pop("report_mvp_player", None)
            cancel_cb = get_match_cancel_cb(context, user_id, match_id)
            fail_text = (
                "⚠️ <b>Не удалось автоматически распознать результат со скриншотов.</b>\n\n"
                "Пожалуйста, выберите способ внесения результата вручную:"
            )
            keyboard = [
                [InlineKeyboardButton("✍️ Ввести результат вручную", callback_data=f"cb_report_choice_manual_{match_id}")],
                [InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)]
            ]
            await query.message.reply_text(fail_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.exception("AI Vision processing error")
        context.user_data["report_photo_id"] = photos_list[0] if photos_list else context.user_data.get("report_photo_id")
        await query.message.reply_text("⚠️ Ошибка при распознавании скриншотов. Попробуйте ещё раз.")
    finally:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

def build_formatted_match_post(
    round_number: int | str,
    home_team: str,
    away_team: str,
    h_score: int,
    a_score: int,
    p1_username: str | None = None,
    p2_username: str | None = None,
    h_goals: dict | list | None = None,
    a_goals: dict | list | None = None,
    h_assists: dict | list | None = None,
    a_assists: dict | list | None = None,
    is_single_timeline: bool = False,
    is_pm: bool = False,
    pm_title: str = "🎉 <b>Результат успешно занесен в лигу!</b>",
    match_id: int | None = None,
    is_draft: bool = False,
    mvp_player: str | None = None
) -> str:
    """
    Constructs a unified match result text block with goals and assists for PM notifications and group posts.
    """
    home_team_esc = safe_escape(home_team)
    away_team_esc = safe_escape(away_team)

    def _format_events(data) -> str:
        if not data:
            return ""
        if isinstance(data, dict):
            items = [f"{p} ({c})" if c > 1 else f"{p} (1)" for p, c in data.items() if c > 0]
            return ", ".join(items)
        if isinstance(data, list):
            return ", ".join(data)
        return str(data)

    h_goals_str = _format_events(h_goals)
    a_goals_str = _format_events(a_goals)
    h_assists_str = _format_events(h_assists)
    a_assists_str = _format_events(a_assists)

    lines = []
    # Home Team Stats
    if h_score > 0:
        lines.append(f"⚽ <b>Голы ({home_team_esc}):</b> {safe_escape(h_goals_str) if h_goals_str else 'не указаны'}")
        if is_single_timeline:
            lines.append(f"🎯 <b>Ассисты ({home_team_esc}):</b> <i>не отображаются в данном формате скриншота</i>")
        else:
            lines.append(f"🎯 <b>Ассисты ({home_team_esc}):</b> {safe_escape(h_assists_str) if h_assists_str else 'Нет'}")

    # Away Team Stats
    if a_score > 0:
        lines.append(f"⚽ <b>Голы ({away_team_esc}):</b> {safe_escape(a_goals_str) if a_goals_str else 'не указаны'}")
        if is_single_timeline:
            lines.append(f"🎯 <b>Ассисты ({away_team_esc}):</b> <i>не отображаются в данном формате скриншота</i>")
        else:
            lines.append(f"🎯 <b>Ассисты ({away_team_esc}):</b> {safe_escape(a_assists_str) if a_assists_str else 'Нет'}")

    # 👑 Игрок матча — только если золотая корона действительно распознана.
    if mvp_player and str(mvp_player).strip():
        lines.append(f"👑 <b>Игрок матча (MVP):</b> {safe_escape(str(mvp_player).strip())}")

    events_block = ("\n\n" + "\n".join(lines)) if lines else ""

    match_info = database.get_match(match_id) if match_id else None

    p1_clean = safe_escape(p1_username.lstrip('@')) if p1_username else ""
    p2_clean = safe_escape(p2_username.lstrip('@')) if p2_username else ""
    p1_str = f" (@{p1_clean})" if p1_clean else ""
    p2_str = f" (@{p2_clean})" if p2_clean else ""

    div_label = ""
    if match_info and match_info.get("division_id"):
        div_row = database.get_division(match_info["division_id"])
        if div_row:
            div_label = f" • {safe_escape(div_row['name'])}"

    if is_draft:
        header = (
            f"📝 <b>ЧЕРНОВИК РЕЗУЛЬТАТА | Тур {round_number}{div_label}</b>\n\n"
            f"🏠 <b>{home_team_esc}</b>{p1_str} <b>{h_score} : {a_score}</b> <b>{away_team_esc}</b>{p2_str} ✈️"
        )
        footer = "\n\n⏳ <i>Ожидает подтверждения администратором...</i>"
    elif is_pm:
        match_id_str = f" #{match_id}" if match_id else ""
        header = (
            f"{pm_title}\n\n"
            f"🏟 <b>Матч{match_id_str} (Тур {round_number}{div_label})</b>\n"
            f"🏠 <b>{home_team_esc}</b> <b>{h_score} : {a_score}</b> <b>{away_team_esc}</b> ✈️"
        )
        footer = "\n\n📊 <i>Турнирная таблица и статистика игроков обновлены.</i>"
    else:
        header = (
            f"🏆 <b>РЕЗУЛЬТАТ МАТЧА | Тур {round_number}{div_label}</b>\n\n"
            f"🏠 <b>{home_team_esc}</b>{p1_str} <b>{h_score} : {a_score}</b> <b>{away_team_esc}</b>{p2_str} ✈️"
        )
        footer = "\n\n📸 <i>Результат официально занесен в турнирную таблицу.</i>"

    return f"{header}{events_block}{footer}"

    return f"{header}{events_block}{footer}"

def collect_report_payload(context: ContextTypes.DEFAULT_TYPE, match: dict) -> dict:
    """Collect the entered report data from user_data into a storable payload.

    Reads the ACTUAL keys written by both reporting flows:
      - AI flow & manual flow: report_home_goals/report_away_goals +
        home_goals_count/away_goals_count/home_assists_count/away_assists_count.
    Returns {h_score, a_score, scorers[], assists[], photo_id, mvp_player} where
    scorers/assists are [{player_name, team_name, count}] suitable for
    confirm_and_finalize_match event conversion.
    """
    ud = context.user_data
    home_team = match.get('player1_team') or match.get('player1_nickname') or "Хозяева"
    away_team = match.get('player2_team') or match.get('player2_nickname') or "Гости"

    scorers = []
    assists = []
    for name, cnt in (ud.get("home_goals_count") or {}).items():
        if cnt:
            scorers.append({"player_name": name, "team_name": home_team, "count": int(cnt)})
    for name, cnt in (ud.get("away_goals_count") or {}).items():
        if cnt:
            scorers.append({"player_name": name, "team_name": away_team, "count": int(cnt)})
    for name, cnt in (ud.get("home_assists_count") or {}).items():
        if cnt:
            assists.append({"player_name": name, "team_name": home_team, "count": int(cnt)})
    for name, cnt in (ud.get("away_assists_count") or {}).items():
        if cnt:
            assists.append({"player_name": name, "team_name": away_team, "count": int(cnt)})

    photos = ud.get("ai_photos_list") or []
    photo_id = photos[0] if photos else ud.get("report_photo_id")

    return {
        "h_score": ud.get("report_home_goals", 0) or 0,
        "a_score": ud.get("report_away_goals", 0) or 0,
        "scorers": scorers,
        "assists": assists,
        "photo_id": photo_id,
        "mvp_player": ud.get("report_mvp_player"),
    }


async def build_debt_footer(match: dict) -> str:
    """Short note appended to result posts when the match was an overdue debt:
    lists each participant and the warn that will be removed (new balance)."""
    try:
        match_id = match["id"]
        if not await asyncio.to_thread(database.is_match_overdue, match_id):
            return ""
        if await asyncio.to_thread(database.has_debt_stage, match_id, "reward_given"):
            # Already rewarded earlier — no note on re-posted results
            return ""

        parts = []
        seen = set()
        for pid_key, team_key in (("player1_id", "player1_team"), ("player2_id", "player2_team")):
            p_id = match.get(pid_key)
            if not p_id and match.get(team_key):
                u = await asyncio.to_thread(database.find_user_by_team, match.get(team_key))
                p_id = dict(u)["telegram_id"] if u else None
            if not p_id or p_id in seen:
                continue
            seen.add(p_id)

            u_row = await asyncio.to_thread(database.get_user, p_id)
            u = dict(u_row) if u_row else {}
            cur = u.get("warn_count") or 0
            new_w = max(0, cur - 1)
            tag = f"@{u['username']}" if u.get("username") else f"ID {p_id}"
            suffix = "" if cur > 0 else " (был 0)"
            parts.append(f"{html.escape(tag)} → <b>{new_w}/{MAX_WARNS_LIMIT}</b>{suffix}")

        if not parts:
            return ""
        return "\n\n🎁 <b>Матч был долгом — списан 1 варн:</b> " + " · ".join(parts)
    except Exception as e:
        logger.warning(f"build_debt_footer failed for match {match.get('id')}: {e}")
        return ""


async def cb_confirm_ai_final(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instantly save and finalize match score in database from AI Vision result."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    match_id = int(query.data.replace("cb_confirm_ai_final_", ""))
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await query.answer("❌ Матч не найден.", show_alert=True)
        return

    if match['status'] == 'confirmed':
        await query.answer("✅ Результат уже зафиксирован!", show_alert=True)
        return

    user_id = query.from_user.id
    h_score = context.user_data.get("report_home_goals", 0)
    a_score = context.user_data.get("report_away_goals", 0)
    h_goals = context.user_data.get("home_goals_count", {})
    a_goals = context.user_data.get("away_goals_count", {})
    h_assists = context.user_data.get("home_assists_count", {})
    a_assists = context.user_data.get("away_assists_count", {})
    photo_id = context.user_data.get("report_photo_id")
    is_single_tl = bool(context.user_data.get("is_single_timeline", False))
    mvp_player = context.user_data.get("report_mvp_player")

    home_team = match['player1_team'] or match['player1_nickname']
    away_team = match['player2_team'] or match['player2_nickname']

    events = []
    for p, c in h_goals.items():
        events.append((home_team, p, "goal", c))
    for p, c in a_goals.items():
        events.append((away_team, p, "goal", c))
    for p, c in h_assists.items():
        events.append((home_team, p, "assist", c))
    for p, c in a_assists.items():
        events.append((away_team, p, "assist", c))

    await asyncio.to_thread(
        database.confirm_and_finalize_match, match_id, h_score, a_score, events,
        reporter_id=user_id, photo_id=photo_id, mvp_player=mvp_player,
    )
    context.user_data.pop("report_mvp_player", None)
    await refresh_debts_summary(context)
    await refresh_league_table(context, division_id=match.get("division_id"))

    # Short "debt closed" note appended to result posts when applicable
    debt_note = await build_debt_footer(match)

    # 1. PM to reporter
    reporter_text = build_formatted_match_post(
        round_number=match['round_number'],
        home_team=home_team,
        away_team=away_team,
        h_score=h_score,
        a_score=a_score,
        h_goals=h_goals,
        a_goals=a_goals,
        h_assists=h_assists,
        a_assists=a_assists,
        is_single_timeline=is_single_tl,
        is_pm=True,
        pm_title="🎉 <b>Результат успешно занесен в лигу!</b>",
        match_id=match_id,
        mvp_player=mvp_player
    ) + debt_note

    is_admin_user = is_admin(user_id) or context.user_data.get("is_admin_reporting", False)
    if is_admin_user:
        back_buttons = [
            [InlineKeyboardButton("« Назад к матчу", callback_data=f"admin_view_match_{match_id}")],
            [InlineKeyboardButton("« Назад к туру", callback_data=f"admin_round_matches_{match['round_number']}")]
        ]
    else:
        back_buttons = [[InlineKeyboardButton("« К своим матчам", callback_data="cabinet_my_matches")]]

    try:
        await query.edit_message_caption(caption=reporter_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_buttons))
    except Exception:
        await context.bot.send_message(chat_id=user_id, text=reporter_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(back_buttons))

    # 2. PM to players
    players_to_notify = []
    if user_id == match['player1_id']:
        if match['player2_id']: players_to_notify.append(match['player2_id'])
    elif user_id == match['player2_id']:
        if match['player1_id']: players_to_notify.append(match['player1_id'])
    else:
        if match['player1_id']: players_to_notify.append(match['player1_id'])
        if match['player2_id']: players_to_notify.append(match['player2_id'])

    opp_text = build_formatted_match_post(
        round_number=match['round_number'],
        home_team=home_team,
        away_team=away_team,
        h_score=h_score,
        a_score=a_score,
        h_goals=h_goals,
        a_goals=a_goals,
        h_assists=h_assists,
        a_assists=a_assists,
        is_single_timeline=is_single_tl,
        is_pm=True,
        pm_title="🔔 <b>Результат вашего матча занесен в лигу!</b>",
        match_id=match_id,
        mvp_player=mvp_player
    ) + debt_note

    for p_id in set(players_to_notify):
        await safe_send_notification(context.bot, p_id, opp_text)

    # 3. Post to Group
    div_id = match.get("division_id")
    target_chat_id, target_topic_id = await resolve_division_target(
        div_id, "results", "reports",
        legacy_topic_keys=("results_topic_id", "reports_topic_id"),
    )
    if not target_chat_id:
        logger.warning(f"No results/reports topic configured for division {div_id}; skipping group result announcement.")

    if target_chat_id:
        group_text = build_formatted_match_post(
            round_number=match['round_number'],
            home_team=home_team,
            away_team=away_team,
            h_score=h_score,
            a_score=a_score,
            p1_username=match['player1_username'],
            p2_username=match['player2_username'],
            h_goals=h_goals,
            a_goals=a_goals,
            h_assists=h_assists,
            a_assists=a_assists,
            is_single_timeline=is_single_tl,
            is_pm=False,
            match_id=match_id,
            mvp_player=mvp_player
        ) + debt_note
        try:
            kwargs = {"chat_id": target_chat_id, "caption": group_text, "parse_mode": "HTML"}
            if target_topic_id:
                kwargs["message_thread_id"] = int(target_topic_id)
            if photo_id:
                await context.bot.send_photo(photo=photo_id, **kwargs)
            else:
                kwargs["text"] = group_text
                kwargs.pop("caption", None)
                await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.exception("Failed to post result to group")

    # Process debt reward (-1 warn) and all-debts-cleared notification
    await handle_debt_played_rewards(
        context,
        match_id=match_id,
        round_number=match['round_number'],
        p1_id=match.get('player1_id'),
        p2_id=match.get('player2_id')
    )

async def submit_report_to_guest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    match_id = None
    if query.data and query.data.startswith("cb_submit_report_to_guest_"):
        try:
            match_id = int(query.data.replace("cb_submit_report_to_guest_", ""))
        except ValueError:
            pass
    if not match_id:
        match_id = context.user_data.get("reporting_match_id")

    match = await asyncio.to_thread(database.get_match, match_id) if match_id else None
    if not match:
        await query.answer("❌ Ошибка: матч не найден.", show_alert=True)
        return

    if match['status'] == 'confirmed':
        await query.answer("⛔ Результат этого матча уже занесён в таблицу!", show_alert=True)
        return

    submitter_id = query.from_user.id

    # Admin-entered results are final by definition: save immediately instead
    # of routing through the opponent-confirmation / admin-review pipeline.
    if is_admin(submitter_id) or context.user_data.get("is_admin_reporting"):
        payload = collect_report_payload(context, match)
        events = _pending_report_events(match, payload)
        next_stage = await asyncio.to_thread(
            database.confirm_and_finalize_match,
            match_id, payload["h_score"], payload["a_score"], events,
            reporter_id=submitter_id,
            photo_id=payload.get("photo_id"),
            mvp_player=payload.get("mvp_player"),
        )
        await asyncio.to_thread(database.delete_pending_report, match_id)
        await notify_match_confirmed(context, match_id)
        await refresh_debts_summary(context)
        await refresh_league_table(context, division_id=match.get("division_id"))

        try:
            await query.edit_message_caption(
                caption=f"✅ <b>Результат матча #{match_id} занесён в таблицу!</b>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            try:
                await query.edit_message_text(
                    f"✅ <b>Результат матча #{match_id} занесён в таблицу!</b>",
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception:
                pass
        return

    is_submitter_home = (submitter_id == match['player1_id'])
    opp_id = match['player2_id'] if is_submitter_home else match['player1_id']
    opp_team = match['player2_team'] if is_submitter_home else match['player1_team']

    if not opp_id:
        await query.answer("⚠️ Соперник не зарегистрирован в боте. Результат будет отправлен администратору.", show_alert=True)
        await submit_report_to_admin(update, context)
        return

    # Check opponent user status
    opp_user = await asyncio.to_thread(database.get_user, opp_id)
    opp_user = dict(opp_user) if opp_user else None
    if not opp_user or not opp_user.get("telegram_id"):
        await query.answer("⚠️ У соперника не найден Telegram ID. Отправляем администратору.", show_alert=True)
        await submit_report_to_admin(update, context)
        return

    # Fetch recorded report details (real keys written by AI/manual flows)
    payload = collect_report_payload(context, match)
    home_team = match['player1_team'] or match['player1_nickname']
    away_team = match['player2_team'] or match['player2_nickname']
    h_score = payload["h_score"]
    a_score = payload["a_score"]
    scorers = payload["scorers"]
    assists = payload["assists"]
    photo_id = payload.get("photo_id")

    # Persist report so the opponent (or an admin) can finalize it later from their own chat
    await asyncio.to_thread(database.save_pending_report, match_id, submitter_id, payload)

    # Format text for opponent confirmation
    sc_text = "\n".join([f"⚽ {s['player_name']} ({s['team_name']}) — {s['count']}" for s in scorers]) if scorers else "<i>(нет голов)</i>"
    ast_text = "\n".join([f"🎯 {a['player_name']} ({a['team_name']}) — {a['count']}" for a in assists]) if assists else "<i>(нет ассистов)</i>"

    text = (
        f"🔔 <b>ПОДТВЕРЖДЕНИЕ РЕЗУЛЬТАТА МАТЧА #{match_id}</b>\n\n"
        f"Соперник отправил результат вашей очной встречи:\n"
        f"🏠 <b>{safe_escape(home_team)}</b> <b>{h_score} : {a_score}</b> <b>{safe_escape(away_team)}</b> ✈️\n\n"
        f"<b>Авторы голов:</b>\n{sc_text}\n\n"
        f"<b>Ассистенты:</b>\n{ast_text}\n\n"
        f"{_mvp_card_line(payload)}"
        f"Пожалуйста, подтвердите результат или отклоните его, если данные неверны."
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data=f"cb_guest_confirm_{match_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"cb_guest_reject_{match_id}")
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    try:
        if photo_id:
            await context.bot.send_photo(chat_id=opp_id, photo=photo_id, caption=text, parse_mode="HTML", reply_markup=markup)
        else:
            await context.bot.send_message(chat_id=opp_id, text=text, parse_mode="HTML", reply_markup=markup)
        
        await safe_edit_or_reply(
            query, context,
            f"✅ <b>Результат матча #{match_id} отправлен сопернику на подтверждение!</b>\n\n"
            f"Ожидайте подтверждения от второго игрока.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад в кабинет", callback_data="menu_cabinet")]])
        )
    except Exception as e:
        logger.warning(f"Could not send match confirmation to opponent {opp_id}: {e}")
        await query.answer("⚠️ Не удалось связаться с соперником. Отправляем администратору.", show_alert=True)
        await submit_report_to_admin(update, context)


async def _build_pending_report_card(context: ContextTypes.DEFAULT_TYPE, match: dict, pending: dict) -> tuple[str, str | None]:
    """Build the human-readable confirmation card from a stored pending report."""
    home_team = match['player1_team'] or match['player1_nickname']
    away_team = match['player2_team'] or match['player2_nickname']
    h_score = pending.get("h_score", 0)
    a_score = pending.get("a_score", 0)
    scorers = pending.get("scorers") or []
    assists = pending.get("assists") or []
    sc_text = "\n".join([f"⚽ {s['player_name']} ({s['team_name']}) — {s['count']}" for s in scorers]) if scorers else "<i>(нет голов)</i>"
    ast_text = "\n".join([f"🎯 {a['player_name']} ({a['team_name']}) — {a['count']}" for a in assists]) if assists else "<i>(нет ассистов)</i>"
    text = (
        f"🔔 <b>ПОДТВЕРЖДЕНИЕ РЕЗУЛЬТАТА МАТЧА #{match['id']}</b>\n\n"
        f"🏠 <b>{safe_escape(str(home_team))}</b> <b>{h_score} : {a_score}</b> <b>{safe_escape(str(away_team))}</b> ✈️\n\n"
        f"<b>Авторы голов:</b>\n{sc_text}\n\n"
        f"<b>Ассистенты:</b>\n{ast_text}\n\n"
        f"{_mvp_card_line(pending)}"
        f"Пожалуйста, подтвердите результат или отклоните его."
    )
    return text, pending.get("photo_id")


def _mvp_card_line(report: dict) -> str:
    """MVP line for a confirmation card; empty when no gold crown was recognized."""
    mvp = str(report.get("mvp_player") or "").strip()
    return f"👑 <b>Игрок матча (MVP):</b> {safe_escape(mvp)}\n\n" if mvp else ""


def _pending_report_events(match: dict, pending: dict) -> list:
    """Convert stored scorers/assists into confirm_and_finalize_match event tuples."""
    home_team = match['player1_team'] or match['player1_nickname']
    away_team = match['player2_team'] or match['player2_nickname']
    events = []
    for s in (pending.get("scorers") or []):
        team = s.get('team_name') or home_team
        events.append((team, s['player_name'], "goal", s.get('count', 1)))
    for a in (pending.get("assists") or []):
        team = a.get('team_name') or home_team
        events.append((team, a['player_name'], "assist", a.get('count', 1)))
    return events


async def cb_guest_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opponent (or admin) confirms a manually submitted result stored in pending_reports."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    try:
        match_id = int(query.data.rsplit("_", 1)[-1])
    except ValueError:
        return

    presser_id = query.from_user.id
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await query.answer("❌ Матч не найден.", show_alert=True)
        return

    is_participant = presser_id in (match['player1_id'], match['player2_id'])
    if not (is_participant or is_admin(presser_id)):
        await query.answer("⛔ Подтвердить результат могут только участники матча или администраторы.", show_alert=True)
        return

    if match['status'] == 'confirmed':
        await query.answer("✅ Результат уже зафиксирован!", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    pending = await asyncio.to_thread(database.get_pending_report, match_id)
    if not pending:
        await query.answer("⚠️ Данные отчёта не найдены. Попросите соперника отправить результат заново.", show_alert=True)
        return

    h_score = int(pending.get("h_score", 0))
    a_score = int(pending.get("a_score", 0))
    events = _pending_report_events(match, pending)

    await asyncio.to_thread(
        database.confirm_and_finalize_match,
        match_id, h_score, a_score, events,
        reporter_id=pending.get("reporter_id"),
        photo_id=pending.get("photo_id"),
        mvp_player=pending.get("mvp_player"),
    )
    await asyncio.to_thread(database.delete_pending_report, match_id)

    # Full post-confirmation pipeline: PMs to both players, debt rewards, group post
    await notify_match_confirmed(context, match_id)
    await refresh_debts_summary(context)
    await refresh_league_table(context, division_id=match.get("division_id"))

    reporter_tag = f"@{pending['reporter_id']}"
    try:
        await query.edit_message_caption(
            caption=f"✅ <b>Результат матча #{match_id} подтверждён и занесён в таблицу!</b>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        try:
            await query.edit_message_text(
                f"✅ <b>Результат матча #{match_id} подтверждён и занесён в таблицу!</b>",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass


async def cb_guest_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opponent (or admin) rejects a manually submitted result."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    try:
        match_id = int(query.data.rsplit("_", 1)[-1])
    except ValueError:
        return

    presser_id = query.from_user.id
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await query.answer("❌ Матч не найден.", show_alert=True)
        return

    is_participant = presser_id in (match['player1_id'], match['player2_id'])
    if not (is_participant or is_admin(presser_id)):
        await query.answer("⛔ Отклонить результат могут только участники матча или администраторы.", show_alert=True)
        return

    pending = await asyncio.to_thread(database.get_pending_report, match_id)
    await asyncio.to_thread(database.delete_pending_report, match_id)

    try:
        await query.edit_message_text(
            f"❌ <b>Результат матча #{match_id} отклонён.</b>\n\n"
            f"Матч остаётся несыгранным. Свяжитесь с соперником и согласуйте верный счёт.",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        pass

    reporter_id = pending.get("reporter_id") if pending else None
    if reporter_id and reporter_id != presser_id:
        try:
            await context.bot.send_message(
                chat_id=reporter_id,
                text=(
                    f"❌ <b>Ваш результат матча #{match_id} был отклонён</b> "
                    f"({'администратором' if is_admin(presser_id) else 'соперником'})."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def submit_report_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback route: send the entered result card to all admins with apply/reject buttons."""
    from config import ADMIN_IDS

    query = update.callback_query
    match_id = context.user_data.get("reporting_match_id")
    if not match_id and query and query.data:
        try:
            match_id = int(query.data.rsplit("_", 1)[-1])
        except ValueError:
            match_id = None
    if not match_id:
        return

    match = await asyncio.to_thread(database.get_match, match_id)
    if not match or match['status'] == 'confirmed':
        return

    submitter_id = query.from_user.id if query and query.from_user else 0
    payload = collect_report_payload(context, match)
    await asyncio.to_thread(database.save_pending_report, match_id, submitter_id, payload)

    text, photo_id = await _build_pending_report_card(context, match, payload)
    text += "\n\n<i>Отправлено администратору на проверку.</i>"
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Занести результат", callback_data=f"cb_guest_confirm_{match_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"cb_guest_reject_{match_id}"),
        ]
    ])

    sent_any = False
    for aid in set(ADMIN_IDS):
        try:
            if photo_id:
                await context.bot.send_photo(chat_id=aid, photo=photo_id, caption=text, parse_mode="HTML", reply_markup=markup)
            else:
                await context.bot.send_message(chat_id=aid, text=text, parse_mode="HTML", reply_markup=markup)
            sent_any = True
        except Exception as e:
            logger.warning(f"Failed to forward report #{match_id} to admin {aid}: {e}")

    if submitter_id and sent_any:
        try:
            await context.bot.send_message(
                chat_id=submitter_id,
                text=f"📨 Результат матча #{match_id} отправлен администрации на проверку.",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def refresh_debts_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh the debts summary in the ПРЕДЫ thread after a match result is recorded."""
    try:
        from handlers.admin import _post_or_update_debts_in_warns
        await _post_or_update_debts_in_warns(context)
    except Exception as e:
        logger.warning(f"Failed to refresh debts summary: {e}")


async def refresh_league_table(context: ContextTypes.DEFAULT_TYPE, division_id: int | None = None) -> None:
    """
    Refresh the graphic league table in the ОТЧЁТЫ topic after a match result.

    division_id=None означает «все активные дивизионы»: без него вызов молча
    ничего не делал, потому что post_league_table_to_reports выходит на None.
    """
    try:
        from handlers.base import post_league_table_to_reports
        await post_league_table_to_reports(context, division_id=division_id)
    except Exception as e:
        logger.warning(f"Failed to refresh league table: {e}")

async def handle_debt_played_rewards(
    context: ContextTypes.DEFAULT_TYPE,
    match_id: int,
    round_number: int,
    p1_id: int | None = None,
    p2_id: int | None = None
) -> None:
    """Check if the completed match was overdue and reward players with -1 warn."""
    is_overdue = await asyncio.to_thread(database.is_match_overdue, match_id)
    if not is_overdue:
        return

    # Idempotency guard: reward only once per match even if the confirmation
    # pipeline fires multiple times (draft confirm + admin re-entry, etc.)
    if await asyncio.to_thread(database.has_debt_stage, match_id, "reward_given"):
        return

    match = await asyncio.to_thread(database.get_match, match_id)
    if match:
        if not p1_id and match.get("player1_id"):
            p1_id = match["player1_id"]
        if not p1_id and match.get("player1_team"):
            u1 = await asyncio.to_thread(database.find_user_by_team, match.get("player1_team"))
            if u1:
                p1_id = u1["telegram_id"]

        if not p2_id and match.get("player2_id"):
            p2_id = match["player2_id"]
        if not p2_id and match.get("player2_team"):
            u2 = await asyncio.to_thread(database.find_user_by_team, match.get("player2_team"))
            if u2:
                p2_id = u2["telegram_id"]

    results_summary = []
    rewards_applied = False
    for p_id in (p1_id, p2_id):
        if not p_id:
            continue
        try:
            # get_user returns a raw sqlite3.Row (no .get) — convert to dict.
            # NOTE: the unwarn below is applied BEFORE building the summary
            # entry, so record_debt_stage("reward_given") must run even if
            # something here fails — otherwise the reward would re-fire.
            u_info_row = await asyncio.to_thread(database.get_user, p_id)
            new_warns, was_unwarned = await asyncio.to_thread(
                database.apply_debt_played_reward, p_id, round_number
            )
            # The unwarn has now been committed — from this point the stage
            # marker MUST be recorded even if summary/DM building fails.
            rewards_applied = True
            u_info = (
                {
                    "username": u_info_row["username"] if "username" in u_info_row.keys() else None,
                    "team_name": u_info_row["team_name"] if "team_name" in u_info_row.keys() else None,
                }
                if u_info_row is not None else {}
            )
            results_summary.append({
                "user_id": p_id,
                "username": u_info.get("username"),
                "team_name": u_info.get("team_name"),
                "new_warns": new_warns,
                "was_unwarned": was_unwarned
            })
            if was_unwarned:
                reward_text = (
                    f"🎉 <b>Матч-долг закрыт: Снят 1 варн!</b>\n\n"
                    f"Результат матча <b>{round_number}-го тура</b> успешно внесён в базу.\n"
                    f"🎁 За закрытие задолженности с вашего аккаунта <b>списан 1 варн</b>.\n"
                    f"📊 Ваш текущий баланс варнов: <b>{new_warns}/{MAX_WARNS_LIMIT}</b>\n\n"
                    f"<i>Спасибо за оперативность!</i>"
                )
                await safe_send_notification(context.bot, p_id, reward_text)
            else:
                zero_warn_text = (
                    f"✅ <b>Матч-долг успешно закрыт!</b>\n\n"
                    f"Результат матча <b>{round_number}-го тура</b> внесён в базу лиги.\n"
                    f"📊 Ваш баланс варнов чист: <b>0/{MAX_WARNS_LIMIT}</b>"
                )
                await safe_send_notification(context.bot, p_id, zero_warn_text)

            # Check if all debts are cleared
            remaining_debts = await asyncio.to_thread(database.count_user_remaining_debts, p_id)
            if remaining_debts == 0:
                all_clear_text = (
                    f"🟢 <b>Отличная работа! Все долги закрыты</b>\n\n"
                    f"У вас больше нет просроченных матчей в лиге.\n"
                    f"Автоматические напоминания и штрафные таймеры отключены. Удачи в следующих турах! ⚽"
                )
                await safe_send_notification(context.bot, p_id, all_clear_text)
        except Exception as e:
            logger.warning(f"Failed to process debt played reward for user {p_id}: {e}")

    if rewards_applied:
        # Mark rewards as granted for this match so repeat invocations are no-ops
        try:
            await asyncio.to_thread(database.record_debt_stage, match_id, "reward_given")
        except Exception as e:
            logger.error(f"CRITICAL: failed to record reward_given stage for match {match_id}: {e}")

    # Post unwarn notification to ПРЕДЫ thread
    if results_summary:
        try:
            from handlers.admin import _send_to_warns_thread
            lines = [
                f"🟢 <b>ДОЛГ СЫГРАН: СНЯТИЕ ВАРНОВ</b>\n",
                f"🏆 <b>{round_number}-й тур</b> (Матч #{match_id})\n",
                f"📊 <b>Текущий баланс варнов участников:</b>"
            ]
            for r in results_summary:
                u_tag = f"@{html.escape(r['username'])}" if r.get("username") else f"ID {r['user_id']}"
                t_tag = f" [{html.escape(r['team_name'])}]" if r.get("team_name") else ""
                badge = "🎁 <i>(Снят 1 варн)</i>" if r["was_unwarned"] else "✅ <i>(Баланс чист)</i>"
                lines.append(f"• {u_tag}{t_tag} — <b>{r['new_warns']}/{MAX_WARNS_LIMIT}</b> {badge}")

            lines.append("\n<i>Результат матча внесён в таблицу лиги.</i>")
            await _send_to_warns_thread(context, "\n".join(lines), (match or {}).get("division_id"))
        except Exception as e:
            logger.warning(f"Failed to send unwarn summary to warns thread: {e}")


async def notify_match_confirmed(context: ContextTypes.DEFAULT_TYPE, match_id: int) -> None:
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        return

    home_team = match['player1_team'] or match['player1_nickname']
    away_team = match['player2_team'] or match['player2_nickname']
    p1_score = match['player1_score']
    p2_score = match['player2_score']

    events = await asyncio.to_thread(database.get_match_events, match_id)

    home_goals = [f"{e['player_name']} ({e['count']})" if e['count'] > 1 else f"{e['player_name']} (1)" for e in events if e['event_type'] == 'goal' and e['team_name'].lower() == home_team.lower()]
    away_goals = [f"{e['player_name']} ({e['count']})" if e['count'] > 1 else f"{e['player_name']} (1)" for e in events if e['event_type'] == 'goal' and e['team_name'].lower() == away_team.lower()]

    home_assists = [f"{e['player_name']} ({e['count']})" if e['count'] > 1 else f"{e['player_name']} (1)" for e in events if e['event_type'] == 'assist' and e['team_name'].lower() == home_team.lower()]
    away_assists = [f"{e['player_name']} ({e['count']})" if e['count'] > 1 else f"{e['player_name']} (1)" for e in events if e['event_type'] == 'assist' and e['team_name'].lower() == away_team.lower()]

    # Short "debt closed" note for result posts
    debt_note = await build_debt_footer(match)

    pm_text = build_formatted_match_post(
        round_number=match['round_number'],
        home_team=home_team,
        away_team=away_team,
        h_score=p1_score,
        a_score=p2_score,
        h_goals=home_goals,
        a_goals=away_goals,
        h_assists=home_assists,
        a_assists=away_assists,
        is_pm=True,
        pm_title="✅ <b>Матч успешно подтвержден и сыгран!</b>",
        match_id=match_id,
        mvp_player=match.get("mvp_player")
    ) + debt_note

    for p_id in (match['player1_id'], match['player2_id']):
        if p_id:
            await safe_send_notification(context.bot, p_id, pm_text)

    # Process debt reward (-1 warn) and all-debts-cleared notification
    await handle_debt_played_rewards(
        context,
        match_id=match_id,
        round_number=match['round_number'],
        p1_id=match['player1_id'],
        p2_id=match['player2_id']
    )

    div_id = match.get("division_id")
    target_chat_id, target_topic_id = await resolve_division_target(
        div_id, "results", "reports",
        legacy_topic_keys=("results_topic_id", "reports_topic_id"),
    )
    if not target_chat_id:
        logger.warning(f"No results/reports topic configured for division {div_id}; skipping admin-approved group result announcement.")

    if target_chat_id:
        group_text = build_formatted_match_post(
            round_number=match['round_number'],
            home_team=home_team,
            away_team=away_team,
            h_score=p1_score,
            a_score=p2_score,
            p1_username=match['player1_username'],
            p2_username=match['player2_username'],
            h_goals=home_goals,
            a_goals=away_goals,
            h_assists=home_assists,
            a_assists=away_assists,
            is_pm=False,
            match_id=match_id,
            mvp_player=match.get("mvp_player")
        ) + debt_note

        photo_id = match.get("photo_id")
        try:
            if photo_id:
                kwargs = {"chat_id": target_chat_id, "photo": photo_id, "caption": group_text, "parse_mode": "HTML"}
                if target_topic_id:
                    kwargs["message_thread_id"] = int(target_topic_id)
                await context.bot.send_photo(**kwargs)
            else:
                kwargs = {"chat_id": target_chat_id, "text": group_text, "parse_mode": "HTML"}
                if target_topic_id:
                    kwargs["message_thread_id"] = int(target_topic_id)
                await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.exception("Failed to post result to topic/group")

SQUAD_PHOTO = 101

async def show_my_squad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        user = query.from_user
    else:
        user = update.effective_user

    if not check_group_card_access(update):
        if query:
            await query.answer("⛔ Просмотр и управление карточками в общем чате доступны только администраторам. Откройте ЛС с ботом!", show_alert=True)
        return ConversationHandler.END

    db_user = await asyncio.to_thread(database.get_user, user.id)
    if not db_user:
        return ConversationHandler.END

    photo_id = db_user['squad_photo_id']
    keyboard = [
        [InlineKeyboardButton("👥 Загрузить основу", callback_data="cabinet_upload_squad")],
        [InlineKeyboardButton("👥 Загрузить резерв / скамейку", callback_data="cabinet_upload_reserves")],
        [InlineKeyboardButton("« Назад в кабинет", callback_data="menu_cabinet")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if photo_id:
        text = "📸 **Ваш текущий состав:**"
        if query:
            try:
                await query.message.delete()
            except Exception:
                pass
        await context.bot.send_photo(chat_id=user.id, photo=photo_id, caption=text, parse_mode="Markdown", reply_markup=markup)
    else:
        text = "📸 **Ваш состав:**\n\nУ вас еще не загружен состав."
        if query:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
            
    return ConversationHandler.END

async def start_upload_squad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()

    context.user_data["squad_upload_mode"] = "main"
    text = (
        "📤 <b>Загрузка основы</b>\n\n"
        "Пожалуйста, отправьте скриншот стартового состава (11 игроков) <i>одним фото</i>.\n"
        "ИИ распознает футболистов и предложит сохранить их в состав."
    )
    keyboard = [[InlineKeyboardButton("Отмена", callback_data="cabinet_my_squad")]]
    
    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        
    return SQUAD_PHOTO


async def start_upload_reserves(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt the user to upload a screenshot of their reserve/bench players."""
    query = update.callback_query
    if query:
        await query.answer()

    context.user_data["squad_upload_mode"] = "reserves"
    text = (
        "👥 <b>Загрузка резерва / скамейки</b>\n\n"
        "Пожалуйста, отправьте скриншот экрана <b>«Резервисты»</b> или списка запасных <i>одним фото</i>.\n"
        "ИИ распознает футболистов и добавит их к вашему составу, сохранив основу!"
    )
    keyboard = [[InlineKeyboardButton("Отмена", callback_data="cabinet_my_squad")]]

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query:
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    return SQUAD_PHOTO

async def save_squad_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or (update.effective_chat and update.effective_chat.type != "private"):
        return ConversationHandler.END
        
    # Get the best quality photo
    photo_id = update.message.photo[-1].file_id
    user_id = update.effective_user.id
    mode = context.user_data.pop("squad_upload_mode", "main")
    is_reserves = (mode == "reserves")

    db_user_row = await asyncio.to_thread(database.get_user, user_id)
    db_user = dict(db_user_row) if db_user_row else None
    team_name = db_user.get("team_name") if db_user else None

    if not is_reserves:
        await asyncio.to_thread(database.update_single_field, user_id, "squad_photo_id", photo_id)
        await update.message.reply_text("✅ Основа состава успешно сохранена!")
        await show_my_squad(update, context)

        # database.get_user отдаёт sqlite3.Row — у него нет .get(), поэтому обращение
        # к division_id роняло весь блок публикации состава в топик СОСТАВЫ.
        u_div_id = db_user.get("division_id") if db_user else None
        target_chat_id, target_topic_id = await resolve_division_target(
            u_div_id, "lineups", legacy_topic_keys=("squad_topic_id",)
        )

        if target_chat_id and target_topic_id:
            try:
                t_name = db_user['team_name'] if db_user and db_user.get('team_name') else "Неизвестный клуб"
                username = update.effective_user.username
                username_str = f"@{username}" if username else update.effective_user.first_name
                caption = f"📸 <b>Обновление состава!</b>\n\n<b>Игрок:</b> {html.escape(username_str)}\n<b>Клуб:</b> {html.escape(t_name)}"
                
                await context.bot.send_photo(
                    chat_id=target_chat_id,
                    message_thread_id=int(target_topic_id),
                    photo=photo_id,
                    caption=caption,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.exception(f"Error sending squad photo to topic: {e}")
    else:
        await update.message.reply_text("✅ Скриншот резерва принят в обработку!")

    if team_name:
        from handlers.squad_ai import offer_recognized_squad
        try:
            await offer_recognized_squad(
                update, context,
                club=team_name,
                file_id=photo_id,
                back_cb="cabinet_my_squad",
                is_reserves=is_reserves,
            )
        except Exception as e:
            logger.exception(f"Squad recognition failed for {team_name}: {e}")

    return ConversationHandler.END

async def cancel_upload_squad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await show_my_squad(update, context)
    return ConversationHandler.END

async def cabinet_view_squad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
        
    await safe_query_answer(query)
    opp_id = int(query.data.replace("cabinet_view_squad_", ""))
    opp_user = await asyncio.to_thread(database.get_user, opp_id)
    
    if not opp_user:
        await safe_query_answer(query, "Игрок не найден.", show_alert=True)
        return
        
    photo_id = opp_user['squad_photo_id']
    team_name = opp_user['team_name'] or opp_user['username']
    
    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else query.from_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if photo_id:
        try:
            await context.bot.send_photo(
                chat_id=target_chat_id,
                message_thread_id=thread_id,
                photo=photo_id, 
                caption=f"⚽ Состав команды <b>{html.escape(team_name)}</b>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.exception("Failed to send squad photo")
    else:
        await safe_query_answer(query, f"❌ Команда {team_name} еще не загрузила свой состав.", show_alert=True)

