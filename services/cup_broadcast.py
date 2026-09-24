"""
services/cup_broadcast.py

Тема «Кубок»: у каждого кубка (общего и пяти дивизионных) своя тема форума,
привязанная `/set_div_topic <дивизион|общий> cup`. В неё идёт всё о кубке:

* первым сообщением — Pillow-сетка, закреплённая ботом. Её не пересылают заново
  на каждый результат, а редактируют (`edit_message_media`); если закреп удалили
  или отредактировать нельзя — публикуется и закрепляется новая;
* результаты игр — тот же пост, что у лиги (`send_result_post`), только адресат
  другой: `handlers.base.resolve_post_target(..., match_id=...)`;
* объявления: серия решена, кубок разыгран, этап стартовал.

Всё здесь best-effort: результат уже записан в базу, и сбой Telegram не должен
ни откатывать его, ни ронять хендлер. Поэтому публичные функции исключений
наружу не пускают, только логируют.
"""

import asyncio
import html
import logging

import database
from time_utils import now_msk

logger = logging.getLogger(__name__)

# Одна перерисовка на кубок за раз: два результата, подтверждённые одновременно,
# иначе оба не смогли бы отредактировать закреп и оба опубликовали бы новый.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(division_id, season_id) -> asyncio.Lock:
    key = f"{season_id}:{database.cup_scope(division_id)}"
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _topic_target(topic: dict) -> dict:
    return {"chat_id": int(topic["group_chat_id"]), "message_thread_id": int(topic["message_thread_id"])}


async def cup_post_target(division_id, season_id=None) -> dict | None:
    """Адресат сообщений кубка: {chat_id, message_thread_id} или None, если тема
    не привязана (тогда кубок молчит — как было до тем)."""
    topic = await asyncio.to_thread(database.get_cup_topic, division_id, season_id)
    return _topic_target(topic) if topic else None


async def match_post_target(match_id: int) -> dict | None:
    """Адресат результата кубкового матча — тема его кубка."""
    scope = await asyncio.to_thread(database.get_match_cup_scope, match_id)
    if not scope:
        return None
    return await cup_post_target(scope["division_id"], scope["season_id"])


# ─── Сетка ────────────────────────────────────────────────────────────────────

def _bracket_status(bracket: list[dict]) -> tuple[str, str | None]:
    """(подпись под заголовком, обладатель кубка или None)."""
    from services.graphics.cup_bracket_generator import stage_caption

    played = [b for b in bracket if b.get("series")]
    if not played:
        return "Жеребьёвка впереди", None
    last = played[-1]
    series = last["series"]
    if len(series) == 1 and series[0].get("winner_name") and last["stage"]["stage"] == "final":
        return "Кубок разыгран", series[0]["winner_name"]
    stage = last["stage"]
    caption = stage_caption(stage["stage"])
    if all(s.get("winner_name") for s in series):
        return f"{caption}: все серии сыграны", None
    if stage.get("is_open"):
        return f"Идёт {caption[:1].lower() + caption[1:]}", None
    if stage.get("bets_open"):
        return f"{caption}: приём прогнозов", None
    return f"{caption}: скоро старт", None


def render_bracket(division_id, season_id=None) -> tuple:
    """Синхронно: (png, подпись к закрепу). Вызывать через to_thread."""
    from services.graphics.cup_bracket_generator import generate_cup_bracket_image

    bracket = database.get_cup_full_bracket(division_id=division_id, season_id=season_id)
    title = database.cup_scope_label(division_id)
    status, champion = _bracket_status(bracket)
    png = generate_cup_bracket_image(
        bracket, title, division_id=database.cup_scope(division_id), subtitle=status,
    )
    lines = [f"🏆 <b>{html.escape(title)}</b> — сетка", html.escape(status)]
    if champion:
        lines.append(f"🥇 Обладатель: <b>{html.escape(champion)}</b>")
    lines.append(f"<i>Обновлено {now_msk().strftime('%d.%m %H:%M')} МСК</i>")
    return png, "\n".join(lines)


async def _publish_new_bracket(bot, topic: dict, division_id, season_id, png, caption) -> int | None:
    msg = await bot.send_photo(**_topic_target(topic), photo=png, caption=caption, parse_mode="HTML")
    try:
        await bot.pin_chat_message(chat_id=msg.chat_id, message_id=msg.message_id, disable_notification=True)
    except Exception as e:
        # Без прав на закреп сетка всё равно в теме — просто не наверху.
        logger.warning(f"cup bracket: pin failed in chat {msg.chat_id}: {e}")
    await asyncio.to_thread(database.set_cup_bracket_anchor, division_id, msg.message_id, season_id)
    return msg.message_id


async def refresh_cup_bracket(bot, division_id, season_id=None, *, republish: bool = False) -> bool:
    """Обновить закреплённую сетку кубка в его теме. True — сетка в теме актуальна.

    `republish=True` — не редактировать старый закреп, а выложить сетку заново
    (кнопка в /cup и новая привязка темы).
    """
    try:
        s_id = await asyncio.to_thread(database._resolve_season_id, season_id)
        async with _lock_for(division_id, s_id):
            topic = await asyncio.to_thread(database.get_cup_topic, division_id, s_id)
            if not topic:
                return False
            png, caption = await asyncio.to_thread(render_bracket, division_id, s_id)
            anchor = topic.get("anchor_message_id")
            if anchor and not republish:
                from telegram import InputMediaPhoto
                try:
                    await bot.edit_message_media(
                        chat_id=int(topic["group_chat_id"]),
                        message_id=int(anchor),
                        media=InputMediaPhoto(media=png, caption=caption, parse_mode="HTML"),
                    )
                    return True
                except Exception as e:
                    if "not modified" in str(e).lower():
                        return True
                    logger.info(f"cup bracket: anchor {anchor} not editable ({e}); republishing")
                    png.seek(0)
            await _publish_new_bracket(bot, topic, division_id, s_id, png, caption)
            return True
    except Exception as e:
        logger.exception(f"cup bracket refresh failed for cup {division_id}: {e}")
        return False


# ─── Объявления ───────────────────────────────────────────────────────────────

def series_decided_text(series: dict) -> str:
    stage = series.get("stage") or ""
    cup = database.cup_scope_label(series.get("division_id"))
    winner = series["winner_name"]
    t1, t2 = series.get("team1_name") or "", series.get("team2_name") or ""
    loser = t2 if winner.strip().lower() == t1.strip().lower() else t1
    w1, w2 = series.get("team1_wins") or 0, series.get("team2_wins") or 0
    score = f"{max(w1, w2)}:{min(w1, w2)}"
    if stage == "final":
        return (
            f"🏆🏆🏆 <b>{html.escape(winner)}</b> — обладатель: <b>{html.escape(cup)}</b>!\n\n"
            f"Финальная серия против {html.escape(loser)}: <b>{score}</b>."
        )
    from services.graphics.cup_bracket_generator import stage_caption
    return (
        f"🏆 <b>{html.escape(stage_caption(stage))} · {html.escape(cup)}</b>\n\n"
        f"✅ <b>{html.escape(winner)}</b> проходит дальше — серия против "
        f"{html.escape(loser)}: <b>{score}</b>."
    )


async def after_cup_result(bot, match_id: int) -> None:
    """После подтверждения кубковой игры: объявить решённую серию и обновить
    закреп. Вызывать после поста результата — объявление идёт следом за ним."""
    try:
        scope = await asyncio.to_thread(database.get_match_cup_scope, match_id)
        if not scope:
            return
        division_id, season_id = scope["division_id"], scope["season_id"]
        target = await cup_post_target(division_id, season_id)
        if not target:
            return
        if scope.get("series_id"):
            series = await asyncio.to_thread(database.claim_cup_series_announcement, scope["series_id"])
            if series:
                try:
                    await bot.send_message(**target, text=series_decided_text(series), parse_mode="HTML")
                except Exception as e:
                    logger.warning(f"cup: series announcement failed for series {scope['series_id']}: {e}")
        await refresh_cup_bracket(bot, division_id, season_id)
    except Exception as e:
        logger.exception(f"cup broadcast after match {match_id} failed: {e}")


async def announce_stage(bot, stage: dict, event: str) -> None:
    """Объявить в теме кубка смену состояния этапа: `bets` — открыт приём
    прогнозов, `start` — этап начался. Затем обновить закреп."""
    try:
        from services.graphics.cup_bracket_generator import stage_caption

        division_id, season_id = stage.get("division_id"), stage.get("season_id")
        target = await cup_post_target(division_id, season_id)
        if target:
            cup = database.cup_scope_label(division_id)
            caption = stage_caption(stage["stage"])
            series = await asyncio.to_thread(database.get_cup_stage_series, stage["id"])
            if event == "start":
                pairs = "\n".join(
                    f"{s['series_num']}. {html.escape(s['team1_name'])} — {html.escape(s['team2_name'])}"
                    for s in series
                )
                text = f"▶️ <b>{html.escape(cup)}: стартовал этап {html.escape(caption)}</b>\n\n"
                text += "Серии до 2 побед.\n\n" + pairs if pairs else "Серии до 2 побед."
                if stage.get("deadline"):
                    text += f"\n\n⏳ Дедлайн: <b>{html.escape(str(stage['deadline']))}</b>"
            else:
                text = (f"🎟 <b>{html.escape(cup)}, {html.escape(caption)}</b>: приём прогнозов открыт — "
                        "линия в Mini App.")
            try:
                await bot.send_message(**target, text=text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"cup: stage announcement failed: {e}")
        await refresh_cup_bracket(bot, division_id, season_id)
    except Exception as e:
        logger.exception(f"cup stage announcement failed: {e}")
