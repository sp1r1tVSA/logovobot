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
from transfers.engine import format_k

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


_global_bot = None


def set_bot(bot) -> None:
    global _global_bot
    _global_bot = bot


def get_bot():
    return _global_bot


def format_request_card(t: dict) -> str:
    """Полная карточка заявки для темы `requests`."""
    kind = KIND_LABELS.get(t.get("kind"), t.get("kind") or "")
    lines = [f"📋 <b>Заявка #{t['id']} — {kind}</b>", ""]
    lines.append(f"Игрок: <b>{html.escape(t.get('player_name') or '')}</b>")
    if t.get("ovr"):
        lines.append(f"OVR: <b>{t['ovr']}</b>")

    if t.get("kind") == "deal":
        lines.append(f"Продавец: <b>{html.escape(t.get('from_club') or '—')}</b>")
        lines.append(f"Покупатель: <b>{html.escape(t.get('to_club') or '—')}</b>")
        lines.append(f"Сумма: <b>{format_k(t.get('price_k'))}</b>")
    elif t.get("kind") == "surcharge":
        lines.append(f"Клуб: <b>{html.escape(t.get('to_club') or '—')}</b>")
        lines.append(f"Доплата за спешл: <b>{format_k(t.get('price_k'))}</b>")
    elif t.get("kind") == "urn_sale":
        lines.append(f"Клуб: <b>{html.escape(t.get('from_club') or '—')}</b>")
        sellable_label = "да" if t.get("sellable") else "нет (непродаваемая)"
        lines.append(f"Выплата из урны: <b>{format_k(t.get('price_k'))}</b>")
        lines.append(f"По TM: {format_k(t.get('tm_price_k'))} | Спешл: {format_k(t.get('special_price_k'))} | Продаваемая: {sellable_label}")
    elif t.get("kind") == "urn_buy":
        lines.append(f"Покупатель: <b>{html.escape(t.get('to_club') or '—')}</b>")
        lines.append(f"Цена выкупа: <b>{format_k(t.get('price_k'))}</b>")
    elif t.get("kind") == "free_agent":
        lines.append(f"Клуб: <b>{html.escape(t.get('to_club') or '—')}</b>")
        lines.append(f"Откуда: <b>{html.escape(t.get('from_club') or '—')}</b>")
        lines.append(f"Сумма: <b>{format_k(t.get('price_k'))}</b>")
        if t.get("commented_at"):
            lines.append(f"Комментарий: {fmt_msk(t['commented_at'])} {MSK_LABEL}")

    warnings_raw = t.get("warnings")
    if warnings_raw:
        try:
            import json
            warns = json.loads(warnings_raw) if isinstance(warnings_raw, str) else warnings_raw
            if warns:
                lines.append("")
                lines.append("⚠️ <b>Предупреждения:</b>")
                for w in warns:
                    msg = w.get("message") if isinstance(w, dict) else str(w)
                    lines.append(f"• {html.escape(msg)}")
        except Exception:
            pass

    return "\n".join(lines)


async def post_request_card(bot, transfer: dict, *, photo_bytes: bytes | None = None,
                            reply_markup=None) -> bool:
    """Отправить карточку заявки в тему `requests` (с фото при наличии)."""
    if bot is None:
        bot = get_bot()
    if bot is None:
        logger.warning("transfers: post_request_card called without bot")
        return False

    text = format_request_card(transfer)
    label = TOPIC_LABELS.get("requests", "Заявки")
    try:
        topic = repo.get_topic("requests")
    except Exception:
        logger.exception("transfers: topic lookup failed")
        topic = None

    if not topic:
        await dm_manager(bot, f"⚠️ Тема «{label}» не привязана — заявка #{transfer['id']} не отправлена в группу:\n\n{text}")
        return False

    chat_id = topic["group_chat_id"]
    thread_id = topic["message_thread_id"]

    try:
        if photo_bytes:
            msg = await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=thread_id,
                photo=photo_bytes,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            if msg.photo:
                file_id = msg.photo[-1].file_id
                repo.set_transfer_photo(transfer["id"], file_id)
            return True
        elif transfer.get("photo_file_id"):
            await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=thread_id,
                photo=transfer["photo_file_id"],
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return True
        else:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return True
    except Exception as exc:
        logger.warning("transfers: failed to post request card #%s: %s", transfer["id"], exc)
        await dm_manager(bot, f"⚠️ Не удалось отправить заявку #{transfer['id']} в тему «{label}»: "
                              f"{html.escape(str(exc))}\n\n{text}")
        return False


async def notify_deal_proposal(bot, transfer: dict, *, photo_bytes: bytes | None = None) -> bool:
    """Уведомить вторую сторону о предложении сделки."""
    if bot is None:
        bot = get_bot()
    if bot is None:
        return False

    initiator_id = transfer.get("initiator_id")
    counterparty = transfer.get("to_user") if initiator_id == transfer.get("from_user") else transfer.get("from_user")
    if not counterparty:
        return False

    initiator_club = transfer.get("from_club") if initiator_id == transfer.get("from_user") else transfer.get("to_club")
    role_desc = "продать вам игрока" if initiator_id == transfer.get("from_user") else "купить у вас игрока"

    text = (
        f"🤝 <b>Предложение сделки #{transfer['id']}</b>\n\n"
        f"Клуб <b>{html.escape(initiator_club or '')}</b> предлагает {role_desc}:\n"
        f"• Игрок: <b>{html.escape(transfer.get('player_name') or '')}</b>"
        + (f" (OVR {transfer['ovr']})" if transfer.get('ovr') else "") + "\n"
        f"• Сумма сделки: <b>{format_k(transfer.get('price_k'))}</b>\n\n"
        f"Подтвердите или отклоните сделку в Mini App (вкладка 🔁 «Трансферы» → «Статус»)."
    )

    if photo_bytes:
        try:
            msg = await bot.send_photo(
                chat_id=int(counterparty),
                photo=photo_bytes,
                caption=text,
                parse_mode="HTML",
            )
            if msg.photo:
                file_id = msg.photo[-1].file_id
                repo.set_transfer_photo(transfer["id"], file_id)
            return True
        except Exception as exc:
            logger.info("transfers: photo DM to %s failed: %s, falling back to text", counterparty, exc)

    return await dm_user(bot, int(counterparty), text)


async def notify_deal_confirmed(bot, transfer: dict) -> bool:
    """Вторая сторона подтвердила: уведомить инициатора и отправить карточку в тему `requests`."""
    if bot is None:
        bot = get_bot()
    initiator_id = transfer.get("initiator_id")
    if initiator_id:
        text = (
            f"✅ <b>Сделка #{transfer['id']} подтверждена второй стороной!</b>\n\n"
            f"Игрок: <b>{html.escape(transfer.get('player_name') or '')}</b>\n"
            f"Заявка передана на рассмотрение ответственному за трансферы."
        )
        await dm_user(bot, int(initiator_id), text)
    return await post_request_card(bot, transfer)


async def notify_deal_declined(bot, transfer: dict) -> bool:
    """Вторая сторона отклонила предложение сделки."""
    if bot is None:
        bot = get_bot()
    initiator_id = transfer.get("initiator_id")
    if initiator_id:
        text = (
            f"❌ <b>Вторая сторона отклонила предложение сделки #{transfer['id']}.</b>\n\n"
            f"Игрок: <b>{html.escape(transfer.get('player_name') or '')}</b>"
        )
        return await dm_user(bot, int(initiator_id), text)
    return False


async def notify_request_withdrawn(bot, transfer: dict) -> bool:
    """Подавший отозвал заявку — уведомить вторую сторону, если сделка ждала её."""
    if bot is None:
        bot = get_bot()
    if transfer.get("kind") == "deal":
        initiator_id = transfer.get("initiator_id")
        other_user = transfer.get("to_user") if initiator_id == transfer.get("from_user") else transfer.get("from_user")
        if other_user:
            text = (
                f"ℹ️ Предложение сделки #{transfer['id']} по игроку "
                f"<b>{html.escape(transfer.get('player_name') or '')}</b> было отозвано инициатором."
            )
            return await dm_user(bot, int(other_user), text)
    return False


async def announce_free_agent(bot, transfer: dict) -> bool:
    """Публикация свободного агента в ленту `feed`."""
    lines = [
        "⚡️ <b>Свободный агент записан</b>", "",
        f"Игрок: <b>{html.escape(transfer.get('player_name') or '')}</b>"
        + (f" (OVR {transfer['ovr']})" if transfer.get("ovr") else ""),
        f"Куда: <b>{html.escape(transfer.get('to_club') or '')}</b>",
        f"Откуда: {html.escape(transfer.get('from_club') or '—')}",
        f"Сумма: <b>{format_k(transfer.get('price_k'))}</b>",
    ]
    if transfer.get("commented_at"):
        lines.append(f"Время комментария: {fmt_msk(transfer['commented_at'])} {MSK_LABEL}")
    return await post_to_topic(bot, "feed", "\n".join(lines))


async def notify_free_agent_recorded(bot, transfer: dict) -> bool:
    """Уведомление тренеру, подписавшему свободного агента."""
    user_id = transfer.get("to_user")
    if not user_id:
        return False
    text = (
        f"🎉 <b>Свободный агент записан!</b>\n\n"
        f"Игрок: <b>{html.escape(transfer.get('player_name') or '')}</b>\n"
        f"Клуб: <b>{html.escape(transfer.get('to_club') or '')}</b>\n"
        f"Сумма: <b>{format_k(transfer.get('price_k'))}</b>\n\n"
        f"Слот покупки и бюджет обновлены."
    )
    return await dm_user(bot, int(user_id), text)


async def notify_free_agent_reassigned(bot, transfer: dict, replaced: dict) -> bool:
    """Уведомление тренеру, чей СА был переписан по более раннему комментарию."""
    old_user = replaced.get("to_user")
    if not old_user:
        return False
    text = (
        f"⚠️ <b>Свободный агент переписан</b>\n\n"
        f"Игрок <b>{html.escape(replaced.get('player_name') or '')}</b> переписан на клуб "
        f"<b>{html.escape(transfer.get('to_club') or '')}</b>, так как его комментарий был оставлен раньше.\n\n"
        f"Слот покупки и бюджет возвращены вашему клубу."
    )
    return await dm_user(bot, int(old_user), text)


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

