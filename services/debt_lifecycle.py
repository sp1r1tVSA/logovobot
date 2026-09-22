"""Что трекер долгов должен сделать с долгом сейчас — чистая функция без базы и Telegram.

Все этапы считаются от `escalate_at` долга (он уже сдвинут на заморозку), а не от
дедлайна тура, поэтому досрочно закрытый тур и обычный идут через один код:

    notify ─ remind каждые DEBT_REMINDER_INTERVAL_HOURS ─┐
    soft_warn за DEBT_SOFT_WARNING_HOURS до escalate_at ─┤
    escalate в escalate_at ──────────────────────────────┤ пока нет вердикта
    reescalate каждые DEBT_REESCALATION_INTERVAL_HOURS ──┤
    escalate_global через DEBT_GLOBAL_ESCALATION_DELAY_HOURS после escalate

Продлённый (замороженный) матч не получает ничего, кроме `expire_extension`, когда
продление истекло. Хендлер исполняет действия по порядку и отмечает в `match_debts`
только то, что действительно ушло адресату.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Mapping

import config
from time_utils import parse_msk

EXPIRE_EXTENSION = "expire_extension"
NOTIFY = "notify"
REMIND = "remind"
SOFT_WARN = "soft_warn"
ESCALATE = "escalate"
REESCALATE = "reescalate"
ESCALATE_GLOBAL = "escalate_global"


def _get(row: Mapping[str, Any] | None, key: str):
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _hours_since(value, now: _dt.datetime) -> float | None:
    moment = value if isinstance(value, _dt.datetime) else parse_msk(value)
    if moment is None:
        return None
    return (now - moment).total_seconds() / 3600.0


def plan_debt_actions(match: Mapping[str, Any], now: _dt.datetime) -> list[str]:
    """Действия для одного долга из `get_detailed_overdue_matches` (с `match["debt"]`).

    `match["escalate_at"]` — срок эскалации со сдвигом на заморозку; `match["debt"]` —
    строка `match_debts` с отметками уже сделанного. Порядок в списке — порядок
    исполнения.
    """
    debt = _get(match, "debt") or {}
    actions: list[str] = []

    if _get(match, "is_extended"):
        until = parse_msk(_get(match, "extended_until"))
        if until is None or now < until:
            return actions
        actions.append(EXPIRE_EXTENSION)

    escalate_at = _get(match, "escalate_at")
    if not isinstance(escalate_at, _dt.datetime):
        escalate_at = parse_msk(escalate_at)
    if escalate_at is None:
        return actions
    hours_left = (escalate_at - now).total_seconds() / 3600.0

    since_reminder = _hours_since(_get(debt, "last_reminder_at"), now)
    if since_reminder is None:
        # Первое сообщение о долге; мягкое предупреждение, если оно уже
        # положено, придёт следующим прогоном, а не вторым ЛС подряд.
        actions.append(NOTIFY)
    elif 0 < hours_left <= config.DEBT_SOFT_WARNING_HOURS and not _get(debt, "soft_warned_at"):
        actions.append(SOFT_WARN)
    elif since_reminder >= config.DEBT_REMINDER_INTERVAL_HOURS:
        actions.append(REMIND)

    escalated_at = _get(debt, "escalated_at")
    if not escalated_at:
        if hours_left <= 0:
            actions.append(ESCALATE)
        return actions

    since_last = _hours_since(_get(debt, "last_escalation_at") or escalated_at, now)
    if since_last is not None and since_last >= config.DEBT_REESCALATION_INTERVAL_HOURS:
        actions.append(REESCALATE)
    since_first = _hours_since(escalated_at, now)
    if (
        not _get(debt, "global_escalated_at")
        and since_first is not None
        and since_first >= config.DEBT_GLOBAL_ESCALATION_DELAY_HOURS
    ):
        actions.append(ESCALATE_GLOBAL)
    return actions
