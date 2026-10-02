"""Сообщения трансферного окна: три темы группы ТО и личка.

Тренерам — в ЛС, всё остальное — в одну группу с темами `requests`, `feed`,
`alerts` (привязываются ссылкой на тему в панели `/to`). Не привязана тема
или Telegram отказал — сообщение не теряется молча: ответственный получает его
в ЛС с пометкой, что в группу оно не ушло. Здесь ничего не бросает наружу.
"""

from __future__ import annotations

import html
import logging

from telegram.error import TelegramError

import config
from time_utils import MSK_LABEL, fmt_msk
from transfers import repo

logger = logging.getLogger(__name__)

TOPIC_LABELS = {"requests": "Заявки", "feed": "Лента", "alerts": "Алерты"}

KIND_LABELS = {
    "deal": "сделка", "free_agent": "свободный агент", "surcharge": "доплата",
    "urn_sale": "продажа в урну", "urn_buy": "покупка из урны",
}


async def dm_user(bot, user_id: int | None, text: str, reply_markup=None) -> bool:
    if not user_id:
        return False
    try:
        await bot.send_message(chat_id=int(user_id), text=text, parse_mode="HTML",
                               reply_markup=reply_markup, disable_web_page_preview=True)
        return True
    except TelegramError as exc:
        logger.info("transfers: DM to %s failed: %s", user_id, exc)
        return False
    except Exception:
        logger.exception("transfers: DM to %s failed", user_id)
        return False


async def dm_manager(bot, text: str, reply_markup=None) -> bool:
    """Ответственному в ЛС. Не задан `TRANSFER_MANAGER_ID` — некому, False."""
    return await dm_user(bot, getattr(config, "TRANSFER_MANAGER_ID", None), text, reply_markup)


async def post_to_topic(bot, topic_type: str, text: str, reply_markup=None) -> bool:
    """В тему группы ТО. Не вышло — копия ответственному с причиной, False."""
    label = TOPIC_LABELS.get(topic_type, topic_type)
    try:
        topic = repo.get_topic(topic_type)
    except Exception:
        logger.exception("transfers: topic lookup failed")
        topic = None
    if not topic:
        await dm_manager(bot, f"⚠️ Тема «{label}» не привязана — сообщение в группу не отправлено:\n\n{text}")
        return False
    try:
        await bot.send_message(chat_id=topic["group_chat_id"], message_thread_id=topic["message_thread_id"],
                               text=text, parse_mode="HTML", reply_markup=reply_markup,
                               disable_web_page_preview=True)
        return True
    except Exception as exc:
        logger.warning("transfers: post to topic %s failed: %s", topic_type, exc)
        await dm_manager(bot, f"⚠️ Не удалось отправить в тему «{label}»: "
                              f"{html.escape(str(exc))}\n\n{text}")
        return False


def _window_title(window: dict) -> str:
    title = (window.get("title") or "").strip()
    return f"«{html.escape(title)}»" if title else f"№{window['id']}"


def describe_transfer(t: dict) -> str:
    """Одна строка о заявке: №, вид, игрок, откуда → куда."""
    route = " → ".join(html.escape(c) for c in (t.get("from_club"), t.get("to_club")) if c)
    kind = KIND_LABELS.get(t.get("kind"), t.get("kind") or "")
    line = f"#{t['id']} {kind}: <b>{html.escape(t.get('player_name') or '')}</b>"
    return f"{line} ({route})" if route else line


async def announce_open(bot, window: dict) -> bool:
    lines = [f"🔓 <b>Трансферное окно {_window_title(window)} открыто</b>"]
    if window.get("auto_close_at"):
        lines.append(f"Закроется {fmt_msk(window['auto_close_at'])} {MSK_LABEL}.")
    lines.append("Заявки подаются в Mini App.")
    return await post_to_topic(bot, "feed", "\n".join(lines))


async def announce_close(bot, window: dict, rejected: list[dict], *, auto: bool) -> None:
    """Лента — о закрытии; тренерам — об их автоотклонённых заявках; алерты — сводка."""
    how = "автоматически" if auto else "ответственным"
    await post_to_topic(bot, "feed", f"🔒 <b>Трансферное окно {_window_title(window)} закрыто</b> ({how}).")
    if not rejected:
        return

    failed: list[int] = []
    recipients: dict[int, list[dict]] = {}
    for t in rejected:
        for uid in {t.get("from_user"), t.get("to_user"), t.get("initiator_id")}:
            if uid:
                recipients.setdefault(int(uid), []).append(t)
    for uid, items in recipients.items():
        text = ("❌ Окно закрылось, а вторая сторона не подтвердила заявку — она отклонена:\n"
                + "\n".join(describe_transfer(t) for t in items))
        if not await dm_user(bot, uid, text):
            failed.append(uid)

    lines = [f"🔒 Окно закрыто — отклонено неподтверждённых заявок: {len(rejected)}"]
    lines += [describe_transfer(t) for t in rejected]
    if failed:
        lines.append("⚠️ Не дошло в ЛС (бот заблокирован или не запущен): "
                     + ", ".join(f"<code>{uid}</code>" for uid in failed))
    await post_to_topic(bot, "alerts", "\n".join(lines))
