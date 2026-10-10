"""
handlers/workshop.py

Мастерская графики ТО: генерация и предпросмотр инфографики трансферного окна
(карточки «HERE WE GO», «СПЕЦКАРТА», «В УРНУ» и постеры «ИТОГИ ОКНА»).

Команда: /workshop (алиасы: /studio, /cards).
Доступ: глобальные админы, админы дивизионов и ответственный за ТО.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
import random
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import ContextTypes

from handlers.base import is_admin, is_global_admin
from services.graphics import player_photos
from services.graphics import transfer_card_generator as card_gen
from services.graphics import transfer_recap_generator as recap_gen
from transfers import card, recap, repo, requests as req_mod, service

logger = logging.getLogger(__name__)

# Примеры для демонстрации в мастерской
SAMPLE_DEALS = [
    {
        "player": "Lionel Messi",
        "price": "160 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Интер Майами",
        "to_club": "Манчестер Сити",
        "id": 101,
    },
    {
        "player": "Erling Haaland",
        "price": "175 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Манчестер Сити",
        "to_club": "Реал Мадрид",
        "id": 102,
    },
    {
        "player": "Kylian Mbappé",
        "price": "150 млн",
        "ovr": 113,
        "event": "Anniversary 26 Live",
        "from_club": "ПСЖ",
        "to_club": "Реал Мадрид",
        "id": 103,
    },
    {
        "player": "Jude Bellingham",
        "price": "135 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Боруссия Дортмунд",
        "to_club": "Реал Мадрид",
        "id": 104,
    },
    {
        "player": "Vinicius Junior",
        "price": "140 млн",
        "ovr": 114,
        "event": "Golden Era 26 Live",
        "from_club": "Реал Мадрид",
        "to_club": "Ливерпуль",
        "id": 105,
    },
    {
        "player": "Lamine Yamal",
        "price": "145 млн",
        "ovr": 114,
        "event": "Patch 4 Special",
        "from_club": "Барселона",
        "to_club": "ПСЖ",
        "id": 106,
    },
    {
        "player": "Jamal Musiala",
        "price": "130 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Бавария",
        "to_club": "Манчестер Сити",
        "id": 107,
    },
    {
        "player": "Mohamed Salah",
        "price": "120 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Ливерпуль",
        "to_club": "Аль-Иттихад",
        "id": 108,
    },
    {
        "player": "Virgil van Dijk",
        "price": "95 млн",
        "ovr": 114,
        "event": "Anniversary Special",
        "from_club": "Ливерпуль",
        "to_club": "Реал Мадрид",
        "id": 109,
    },
    {
        "player": "Harry Kane",
        "price": "115 млн",
        "ovr": 114,
        "event": "Golden Era 26 Live",
        "from_club": "Бавария",
        "to_club": "Манчестер Юнайтед",
        "id": 110,
    },
    {
        "player": "Luka Modrić",
        "price": "50 млн",
        "ovr": 114,
        "event": "Record Breakers",
        "from_club": "Реал Мадрид",
        "to_club": "Милан",
        "id": 111,
    },
    {
        "player": "Kevin De Bruyne",
        "price": "85 млн",
        "ovr": 114,
        "event": "World Cup Special",
        "from_club": "Манчестер Сити",
        "to_club": "Наполи",
        "id": 112,
    },
    {
        "player": "Cole Palmer",
        "price": "125 млн",
        "ovr": 114,
        "event": "Special Event",
        "from_club": "Челси",
        "to_club": "Бавария",
        "id": 113,
    },
    {
        "player": "Khvicha Kvaratskhelia",
        "price": "110 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Наполи",
        "to_club": "ПСЖ",
        "id": 114,
    },
    {
        "player": "Rafael Leão",
        "price": "115 млн",
        "ovr": 114,
        "event": "Record Breakers",
        "from_club": "Милан",
        "to_club": "Арсенал",
        "id": 115,
    },
    {
        "player": "Victor Osimhen",
        "price": "105 млн",
        "ovr": 114,
        "event": "Patch 5 Special",
        "from_club": "Наполи",
        "to_club": "Челси",
        "id": 116,
    },
    {
        "player": "Eduardo Camavinga",
        "price": "90 млн",
        "ovr": 114,
        "event": "Lunar New Year",
        "from_club": "Реал Мадрид",
        "to_club": "Арсенал",
        "id": 117,
    },
    {
        "player": "Thibaut Courtois",
        "price": "80 млн",
        "ovr": 113,
        "event": "Anniversary 26 Live",
        "from_club": "Реал Мадрид",
        "to_club": "ПСЖ",
        "id": 118,
    },
]

SAMPLE_SURCHARGES = [
    {
        "player": "Erling Haaland",
        "price": "80 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Манчестер Сити",
        "id": 201,
    },
    {
        "player": "Jude Bellingham",
        "price": "75 млн",
        "ovr": 114,
        "event": "TOTY 26 Live",
        "from_club": "Реал Мадрид",
        "id": 202,
    },
    {
        "player": "Rodri",
        "price": "60 млн",
        "ovr": 114,
        "event": "Patch 2 Special",
        "from_club": "Манчестер Сити",
        "id": 203,
    },
    {
        "player": "Florian Wirtz",
        "price": "70 млн",
        "ovr": 114,
        "event": "Patch 2 Special",
        "from_club": "Байер",
        "id": 204,
    },
    {
        "player": "Federico Valverde",
        "price": "65 млн",
        "ovr": 114,
        "event": "Patch 4 Special",
        "from_club": "Реал Мадрид",
        "id": 205,
    },
    {
        "player": "Alejandro Grimaldo",
        "price": "55 млн",
        "ovr": 114,
        "event": "TOTS 26 Live",
        "from_club": "Байер",
        "id": 206,
    },
    {
        "player": "Bruno Fernandes",
        "price": "50 млн",
        "ovr": 114,
        "event": "FUT Founders",
        "from_club": "Манчестер Юнайтед",
        "id": 207,
    },
    {
        "player": "Gianluigi Donnarumma",
        "price": "45 млн",
        "ovr": 114,
        "event": "Special Event",
        "from_club": "ПСЖ",
        "id": 208,
    },
]

SAMPLE_URNS = [
    {
        "player": "Antony",
        "price": "25 млн",
        "ovr": 99,
        "from_club": "Манчестер Юнайтед",
        "id": 301,
    },
    {
        "player": "Mykhaylo Mudryk",
        "price": "30 млн",
        "ovr": 95,
        "from_club": "Челси",
        "id": 302,
    },
    {
        "player": "Richarlison",
        "price": "35 млн",
        "ovr": 101,
        "from_club": "Тоттенхэм",
        "id": 303,
    },
    {
        "player": "Harry Maguire",
        "price": "20 млн",
        "ovr": 100,
        "from_club": "Манчестер Юнайтед",
        "id": 304,
    },
]


def _resolve_sample_portrait(player_name: str, *clubs: str | None) -> str | None:
    """Ищет портрет игрока в кэше/Renderz во всех вариантах написания (полное имя, фамилия, инициалы), а если нет — подгружает."""
    try:
        # 1. Штатный поиск по заявкам (assets/players/ и assets/renderz_portraits/)
        path = req_mod.portrait_path(player_name, *clubs)
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path

        parts = player_name.strip().split()
        surname = parts[-1] if len(parts) > 1 else player_name
        first_initial = parts[0][0] if len(parts) > 1 else ""

        # 2. Поиск по фамилии (на сервере многие файлы сохранены как saka_арсенал.png)
        if surname != player_name:
            path = req_mod.portrait_path(surname, *clubs)
            if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                return path

        # 3. Прямая проверка файлов во всех возможных вариантах написания
        name_slugs = [player_photos._slugify(player_name)]
        if surname != player_name:
            name_slugs.append(player_photos._slugify(surname))
            if first_initial:
                name_slugs.append(f"{player_photos._slugify(first_initial)}_{player_photos._slugify(surname)}")

        club_slugs = [player_photos._slugify(c) for c in clubs if c]

        for c_slug in club_slugs:
            for n_slug in name_slugs:
                cand = player_photos.PROJECT_ROOT / "assets" / "players" / f"{n_slug}_{c_slug}.png"
                if cand.is_file() and cand.stat().st_size > 0:
                    return str(cand)

        for n_slug in name_slugs:
            cand = player_photos.PROJECT_ROOT / "assets" / "players" / f"{n_slug}.png"
            if cand.is_file() and cand.stat().st_size > 0:
                return str(cand)
            cand_rz = player_photos.PROJECT_ROOT / "assets" / "renderz_portraits" / f"{n_slug}.png"
            if cand_rz.is_file() and cand_rz.stat().st_size > 0:
                return str(cand_rz)

        # 4. Фоновая выкачка, если файла ещё нет
        target = next((c for c in clubs if c), None)
        return player_photos.get_player_photo(player_name, target) or player_photos.get_player_photo(player_name)
    except Exception:
        logger.debug("Failed to resolve sample portrait for %s", player_name, exc_info=True)
        return None


def _resolve_sample_card(player_name: str, ovr: int | None = None, *clubs: str | None) -> str | None:
    """Ищет карточку игрока FC Mobile/Renderz под нужный OVR (или ближайший)."""
    try:
        path = req_mod.card_path(player_name, ovr, *clubs)
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        cid = req_mod.resolve_card_id(player_name, ovr)
        if cid:
            filename = f"{cid}.png"
            for folder in (
                player_photos.PROJECT_ROOT / "assets" / "cards",
                player_photos.PROJECT_ROOT / "renderz_sync" / "cards",
                Path("C:/Users/Ислам/Desktop/Projects/log/renderz_sync/cards"),
            ):
                cand = folder / filename
                if cand.is_file() and cand.stat().st_size > 0:
                    return str(cand)
        return None
    except Exception:
        logger.debug("Failed to resolve sample card for %s (OVR %s)", player_name, ovr, exc_info=True)
        return None


def _render_sample_deal(sample: dict, style: str = "portrait") -> bytes:
    portrait = sample.get("portrait") or _resolve_sample_portrait(
        sample["player"], sample.get("from_club"), sample.get("to_club")
    )
    card_path = _resolve_sample_card(
        sample["player"], sample.get("ovr"), sample.get("from_club"), sample.get("to_club")
    ) if style == "card" else None

    return card_gen.render_transfer_card(
        kind="deal",
        player_name=sample["player"],
        price_text=sample["price"],
        ovr=sample["ovr"],
        from_club=sample["from_club"],
        to_club=sample["to_club"],
        portrait_path=portrait,
        card_path=card_path,
        transfer_id=sample["id"],
    )


def _render_sample_surcharge(sample: dict, style: str = "portrait") -> bytes:
    portrait = sample.get("portrait") or _resolve_sample_portrait(
        sample["player"], sample.get("from_club")
    )
    card_path = _resolve_sample_card(
        sample["player"], sample.get("ovr"), sample.get("from_club")
    ) if style == "card" else None

    return card_gen.render_transfer_card(
        kind="surcharge",
        player_name=sample["player"],
        price_text=sample["price"],
        ovr=sample["ovr"],
        from_club=sample["from_club"],
        portrait_path=portrait,
        card_path=card_path,
        transfer_id=sample["id"],
    )


def _render_sample_urn(sample: dict, style: str = "portrait") -> bytes:
    portrait = sample.get("portrait") or _resolve_sample_portrait(
        sample["player"], sample.get("from_club")
    )
    card_path = _resolve_sample_card(
        sample["player"], sample.get("ovr"), sample.get("from_club")
    ) if style == "card" else None

    return card_gen.render_transfer_card(
        kind="urn_sale",
        player_name=sample["player"],
        price_text=sample["price"],
        ovr=sample["ovr"],
        from_club=sample["from_club"],
        portrait_path=portrait,
        card_path=card_path,
        transfer_id=sample["id"],
    )


def _render_demo_recap() -> bytes:
    deals = [
        recap_gen.RecapDeal("Lamine Yamal", "Барселона → ПСЖ", "140 млн", portrait_path=_resolve_sample_portrait("Lamine Yamal", "Барселона")),
        recap_gen.RecapDeal("Kylian Mbappé", "ПСЖ → Реал Мадрид", "125 млн", portrait_path=_resolve_sample_portrait("Kylian Mbappé", "Реал Мадрид")),
        recap_gen.RecapDeal("Florian Wirtz", "Байер → Манчестер Сити", "115 млн", portrait_path=_resolve_sample_portrait("Florian Wirtz", "Байер")),
        recap_gen.RecapDeal("Bukayo Saka", "Арсенал → Бавария", "95 млн", portrait_path=_resolve_sample_portrait("Bukayo Saka", "Арсенал")),
        recap_gen.RecapDeal("Vinícius Júnior", "Реал Мадрид → Ливерпуль", "85 млн", portrait_path=_resolve_sample_portrait("Vinícius Júnior", "Реал Мадрид")),
    ]
    return recap_gen.render_window_recap(
        title="ТО 1-ГО КРУГА 2026",
        turnover_text="1.15 млрд",
        deals=deals,
        requests_count=46,
        swaps=9,
        urn_sales=14,
        top_club="Реал Мадрид",
        top_club_count=7,
    )


def is_workshop_allowed(user_id: int | None) -> bool:
    """Доступ для админов лиги и ответственного за трансферное окно."""
    if not user_id:
        return False
    if is_global_admin(user_id) or is_admin(user_id):
        return True
    return service.can_manage_window(user_id)


def _hub_text() -> str:
    return (
        "🎨 <b>Мастерская графики ТО</b>\n\n"
        "Интерактивная панель для генерации промо-карточек и инфографики трансферного окна "
        "в высоком разрешении (1440×810 Retina 2x):\n\n"
        "• <b>HERE WE GO (Фото)</b> — вариант с крупным портретом игрока\n"
        "• <b>HERE WE GO (Карта)</b> — вариант с оригинальной карточкой FC Mobile под OVR покупки\n"
        "• <b>СПЕЦКАРТА</b> — карточка доплаты (портрет или карточка FC)\n"
        "• <b>В УРНУ</b> — карточка сдачи игрока в урну\n"
        "• <b>ИТОГИ ОКНА</b> — итоговый постер с топом сделок и портретами\n"
        "• <b>ИЗ БАЗЫ ТО</b> — реальная карточка заявки из текущего турнира\n\n"
        "<i>Выберите шаблон для генерации:</i>"
    )


def _hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 HERE WE GO (Фото)", callback_data="ws:deal:portrait"),
            InlineKeyboardButton("🃏 HERE WE GO (Карта)", callback_data="ws:deal:card"),
        ],
        [
            InlineKeyboardButton("🌟 Спецкарта (Фото)", callback_data="ws:surcharge:portrait"),
            InlineKeyboardButton("🌟 Спецкарта (Карта)", callback_data="ws:surcharge:card"),
        ],
        [
            InlineKeyboardButton("🗑 В Урну", callback_data="ws:urn"),
            InlineKeyboardButton("🏁 Итоги ТО (Recap)", callback_data="ws:recap"),
        ],
        [
            InlineKeyboardButton("🎲 Сделка из базы ТО", callback_data="ws:from_db:card"),
        ],
        [
            InlineKeyboardButton("❌ Закрыть", callback_data="ws:close"),
        ],
    ])


async def cmd_workshop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Точка входа: /workshop, /studio, /cards."""
    user = update.effective_user
    if not user or not is_workshop_allowed(user.id):
        if update.effective_chat and update.effective_chat.type == "private":
            await update.effective_message.reply_text(
                "⛔ <b>Доступ запрещён</b>\n\nМастерская графики доступна администраторам и ответственному за ТО.",
                parse_mode="HTML",
            )
        return

    await update.effective_message.reply_text(
        _hub_text(),
        reply_markup=_hub_keyboard(),
        parse_mode="HTML",
    )


async def cb_workshop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик интерактивных кнопок мастерской."""
    query = update.callback_query
    if not query:
        return
    user = update.effective_user
    if not user or not is_workshop_allowed(user.id):
        await query.answer("⛔ Недостаточно прав", show_alert=True)
        return

    data = query.data or ""

    if data == "ws:close":
        await query.answer()
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if data == "ws:menu":
        await query.answer()
        try:
            await query.edit_message_text(
                _hub_text(),
                reply_markup=_hub_keyboard(),
                parse_mode="HTML",
            )
        except Exception:
            await query.message.reply_text(
                _hub_text(),
                reply_markup=_hub_keyboard(),
                parse_mode="HTML",
            )
        return

    if data.startswith("ws:deal"):
        is_toggle = "toggle" in data
        style = "card" if ":card" in data else "portrait"

        if is_toggle:
            # ws:deal_toggle:{id}:{target_style}
            parts = data.split(":")
            sample_id = parts[2] if len(parts) > 2 else ""
            style = parts[3] if len(parts) > 3 else "portrait"
            sample = next((s for s in SAMPLE_DEALS if str(s["id"]) == sample_id), SAMPLE_DEALS[0])
            await query.answer("🔄 Переключаю стиль оформления...")
        else:
            sample = random.choice(SAMPLE_DEALS)
            await query.answer(f"🎨 Генерирую HERE WE GO ({'карточка FC' if style == 'card' else 'портрет'})...")

        png = await asyncio.to_thread(_render_sample_deal, sample, style)

        mode_desc = "вариант с карточкой FC Mobile" if style == "card" else "вариант с портретом"
        event_str = f" ({sample['event']})" if sample.get("event") else ""
        caption = (
            f"🔁 <b>HERE WE GO — {sample['player']}</b> ({mode_desc})\n"
            f"Маршрут: {sample['from_club']} → {sample['to_club']}\n"
            f"Сумма: <b>{sample['price']}</b> | Рейтинг: <b>{sample['ovr']} OVR</b>{event_str}\n\n"
            f"<i>Разрешение: 1440×810 (Retina 2x). Готово к публикации.</i>"
        )

        other_style = "portrait" if style == "card" else "card"
        toggle_label = "👤 Показать с портретом" if style == "card" else "🃏 Показать карточку FC"

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(toggle_label, callback_data=f"ws:deal_toggle:{sample['id']}:{other_style}"),
                InlineKeyboardButton("🎲 Другой пример", callback_data=f"ws:deal_next:{style}"),
            ],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])

        if is_toggle:
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=io.BytesIO(png), caption=caption, parse_mode="HTML"),
                    reply_markup=kb,
                )
                return
            except Exception:
                pass

        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ws:surcharge"):
        is_toggle = "toggle" in data
        style = "card" if ":card" in data else "portrait"

        if is_toggle:
            parts = data.split(":")
            sample_id = parts[2] if len(parts) > 2 else ""
            style = parts[3] if len(parts) > 3 else "portrait"
            sample = next((s for s in SAMPLE_SURCHARGES if str(s["id"]) == sample_id), SAMPLE_SURCHARGES[0])
            await query.answer("🔄 Переключаю стиль оформления...")
        else:
            sample = random.choice(SAMPLE_SURCHARGES)
            await query.answer(f"🎨 Генерирую спецкарту ({'карточка FC' if style == 'card' else 'портрет'})...")

        png = await asyncio.to_thread(_render_sample_surcharge, sample, style)
        mode_desc = "вариант с карточкой FC Mobile" if style == "card" else "вариант с портретом"
        event_str = f" ({sample['event']})" if sample.get("event") else ""
        caption = (
            f"🌟 <b>СПЕЦКАРТА — {sample['player']}</b> ({mode_desc})\n"
            f"Клуб: {sample['from_club']}\n"
            f"Доплата: <b>{sample['price']}</b> | Новый рейтинг: <b>{sample['ovr']} OVR</b>{event_str}\n\n"
            f"<i>Разрешение: 1440×810 (Retina).</i>"
        )

        other_style = "portrait" if style == "card" else "card"
        toggle_label = "👤 Показать с портретом" if style == "card" else "🃏 Показать карточку FC"

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(toggle_label, callback_data=f"ws:surcharge_toggle:{sample['id']}:{other_style}"),
                InlineKeyboardButton("🎲 Другой пример", callback_data=f"ws:surcharge_next:{style}"),
            ],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])

        if is_toggle:
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=io.BytesIO(png), caption=caption, parse_mode="HTML"),
                    reply_markup=kb,
                )
                return
            except Exception:
                pass

        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ws:urn"):
        is_toggle = "toggle" in data
        style = "card" if ":card" in data else "portrait"

        if is_toggle:
            parts = data.split(":")
            sample_id = parts[2] if len(parts) > 2 else ""
            style = parts[3] if len(parts) > 3 else "portrait"
            sample = next((s for s in SAMPLE_URNS if str(s["id"]) == sample_id), SAMPLE_URNS[0])
            await query.answer("🔄 Переключаю стиль оформления...")
        else:
            sample = random.choice(SAMPLE_URNS)
            await query.answer("🎨 Генерирую карточку сдачи в урну...")

        png = await asyncio.to_thread(_render_sample_urn, sample, style)
        mode_desc = "вариант с карточкой FC Mobile" if style == "card" else "вариант с портретом"
        caption = (
            f"🗑 <b>В УРНУ — {sample['player']}</b> ({mode_desc})\n"
            f"Клуб: {sample['from_club']} → Урна\n"
            f"Выплата клубу: <b>{sample['price']}</b> | Рейтинг: <b>{sample['ovr']} OVR</b>\n\n"
            f"<i>Разрешение: 1440×810 (Retina).</i>"
        )

        other_style = "portrait" if style == "card" else "card"
        toggle_label = "👤 Показать с портретом" if style == "card" else "🃏 Показать карточку FC"

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(toggle_label, callback_data=f"ws:urn_toggle:{sample['id']}:{other_style}"),
                InlineKeyboardButton("🎲 Другой пример", callback_data=f"ws:urn_next:{style}"),
            ],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])

        if is_toggle:
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=io.BytesIO(png), caption=caption, parse_mode="HTML"),
                    reply_markup=kb,
                )
                return
            except Exception:
                pass

        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

    if data == "ws:recap":
        await query.answer("📊 Генерирую итоговый постер окна...")
        win = repo.get_active_window()
        rc = recap.build(win["id"]) if win else None

        if rc and rc.requests_count > 0:
            png = await recap.build_image_async(rc)
            caption = recap.caption(rc, win.get("title") or "ТО")
        else:
            png = await asyncio.to_thread(_render_demo_recap)
            caption = (
                "🏁 <b>Демо-итоги трансферного окна</b>\n\n"
                "Оборот: <b>1.15 млрд</b>\n"
                "Заявок одобрено: 46 (обменов: 9, продаж в урну: 14)\n"
                "Самый активный клуб: <b>Реал Мадрид</b> (7 сделок)\n\n"
                "<i>Перед именами игроков выводятся круглые аватары/монограммы.</i>"
            )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("↻ Обновить", callback_data="ws:recap")],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])
        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ws:from_db"):
        is_toggle = "toggle" in data
        style = "portrait" if ":portrait" in data else "card"

        win = repo.get_active_window()
        transfers = repo.list_transfers(win["id"], statuses=("approved",)) if win else []
        if not transfers:
            for w in repo.list_windows()[:5]:
                transfers = repo.list_transfers(w["id"], statuses=("approved",))
                if transfers:
                    break

        if not transfers:
            await query.answer()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")]])
            await query.message.reply_text(
                "ℹ️ В базе пока нет одобренных заявок ТО.\n\n"
                "Создайте и одобрите сделку через Mini App или воспользуйтесь демо-шаблонами в мастерской.",
                reply_markup=kb,
            )
            return

        if is_toggle:
            parts = data.split(":")
            tr_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else transfers[0]["id"]
            latest = next((t for t in transfers if t["id"] == tr_id), transfers[0])
            await query.answer("🔄 Переключаю стиль оформления...")
        else:
            latest = transfers[0]
            await query.answer(f"🔍 Рендерю заявку #{latest.get('id')} ({style})...")

        png = await card.build_card_async(latest, style=style)
        if not png:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")]])
            await query.message.reply_text("⚠️ Не удалось сгенерировать карточку для заявки.", reply_markup=kb)
            return

        mode_desc = "карточка FC Mobile" if style == "card" else "портрет"
        caption = (
            f"📦 <b>Заявка #{latest.get('id')} из базы данных</b> ({mode_desc})\n"
            f"Игрок: <b>{latest.get('player_name')}</b> | Рейтинг: <b>{latest.get('ovr', '—')} OVR</b>\n"
            f"Маршрут: {latest.get('from_club') or '—'} → {latest.get('to_club') or '—'}\n"
            f"Сумма: <b>{latest.get('price_k', 0) // 1000} млн</b>"
        )

        other_style = "portrait" if style == "card" else "card"
        toggle_label = "👤 Показать с портретом" if style == "card" else "🃏 Показать карточку FC"

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(toggle_label, callback_data=f"ws:from_db_toggle:{latest['id']}:{other_style}"),
            ],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])

        if is_toggle:
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=io.BytesIO(png), caption=caption, parse_mode="HTML"),
                    reply_markup=kb,
                )
                return
            except Exception:
                pass

        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

