"""
handlers/bot_menu.py

Меню команд бота (кнопка «/» в Telegram). Игроки видят только `/start`; админы
лиги и дивизионов — ещё и админские команды. Telegram хранит меню по скоупам:
общее (`BotCommandScopeDefault`) и личное для чата с конкретным пользователем
(`BotCommandScopeChat`), которое перекрывает общее. Поэтому админу пишется
личное меню, а при снятии прав оно удаляется — и человек снова видит общее.

Личное меню работает только в личке с ботом, и Telegram отказывает («chat not
found»), пока админ не написал боту ни разу. Это не ошибка: меню появится после
следующего рестарта или переназначения. Меню — лишь подсказка, права
проверяет сама команда.
"""

import asyncio
import logging

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.error import TelegramError

import config
import database
from handlers.league_overview import can_view_overview

logger = logging.getLogger(__name__)

DEFAULT_COMMANDS = [
    BotCommand("start", "Открыть главное меню"),
]

ADMIN_COMMANDS = DEFAULT_COMMANDS + [
    BotCommand("overview", "Сводка по всем дивизионам"),
]


async def set_default_menu(bot) -> None:
    await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())


async def refresh_admin_menu(bot, user_id: int) -> bool:
    """Выставить пользователю меню по его текущим правам. True — меню админа.

    Никогда не бросает: вызывается после назначения / снятия админа, и сбой
    меню не должен ломать сам этот экран.
    """
    if not user_id or user_id <= 0:
        return False
    scope = BotCommandScopeChat(chat_id=user_id)
    try:
        is_admin = await asyncio.to_thread(can_view_overview, user_id)
        if is_admin:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
        return is_admin
    except TelegramError as e:
        logger.info(f"Admin command menu not updated for user={user_id}: {e}")
    except Exception as e:
        logger.warning(f"Admin command menu failed for user={user_id}: {e}")
    return False


async def sync_admin_menus(bot) -> int:
    """На старте: меню админа всем, у кого сейчас есть права. Возвращает, скольким выставлено."""
    candidates = set(await asyncio.to_thread(database.get_admin_candidate_ids))
    candidates.update(int(uid) for uid in config.ADMIN_IDS)
    applied = 0
    for user_id in sorted(candidates):
        if await refresh_admin_menu(bot, user_id):
            applied += 1
    return applied
