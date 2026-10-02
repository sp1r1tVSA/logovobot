"""Модерация чатов дивизионов: «Темшик мут 30м спам» и «Темшик размут».

Мут — нативное ограничение Telegram (`restrict_chat_member`), поэтому срок
снимает сам Telegram и боту не нужен ни планировщик, ни таблица: после
рестарта ничего не теряется. Каждый дивизион живёт в своей супергруппе, так
что «мут в группе» — это мут в чате этого дивизиона. Все действия пишутся в
журнал (`/audit`, категория «Дисциплина»).

Кто может: глобальные админы — в любом чате, админы дивизиона — только в чате
своего дивизиона. Админов лиги, админов группы и ботов заглушить нельзя.
Мут не связан с варнами и долгами: это отдельная мера, на кик она не влияет.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import timedelta

import telegram.error
from telegram import ChatPermissions, Update
from telegram.ext import ContextTypes

import database
from handlers.base import is_admin, is_global_admin
from services import admin_journal
from time_utils import MSK, now_msk

logger = logging.getLogger(__name__)

DEFAULT_MUTE = timedelta(hours=1)
MIN_MUTE = timedelta(minutes=1)
# Telegram считает ограничение дольше 366 суток вечным; вечный мут вручную не даём.
MAX_MUTE = timedelta(days=30)
MAX_REASON_LEN = 200

_MIN = r"м|мин|минут[а-яё]*|m|mins?|minutes?"
_HOUR = r"ч|час[а-яё]*|h|hrs?|hours?"
_DAY = r"д|дн|дня|дней|день|сут[а-яё]*|d|days?"
# «30м», «30 мин», «2ч», «на 1 день»: число и единица.
_NUMBERED = re.compile(
    rf"^\s*(?:на\s+)?(\d{{1,6}})\s*(?:({_MIN})|({_HOUR})|({_DAY}))(?![a-zа-яё])\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
# «на час», «минуту», «сутки»: без числа — только полные слова, чтобы «мин» или «д»
# в начале причины не принимались за срок.
_BARE = re.compile(
    r"^\s*(?:на\s+)?(?:(минут[уы]?)|(час(?:а|ов)?)|(день|сутки))(?![a-zа-яё])\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_UNIT_SECONDS = (60, 3600, 86400)


def parse_duration(text: str) -> tuple[timedelta | None, str]:
    """Вынуть срок из начала аргументов: (срок | None, остаток — причина)."""
    m = _NUMBERED.match(text or "")
    if m:
        unit = next(i for i, g in enumerate(m.group(2, 3, 4)) if g)
        return timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[unit]), m.group(5).strip()
    m = _BARE.match(text or "")
    if m:
        unit = next(i for i, g in enumerate(m.group(1, 2, 3)) if g)
        return timedelta(seconds=_UNIT_SECONDS[unit]), m.group(4).strip()
    return None, (text or "").strip()


def format_duration(delta: timedelta) -> str:
    """«30 мин», «2 ч», «1 дн 6 ч» — для сообщения в чат."""
    total = int(delta.total_seconds())
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and not days:
        parts.append(f"{minutes} мин")
    return " ".join(parts) or "1 мин"


def _mention(user_id: int, username: str | None, name: str | None) -> str:
    if username:
        return f"@{html.escape(username)}"
    return f'<a href="tg://user?id={user_id}">{html.escape(name or str(user_id))}</a>'


async def _chat_division_id(update: Update) -> int | None:
    """Дивизион чата по топику или группе. Без привязки игрока: админ чужого
    дивизиона не должен получить права из-за своей же записи в users."""
    chat = update.effective_chat
    msg = update.effective_message
    thread_id = getattr(msg, "message_thread_id", None)
    if thread_id:
        try:
            from services.topic_cache import topic_cache
            binding = topic_cache.get_by_topic(chat.id, thread_id)
            if binding and binding.get("division_id"):
                return binding["division_id"]
        except Exception:
            logger.warning("chat moderation: topic_cache lookup failed", exc_info=True)
    try:
        div = await asyncio.to_thread(database.get_division_by_group, chat.id)
        if div and div.get("id"):
            return div["id"]
    except Exception:
        logger.warning("chat moderation: division-by-group lookup failed", exc_info=True)
    return None


async def _check_access(update: Update) -> tuple[bool, int | None, str | None]:
    """(можно ли, дивизион чата, текст отказа)."""
    chat = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else 0
    if not chat or chat.type not in ("group", "supergroup"):
        return False, None, "ℹ️ Мут работает только в чатах дивизионов."
    division_id = await _chat_division_id(update)
    if is_global_admin(user_id):
        return True, division_id, None
    if division_id and await asyncio.to_thread(database.is_division_admin, user_id, division_id):
        return True, division_id, None
    return False, division_id, "⚠️ Мут могут выдавать только администраторы этого дивизиона."


class _Target:
    __slots__ = ("id", "username", "name", "is_bot")

    def __init__(self, user_id: int, username: str | None, name: str | None, is_bot: bool = False):
        self.id, self.username, self.name, self.is_bot = user_id, username, name, is_bot

    @property
    def mention(self) -> str:
        return _mention(self.id, self.username, self.name)


async def _resolve_target(msg, args_str: str) -> tuple[_Target | None, str]:
    """Нарушитель: автор сообщения, на которое ответила команда, иначе первый
    аргумент (@username или id). Возвращает (цель | None, остаток аргументов)."""
    reply = msg.reply_to_message
    # В форумном топике ответ «в пустоту» цепляется к служебному сообщению топика.
    if reply and reply.from_user and not getattr(reply, "forum_topic_created", None):
        u = reply.from_user
        return _Target(u.id, u.username, u.full_name, bool(u.is_bot)), args_str

    parts = args_str.split(None, 1)
    if not parts:
        return None, ""
    ref = parts[0]
    if not (ref.startswith("@") or ref.lstrip("@").isdigit()):
        return None, args_str
    found = await asyncio.to_thread(database.find_user_by_ref, ref)
    if not found:
        return None, args_str
    return _Target(int(found["telegram_id"]), found.get("username"), found.get("team_name")), (
        parts[1] if len(parts) > 1 else ""
    )


async def handle_mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE, args_str: str) -> None:
    """«Темшик мут [@user] [срок] [причина]» ответом на сообщение нарушителя."""
    msg = update.effective_message
    actor_id = update.effective_user.id

    allowed, division_id, denial = await _check_access(update)
    if not allowed:
        await msg.reply_text(denial, parse_mode="HTML")
        return

    target, rest = await _resolve_target(msg, args_str.strip())
    if not target:
        await msg.reply_text(
            "ℹ️ Ответьте командой на сообщение нарушителя:\n"
            "<code>Темшик мут 30м причина</code>\n"
            "Срок: <code>15м</code>, <code>2ч</code>, <code>1д</code> (от 1 минуты до 30 дней, "
            "без срока — 1 час). Без ответа: <code>Темшик мут @username 30м причина</code>.",
            parse_mode="HTML",
        )
        return

    duration, reason = parse_duration(rest)
    if duration is None:  # не `or`: «0м» — это timedelta(0), срок задан и неверен
        duration = DEFAULT_MUTE
    if duration < MIN_MUTE or duration > MAX_MUTE:
        await msg.reply_text("⚠️ Срок мута — от 1 минуты до 30 дней.", parse_mode="HTML")
        return
    reason = reason[:MAX_REASON_LEN]

    chat_id = update.effective_chat.id
    refusal = await _protected_reason(context, chat_id, actor_id, target)
    if refusal:
        await msg.reply_text(refusal, parse_mode="HTML")
        return

    # now_msk() naive: PTB принял бы его за UTC и сдвинул срок на 3 часа.
    until = now_msk().replace(tzinfo=MSK) + duration
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
    except telegram.error.TelegramError as e:
        logger.warning("chat mute failed for %s in %s: %s", target.id, chat_id, e)
        await msg.reply_text(_failure_text(e), parse_mode="HTML")
        return

    await admin_journal.record(
        actor_id, "chat_mute", "user", target.id,
        old=f"@{target.username}" if target.username else str(target.id),
        new=format_duration(duration), division_id=division_id, reason=reason or None,
    )
    text = (
        f"🔇 {target.mention} заглушен на <b>{format_duration(duration)}</b> "
        f"(до {until:%d.%m %H:%M} МСК)."
    )
    if reason:
        text += f"\n📝 Причина: {html.escape(reason)}"
    await msg.reply_text(text, parse_mode="HTML")


async def handle_unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE, args_str: str) -> None:
    """«Темшик размут [@user]» — ответом на сообщение или по @username."""
    msg = update.effective_message
    actor_id = update.effective_user.id

    allowed, division_id, denial = await _check_access(update)
    if not allowed:
        await msg.reply_text(denial, parse_mode="HTML")
        return

    target, _ = await _resolve_target(msg, args_str.strip())
    if not target:
        await msg.reply_text(
            "ℹ️ Ответьте командой на сообщение игрока: <code>Темшик размут</code>\n"
            "или укажите его: <code>Темшик размут @username</code>.",
            parse_mode="HTML",
        )
        return

    chat_id = update.effective_chat.id
    try:
        chat = await context.bot.get_chat(chat_id)
        permissions = chat.permissions or ChatPermissions(can_send_messages=True)
        await context.bot.restrict_chat_member(chat_id=chat_id, user_id=target.id, permissions=permissions)
    except telegram.error.TelegramError as e:
        logger.warning("chat unmute failed for %s in %s: %s", target.id, chat_id, e)
        await msg.reply_text(_failure_text(e), parse_mode="HTML")
        return

    await admin_journal.record(
        actor_id, "chat_unmute", "user", target.id,
        old=f"@{target.username}" if target.username else str(target.id),
        division_id=division_id,
    )
    await msg.reply_text(f"🔊 С {target.mention} снят мут.", parse_mode="HTML")


async def _protected_reason(context, chat_id: int, actor_id: int, target: _Target) -> str | None:
    """Почему этого человека нельзя заглушить (None — можно)."""
    if target.id == actor_id:
        return "🤨 Себя заглушить нельзя."
    if target.is_bot or target.id == context.bot.id:
        return "🤖 Ботов заглушить нельзя."
    if await asyncio.to_thread(is_admin, target.id):
        return "🛡 Администраторов лиги заглушить нельзя."
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status in ("administrator", "creator"):
            return "🛡 Администраторов чата заглушить нельзя."
    except telegram.error.TelegramError:
        pass  # не смогли узнать статус — решит сам restrict_chat_member
    return None


def _failure_text(error: telegram.error.TelegramError) -> str:
    return (
        "❌ Telegram не принял команду: "
        f"<code>{html.escape(str(error))}</code>\n"
        "Проверьте, что у бота есть право администратора «Блокировка пользователей»."
    )
