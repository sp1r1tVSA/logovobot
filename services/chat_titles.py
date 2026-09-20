import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot

import database

from telegram.error import RetryAfter

logger = logging.getLogger(__name__)

# Telegram limits custom titles to 16 UTF-8 characters
MAX_CUSTOM_TITLE_LEN = 16


async def assign_club_title(
    bot: "Bot",
    chat_id: int,
    user_id: int,
    club_name: str,
    max_retries: int = 1,
) -> tuple[bool, str]:
    """
    Assign custom club tag to a user in a Telegram supergroup without giving admin permissions.
    Uses Telegram Bot API setChatMemberTag method (Bot API 9.5+).
    """
    if not club_name or not club_name.strip():
        return False, "Название клуба пустое"

    tag = club_name.strip()[:MAX_CUSTOM_TITLE_LEN]

    # 1. Нативный метод: setChatMemberTag (без назначения администратором!)
    # Участник остаётся обычным членом чата с 0 административных прав.
    if hasattr(bot, "_post"):
        try:
            await bot._post(
                "setChatMemberTag",
                data={
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "tag": tag,
                }
            )
            return True, f"Установлен тег «{tag}»"
        except RetryAfter as ra:
            if max_retries > 0:
                wait_s = max(1, int(ra.retry_after)) + 1
                logger.warning(f"RetryAfter during setChatMemberTag for {user_id}: sleeping {wait_s}s...")
                await asyncio.sleep(wait_s)
                return await assign_club_title(bot, chat_id, user_id, club_name, max_retries=max_retries - 1)
            return False, f"Flood control Telegram (повторите через {ra.retry_after}с)"
        except Exception as e:
            err_str = str(e)
            err_low = err_str.lower()
            if "not enough rights" in err_low or "can_manage_tags" in err_low or "rights" in err_low:
                return False, "У бота нет права «Изменение тегов участников» (can_manage_tags)"
            if "participant" in err_low or "not found" in err_low:
                return False, "Пользователь не найден в группе"
            if "creator" in err_low:
                return False, "Владелец группы (Telegram запрещает ботам менять тег создателя)"
            logger.info(f"setChatMemberTag failed for {user_id}: {e}, checking fallback...")

    # 2. Фолбэк на случай если пользователь уже является администратором
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        err_str = str(e).lower()
        if "participant" in err_str or "not found" in err_str:
            return False, "Пользователь не найден в группе"
        return False, f"Ошибка проверки участника: {e}"

    if getattr(member, "status", None) == "creator":
        return False, "Владелец группы (Telegram запрещает ботам менять звание создателя)"

    if getattr(member, "status", None) in ("left", "kicked"):
        return False, "Пользователь покинул группу"

    try:
        await bot.set_chat_administrator_custom_title(
            chat_id=chat_id,
            user_id=user_id,
            custom_title=tag,
        )
        return True, f"Установлена плашка «{tag}»"
    except Exception as e:
        err_str = str(e)
        if "rights_not_modified" in err_str.lower():
            return True, f"Тег «{tag}» уже установлен"
        logger.warning(f"Failed to set tag for {user_id} in {chat_id}: {err_str}")
        return False, f"Не удалось установить тег: {err_str}"


async def sync_division_club_titles(bot: "Bot", chat_id: int, division_id: int | None = None) -> dict:
    """
    Synchronize custom tags for all registered coaches in a division/chat.
    Iterates sequentially with rate-limit pauses.

    Returns stats dict: {
        'total': int,
        'success': int,
        'skipped': int,
        'failed': int,
        'details': list[str]
    }
    """
    coaches = await asyncio.to_thread(database.get_coaches_for_division, division_id)
    stats = {
        "total": len(coaches),
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
    }

    if not coaches:
        return stats

    # Проверяем права самого бота в группе заранее
    try:
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        if getattr(bot_member, "status", None) != "administrator":
            stats["error"] = "not_admin"
            stats["details"].append("❌ Бот не является администратором этого чата.")
            return stats

        can_manage_tags = getattr(bot_member, "can_manage_tags", None)
        if can_manage_tags is None:
            api_kwargs = getattr(bot_member, "api_kwargs", None) or {}
            can_manage_tags = api_kwargs.get("can_manage_tags")

        if can_manage_tags is False:
            stats["error"] = "no_tags_rights"
            stats["details"].append("❌ У бота нет права «Изменение тегов участников» (can_manage_tags). Включите это право боту в настройках группы.")
            return stats
    except RetryAfter as ra:
        wait_s = max(1, int(ra.retry_after)) + 1
        logger.warning(f"RetryAfter during check bot permissions: sleeping {wait_s}s...")
        await asyncio.sleep(wait_s)
    except Exception as e:
        logger.warning(f"Could not verify bot permissions upfront: {e}")

    for coach in coaches:
        user_id = coach.get("telegram_id")
        team_name = coach.get("team_name")
        username = coach.get("username")
        display = f"@{username}" if username else f"ID {user_id}"

        if not user_id or not team_name:
            stats["skipped"] += 1
            continue

        ok, msg = await assign_club_title(bot, chat_id, user_id, team_name)
        if ok:
            stats["success"] += 1
            stats["details"].append(f"✅ {display} ({team_name}): {msg}")
        else:
            if "не найден" in msg or "Владелец" in msg or "покинул" in msg:
                stats["skipped"] += 1
                stats["details"].append(f"⚠️ {display} ({team_name}): {msg}")
            else:
                stats["failed"] += 1
                stats["details"].append(f"❌ {display} ({team_name}): {msg}")

        # Rate-limiting pacing: Telegram allows ~20-30 admin API mutations per minute
        await asyncio.sleep(1.0)

    return stats
