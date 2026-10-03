"""Модуль учёта активности участников в чате и формирования карточки профиля («Neat Tree»).

Отслеживает типы сообщений (текст, стикеры, ГС, видеокружочки, фото, токс),
форматирует турнирную карточку участника для чата с иерархической структурой.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import database
from time_utils import MSK, now_msk, today_msk

# Базовый набор регулярок для детекции ненормативной / токсичной лексики в чате
_TOXIC_PATTERNS = [
    re.compile(r"(?i)\b(?:ху[йиеяюё]|хер|пизд|бля[дт]|еб[а-яё]|ёб[а-яё]|заеб|выеб|уеб|наеб|доеб|приеб|проеб|сук[а-я]?|муда[кч]|гандон|презерватив|пидор|педик|чмо[шх]?|шлюх|шалав|мраз|тварь|петух|долбо[её]б|дебил|даун|ублюд)\w*"),
    re.compile(r"(?i)\b(?:похуй|нахуй|захуй|нахер|похер|нихуя|похую|нихера|охерел|охуел)\b"),
]


def is_toxic_message(text: str | None) -> bool:
    """Проверяет текст сообщения на наличие токсичных / нецензурных слов."""
    if not text:
        return False
    # Нормализуем пробелы и символы
    clean = re.sub(r"[\s_.\-]+", " ", text)
    return any(p.search(clean) for p in _TOXIC_PATTERNS)


def _format_date_msk(dt_raw: str | None) -> str:
    """Форматирует дату по МСК (ДД.ММ.ГГГГ)."""
    if not dt_raw:
        return "Неизвестно"
    clean = str(dt_raw).strip()
    try:
        # Может быть YYYY-MM-DD HH:MM:SS или YYYY-MM-DD
        dt_val = datetime.strptime(clean.split(".")[0], "%Y-%m-%d %H:%M:%S")
        return dt_val.strftime("%d.%m.%Y")
    except Exception:
        try:
            dt_val = datetime.strptime(clean.split("T")[0], "%Y-%m-%d")
            return dt_val.strftime("%d.%m.%Y")
        except Exception:
            return clean[:10]


def _format_last_active_msk(dt_raw: str | None) -> str:
    """Форматирует время последней активности по МСК (Сегодня 11:24 / 25.09 11:14)."""
    if not dt_raw:
        return "Не зафиксировано"
    clean = str(dt_raw).strip()
    try:
        dt_val = datetime.strptime(clean.split(".")[0], "%Y-%m-%d %H:%M:%S")
        today = today_msk()
        if dt_val.date() == today:
            return f"Сегодня {dt_val.strftime('%H:%M')}"
        elif (today - dt_val.date()).days == 1:
            return f"Вчера {dt_val.strftime('%H:%M')}"
        else:
            return dt_val.strftime("%d.%m %H:%M")
    except Exception:
        return clean[:16]


def _get_warn_badge(warn_count: int, max_warns: int = 3) -> str:
    """Возвращает форматированную строку варнов с эмодзи статуса."""
    if warn_count <= 0:
        return f"{warn_count}/{max_warns} 😇"
    elif warn_count == 1:
        return f"{warn_count}/{max_warns} 🟡"
    elif warn_count == 2:
        return f"{warn_count}/{max_warns} 🟠 (Опасная зона)"
    else:
        return f"{warn_count}/{max_warns} 🔴 (Дисквалификация)"


def _get_elo_tier(elo: float) -> str:
    """Возвращает ранг/тир по ELO-рейтингу."""
    if elo >= 1700:
        return "MASTER"
    elif elo >= 1550:
        return "ELITE"
    elif elo >= 1400:
        return "PRO"
    elif elo >= 1250:
        return "CHALLENGER"
    else:
        return "ROOKIE"


def _format_form_emojis(form_list: list[str]) -> str:
    """Преобразует список исходов ['W', 'D', 'L'] в цветные эмодзи."""
    if not form_list:
        return "—"
    mapping = {
        "W": "🟢",
        "D": "🟡",
        "L": "🔴",
    }
    return " ".join(mapping.get(x.upper(), "⚪") for x in form_list)


def build_profile_card(
    target_user_id: int,
    target_name: str | None = None,
    target_username: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Собирает данные игрока и формирует текст карточки в стиле «Neat Tree» с кнопками.
    """
    profile_data = database.get_user_chat_profile_data(target_user_id)
    chat_activity = profile_data["chat_activity"]
    tourney = profile_data["tournament_summary"]
    user_row = profile_data["user"]

    # Имя и юзернейм
    display_name = target_name or (user_row["username"] if user_row else None) or f"Участник #{target_user_id}"
    username_val = target_username or (user_row["username"] if user_row else None)
    if username_val:
        user_link = f"@{html.escape(username_val)}"
    else:
        user_link = f'<a href="tg://user?id={target_user_id}">профиль</a>'

    # Роль / Должность
    from handlers.base import is_admin, is_global_admin
    if is_global_admin(target_user_id):
        role_title = "👑 Главный Администратор"
    elif is_admin(target_user_id):
        role_title = "🛡 Администратор Дивизиона"
    elif tourney.get("registered"):
        role_title = "Участник"
    else:
        role_title = "Гость лиги"

    # Варны
    warn_count = int(user_row["warn_count"] or 0) if user_row else 0
    warns_badge = _get_warn_badge(warn_count)

    # Даты
    registered_at_str = _format_date_msk(user_row["registered_at"] if user_row else None)
    last_active_str = _format_last_active_msk(chat_activity.get("last_message_at"))

    # Активность в чате
    sms_cnt = chat_activity.get("messages_count", 0)
    toks_cnt = chat_activity.get("toxic_count", 0)
    stik_cnt = chat_activity.get("stickers_count", 0)
    gs_cnt = chat_activity.get("voice_count", 0)
    photo_cnt = chat_activity.get("photos_count", 0)

    # Собираем строки
    lines = [
        f"👤 <b>{html.escape(display_name)}</b>",
        f"├ 💬 {user_link}",
        f"├ 👑 Должность: {role_title}",
        f"╰ ⚠️ Варны: {warns_badge}",
        "",
    ]

    has_team = tourney.get("registered") and tourney.get("team_name")
    buttons = []

    if has_team:
        team_name = tourney["team_name"]
        div_name = tourney.get("division_name") or "Основной дивизион"
        pos = tourney.get("position")
        total_teams = tourney.get("total_teams", 0)
        pts = tourney.get("points", 0)

        pos_str = f"#{pos} из {total_teams} ({pts} очков)" if pos else f"{pts} очков"

        lines.extend([
            "🌐 <b>КЛУБ И ДИВИЗИОН:</b>",
            f"├ 🛡 Клуб: {html.escape(team_name)}",
            f"├ 🏆 Дивизион: {html.escape(div_name)}",
            f"╰ 📊 Позиция: {pos_str}",
            "",
        ])

        # Турнирный рейтинг и статистика
        elo = profile_data.get("elo", 1500.0)
        tier = _get_elo_tier(elo)
        played = tourney.get("played", 0)
        wins = tourney.get("wins", 0)
        draws = tourney.get("draws", 0)
        losses = tourney.get("losses", 0)
        scored = tourney.get("goals_scored", 0)
        conceded = tourney.get("goals_conceded", 0)
        diff = tourney.get("goal_diff", 0)
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        form_emojis = _format_form_emojis(tourney.get("form", []))

        medals = profile_data.get("medals", {})
        gold = medals.get("gold", 0)
        silver = medals.get("silver", 0)
        bronze = medals.get("bronze", 0)

        lines.extend([
            "⚔️ <b>ТУРНИРНЫЙ РЕЙТИНГ:</b>",
            f"├ 🎖 ELO: {int(elo)} [{tier}]",
            f"├ 🕹 Игры: {played} ({wins}В | {draws}Н | {losses}П)",
            f"├ ⚽ Мячи: {scored}:{conceded} (разница {diff_str})",
            f"├ 📈 Форма: {form_emojis}",
            f"╰ 🏆 Трофеи: 🥇 {gold} | 🥈 {silver} | 🥉 {bronze}",
            "",
        ])

        # Кнопки быстрых действий для зарегистрированного игрока
        btn_row = [
            InlineKeyboardButton("📋 Мои матчи", callback_data="cabinet_my_matches"),
            InlineKeyboardButton("📸 Состав", callback_data="cabinet_my_squad"),
        ]
        buttons.append(btn_row)
    else:
        lines.extend([
            "🌐 <b>КЛУБ И ДИВИЗИОН:</b>",
            "╰ ⚠️ Свободный игрок (без привязки к клубу)",
            "",
        ])

    # Блок активности
    lines.extend([
        "💬 <b>АКТИВНОСТЬ:</b>",
        f"├ ✉️ СМС: {sms_cnt} | 🤬 Токс: {toks_cnt}",
        f"╰ 🎭 Стик: {stik_cnt} | 🎙 ГС: {gs_cnt} | 📸 Фото: {photo_cnt}",
        "",
        f"🕒 В лиге: с {registered_at_str} (МСК)",
        f"🟢 Онлайн: {last_active_str} (МСК)",
    ])

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup
