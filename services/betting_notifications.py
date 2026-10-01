"""
services/betting_notifications.py

Уведомления об открытии и закрытии линии ставок Logovo.bet:
- Отправка в ЛС игрокам дивизиона с инлайн-кнопкой "🎰 Сделать ставку", открывающей Telegram Mini App.
- В топики группы объявления не дублируются: открытие и закрытие линии на каждый тур
  засоряли топик «📊 Аналитика».
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

import config
import database
from handlers.cabinet import safe_send_notification

logger = logging.getLogger(__name__)

# Защита от спама и повторных вызовов (cooldown 30 секунд на пару (division_id, round, action))
_notif_cooldown: dict[tuple[int, int, str], float] = {}
COOLDOWN_SECONDS = 30.0


def get_betting_webapp_markup(is_private: bool = True, bot_username: str | None = None) -> InlineKeyboardMarkup:
    """
    Инлайн-кнопка для перехода к ставкам:
    - В ЛС: открывает Telegram Web App (Mini App).
    - В группе/топике: ссылка на бота в ЛС https://t.me/<bot>?start=bet (Telegram не разрешает web_app кнопки в группах).
    """
    webapp_url = getattr(config, "WEBAPP_URL", "") or "http://localhost:8080"
    if is_private:
        if webapp_url.startswith("https://") or "localhost" in webapp_url or "127.0.0.1" in webapp_url:
            btn = InlineKeyboardButton("🎰 Сделать ставку", web_app=WebAppInfo(url=webapp_url))
        elif webapp_url.startswith("http://"):
            btn = InlineKeyboardButton("🎰 Сделать ставку", url=webapp_url)
        else:
            btn = InlineKeyboardButton("🎰 Сделать ставку", web_app=WebAppInfo(url=f"https://{webapp_url}"))
    else:
        if bot_username:
            btn = InlineKeyboardButton("🎰 Сделать ставку", url=f"https://t.me/{bot_username}?start=bet")
        elif webapp_url and webapp_url.startswith("http"):
            btn = InlineKeyboardButton("🎰 Сделать ставку", url=webapp_url)
        else:
            btn = InlineKeyboardButton("🎰 Сделать ставку", callback_data="bet_view_tours")

    return InlineKeyboardMarkup([[btn]])


def build_line_opened_text(division_name: str, round_number: int, matches: list[dict] | None = None) -> str:
    """Текст сообщения об открытии линии ставок."""
    lines = [
        f"🎰 <b>Линия ставок открыта! | Тур {round_number}</b>",
        f"<i>{html.escape(division_name)}</i>\n",
        f"Букмекерская контора <b>«Logovo.bet»</b> открыла приём прогнозов на {round_number}-й тур!",
    ]
    if matches:
        match_lines = []
        for m in matches[:4]:
            t1 = m.get("player1_team") or m.get("player1_nickname") or ""
            t2 = m.get("player2_team") or m.get("player2_nickname") or ""
            if t1 and t2:
                match_lines.append(f"• <b>{html.escape(str(t1))}</b> — <b>{html.escape(str(t2))}</b>")
        if match_lines:
            lines.append("\n⚽ <b>Матчи тура:</b>")
            lines.extend(match_lines)

    lines.append("\nКотировки уже выставлены. Успейте сделать ставку в Mini App до старта матчей!")
    lines.append("🕒 <i>Приём прогнозов завершится автоматически в момент открытия тура для игр.</i>")
    return "\n".join(lines)


def build_line_closed_text(division_name: str, round_number: int) -> str:
    """Текст сообщения о закрытии линии ставок."""
    lines = [
        f"🚫 <b>Линия ставок закрыта | Тур {round_number}</b>",
        f"<i>{html.escape(division_name)}</i>\n",
        f"Приём прогнозов на {round_number}-й тур завершён. Все коэффициенты зафиксированы!",
        "Матчи начинаются. Расчёт ставок произойдёт автоматически после подтверждения результатов игр.\n",
        "<i>Желаем удачи всем капперам турнира! 🍀</i>"
    ]
    return "\n".join(lines)


async def notify_division_betting_line_opened(
    context: ContextTypes.DEFAULT_TYPE,
    division_id: int,
    round_number: int,
) -> int:
    """
    Уведомить игроков дивизиона в ЛС об открытии линии ставок:
    - Сообщение с перечнем матчей тура;
    - Инлайн-кнопка "🎰 Сделать ставку", открывающая Telegram Mini App.

    Возвращает количество успешно доставленных ЛС-сообщений.
    """
    key = (division_id, round_number, "open")
    now = time.time()
    if now - _notif_cooldown.get(key, 0.0) < COOLDOWN_SECONDS:
        logger.debug(f"Betting line open notification throttled for div {division_id} round {round_number}")
        return 0
    _notif_cooldown[key] = now

    division = await asyncio.to_thread(database.get_division, division_id)
    div_name = division.get("name", f"Дивизион {division_id}") if division else f"Дивизион {division_id}"
    matches = await asyncio.to_thread(database.get_matches_by_round, round_number, division_id=division_id)

    pm_text = build_line_opened_text(div_name, round_number, matches)
    pm_markup = get_betting_webapp_markup(is_private=True)

    users = await asyncio.to_thread(database.get_division_users, division_id)
    sent_count = 0
    bot = context.bot

    for u in users:
        uid = u.get("telegram_id")
        if not uid or uid <= 0:
            continue
        try:
            ok = await safe_send_notification(bot, uid, pm_text, reply_markup=pm_markup)
            if ok:
                sent_count += 1
            await asyncio.sleep(0.04)  # Защита от Telegram rate limits
        except Exception as e:
            logger.debug(f"Failed to send betting line open PM to {uid}: {e}")

    logger.info(
        f"Betting line open notification for div {division_id} round {round_number}: sent to {sent_count} player(s)."
    )
    return sent_count


async def notify_division_betting_line_closed(
    context: ContextTypes.DEFAULT_TYPE,
    division_id: int,
    round_number: int,
    check_was_open: bool = False,
    was_open: bool | None = None,
) -> int:
    """
    Уведомить игроков дивизиона в ЛС о закрытии линии ставок:
    - Сообщение о завершении приёма прогнозов и старте матчей.

    `was_open` — явный флаг того, была ли линия открыта до закрытия тура.
    `check_was_open=True` — проверяет `bets_open` в БД, если `was_open` не указан.
    """
    if was_open is not None and not was_open:
        return 0
    if was_open is None and check_was_open:
        r_info = await asyncio.to_thread(database.get_round_info, round_number, division_id)
        if not r_info or not r_info.get("bets_open"):
            return 0

    key = (division_id, round_number, "close")
    now = time.time()
    if now - _notif_cooldown.get(key, 0.0) < COOLDOWN_SECONDS:
        logger.debug(f"Betting line close notification throttled for div {division_id} round {round_number}")
        return 0
    _notif_cooldown[key] = now

    division = await asyncio.to_thread(database.get_division, division_id)
    div_name = division.get("name", f"Дивизион {division_id}") if division else f"Дивизион {division_id}"

    close_text = build_line_closed_text(div_name, round_number)

    users = await asyncio.to_thread(database.get_division_users, division_id)
    sent_count = 0
    bot = context.bot

    for u in users:
        uid = u.get("telegram_id")
        if not uid or uid <= 0:
            continue
        try:
            ok = await safe_send_notification(bot, uid, close_text)
            if ok:
                sent_count += 1
            await asyncio.sleep(0.04)
        except Exception as e:
            logger.debug(f"Failed to send betting line close PM to {uid}: {e}")

    logger.info(
        f"Betting line close notification for div {division_id} round {round_number}: sent to {sent_count} player(s)."
    )
    return sent_count
