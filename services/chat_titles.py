import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot

import database

logger = logging.getLogger(__name__)

# Telegram limits custom titles to 16 UTF-8 characters
MAX_CUSTOM_TITLE_LEN = 16


async def assign_club_title(bot: "Bot", chat_id: int, user_id: int, club_name: str) -> tuple[bool, str]:
    """
    Safely assign a custom title (club badge) to a user in a Telegram supergroup.
    Promotes the user to administrator with minimal benign privileges if they are not one,
    then sets their custom title up to 16 characters.

    Returns (ok, message).
    """
    if not club_name or not club_name.strip():
        return False, "Название клуба пустое"

    title = club_name.strip()[:MAX_CUSTOM_TITLE_LEN]

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        err_str = str(e).lower()
        if "participant" in err_str or "not found" in err_str:
            return False, "Пользователь не найден в группе"
        logger.warning(f"Failed to get chat member {user_id} in {chat_id}: {e}")
        return False, f"Ошибка проверки участника: {e}"

    # Cannot alter creator title via bot
    if getattr(member, "status", None) == "creator":
        return False, "Владелец группы (Telegram запрещает ботам менять звание создателя)"

    if getattr(member, "status", None) in ("left", "kicked"):
        return False, "Пользователь покинул группу"

    # Promote to admin if regular member or restricted
    if getattr(member, "status", None) != "administrator":
        try:
            await bot.promote_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                can_manage_chat=False,
                can_delete_messages=False,
                can_manage_video_chats=False,
                can_restrict_members=False,
                can_promote_members=False,
                can_change_info=False,
                can_invite_users=True,  # Minimal safe privilege to hold admin status
                can_pin_messages=False,
                can_manage_topics=False,
            )
        except Exception as e:
            err_str = str(e)
            if "CHAT_ADMIN_LIMIT_EXCEEDED" in err_str or "admin limit" in err_str.lower():
                return False, "Достигнут лимит Telegram (максимум 50 администраторов в группе)"
            if "not enough rights" in err_str.lower():
                return False, "У бота нет права назначать администраторов (требуется can_promote_members)"
            logger.warning(f"Failed to promote member {user_id} in {chat_id}: {e}")
            return False, f"Не удалось повысить участника: {err_str}"

    # Set custom title
    try:
        await bot.set_chat_administrator_custom_title(
            chat_id=chat_id,
            user_id=user_id,
            custom_title=title,
        )
        return True, f"Установлена плашка «{title}»"
    except Exception as e:
        err_str = str(e)
        if "rights_not_modified" in err_str.lower():
            return True, f"Плашка «{title}» уже установлена"
        if "not enough rights" in err_str.lower():
            return False, "У бота нет прав для изменения звания (возможно, админ был назначен владельцем)"
        logger.warning(f"Failed to set custom title for {user_id} in {chat_id}: {e}")
        return False, f"Не удалось установить звание: {err_str}"


async def sync_division_club_titles(bot: "Bot", chat_id: int, division_id: int | None = None) -> dict:
    """
    Synchronize custom titles for all registered coaches in a division/chat.
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

    # Проверяем права самого бота в группе заранее, чтобы не крутить цикл впустую
    try:
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        if getattr(bot_member, "status", None) == "administrator" and getattr(bot_member, "can_promote_members", True) is False:
            stats["error"] = "no_promote_rights"
            stats["details"].append("❌ У бота нет права «Добавление администраторов» (can_promote_members). Включите это право боту в настройках группы.")
            return stats
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
        await asyncio.sleep(0.35)

    return stats
