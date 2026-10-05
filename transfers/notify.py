"""Сообщения трансферного окна: три темы группы ТО и личка.

Тренерам — в ЛС, всё остальное — в одну группу с темами `requests`, `feed`,
`alerts` (привязываются ссылкой на тему в панели `/to`). Не привязана тема
или Telegram отказал — сообщение не теряется молча: ответственный получает его
в ЛС с пометкой, что в группу оно не ушло. Здесь ничего не бросает наружу.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telegram.error import TelegramError

import config
from time_utils import MSK_LABEL, fmt_msk
from transfers import card, recap, repo
from transfers.engine import format_k

logger = logging.getLogger(__name__)

TOPIC_LABELS = {"requests": "Заявки", "feed": "Лента", "alerts": "Алерты", "fa": "Свободные агенты"}

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


CAPTION_LIMIT = 1024      # предел подписи к фото в Telegram


async def post_card_to_topic(bot, topic_type: str, transfer: dict, caption: str) -> bool:
    """Карточка трансфера в тему с подписью; не вышло (нет темы, картинки, отказ) — обычный текст.

    Текстовая публикация — прежний путь `post_to_topic`, со своим запасным ЛС ответственному,
    поэтому сбой картинки ничего не теряет.
    """
    try:
        topic = repo.get_topic(topic_type)
    except Exception:
        logger.exception("transfers: topic lookup failed")
        topic = None
    if topic and len(caption) <= CAPTION_LIMIT:
        png = await card.build_card_async(transfer)
        if png:
            try:
                await bot.send_photo(chat_id=topic["group_chat_id"], message_thread_id=topic["message_thread_id"],
                                     photo=png, caption=caption, parse_mode="HTML")
                return True
            except Exception as exc:
                logger.warning("transfers: card post to %s failed: %s", topic_type, exc)
    return await post_to_topic(bot, topic_type, caption)


async def post_image_to_topic(bot, topic_type: str, png: bytes | None, caption: str) -> bool:
    """Готовая картинка в тему с подписью; нет темы, картинки или отказ — обычный текст."""
    try:
        topic = repo.get_topic(topic_type)
    except Exception:
        logger.exception("transfers: topic lookup failed")
        topic = None
    if topic and png and len(caption) <= CAPTION_LIMIT:
        try:
            await bot.send_photo(chat_id=topic["group_chat_id"], message_thread_id=topic["message_thread_id"],
                                 photo=png, caption=caption, parse_mode="HTML")
            return True
        except Exception as exc:
            logger.warning("transfers: image post to %s failed: %s", topic_type, exc)
    return await post_to_topic(bot, topic_type, caption)


RECAP_TAG = "recap"


async def announce_recap(bot, window: dict, *, force: bool = False) -> bool:
    """Итоги окна в ленту. Сам по себе — один раз на окно (`claim_reminder`), `force` — повтор вручную.

    Окно без одобренных заявок итогов не получает: пустая карточка в ленте никому не нужна.
    """
    try:
        data = await asyncio.to_thread(recap.build, window["id"])
    except Exception:
        logger.exception("transfers: recap build failed for window %s", window.get("id"))
        return False
    if data.empty:
        return False
    if not force and not await asyncio.to_thread(repo.claim_reminder, window["id"], RECAP_TAG):
        return False
    png = await recap.build_image_async(data)
    return await post_image_to_topic(bot, "feed", png, recap.caption(data, _window_title(window)))


def _window_title(window: dict) -> str:
    title = (window.get("title") or "").strip()
    return f"«{html.escape(title)}»" if title else f"№{window['id']}"


def describe_transfer(t: dict) -> str:
    """Одна строка о заявке: №, вид, игрок, откуда → куда."""
    route = " → ".join(html.escape(c) for c in (t.get("from_club"), t.get("to_club")) if c)
    kind = KIND_LABELS.get(t.get("kind"), t.get("kind") or "")
    swap = f" 🔁 обмен с #{t['swap_partner_id']}" if t.get("swap_partner_id") else ""
    line = f"#{t['id']} {kind}{swap}: <b>{html.escape(t.get('player_name') or '')}</b>"
    return f"{line} ({route})" if route else line


def swap_pair(t: dict) -> tuple[dict, dict | None]:
    """(первая половина, вторая) для заявки из обмена, иначе (t, None).

    Первая — с меньшим номером: сообщения об обмене уходят один раз, от неё,
    с какой бы половины ни пришло событие.
    """
    partner = repo.get_swap_partner(t)
    if partner is None:
        return t, None
    return (t, partner) if t["id"] < partner["id"] else (partner, t)


def _leg_line(t: dict) -> str:
    ovr = f" (OVR {t['ovr']})" if t.get("ovr") else ""
    return (f"• <b>{html.escape(t.get('player_name') or '')}</b>{ovr}: "
            f"{html.escape(t.get('from_club') or '—')} → {html.escape(t.get('to_club') or '—')}, "
            f"<b>{format_k(t.get('price_k'))}</b>")


def _swap_lines(lead: dict, partner: dict) -> list[str]:
    return [_leg_line(lead), _leg_line(partner)]


def _swap_title(lead: dict, partner: dict) -> str:
    return f"#{lead['id']}+#{partner['id']}"


_global_bot = None


def set_bot(bot) -> None:
    global _global_bot
    _global_bot = bot


def get_bot():
    return _global_bot


def _warning_lines(*transfers: dict) -> list[str]:
    out = []
    for t in transfers:
        raw = t.get("warnings")
        if not raw:
            continue
        try:
            import json
            items = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        out.extend(f"• {html.escape(w.get('message') if isinstance(w, dict) else str(w))}" for w in items or [])
    return out


def format_request_card(t: dict) -> str:
    """Полная карточка заявки для темы `requests`."""
    lead, partner = swap_pair(t)
    if partner is not None:
        lines = [f"📋 <b>Заявка {_swap_title(lead, partner)} — обмен игроками</b>", ""]
        lines.extend(_swap_lines(lead, partner))
        warns = _warning_lines(lead, partner)
        if warns:
            lines.extend(["", "⚠️ <b>Предупреждения:</b>", *warns])
        return "\n".join(lines)
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
    if reply_markup is None and transfer.get("status") == "pending_manager":
        reply_markup = approval_keyboard(transfer["id"])
    try:
        topic = repo.get_topic("requests")
    except Exception:
        logger.exception("transfers: topic lookup failed")
        topic = None

    if not topic:
        await dm_manager(bot, f"⚠️ Тема «{label}» не привязана — заявка #{transfer['id']} не отправлена в группу:\n\n{text}",
                         reply_markup)
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
                              f"{html.escape(str(exc))}\n\n{text}", reply_markup)
        return False


async def notify_deal_proposal(bot, transfer: dict, *, photo_bytes: bytes | None = None,
                               note: str | None = None) -> bool:
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

    lead, partner = swap_pair(transfer)
    if partner is not None:
        return await dm_user(bot, int(counterparty), (
            f"🔁 <b>Предложение обмена {_swap_title(lead, partner)}</b>\n\n"
            f"Клуб <b>{html.escape(initiator_club or '')}</b> предлагает обмен игроками:\n"
            + "\n".join(_swap_lines(lead, partner))
            + "\n\nПодтвердите или отклоните обмен в Mini App (вкладка 🔁 «Трансферы» → «Статус»). "
              "Обе половины решаются вместе."))

    text = (
        f"🤝 <b>Предложение сделки #{transfer['id']}</b>\n\n"
        f"Клуб <b>{html.escape(initiator_club or '')}</b> предлагает {role_desc}:\n"
        f"• Игрок: <b>{html.escape(transfer.get('player_name') or '')}</b>"
        + (f" (OVR {transfer['ovr']})" if transfer.get('ovr') else "") + "\n"
        f"• Сумма сделки: <b>{format_k(transfer.get('price_k'))}</b>\n\n"
        f"Подтвердите или отклоните сделку в Mini App (вкладка 🔁 «Трансферы» → «Статус»)."
    )
    if note:
        text = f"{note}\n\n{text}"

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


def lot_line(lot: dict) -> str:
    """«Продаю: Rodri (OVR 106) · 20 млн» — одна строка лота."""
    side = "Продаю" if lot["side"] == "sell" else "Ищу"
    what = html.escape(lot.get("player_name") or "")
    if lot.get("ovr"):
        what += f" (OVR {lot['ovr']})"
    parts = [f"{side}: <b>{what}</b>" if what else f"<b>{side}</b>"]
    if lot.get("price_k") is not None:
        parts.append(format_k(lot["price_k"]))
    return " · ".join(parts)


def board_response_note(lot: dict) -> str:
    return f"📌 Отклик на ваш лот на доске — {lot_line(lot)}"


async def announce_board_lot(bot, lot: dict) -> bool:
    """Новый лот — тихо в ленту. Лента не привязана — молчим: лот виден на доске Mini App."""
    try:
        topic = repo.get_topic("feed")
    except Exception:
        logger.exception("transfers: topic lookup failed")
        topic = None
    if not topic or bot is None:
        return False
    text = f"📌 <b>Доска ТО</b> · {html.escape(lot['club_name'])}\n{lot_line(lot)}"
    if lot.get("note"):
        text += f"\n<i>{html.escape(lot['note'])}</i>"
    text += "\n\nОткликнуться — в Mini App: 🔁 «Трансферы» → «📌 Доска»."
    try:
        await bot.send_message(chat_id=topic["group_chat_id"], message_thread_id=topic["message_thread_id"],
                               text=text, parse_mode="HTML", disable_notification=True,
                               disable_web_page_preview=True)
        return True
    except Exception as exc:
        logger.warning("transfers: board lot post failed: %s", exc)
        return False


async def notify_board_lot_removed(bot, lot: dict) -> bool:
    return await dm_user(bot, lot.get("user_id"),
                         f"📌 Ответственный снял ваш лот с доски ТО — {lot_line(lot)}")


async def notify_deal_confirmed(bot, transfer: dict) -> bool:
    """Вторая сторона подтвердила: уведомить инициатора и отправить карточку в тему `requests`."""
    if bot is None:
        bot = get_bot()
    lead, partner = swap_pair(transfer)
    if partner is not None:
        transfer = lead
        if transfer.get("initiator_id"):
            await dm_user(bot, int(transfer["initiator_id"]), (
                f"✅ <b>Обмен {_swap_title(lead, partner)} подтверждён второй стороной!</b>\n\n"
                + "\n".join(_swap_lines(lead, partner))
                + "\n\nЗаявка передана на рассмотрение ответственному за трансферы."))
        return await post_request_card(bot, transfer)
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
    lead, partner = swap_pair(transfer)
    if partner is not None and initiator_id:
        return await dm_user(bot, int(initiator_id), (
            f"❌ <b>Вторая сторона отклонила обмен {_swap_title(lead, partner)}.</b>\n\n"
            + "\n".join(_swap_lines(lead, partner))))
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
        lead, partner = swap_pair(transfer)
        if other_user and partner is not None:
            return await dm_user(bot, int(other_user), (
                f"ℹ️ Предложение обмена {_swap_title(lead, partner)} было отозвано инициатором.\n\n"
                + "\n".join(_swap_lines(lead, partner))))
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
    return await post_card_to_topic(bot, "feed", transfer, "\n".join(lines))


async def announce_slot_bought(bot, purchase: dict) -> bool:
    """Клуб докупил слот за монеты — строка в ленту `feed`."""
    what = "покупки" if purchase.get("slot_type") == "buy" else "продажи"
    state = purchase.get("state") or {}
    text = (f"🪙 <b>{html.escape(purchase.get('club') or '')}</b> докупил слот {what} за "
            f"{purchase.get('price')} 🪙 (докупок {state.get('bought', '?')} из {state.get('max_extra', '?')}).")
    return await post_to_topic(bot, "feed", text)


async def notify_slot_refunded(bot, purchase: dict) -> bool:
    """Ответственный вернул монеты за слот — ЛС тренеру, купившему его."""
    user_id = purchase.get("user_id")
    if not user_id:
        return False
    what = "покупки" if purchase.get("slot_type") == "buy" else "продажи"
    text = (f"↩️ <b>Слот {what} возвращён</b>\n\nКлуб: <b>{html.escape(purchase.get('club_name') or '')}</b>\n"
            f"Вам возвращено {purchase.get('price_coins')} 🪙, лимит клуба уменьшен на один слот.")
    return await dm_user(bot, int(user_id), text)


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



# ─── Решение ответственного ──────────────────────────────────────────────────

def approval_keyboard(transfer_id: int):
    """✅/❌ под карточкой заявки. Нажимает только ответственный."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"tw:ap:{transfer_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"tw:rj:{transfer_id}"),
    ]])


def parties(t: dict) -> list[int]:
    """Тренеры заявки: обе стороны сделки и подавший, без повторов."""
    seen: list[int] = []
    for uid in (t.get("from_user"), t.get("to_user"), t.get("initiator_id")):
        if uid and int(uid) not in seen:
            seen.append(int(uid))
    return seen


def _decision_lines(t: dict) -> list[str]:
    lines = [f"Игрок: <b>{html.escape(t.get('player_name') or '')}</b>"
             + (f" (OVR {t['ovr']})" if t.get("ovr") else "")]
    kind = t.get("kind")
    if kind == "deal":
        lines.append(f"{html.escape(t.get('from_club') or '—')} → {html.escape(t.get('to_club') or '—')}, "
                     f"<b>{format_k(t.get('price_k'))}</b>")
    elif kind == "surcharge":
        lines.append(f"Клуб: {html.escape(t.get('to_club') or '—')}, доплата <b>{format_k(t.get('price_k'))}</b>")
    elif kind == "urn_sale":
        lines.append(f"Клуб: {html.escape(t.get('from_club') or '—')}, выплата из урны "
                     f"<b>{format_k(t.get('price_k'))}</b>")
    elif kind == "urn_buy":
        lines.append(f"Покупатель: {html.escape(t.get('to_club') or '—')}, выкуп из урны "
                     f"<b>{format_k(t.get('price_k'))}</b>")
    elif kind == "free_agent":
        lines.append(f"{html.escape(t.get('from_club') or '—')} → {html.escape(t.get('to_club') or '—')}, "
                     f"<b>{format_k(t.get('price_k'))}</b>")
    return lines


async def _dm_parties(bot, transfer: dict, text: str) -> None:
    """ЛС сторонам; кому не дошло — в `alerts`, чтобы ответственный знал."""
    failed = [uid for uid in parties(transfer) if not await dm_user(bot, uid, text)]
    if failed:
        await post_to_topic(
            bot, "alerts",
            f"⚠️ По заявке #{transfer['id']} не дошло в ЛС (бот заблокирован или не запущен): "
            + ", ".join(f"<code>{uid}</code>" for uid in failed))


async def notify_approved(bot, transfer: dict) -> bool:
    """Одобрено: ЛС сторонам и публикация в ленту. True — лента получила пост."""
    if transfer.get("kind") == "free_agent":
        if transfer.get("to_user") and not await notify_free_agent_recorded(bot, transfer):
            await post_to_topic(
                bot, "alerts",
                f"⚠️ По заявке #{transfer['id']} не дошло в ЛС (бот заблокирован или не запущен): "
                f"<code>{transfer['to_user']}</code>")
        return await announce_free_agent(bot, transfer)
    lead, partner = swap_pair(transfer)
    if partner is not None:
        body = "\n".join(_swap_lines(lead, partner))
        title = _swap_title(lead, partner)
        await _dm_parties(bot, lead, f"✅ <b>Обмен {title} одобрен</b>\n\n{body}")
        # В ленту — карточка на каждую половину: у каждой свой игрок и маршрут.
        posted = True
        for leg in (lead, partner):
            posted = await post_card_to_topic(
                bot, "feed", leg,
                f"✅ <b>Одобрен обмен {title}</b>\n\n{_leg_line(leg)}") and posted
        return posted
    kind = KIND_LABELS.get(transfer.get("kind"), "")
    body = "\n".join(_decision_lines(transfer))
    await _dm_parties(bot, transfer, f"✅ <b>Заявка #{transfer['id']} одобрена</b> ({kind})\n\n{body}")
    return await post_card_to_topic(bot, "feed", transfer,
                                    f"✅ <b>Одобрен трансфер #{transfer['id']}</b> ({kind})\n\n{body}")


async def notify_rejected(bot, transfer: dict) -> bool:
    """Отклонено ответственным: ЛС сторонам с причиной и пост в ленту."""
    reason = transfer.get("decided_reason")
    why = f"\n\nПричина: {html.escape(reason)}" if reason else ""
    lead, partner = swap_pair(transfer)
    if partner is not None:
        body = "\n".join(_swap_lines(lead, partner))
        title = _swap_title(lead, partner)
        await _dm_parties(bot, lead, f"❌ <b>Обмен {title} отклонён</b>\n\n{body}{why}")
        return await post_to_topic(bot, "feed", f"❌ <b>Отклонён обмен {title}</b>\n\n{body}{why}")
    kind = KIND_LABELS.get(transfer.get("kind"), "")
    body = "\n".join(_decision_lines(transfer))
    await _dm_parties(bot, transfer, f"❌ <b>Заявка #{transfer['id']} отклонена</b> ({kind})\n\n{body}{why}")
    return await post_to_topic(bot, "feed", f"❌ <b>Отклонён трансфер #{transfer['id']}</b> ({kind})\n\n{body}{why}")


async def notify_squad(bot, transfer: dict, lines: list[str], *, reverted: bool = False) -> None:
    """Состав клуба изменили или вернули — ЛС сторонам со списком изменений."""
    if not lines:
        return
    head = "↩️ <b>Состав возвращён</b>" if reverted else "📋 <b>Состав обновлён</b>"
    body = "\n".join(html.escape(line) for line in lines)
    await _dm_parties(bot, transfer, f"{head} по заявке #{transfer['id']}\n\n{body}")


async def notify_cancelled(bot, transfer: dict, lines: list[str]) -> bool:
    """Одобренную заявку отменили: ЛС сторонам и пост в ленту."""
    kind = KIND_LABELS.get(transfer.get("kind"), "")
    lead, partner = swap_pair(transfer)
    if partner is not None:
        transfer, kind = lead, f"обмен {_swap_title(lead, partner)}"
        body = "\n".join(_swap_lines(lead, partner))
    else:
        body = "\n".join(_decision_lines(transfer))
    if lines:
        body += "\n\n" + "\n".join(html.escape(line) for line in lines)
    reason = transfer.get("decided_reason")
    why = f"\n\nПричина: {html.escape(reason)}" if reason else ""
    await _dm_parties(bot, transfer, f"🛑 <b>Заявка #{transfer['id']} отменена</b> ({kind})\n\n{body}{why}\n\n"
                                     "Бюджет и слоты возвращены.")
    return await post_to_topic(bot, "feed", f"🛑 <b>Отменён трансфер #{transfer['id']}</b> ({kind})\n\n{body}{why}")


async def notify_sanction(bot, sanction: dict, recipients: list[int], *, subject: str, span: str,
                          lifted: bool = False) -> int:
    """Тренеру (или всем тренерам клуба) в ЛС: санкция поставлена или снята. Сколько ЛС дошло."""
    if lifted:
        text = (f"✅ <b>Санкция снята</b>\n\n{html.escape(subject)}: ограничения трансферного окна "
                "больше нет — заявки снова можно подавать.")
    else:
        reason = (sanction.get("reason") or "").strip()
        why = f"\nПричина: {html.escape(reason)}" if reason else ""
        text = (f"⛔ <b>Санкция трансферного окна</b>\n\n{html.escape(subject)} лишён окна: "
                f"{html.escape(span)}.{why}\n\nЗаявки и докупка слотов недоступны до конца срока.")
    sent = 0
    for user_id in dict.fromkeys(recipients):
        if await dm_user(bot, user_id, text):
            sent += 1
    return sent
