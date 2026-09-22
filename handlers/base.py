import os
import sys
import io
import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, WebAppInfo
from telegram.ext import ContextTypes
import html
import asyncio
import database
import logging
import config
from time_utils import now_msk
from config import ADMIN_IDS
from services.graphics.table_generator import generate_league_table_image
from services.graphics import top_stats_generator
from constants import (
    CB_MAIN_MENU, CB_MENU_CABINET,
    CB_MENU_DIVISIONS, CB_MENU_SUPPORT,
    CB_ADMIN_MAIN_MENU, CUP_DIVISION_SENTINEL
)

logger = logging.getLogger(__name__)


async def resolve_division_id(update: Update, user_data=None) -> int | None:
    """
    Определить дивизион, в контексте которого говорит пользователь.

    Приоритет: топик дивизиона → группа дивизиона → дивизион самого игрока.
    В личке первые два шага не срабатывают, остаётся привязка из users.division_id.
    Возвращает None, если игрок никуда не приписан — вызывающий сам решает, что
    показать, но кросс-дивизионные данные подсовывать вместо этого нельзя.

    Живёт здесь, а не в handlers/chat.py, потому что нужен и чату, и текстовым
    командам, а chat импортирует text_commands — общий помощник может лежать
    только ниже обоих.
    """
    chat = update.effective_chat
    msg = update.effective_message

    if chat and chat.type in ("group", "supergroup"):
        thread_id = getattr(msg, "message_thread_id", None) if msg else None
        if thread_id:
            try:
                from services.topic_cache import topic_cache
                binding = topic_cache.get_by_topic(chat.id, thread_id)
                if binding and binding.get("division_id"):
                    return binding["division_id"]
            except Exception:
                logger.warning("resolve_division_id: topic_cache lookup failed", exc_info=True)

        try:
            div = await asyncio.to_thread(database.get_division_by_group, chat.id)
            if div and div.get("id"):
                return div["id"]
        except Exception:
            logger.warning("resolve_division_id: division-by-group lookup failed", exc_info=True)

    if user_data is None:
        user = update.effective_user
        if user:
            try:
                user_data = await asyncio.to_thread(database.get_user, user.id)
            except Exception:
                logger.warning("resolve_division_id: get_user failed", exc_info=True)

    try:
        if user_data is not None and user_data["division_id"]:
            return user_data["division_id"]
    except (KeyError, IndexError):
        pass

    return None


async def resolve_division_target(
    division_id: int | None,
    *topic_types: str,
    legacy_topic_keys: tuple[str, ...] = (),
) -> tuple[int | None, int | None]:
    """
    Пара (chat_id, message_thread_id), куда публиковать сообщение дивизиона.

    Чат и тред всегда берутся из одной привязки, поэтому тред не может оказаться
    склеен с чужим чатом. Дивизионы живут в отдельных супергруппах, а глобальный
    `system_config.group_id` хранит ровно одну из них: пара «глобальный чат +
    тред дивизиона» уводила анонс в чужую группу или роняла отправку с
    "message thread not found".

    Порядок: привязанный топик дивизиона (первый найденный из `topic_types`) →
    тот же топик без chat_id, склеенный с легаси-группой (одногрупповые
    инсталляции) → группа дивизиона, тема General. Если у дивизиона не привязано
    ничего — возвращается (None, None): молча пропустить сообщение безопаснее,
    чем отправить его не тому дивизиону.

    Вызов с `division_id=None` — легаси-путь без дивизионов: глобальная группа
    плюс первый непустой ключ из `legacy_topic_keys` (`results_topic_id` и т.п.).
    """
    async def _legacy_chat() -> int | None:
        chat = config.GROUP_ID or await asyncio.to_thread(database.get_group_id)
        return int(chat) if chat else None

    try:
        is_cup = division_id is not None and int(division_id) == CUP_DIVISION_SENTINEL
    except (ValueError, TypeError):
        is_cup = False

    if is_cup:
        c_type = "line" if "line" in topic_types else "reports"
        try:
            cup_topic = await asyncio.to_thread(database.get_cup_topic, c_type)
            if cup_topic and cup_topic.get("group_chat_id") and cup_topic.get("message_thread_id"):
                return int(cup_topic["group_chat_id"]), int(cup_topic["message_thread_id"])
        except Exception:
            logger.warning("resolve_division_target: cup_topic lookup failed", exc_info=True)
        logger.warning(
            f"resolve_division_target: cup has no binding for {c_type}; message skipped."
        )
        return None, None

    if division_id:
        try:
            from services.topic_cache import topic_cache
            for t_type in topic_types:
                entry = topic_cache.get_by_division(division_id, t_type)
                if entry and entry.get("group_chat_id") and entry.get("message_thread_id"):
                    return int(entry["group_chat_id"]), int(entry["message_thread_id"])
        except Exception:
            logger.warning("resolve_division_target: topic_cache lookup failed", exc_info=True)

        try:
            topics_map = await asyncio.to_thread(database.get_division_topics_map, division_id)
        except Exception:
            logger.warning("resolve_division_target: topics map lookup failed", exc_info=True)
            topics_map = {}

        entries = [
            topics_map.get(database.normalize_topic_type(t_type))
            for t_type in topic_types
        ]
        for entry in entries:
            if entry and entry.get("group_chat_id") and entry.get("message_thread_id"):
                return int(entry["group_chat_id"]), int(entry["message_thread_id"])

        # Тред без chat_id — привязка одногрупповой инсталляции: тут глобальная
        # группа и есть та самая, в которой этот тред живёт.
        for entry in entries:
            if entry and entry.get("message_thread_id") and not entry.get("group_chat_id"):
                legacy = await _legacy_chat()
                if legacy:
                    return legacy, int(entry["message_thread_id"])
                break

        chat_id = await asyncio.to_thread(database.get_division_group_chat_id, division_id)
        if chat_id:
            # Глобальный topic_id — идентификатор треда внутри глобальной группы,
            # и осмыслен он только если дивизион живёт в ней же. В чужой группе
            # тот же номер указывает на чужую тему либо не существует вовсе.
            if legacy_topic_keys and int(chat_id) == (await _legacy_chat()):
                for key in legacy_topic_keys:
                    raw = await asyncio.to_thread(database.get_config, key)
                    if raw and str(raw).strip().isdigit():
                        return int(chat_id), int(str(raw).strip())
            return int(chat_id), None

        logger.warning(
            f"resolve_division_target: division {division_id} has no binding for "
            f"{topic_types or ('<any>',)}; message skipped."
        )
        return None, None

    legacy = await _legacy_chat()
    if not legacy:
        return None, None
    for key in legacy_topic_keys:
        raw = await asyncio.to_thread(database.get_config, key)
        if raw and str(raw).strip().isdigit():
            return legacy, int(str(raw).strip())
    return legacy, None


def is_admin(telegram_id: int) -> bool:
    """Check if the user is in configured Admin IDs, has admin role, or is assigned as a division admin."""
    if not telegram_id:
        return False
    import config
    if telegram_id in config.ADMIN_IDS:
        return True
    try:
        user = database.get_user(telegram_id)
        if user and (user["role"] if "role" in user.keys() else None) in ("admin", "division_admin"):
            return True
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM division_admins WHERE user_id = ?", (telegram_id,))
            if cur.fetchone():
                return True
    except Exception:
        pass
    return False


def is_global_admin(telegram_id: int) -> bool:
    """Check if the user is a superadmin/global admin, excluding division-scoped admins."""
    if not telegram_id:
        return False
    import config
    if telegram_id in config.ADMIN_IDS:
        return True
    try:
        user = database.get_user(telegram_id)
        if user:
            u_dict = dict(user)
            role = u_dict.get("role")
            if role == "division_admin" or u_dict.get("division_id"):
                return False
            if role == "admin":
                return True
    except Exception:
        pass

    # Check division_admins table: if assigned to a division, user is not global
    try:
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM division_admins WHERE user_id = ?", (telegram_id,))
            if cur.fetchone():
                return False
    except Exception:
        pass

    # Fallback to is_admin check (supports test suite mocks while preserving division isolation)
    return is_admin(telegram_id)


def is_admin_user(user_id: int) -> bool:
    """Check if the user has global admin privileges (excluding division-only admins)."""
    return is_global_admin(user_id)


def round_schedule_missing_message(round_number: int, division_name: str) -> str:
    """Единый текст отказа, когда тур пытаются открыть без расписания.

    Используется и текстовыми командами «Темшик открыть тур ...», и админ-панелью,
    чтобы админ видел одну и ту же формулировку независимо от точки входа.
    Название дивизиона экранируется — отправлять с `parse_mode="HTML"`.
    """
    return (
        f"❌ Нельзя открыть Тур {round_number} — {html.escape(str(division_name))}: "
        "расписание ещё не сгенерировано. Сначала создайте матчи через меню админа."
    )


def _format_round_list(round_numbers: list[int]) -> str:
    """«3», «3 и 4», «3, 4 и 5» — перечисление номеров туров в тексте админу."""
    nums = [str(r) for r in round_numbers]
    if len(nums) <= 1:
        return "".join(nums)
    return ", ".join(nums[:-1]) + " и " + nums[-1]


def _active_rounds_deadline(active_rounds: list[dict]) -> str:
    """Дедлайн, до которого дивизион заблокирован.

    Туры открываются парой с общим дедлайном, поэтому берём дедлайн самого
    старшего активного тура — он же и последний по времени открытия.
    """
    if not active_rounds:
        return ""
    return str(active_rounds[-1].get("deadline") or "")


def max_active_rounds_message(active_rounds: list[dict]) -> str:
    """Отказ админ-панели: свободных слотов под новые туры в дивизионе нет.

    На вход — строки из `database.get_active_open_rounds`. Отправлять с
    `parse_mode="HTML"`.
    """
    nums = [r["round_number"] for r in active_rounds]
    return (
        f"⛔ <b>В дивизионе уже открыты туры {_format_round_list(nums)}.</b>\n"
        f"Дедлайн: <code>{html.escape(_active_rounds_deadline(active_rounds))}</code>.\n"
        "Следующие туры можно открыть после наступления дедлайна."
    )


def is_logovo_access_allowed(user_id: int) -> bool:
    """
    Check if a user is permitted to access Logovo.bet.
    If LOGOVO_LOCKDOWN=true: only Global Admins are allowed.
    If LOGOVO_LOCKDOWN=false: regular access rules apply.
    """
    import config
    if config.is_global_lockdown_enabled():
        return is_admin_user(user_id)
    return True


from functools import wraps
from telegram.ext import ConversationHandler

def admin_only(func):
    """Decorator to enforce admin permissions and answer CallbackQuery early."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        query = update.callback_query
        user_id = user.id if user else (query.from_user.id if query else None)

        if not user_id or not is_admin(user_id):
            if query:
                try:
                    await query.answer("⛔ Доступ запрещён", show_alert=True)
                except Exception:
                    pass
            elif update.message:
                try:
                    await update.message.reply_text("❌ У вас нет прав доступа к этой панели.")
                except Exception:
                    pass
            return ConversationHandler.END

        if query:
            try:
                await query.answer()
            except Exception:
                pass

        return await func(update, context, *args, **kwargs)
    return wrapper

def get_main_inline_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Generate main InlineKeyboardMarkup based on the user's role (matched to screenshot)."""
    keyboard = []
    
    webapp_url = getattr(config, "WEBAPP_URL", "")
    if webapp_url and (webapp_url.startswith("https://") or "localhost" in webapp_url):
        keyboard.append([InlineKeyboardButton("🎰 Logovo.bet", web_app=WebAppInfo(url=webapp_url))])
    elif webapp_url and webapp_url.startswith("http"):
        keyboard.append([InlineKeyboardButton("🎰 Logovo.bet", url=webapp_url)])

    keyboard.append([InlineKeyboardButton("👤 Мой Кабинет", callback_data=CB_MENU_CABINET)])
    if is_admin(telegram_id):
        keyboard.append([InlineKeyboardButton("👑 Админ-панель", callback_data=CB_ADMIN_MAIN_MENU)])
        
    keyboard.extend([
        [InlineKeyboardButton("🏆 Дивизионы", callback_data=CB_MENU_DIVISIONS)],
        [InlineKeyboardButton("🆘 Поддержка", callback_data=CB_MENU_SUPPORT)]
    ])
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command: welcomes user and displays inline main menu."""
    if update.effective_chat.type != "private":
        try:
            bot_info = context.bot.username
            url = f"https://t.me/{bot_info}?start=menu" if bot_info else None
            kb = [[InlineKeyboardButton("📱 Открыть меню в ЛС", url=url)]] if url else None
            await update.message.reply_text(
                "ℹ️ Главное меню и личный кабинет доступны в <b>личных сообщениях</b> с ботом.",
                reply_markup=InlineKeyboardMarkup(kb) if kb else None,
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    user = update.effective_user
    if not user:
        return

    # Clear old text keyboards if they are stuck
    try:
        temp_msg = await update.message.reply_text("🔄 Загрузка...", reply_markup=ReplyKeyboardRemove())
        await temp_msg.delete()
    except Exception:
        pass

    # Determine role and upsert/match user
    role = "admin" if is_admin(user.id) else "user"
    try:
        await asyncio.to_thread(database.handle_user_startup, user.id, user.username, role)
    except Exception as e:
        logger.exception(f"Error in handle_user_startup for user {user.id}: {e}")

    # Deliver pending notification if exists
    try:
        if await asyncio.to_thread(database.get_pending_notification, user.id):
            team = await asyncio.to_thread(database.get_user_team, user.id)
            if team:
                await update.message.reply_text(
                    f"🎉 Организатор закрепил за вашим аккаунтом игровой клуб <b>{html.escape(team)}</b>! "
                    f"Теперь вам доступен Личный кабинет и участие в лиге.",
                    parse_mode="HTML"
                )
                await asyncio.to_thread(database.set_pending_notification, user.id, 0)
    except Exception as e:
        logger.warning(f"Error delivering notification to user {user.id}: {e}")

    first_name_clean = html.escape(user.first_name or "Участник")
    welcome_text = (
        f"⚽️ <b>Добро пожаловать в систему Лиги, {first_name_clean}!</b>\n\n"
        f"🏆 Здесь вы можете управлять своей карьерой, следить за турнирной таблицей и статистикой клубов.\n\n"
        f"👇 Выберите нужный раздел в меню ниже:"
    )
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_main_inline_keyboard(user.id),
        parse_mode="HTML"
    )

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback query handler to display the main inline menu."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    # Clear lingering FSM temporary data when returning to main menu
    context.user_data.clear()

    user = query.from_user
    first_name_clean = html.escape(user.first_name or "Участник")
    welcome_text = (
        f"⚽️ <b>Добро пожаловать в систему Лиги, {first_name_clean}!</b>\n\n"
        f"🏆 Здесь вы можете управлять своей карьерой, следить за турнирной таблицей и статистикой клубов.\n\n"
        f"👇 Выберите нужный раздел в меню ниже:"
    )
    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=user.id, text=welcome_text, reply_markup=get_main_inline_keyboard(user.id), parse_mode="HTML")
    else:
        try:
            await query.edit_message_text(welcome_text, reply_markup=get_main_inline_keyboard(user.id), parse_mode="HTML")
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=user.id, text=welcome_text, reply_markup=get_main_inline_keyboard(user.id), parse_mode="HTML")

DIV_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉", 4: "🎖️", 5: "🏅"}


async def show_divisions_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sub-menu displaying the list of active divisions for the current season."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    season_id = await asyncio.to_thread(database.get_active_season)
    if season_id is None:
        text = "🏆 <b>Дивизионы</b>\n\nСейчас нет активного сезона."
        keyboard = [[InlineKeyboardButton("« Назад в меню", callback_data=CB_MAIN_MENU)]]
    else:
        divisions = await asyncio.to_thread(database.get_active_divisions, int(season_id))
        keyboard = []
        for div in divisions:
            d_id = div["id"]
            d_name = div.get("name") or f"Дивизион {d_id}"
            emoji = DIV_EMOJIS.get(d_id, "⚽")
            keyboard.append([
                InlineKeyboardButton(f"{emoji} {d_name}", callback_data=f"division_view:{int(season_id)}:{d_id}")
            ])
        keyboard.append([InlineKeyboardButton("« Назад в меню", callback_data=CB_MAIN_MENU)])
        text = (
            "🏆 <b>Дивизионы</b>\n\n"
            "Выберите интересующий дивизион для просмотра турнирной таблицы и статистики:"
        )

    markup = InlineKeyboardMarkup(keyboard)

    if query:
        target_chat_id = query.message.chat_id if query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
        thread_id = query.message.message_thread_id if query.message and query.message.is_topic_message else None
        if query.message and query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=markup, parse_mode="HTML")
        else:
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=markup, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def show_division_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the menu for a specific division (Table, Scorers, Assists)."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    season_id = 1
    division_id = 1
    if context.matches:
        season_id = int(context.matches[0].group(1))
        division_id = int(context.matches[0].group(2))
    elif query and query.data:
        parts = query.data.split(":")
        if len(parts) >= 3:
            season_id = int(parts[1])
            division_id = int(parts[2])

    div_info = await asyncio.to_thread(database.get_division, division_id)
    div_name = div_info["name"] if div_info and "name" in div_info else f"Дивизион {division_id}"
    emoji = DIV_EMOJIS.get(division_id, "🏆")

    text = (
        f"{emoji} <b>{html.escape(div_name)}</b>\n\n"
        "Выберите интересующий раздел:"
    )

    keyboard = [
        [InlineKeyboardButton("📋 Турнирная таблица", callback_data=f"division_table:{season_id}:{division_id}")],
        [InlineKeyboardButton("⚽ Бомбардиры", callback_data=f"division_scorers:{season_id}:{division_id}")],
        [InlineKeyboardButton("🎯 Ассистенты", callback_data=f"division_assists:{season_id}:{division_id}")],
        [InlineKeyboardButton("« Назад к дивизионам", callback_data=CB_MENU_DIVISIONS)]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if query:
        target_chat_id = query.message.chat_id if query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
        thread_id = query.message.message_thread_id if query.message and query.message.is_topic_message else None
        if query.message and query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=markup, parse_mode="HTML")
        else:
            try:
                await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=markup, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


# Backward compatibility alias
show_league_menu = show_divisions_list


async def show_division_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and display graphic league table for a specific division and season."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    season_id = 1
    div_id = 1
    if context.matches:
        season_id = int(context.matches[0].group(1))
        div_id = int(context.matches[0].group(2))
    elif query and query.data:
        parts = query.data.split(":")
        if len(parts) >= 3:
            season_id = int(parts[1])
            div_id = int(parts[2])

    div_info = await asyncio.to_thread(database.get_division, div_id)
    div_name = div_info["name"] if div_info and "name" in div_info else f"Дивизион {div_id}"

    standings = await asyncio.to_thread(database.get_standings, division_id=div_id, season_id=season_id)
    form_map = await asyncio.to_thread(database.get_teams_recent_form, limit=5, division_id=div_id, season_id=season_id)
    img_buf = await asyncio.to_thread(generate_league_table_image, standings, form_map, div_name, div_id)
    if hasattr(img_buf, "seek"):
        img_buf.seek(0)

    keyboard = [
        [InlineKeyboardButton("« Назад к меню дивизиона", callback_data=f"division_view:{season_id}:{div_id}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    caption = f"🏆 <b>Турнирная таблица — {html.escape(div_name)}</b>"
    await context.bot.send_photo(
        chat_id=target_chat_id,
        message_thread_id=thread_id,
        photo=img_buf,
        caption=caption,
        parse_mode="HTML",
        reply_markup=markup
    )


async def show_division_scorers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and display graphic top scorers card for a specific division and season."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    season_id = 1
    div_id = 1
    if context.matches:
        season_id = int(context.matches[0].group(1))
        div_id = int(context.matches[0].group(2))
    elif query and query.data:
        parts = query.data.split(":")
        if len(parts) >= 3:
            season_id = int(parts[1])
            div_id = int(parts[2])

    div_info = await asyncio.to_thread(database.get_division, div_id)
    div_name = div_info["name"] if div_info and "name" in div_info else f"Дивизион {div_id}"

    img_buf = await asyncio.to_thread(
        top_stats_generator.generate_top_stats_image,
        mode="goals",
        limit=10,
        division_id=div_id,
        division_name=div_name,
        season_id=season_id,
    )
    if hasattr(img_buf, "seek"):
        img_buf.seek(0)

    keyboard = [
        [InlineKeyboardButton("« Назад к меню дивизиона", callback_data=f"division_view:{season_id}:{div_id}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    caption = f"⚽ <b>Топ бомбардиров — {html.escape(div_name)}</b>"
    await context.bot.send_photo(
        chat_id=target_chat_id,
        message_thread_id=thread_id,
        photo=img_buf,
        caption=caption,
        parse_mode="HTML",
        reply_markup=markup
    )


async def show_division_assists(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate and display graphic top assists card for a specific division and season."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    season_id = 1
    div_id = 1
    if context.matches:
        season_id = int(context.matches[0].group(1))
        div_id = int(context.matches[0].group(2))
    elif query and query.data:
        parts = query.data.split(":")
        if len(parts) >= 3:
            season_id = int(parts[1])
            div_id = int(parts[2])

    div_info = await asyncio.to_thread(database.get_division, div_id)
    div_name = div_info["name"] if div_info and "name" in div_info else f"Дивизион {div_id}"

    img_buf = await asyncio.to_thread(
        top_stats_generator.generate_top_stats_image,
        mode="assists",
        limit=10,
        division_id=div_id,
        division_name=div_name,
        season_id=season_id,
    )
    if hasattr(img_buf, "seek"):
        img_buf.seek(0)

    keyboard = [
        [InlineKeyboardButton("« Назад к меню дивизиона", callback_data=f"division_view:{season_id}:{div_id}")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    caption = f"🎯 <b>Топ ассистентов — {html.escape(div_name)}</b>"
    await context.bot.send_photo(
        chat_id=target_chat_id,
        message_thread_id=thread_id,
        photo=img_buf,
        caption=caption,
        parse_mode="HTML",
        reply_markup=markup
    )

async def show_round_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    
    round_number = int(query.data.replace("show_round_matches_", ""))
    matches, info = await asyncio.gather(
        asyncio.to_thread(database.get_matches_by_round, round_number),
        asyncio.to_thread(database.get_round_info, round_number)
    )
    
    text = f"📅 <b>Расписание: {round_number}-й Тур</b>\n"
    if info:
        is_open = info["is_open"]
        deadline_text = info["deadline"]
        
        if is_open and deadline_text:
            dt = database.parse_flexible_datetime(deadline_text)
            if dt and now_msk() > dt:
                text += "Статус: 🔴 Дедлайн истек (результаты принимаются только администратором)\n"
            else:
                text += f"Статус: 🟢 Открыт\nДедлайн: {deadline_text}\n"
        else:
            text += f"Статус: {'🟢 Открыт' if is_open else '🔴 Закрыт'}\n"
    text += "\n"
    
    for m in matches:
        p1 = html.escape(str(m["player1_team"] or m["player1_nickname"] or "Игрок 1"))
        p2 = html.escape(str(m["player2_team"] or m["player2_nickname"] or "Игрок 2"))
        if m["status"] == "confirmed":
            text += f"<b>{p1}</b> {m['player1_score']} : {m['player2_score']} <b>{p2}</b>\n<i>Статус: ✅ Завершен</i>\n\n"
        else:
            text += f"<b>{p1}</b> 🆚 <b>{p2}</b>\n<i>Статус: ⏳ Ожидается игра</i>\n\n"
            
    if not matches:
        text += "Матчи не найдены."
        
    keyboard = [[InlineKeyboardButton("« Назад в меню", callback_data="main_menu")]]
    markup = InlineKeyboardMarkup(keyboard)

    target_chat_id = query.message.chat_id if query and query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query.message and (query.message.photo or query.message.document):
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

async def group_table_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /table command in any chat — send graphic league standings, supporting division routing."""
    division_id = None
    division_name = None

    # Check topic ID with strict chat isolation
    chat_id = update.effective_chat.id if update.effective_chat else None
    thread_id = update.message.message_thread_id if update.message else None
    if thread_id and chat_id:
        from services.topic_cache import topic_cache
        binding = topic_cache.get_by_topic(chat_id, thread_id)
        if not binding:
            binding = await asyncio.to_thread(database.get_topic_binding, chat_id, thread_id)
        if binding:
            division_id = binding.get("division_id")
            division_name = binding.get("division_name")
        else:
            for t_type in ("tables", "drafts", "results", "reports"):
                div = await asyncio.to_thread(database.get_division_by_topic, thread_id, t_type, group_chat_id=chat_id)
                if div:
                    division_id = div.get("id")
                    division_name = div.get("name")
                    break

    # Check command arguments: /table [div_id_or_code]
    args = context.args or []
    if args:
        target = args[0].strip()
        div = None
        if target.isdigit():
            div = await asyncio.to_thread(database.get_division, int(target))
        if not div:
            div = await asyncio.to_thread(database.get_division_by_code, target)
        if div:
            division_id = div["id"]
            division_name = div["name"]

    if division_id:
        standings = await asyncio.to_thread(database.get_standings, division_id=division_id)
        form_map = await asyncio.to_thread(database.get_teams_recent_form, limit=5, division_id=division_id)
        img_buf = await asyncio.to_thread(generate_league_table_image, standings=standings, form_map=form_map, division_name=division_name, division_id=division_id)
        caption = f"🏆 <b>Турнирная таблица дивизиона «{html.escape(division_name or '')}»</b>"
        refresh_cb = f"refresh_div_table_{division_id}"
    else:
        standings = await asyncio.to_thread(database.get_standings)
        img_buf = await asyncio.to_thread(generate_league_table_image, standings=standings)
        caption = "🏆 <b>Турнирная таблица</b>"
        refresh_cb = "refresh_div_table_0"

    keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data=refresh_cb)]]
    await update.message.reply_photo(photo=img_buf, caption=caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def post_league_table_to_reports(context: ContextTypes.DEFAULT_TYPE, division_id: int | None = None) -> None:
    """Post or update the graphic league table in the reports topic for a specific division."""
    from telegram.error import BadRequest, TelegramError
    from services.topic_cache import topic_cache

    if division_id is None:
        # Раньше здесь был безусловный return, и все вызовы без division_id
        # (подтверждение результата, кнопка «Обновить таблицы») молча ничего
        # не делали. Без дивизиона обновляем таблицу каждого активного.
        divisions = await asyncio.to_thread(database.get_active_divisions)
        for d in divisions:
            await post_league_table_to_reports(context, division_id=d["id"])
        return

    div_topic = topic_cache.get_by_division(division_id, "reports")
    if not div_topic:
        div_topic = topic_cache.get_by_division(division_id, "tables")
    if not div_topic:
        topics_map = await asyncio.to_thread(database.get_division_topics_map, division_id)
        div_topic = topics_map.get("reports") or topics_map.get("tables")

    if not div_topic or not div_topic.get("group_chat_id") or not div_topic.get("message_thread_id"):
        logger.warning(f"No reports/tables topic configured for division {division_id}; skipping table posting.")
        return

    group_id = div_topic["group_chat_id"]
    reports_topic_id = div_topic["message_thread_id"]
    div_record = await asyncio.to_thread(database.get_division, division_id)
    division_name = div_record["name"] if div_record else f"Дивизион {division_id}"

    standings = await asyncio.to_thread(database.get_standings, division_id=division_id)
    form_map = await asyncio.to_thread(database.get_teams_recent_form, limit=5, division_id=division_id)
    img_buf = await asyncio.to_thread(generate_league_table_image, standings=standings, form_map=form_map, division_name=division_name, division_id=division_id)
    caption = f"🏆 <b>ТУРНИРНАЯ ТАБЛИЦА — {html.escape(division_name).upper()}</b>"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить таблицу", callback_data=f"refresh_div_table_{division_id}")]])
    config_key = f"league_table_msg_id_div_{division_id}"

    existing_raw = await asyncio.to_thread(database.get_config, config_key)
    existing_id = int(existing_raw) if str(existing_raw or "").strip().isdigit() else None

    if existing_id:
        try:
            from telegram import InputMediaPhoto
            await context.bot.edit_message_media(
                chat_id=group_id,
                message_id=existing_id,
                media=InputMediaPhoto(media=img_buf, caption=caption, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
        except (BadRequest, TelegramError):
            pass  # Deleted / too old — repost fresh

    try:
        kwargs = {"chat_id": group_id, "photo": img_buf, "caption": caption, "parse_mode": "HTML", "reply_markup": markup}
        if reports_topic_id:
            kwargs["message_thread_id"] = int(reports_topic_id)
        msg = await context.bot.send_photo(**kwargs)
        await asyncio.to_thread(database.set_config, config_key, str(msg.message_id))
    except Exception:
        logger.exception("Failed to post graphic league table to reports topic")

async def cb_refresh_division_table_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("🔄 Таблица обновлена!")

    div_id = None
    div_name = None
    if query.data and query.data.startswith("refresh_div_table_"):
        try:
            div_id = int(query.data.replace("refresh_div_table_", ""))
            div = await asyncio.to_thread(database.get_division, div_id)
            if div:
                div_name = div["name"]
        except Exception:
            pass

    if not div_id:
        return

    standings = await asyncio.to_thread(database.get_standings, division_id=div_id)
    form_map = await asyncio.to_thread(database.get_teams_recent_form, limit=5, division_id=div_id)
    img_buf = await asyncio.to_thread(generate_league_table_image, standings=standings, form_map=form_map, division_name=div_name, division_id=div_id)
    caption = f"🏆 <b>ТЕКУЩАЯ ТУРНИРНАЯ ТАБЛИЦА ДИВИЗИОНА «{html.escape(div_name)}»</b>"
    refresh_cb = f"refresh_div_table_{div_id}"

    keyboard = [[InlineKeyboardButton("🔄 Обновить таблицу", callback_data=refresh_cb)]]
    markup = InlineKeyboardMarkup(keyboard)

    try:
        from telegram import InputMediaPhoto
        await query.edit_message_media(
            media=InputMediaPhoto(media=img_buf, caption=caption, parse_mode="HTML"),
            reply_markup=markup
        )
    except Exception as e:
        if "Message is not modified" in str(e):
            await query.answer("✅ Данные таблицы уже актуальны!", show_alert=True)
        else:
            logger.exception("Failed to refresh graphic table")


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    keyboard = [[InlineKeyboardButton("« Назад в меню", callback_data="main_menu")]]
    markup = InlineKeyboardMarkup(keyboard)
    text = "🚧 **В разработке**\n\nРаздел поддержки находится в разработке."
    if query:
        await query.answer()
        target_chat_id = query.message.chat_id if query.message else (update.effective_chat.id if update.effective_chat else update.effective_user.id)
        thread_id = query.message.message_thread_id if query.message and query.message.is_topic_message else None
        if query.message and (query.message.photo or query.message.document):
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="Markdown", reply_markup=markup)
        else:
            try:
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            except Exception:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, parse_mode="Markdown", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
