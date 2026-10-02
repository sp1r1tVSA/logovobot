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
from handlers.base import is_global_admin
from handlers.league_overview import can_view_overview
from transfers.service import can_manage_window

logger = logging.getLogger(__name__)

DEFAULT_COMMANDS = [
    BotCommand("start", "Открыть главное меню"),
]

ADMIN_COMMANDS = DEFAULT_COMMANDS + [
    BotCommand("overview", "Сводка по всем дивизионам"),
]

# Глобальным админам — ещё эксплуатация (handlers/admin_ops.py, только в ЛС).
GLOBAL_ADMIN_COMMANDS = ADMIN_COMMANDS + [
    BotCommand("health", "Состояние бота"),
    BotCommand("backup", "Бэкап базы"),
    BotCommand("ocr_stats", "Метрики распознавания скриншотов"),
    BotCommand("audit", "Журнал действий админов"),
]

# Панель трансферного окна: ответственному (даже если он не админ) и админам из ADMIN_IDS.
TRANSFER_MANAGER_COMMANDS = [
    BotCommand("to", "Трансферное окно"),
]


async def set_default_menu(bot) -> None:
    await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())


async def refresh_admin_menu(bot, user_id: int) -> bool:
    """Выставить пользователю меню по его текущим правам. True — меню админа.

    Ответственный за ТО и админы из `ADMIN_IDS` получают ещё `/to`; если
    ответственный не админ, меню у него личное, но функция вернёт False.

    Никогда не бросает: вызывается после назначения / снятия админа, и сбой
    меню не должен ломать сам этот экран.
    """
    if not user_id or user_id <= 0:
        return False
    scope = BotCommandScopeChat(chat_id=user_id)
    try:
        is_admin = await asyncio.to_thread(can_view_overview, user_id)
        if is_admin:
            commands = GLOBAL_ADMIN_COMMANDS if is_global_admin(user_id) else ADMIN_COMMANDS
        else:
            commands = DEFAULT_COMMANDS
        can_transfer = can_manage_window(user_id)
        if can_transfer:
            commands = commands + TRANSFER_MANAGER_COMMANDS
        if is_admin or can_transfer:
            await bot.set_my_commands(commands, scope=scope)
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
    manager = getattr(config, "TRANSFER_MANAGER_ID", None)
    if manager:
        candidates.add(int(manager))
    applied = 0
    for user_id in sorted(candidates):
        if await refresh_admin_menu(bot, user_id):
            applied += 1
    return applied
