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
import random
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from handlers.base import is_admin, is_global_admin
from services.graphics import transfer_card_generator as card_gen
from services.graphics import transfer_recap_generator as recap_gen
from transfers import card, recap, repo, service

logger = logging.getLogger(__name__)

# Примеры для демонстрации в мастерской
SAMPLE_DEALS = [
    {
        "player": "Kylian Mbappé",
        "price": "125 млн",
        "ovr": 113,
        "from_club": "ПСЖ",
        "to_club": "Реал Мадрид",
        "id": 101,
        "portrait": None,
    },
    {
        "player": "Jude Bellingham",
        "price": "110 млн",
        "ovr": 112,
        "from_club": "Боруссия Д",
        "to_club": "Манчестер Сити",
        "id": 102,
        "portrait": None,
    },
    {
        "player": "Vinícius Júnior",
        "price": "95 млн",
        "ovr": 111,
        "from_club": "Реал Мадрид",
        "to_club": "Ливерпуль",
        "id": 103,
        "portrait": None,
    },
    {
        "player": "Bukayo Saka",
        "price": "80 млн",
        "ovr": 109,
        "from_club": "Арсенал",
        "to_club": "Бавария",
        "id": 104,
        "portrait": "assets/players/b_saka_арсенал.png",
    },
    {
        "player": "Lamine Yamal",
        "price": "140 млн",
        "ovr": 110,
        "from_club": "Барселона",
        "to_club": "ПСЖ",
        "id": 105,
        "portrait": None,
    },
]

SAMPLE_SURCHARGES = [
    {
        "player": "Erling Haaland",
        "price": "65 млн",
        "ovr": 112,
        "from_club": "Манчестер Сити",
        "id": 201,
    },
    {
        "player": "Rodri",
        "price": "50 млн",
        "ovr": 111,
        "from_club": "Барселона",
        "id": 202,
    },
    {
        "player": "Florian Wirtz",
        "price": "70 млн",
        "ovr": 112,
        "from_club": "Байер",
        "id": 203,
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
        "player": "Mykhailo Mudryk",
        "price": "30 млн",
        "ovr": 100,
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
]


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
        "• <b>HERE WE GO</b> — карточка перехода игрока\n"
        "• <b>СПЕЦКАРТА</b> — карточка доплаты за повышение OVR\n"
        "• <b>В УРНУ</b> — карточка сдачи игрока в урну\n"
        "• <b>ИТОГИ ОКНА</b> — итоговый постер с топом сделок и портретами\n"
        "• <b>ИЗ БАЗЫ ТО</b> — карточка последней реальной одобренной заявки\n\n"
        "<i>Выберите шаблон для генерации:</i>"
    )


def _hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 HERE WE GO", callback_data="ws:deal"),
            InlineKeyboardButton("🌟 Спецкарта", callback_data="ws:surcharge"),
        ],
        [
            InlineKeyboardButton("🗑 В Урну", callback_data="ws:urn"),
            InlineKeyboardButton("🏁 Итоги ТО (Recap)", callback_data="ws:recap"),
        ],
        [
            InlineKeyboardButton("🎲 Сделка из базы ТО", callback_data="ws:from_db"),
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

    if data in ("ws:deal", "ws:deal_next"):
        await query.answer("🎨 Генерирую карточку HERE WE GO...")
        sample = random.choice(SAMPLE_DEALS)
        png = await asyncio.to_thread(
            card_gen.render_transfer_card,
            kind="deal",
            player_name=sample["player"],
            price_text=sample["price"],
            ovr=sample["ovr"],
            from_club=sample["from_club"],
            to_club=sample["to_club"],
            portrait_path=sample.get("portrait"),
            transfer_id=sample["id"],
        )
        caption = (
            f"🔁 <b>HERE WE GO — {sample['player']}</b>\n"
            f"Маршрут: {sample['from_club']} → {sample['to_club']}\n"
            f"Сумма: <b>{sample['price']}</b> | Рейтинг: <b>{sample['ovr']} OVR</b>\n\n"
            f"<i>Разрешение: 1440×810 (Retina). Готово к публикации.</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Другой пример", callback_data="ws:deal_next")],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])
        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

    if data in ("ws:surcharge", "ws:surcharge_next"):
        await query.answer("🎨 Генерирую карточку спецкарты...")
        sample = random.choice(SAMPLE_SURCHARGES)
        png = await asyncio.to_thread(
            card_gen.render_transfer_card,
            kind="surcharge",
            player_name=sample["player"],
            price_text=sample["price"],
            ovr=sample["ovr"],
            from_club=sample["from_club"],
            transfer_id=sample["id"],
        )
        caption = (
            f"🌟 <b>СПЕЦКАРТА — {sample['player']}</b>\n"
            f"Клуб: {sample['from_club']}\n"
            f"Доплата: <b>{sample['price']}</b> | Новый рейтинг: <b>{sample['ovr']} OVR</b>\n\n"
            f"<i>Разрешение: 1440×810 (Retina).</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Другой пример", callback_data="ws:surcharge_next")],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])
        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

    if data in ("ws:urn", "ws:urn_next"):
        await query.answer("🎨 Генерирую карточку сдачи в урну...")
        sample = random.choice(SAMPLE_URNS)
        png = await asyncio.to_thread(
            card_gen.render_transfer_card,
            kind="urn_sale",
            player_name=sample["player"],
            price_text=sample["price"],
            ovr=sample["ovr"],
            from_club=sample["from_club"],
            transfer_id=sample["id"],
        )
        caption = (
            f"🗑 <b>В УРНУ — {sample['player']}</b>\n"
            f"Клуб: {sample['from_club']} → Урна\n"
            f"Выплата клубу: <b>{sample['price']}</b>\n\n"
            f"<i>Разрешение: 1440×810 (Retina).</i>"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎲 Другой пример", callback_data="ws:urn_next")],
            [InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")],
        ])
        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return

    if data == "ws:recap":
        await query.answer("📊 Генерирую итоговый постер окна...")
        # Проверим, есть ли окно с реальными одобренными сделками
        win = repo.get_active_window()
        rc = recap.build(win["id"]) if win else None

        if rc and rc.requests_count > 0:
            png = await recap.build_image_async(rc)
            caption = recap.caption(rc, win.get("title") or "ТО")
        else:
            # Демонстрационный постер с топ-5 сделками и аватарами
            deals = [
                recap_gen.RecapDeal("Lamine Yamal", "Барселона → ПСЖ", "140 млн"),
                recap_gen.RecapDeal("Kylian Mbappé", "ПСЖ → Реал Мадрид", "125 млн"),
                recap_gen.RecapDeal("Florian Wirtz", "Байер → Манчестер Сити", "115 млн"),
                recap_gen.RecapDeal("Bukayo Saka", "Арсенал → Бавария", "95 млн", portrait_path="assets/players/b_saka_арсенал.png"),
                recap_gen.RecapDeal("Vinícius Júnior", "Реал Мадрид → Ливерпуль", "85 млн"),
            ]
            png = await asyncio.to_thread(
                recap_gen.render_window_recap,
                title="ТО 1-ГО КРУГА 2026",
                turnover_text="1.15 млрд",
                deals=deals,
                requests_count=46,
                swaps=9,
                urn_sales=14,
                top_club="Реал Мадрид",
                top_club_count=7,
            )
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

    if data == "ws:from_db":
        await query.answer("🔍 Ищу последнюю сделку в базе...")
        # Ищем одобренные заявки из текущего или любого окна
        win = repo.get_active_window()
        transfers = repo.list_transfers(win["id"], statuses=("approved",)) if win else []
        if not transfers:
            # Попробуем найти вообще любую одобренную сделку
            for w in repo.list_windows()[:5]:
                transfers = repo.list_transfers(w["id"], statuses=("approved",))
                if transfers:
                    break

        if not transfers:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")]])
            await query.message.reply_text(
                "ℹ️ В базе пока нет одобренных заявок ТО.\n\n"
                "Создайте и одобрите сделку через Mini App или воспользуйтесь демо-шаблонами в мастерской.",
                reply_markup=kb,
            )
            return

        latest = transfers[0]
        png = await card.build_card_async(latest)
        if not png:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")]])
            await query.message.reply_text("⚠️ Не удалось сгенерировать карточку для заявки.", reply_markup=kb)
            return

        caption = (
            f"📦 <b>Заявка #{latest.get('id')} из базы данных</b>\n"
            f"Игрок: <b>{latest.get('player_name')}</b>\n"
            f"Маршрут: {latest.get('from_club') or '—'} → {latest.get('to_club') or '—'}\n"
            f"Сумма: <b>{latest.get('price_k', 0) // 1000} млн</b>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В мастерскую", callback_data="ws:menu")]])
        await query.message.reply_photo(photo=io.BytesIO(png), caption=caption, parse_mode="HTML", reply_markup=kb)
        return
