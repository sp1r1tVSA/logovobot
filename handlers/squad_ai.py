"""
AI-assisted squad recognition shared by the admin roster panel and the cabinet.

Lives in its own module because `handlers/admin.py` already imports from
`handlers/cabinet.py` — both need this, so it cannot sit in either.
"""

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
from services.ai.squad_recognizer import is_same_footballer, recognize_squad_screenshot_bytes

logger = logging.getLogger(__name__)

PENDING_KEY = "squad_ai_pending"


async def recognize_squad_photo(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    is_reserves: bool = False,
) -> list[dict] | None:
    """Download a Telegram photo and read its squad off the screen. None on failure."""
    try:
        f_obj = await context.bot.get_file(file_id)
        img_bytes = bytes(await f_obj.download_as_bytearray())
    except Exception as e:
        logger.exception(f"Failed to download squad photo {file_id}: {e}")
        return None

    return await asyncio.to_thread(recognize_squad_screenshot_bytes, img_bytes, is_reserves=is_reserves)


def build_review_message(
    club: str,
    players: list[dict],
    current_count: int,
    is_reserves: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the recognized roster with apply/replace/cancel controls."""
    title = f"🤖 <b>Распознан {'резерв (скамейка)' if is_reserves else 'состав'} клуба {html.escape(club)}</b>"
    lines = [title, ""]
    for idx, p in enumerate(players, 1):
        pos = p.get("position")
        suffix = f" — <i>{html.escape(pos)}</i>" if pos else ""
        lines.append(f"{idx}. {html.escape(p['player_name'])}{suffix}")
    lines.append("")
    label = "резервистов" if is_reserves else "футболистов"
    lines.append(f"Найдено {label}: <b>{len(players)}</b>. Сейчас в составе: <b>{current_count}</b>.")
    lines.append("")
    lines.append("Проверьте список и выберите действие:")

    if is_reserves:
        keyboard = [
            [InlineKeyboardButton("➕ Добавить к составу", callback_data="squadai_add")],
            [InlineKeyboardButton("❌ Отмена", callback_data="squadai_cancel")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("➕ Добавить к составу", callback_data="squadai_add")],
            [InlineKeyboardButton("🔄 Заменить состав", callback_data="squadai_replace")],
            [InlineKeyboardButton("❌ Отмена", callback_data="squadai_cancel")],
        ]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def offer_recognized_squad(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    club: str,
    file_id: str,
    back_cb: str,
    is_reserves: bool = False,
) -> None:
    """Run recognition on `file_id` and reply with the review keyboard."""
    message = update.effective_message
    status = await message.reply_text("🤖 Распознаю состав, подождите…")

    players = await recognize_squad_photo(context, file_id, is_reserves=is_reserves)

    if players is None:
        await status.edit_text(
            "❌ Не удалось распознать состав (ИИ недоступен). Попробуйте позже или введите игроков текстом.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data=back_cb)]]),
        )
        return

    if not players:
        await status.edit_text(
            "🤷 На скриншоте не найдено ни одного футболиста. Пришлите скриншот экрана состава покрупнее.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data=back_cb)]]),
        )
        return

    current = await asyncio.to_thread(database.get_squad, club)

    if is_reserves and current and players:
        filtered = []
        for p in players:
            p_name = p.get("player_name") or ""
            if any(is_same_footballer(p_name, cur) for cur in current):
                logger.info("Filtered starter '%s' out of reserves for '%s'", p_name, club)
                continue
            filtered.append(p)
        players = filtered

        if not players:
            await status.edit_text(
                "🤷 В резерве не найдено новых футболистов (все распознанные игроки уже есть в основе клуба).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data=back_cb)]]),
            )
            return

    if not is_reserves and not current and len(players) >= 11:
        deleted, added = await asyncio.to_thread(database.replace_squad, club, players)
        asyncio.create_task(_prefetch_squad_photos(players, club))
        context.user_data.pop(PENDING_KEY, None)

        lines = [
            f"✅ <b>ИИ распознал и добавил {added} игроков в состав клуба {html.escape(club)}!</b>",
            "",
        ]
        for idx, p in enumerate(players, 1):
            pos = p.get("position")
            suffix = f" — <i>{html.escape(pos)}</i>" if pos else ""
            lines.append(f"{idx}. {html.escape(p['player_name'])}{suffix}")
        lines.append("")
        lines.append("Если нужно отредактировать — нажмите [Изменить].")

        keyboard = [[InlineKeyboardButton("✏️ Изменить", callback_data=back_cb)]]
        await status.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    context.user_data[PENDING_KEY] = {
        "club": club,
        "players": players,
        "back_cb": back_cb,
        "is_reserves": is_reserves,
    }

    text, markup = build_review_message(club, players, len(current), is_reserves=is_reserves)
    await status.edit_text(text, parse_mode="HTML", reply_markup=markup)


async def _prefetch_squad_photos(players: list[dict], club: str) -> None:
    """Fire-and-forget: warm the player_photos cache for a freshly recognized squad."""
    from services.graphics import player_photos

    # Позиция разводит однофамильцев внутри ростера клуба («MARTÍNEZ» ST — Lautaro).
    pairs = [
        (name, club, p.get("position"))
        for p in players
        if (name := (p.get("player_name") or p.get("name")))
    ]
    try:
        await asyncio.to_thread(player_photos.fetch_all_players, pairs)
    except Exception:
        logger.exception(f"Failed to prefetch player photos for squad '{club}'")


async def squad_ai_apply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the add / replace / cancel buttons of a pending recognition."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    pending = context.user_data.pop(PENDING_KEY, None)
    if not pending:
        await query.edit_message_text("⌛ Результат распознавания устарел. Загрузите состав заново.")
        return

    club, players, back_cb = pending["club"], pending["players"], pending["back_cb"]
    is_reserves = pending.get("is_reserves", False)
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👥 Просмотреть состав", callback_data=back_cb)]])

    if query.data == "squadai_cancel":
        label = "резерв клуба" if is_reserves else "состав клуба"
        await query.edit_message_text(
            f"❌ Распознанный {label} <b>{html.escape(club)}</b> не сохранён.",
            parse_mode="HTML",
            reply_markup=back_kb,
        )
        return

    if query.data == "squadai_replace":
        deleted, added = await asyncio.to_thread(database.replace_squad, club, players)
        text = (
            f"🔄 Состав клуба <b>{html.escape(club)}</b> обновлён.\n"
            f"Удалено: <b>{deleted}</b>, записано: <b>{added}</b>."
        )
    else:
        added = await asyncio.to_thread(database.add_squad, club, players)
        label = "резервистов" if is_reserves else "футболистов"
        text = f"✅ В состав клуба <b>{html.escape(club)}</b> добавлено {label}: <b>{added}</b>."

    asyncio.create_task(_prefetch_squad_photos(players, club))

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=back_kb)

