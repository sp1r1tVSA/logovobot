"""Единая политика «долг или нет» — чистые функции без базы и без Telegram.

Раньше правило жило в двух местах (`_league_round_is_overdue` для списка долгов и
`is_match_overdue` для карточки матча) и расходилось: один экран показывал матч
долгом, другой — обычным. Теперь оба грузят данные и спрашивают здесь.

Тур:
    scheduled ──open(deadline)──▶ open ──close()──▶ closed
Фаза «дедлайн прошёл» не хранится: это `open` + `deadline <= now` (`overdue`).

Матч становится долгом:
    • в момент дедлайна тура — если тур открыт или закрыт после дедлайна;
    • в момент закрытия — если тур закрыт РАНЬШЕ дедлайна. Тогда к регламентным
      `DEBT_ESCALATION_HOURS` добавляется остаток до дедлайна, округлённый вверх
      до часа (`grace_hours`).
Тур без дедлайна долгов не порождает: открыть тур без дедлайна больше нельзя,
а старые такие туры бот показывает админам отдельным предупреждением.

Модуль импортирует только `time_utils` и `config` — его можно звать откуда угодно,
включая `database.py`.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from typing import Any, Mapping

import config
from time_utils import parse_msk

ROUND_SCHEDULED = "scheduled"
ROUND_OPEN = "open"
ROUND_CLOSED = "closed"
ROUND_OVERDUE = "overdue"  # вычисляемая фаза, в базе не хранится


@dataclass(frozen=True)
class DebtTerms:
    """Срок долга: когда возник, сколько добавлено за досрочное закрытие, когда эскалация."""

    became_debt_at: _dt.datetime
    grace_hours: int
    escalate_at: _dt.datetime


def _get(row: Mapping[str, Any] | None, key: str, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def round_status(row: Mapping[str, Any] | None) -> str:
    """Хранимый статус тура: scheduled / open / closed.

    `is_open` авторитетен для «открыт ли тур» — его пишут Mini App, ставки и
    старые тесты, и он всегда синхронен со `status`. `status` различает только
    два закрытых состояния: тур ещё не открывали (`scheduled`) и тур закрыт
    (`closed`). Строки без `status` (до миграции 019 и вставленные руками)
    выводятся по старому признаку: был дедлайн — значит, тур уже шёл.
    """
    if row is None:
        return ROUND_SCHEDULED
    if _get(row, "is_open", 0):
        return ROUND_OPEN
    stored = _get(row, "status")
    if stored in (ROUND_CLOSED, ROUND_SCHEDULED):
        return stored
    return ROUND_CLOSED if _get(row, "deadline") else ROUND_SCHEDULED


def round_deadline(row: Mapping[str, Any] | None) -> _dt.datetime | None:
    return parse_msk(_get(row, "deadline"))


def round_phase(row: Mapping[str, Any] | None, now: _dt.datetime) -> str:
    """scheduled / open / overdue / closed — то, что видит админ."""
    status = round_status(row)
    if status == ROUND_OPEN:
        dl = round_deadline(row)
        if dl is not None and dl <= now:
            return ROUND_OVERDUE
    return status


def ceil_hours(delta: _dt.timedelta) -> int:
    """Остаток до дедлайна в часах, вверх до целого часа (23 ч 20 мин → 24)."""
    seconds = delta.total_seconds()
    if seconds <= 0:
        return 0
    return int(math.ceil(seconds / 3600.0))


def terms_for(
    became_debt_at: _dt.datetime,
    grace_hours: int = 0,
    escalation_hours: int | None = None,
) -> DebtTerms:
    esc = config.DEBT_ESCALATION_HOURS if escalation_hours is None else escalation_hours
    return DebtTerms(
        became_debt_at=became_debt_at,
        grace_hours=int(grace_hours),
        escalate_at=became_debt_at + _dt.timedelta(hours=int(grace_hours) + esc),
    )


def early_close_terms(
    deadline: _dt.datetime | None,
    closed_at: _dt.datetime,
    escalation_hours: int | None = None,
) -> DebtTerms | None:
    """Срок долга для матча тура, закрытого в `closed_at`.

    Закрытие после дедлайна срок не меняет — долг уже идёт от дедлайна, и
    возвращается None (строку создаст обычная синхронизация).
    """
    if deadline is None or closed_at >= deadline:
        return None
    return terms_for(closed_at, ceil_hours(deadline - closed_at), escalation_hours)


def debt_terms(
    round_row: Mapping[str, Any] | None,
    now: _dt.datetime,
    escalation_hours: int | None = None,
) -> DebtTerms | None:
    """Срок долга несыгранного матча, выведенный из его тура, или None, если это не долг."""
    status = round_status(round_row)
    if status == ROUND_SCHEDULED:
        return None
    deadline = round_deadline(round_row)
    if deadline is None:
        return None
    if status == ROUND_CLOSED:
        closed_at = parse_msk(_get(round_row, "closed_at"))
        if closed_at is not None and closed_at < deadline:
            return early_close_terms(deadline, closed_at, escalation_hours)
    if deadline <= now:
        return terms_for(deadline, 0, escalation_hours)
    return None


def stored_terms(debt_row: Mapping[str, Any] | None) -> DebtTerms | None:
    """Срок долга из строки `match_debts` (None — если строка битая)."""
    if debt_row is None:
        return None
    became = parse_msk(_get(debt_row, "became_debt_at"))
    escalate = parse_msk(_get(debt_row, "escalate_at"))
    if became is None or escalate is None:
        return None
    return DebtTerms(became, int(_get(debt_row, "grace_hours", 0)), escalate)


def frozen_seconds(match: Mapping[str, Any], now: _dt.datetime) -> float:
    """Сколько матч простоял замороженным, включая текущую заморозку."""
    total = float(_get(match, "frozen_seconds", 0) or 0)
    if _get(match, "is_extended", 0):
        f_at = parse_msk(_get(match, "frozen_at"))
        if f_at is not None and now > f_at:
            total += (now - f_at).total_seconds()
    return max(0.0, total)


def effective_escalate_at(terms: DebtTerms, frozen: float) -> _dt.datetime:
    """Заморозка и продление сдвигают эскалацию ровно на замороженное время."""
    return terms.escalate_at + _dt.timedelta(seconds=frozen)


def hours_overdue(terms: DebtTerms, frozen: float, now: _dt.datetime) -> float:
    """Часы с момента, когда матч стал долгом, без замороженного времени."""
    seconds = (now - terms.became_debt_at).total_seconds() - frozen
    return max(0.0, seconds / 3600.0)


def hours_to_escalation(terms: DebtTerms, frozen: float, now: _dt.datetime) -> float:
    return (effective_escalate_at(terms, frozen) - now).total_seconds() / 3600.0


def plan_deadline_reminder(
    hours_left: float,
    sent_tags: set[str] | frozenset[str],
    milestones: tuple[int, ...] | None = None,
) -> tuple[int, list[str]] | None:
    """Какое напоминание о дедлайне тура отправить сейчас.

    Вехи — `ROUND_DEADLINE_REMINDER_HOURS`. Отправляется ближайшая к дедлайну
    уже наступившая неотправленная веха; более ранние неотправленные
    помечаются вместе с ней. Пропущенный запуск джоба (рестарт бота) поэтому
    даёт одно сообщение с актуальным остатком, а не пачку устаревших.
    Возвращает (часы вехи, теги к записи) или None.
    """
    if hours_left <= 0:
        return None
    ms = sorted(config.ROUND_DEADLINE_REMINDER_HOURS if milestones is None else milestones)
    due = [h for h in ms if h >= hours_left and f"{h}h" not in sent_tags]
    if not due:
        return None
    return due[0], [f"{h}h" for h in due]


def deadline_reminder_label(milestone: int, hours_left: float) -> str:
    """Подпись «осталось …»: веха, если до неё рукой подать, иначе фактический остаток."""
    if milestone == 1:
        return "1 час! 🚨"
    if milestone - hours_left <= 1.5:
        return hours_label(milestone)
    return hours_label(max(1, int(hours_left)))


def hours_label(hours: int) -> str:
    """«1 час», «24 часа», «48 часов»."""
    n = abs(int(hours))
    if n % 10 == 1 and n % 100 != 11:
        word = "час"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "часа"
    else:
        word = "часов"
    return f"{hours} {word}"


def resolve_terms(
    debt_row: Mapping[str, Any] | None,
    round_row: Mapping[str, Any] | None,
    now: _dt.datetime,
) -> DebtTerms | None:
    """Строка `match_debts` главнее тура: срок, записанный при закрытии, не пересчитывается."""
    return stored_terms(debt_row) or debt_terms(round_row, now)


def is_debt(
    match: Mapping[str, Any],
    round_row: Mapping[str, Any] | None,
    debt_row: Mapping[str, Any] | None,
    now: _dt.datetime,
) -> bool:
    """Долг ли матч — одинаково для карточки, кабинета, /check_debts и трекера.

    Статус матча здесь не главный: функцию зовут и после внесения результата,
    чтобы понять, положена ли награда за сыгранный долг. Поэтому матч, сыгранный
    до того, как стал долгом, долгом не считается.
    """
    if debt_row is not None:
        return True
    terms = debt_terms(round_row, now)
    if terms is None:
        return False
    if _get(match, "status", "pending") == "pending":
        return True
    played_at = parse_msk(_get(match, "played_at"))
    return played_at is None or played_at >= terms.became_debt_at
