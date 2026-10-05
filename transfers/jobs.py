"""Фоновые задачи трансферного окна."""

from __future__ import annotations

import logging

from time_utils import MSK_LABEL, fmt_msk
from transfers import notify, service

logger = logging.getLogger(__name__)


async def job_auto_close(context) -> None:
    """Закрыть открытое окно, когда наступило его время автозакрытия."""
    window = service.due_auto_close()
    if window is None:
        return
    result = service.close_window(window["id"], service.SYSTEM_ACTOR)
    if not result.closed:
        return
    logger.info("transfers: window %s auto-closed, %d requests rejected", window["id"], len(result.rejected))
    bot = context.bot
    await notify.announce_close(bot, window, result.rejected, auto=True)
    await notify.announce_recap(bot, window)
    await notify.dm_manager(
        bot,
        f"🔒 Окно закрыто автоматически ({fmt_msk(window['auto_close_at'])} {MSK_LABEL}). "
        f"Отклонено неподтверждённых заявок: {len(result.rejected)}. "
        "Заявки, ждущие вашего решения, остались — /to",
    )


async def job_reminders(context) -> None:
    """Напоминания перед автозакрытием окна и ответственному о зависших заявках."""
    from transfers import reminders

    try:
        sent = await reminders.run(context.bot)
    except Exception:
        logger.exception("transfers: reminders failed")
        raise
    if sent:
        logger.info("transfers: %d reminder(s) sent", sent)
