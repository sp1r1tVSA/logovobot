"""Напоминания трансферного окна.

Три вида, все — ЛС:
  * тренерам перед автозакрытием окна — за сутки и за час: свободные слоты, остаток бюджета и
    предложения сделок, которые ждут ответа;
  * ответственному в те же моменты — сколько заявок ждёт его решения;
  * ответственному — о заявках, которые он не решает дольше `STALE_HOURS`.

Что отправлено, помнит `transfer_reminders` (метка уникальна в окне), поэтому рестарт бота
не повторяет сообщение. Метка закрытия включает время автозакрытия: сдвинули время — напоминания
настроены заново. Здесь ничего не бросает наружу.
"""

from __future__ import annotations

import datetime as dt
import html
import logging

import club_registry
from time_utils import MSK_LABEL, fmt_msk, now_msk, parse_msk
from transfers import notify, repo, sanctions
from transfers.engine import format_k, norm_club

logger = logging.getLogger(__name__)

# (за сколько минут до закрытия, ключ метки, подпись). От ближнего к дальнему.
CLOSE_STEPS = ((60, "1h", "час"), (24 * 60, "24h", "сутки"))
STALE_HOURS = 12
MANAGER_LIST_LIMIT = 10


def due_close_step(window: dict, now: dt.datetime | None = None) -> tuple[str, str] | None:
    """(метка, подпись) ближайшего подходящего шага перед автозакрытием, иначе `None`."""
    if not window or window.get("status") != "open" or not window.get("auto_close_at"):
        return None
    moment = parse_msk(window["auto_close_at"])
    now = now or now_msk()
    if moment is None or moment <= now:
        return None
    left = moment - now
    for minutes, key, label in CLOSE_STEPS:
        if left <= dt.timedelta(minutes=minutes):
            return f"close{key}@{window['auto_close_at']}", label
    return None


def _left_text(window: dict, now: dt.datetime) -> str:
    left = parse_msk(window["auto_close_at"]) - now
    minutes = max(int(left.total_seconds() // 60), 1)
    if minutes >= 120:
        return f"{round(minutes / 60)} ч"
    return f"{minutes} мин"


def _incoming(transfers: list[dict], user_id: int) -> list[dict]:
    """Предложения, где тренер — вторая сторона и ещё не ответил."""
    return [t for t in transfers
            if t["status"] == "pending_counterparty" and t.get("initiator_id") != user_id
            and user_id in (t.get("from_user"), t.get("to_user"))]


def coach_text(window: dict, club: str, ledger, incoming: list[dict], now: dt.datetime) -> str | None:
    """Текст тренеру или `None`, если ему нечего делать (слоты выбраны, предложений нет)."""
    buys_left = max(ledger.buys_limit - ledger.buys_used, 0)
    sells_left = max(ledger.sells_limit - ledger.sells_used, 0)
    if not buys_left and not sells_left and not incoming:
        return None
    lines = [f"⏰ <b>Окно закроется через {_left_text(window, now)}</b>",
             f"{fmt_msk(window['auto_close_at'])} {MSK_LABEL} · {html.escape(club)}", ""]
    lines.append(f"Покупки: {ledger.buys_used}/{ledger.buys_limit} · продажи: {ledger.sells_used}/{ledger.sells_limit}")
    lines.append(f"Бюджет: <b>{format_k(ledger.remaining_k)}</b>")
    if incoming:
        lines += ["", "🤝 <b>Ждут вашего ответа:</b>"]
        lines += [f"• {notify.describe_transfer(t)}" for t in incoming[:5]]
    lines += ["", "Заявки подаются в Mini App → 🔁 Трансферы."]
    return "\n".join(lines)


def manager_text(window: dict, pending: list[dict], waiting_counterparty: int, now: dt.datetime) -> str | None:
    if not pending and not waiting_counterparty:
        return None
    lines = [f"⏰ <b>Окно закроется через {_left_text(window, now)}</b>", ""]
    if pending:
        lines.append(f"Ждут вашего решения: <b>{len(pending)}</b>")
        lines += [f"• {notify.describe_transfer(t)}" for t in pending[:MANAGER_LIST_LIMIT]]
        if len(pending) > MANAGER_LIST_LIMIT:
            lines.append(f"… и ещё {len(pending) - MANAGER_LIST_LIMIT}")
    if waiting_counterparty:
        lines.append(f"Не подтверждены второй стороной: {waiting_counterparty} "
                     "(при закрытии окна будут отклонены)")
    return "\n".join(lines)


def stale_pending(transfers: list[dict], now: dt.datetime) -> list[dict]:
    """Заявки у ответственного, которые лежат дольше `STALE_HOURS`."""
    border = now - dt.timedelta(hours=STALE_HOURS)
    out = []
    for t in transfers:
        if t["status"] != "pending_manager":
            continue
        since = parse_msk(t.get("updated_at") or t.get("created_at"))
        if since is not None and since <= border:
            out.append(t)
    return out


def _coach_clubs() -> dict[str, tuple[str, list[int]]]:
    """{ключ клуба: (имя, [telegram_id тренеров])} одним проходом по `users`."""
    clubs: dict[str, tuple[str, list[int]]] = {}
    for row in repo.list_coaches():
        name = club_registry.resolve_team_name(row["team_name"]) or row["team_name"]
        key = norm_club(name)
        if key:
            clubs.setdefault(key, (name, []))[1].append(int(row["telegram_id"]))
    return clubs


async def send_close_reminders(bot, window: dict, now: dt.datetime) -> int:
    """Напоминания тренерам и ответственному. Возвращает число отправленных сообщений."""
    sent = 0
    transfers = repo.list_transfers(window["id"])
    for club, coaches in _coach_clubs().values():
        ledger = repo.get_club_ledger(window["id"], club)
        for user_id in coaches:
            if sanctions.for_user(user_id, club):
                continue
            text = coach_text(window, club, ledger, _incoming(transfers, user_id), now)
            if text and await notify.dm_user(bot, user_id, text):
                sent += 1

    pending = [t for t in transfers if t["status"] == "pending_manager"]
    waiting = sum(1 for t in transfers if t["status"] == "pending_counterparty")
    text = manager_text(window, pending, waiting, now)
    if text and await notify.dm_manager(bot, text):
        sent += 1
    return sent


async def send_stale_nudge(bot, window: dict, now: dt.datetime) -> int:
    """Одно сообщение ответственному о заявках, которых он ещё не касался; каждая — один раз."""
    fresh = [t for t in stale_pending(repo.list_transfers(window["id"]), now)
             if repo.claim_reminder(window["id"], f"stale:{t['id']}")]
    if not fresh:
        return 0
    lines = [f"🔔 <b>Заявки ждут решения больше {STALE_HOURS} ч:</b>"]
    lines += [f"• {notify.describe_transfer(t)}" for t in fresh[:MANAGER_LIST_LIMIT]]
    if len(fresh) > MANAGER_LIST_LIMIT:
        lines.append(f"… и ещё {len(fresh) - MANAGER_LIST_LIMIT}")
    lines += ["", "Откройте /to → «Заявки» или решите прямо в теме заявок."]
    return 1 if await notify.dm_manager(bot, "\n".join(lines)) else 0


async def run(bot, now: dt.datetime | None = None) -> int:
    """Один проход задачи. Возвращает число отправленных сообщений."""
    now = now or now_msk()
    window = repo.get_active_window() or repo.get_latest_window()
    if window is None:
        return 0
    sent = 0
    step = due_close_step(window, now)
    if step and repo.claim_reminder(window["id"], step[0]):
        sent += await send_close_reminders(bot, window, now)
    sent += await send_stale_nudge(bot, window, now)
    return sent
