"""Сводка по дивизионам для `/overview` — чистые функции без базы и без Telegram.

Хэндлер грузит сырые строки (`database.get_league_overview_rows` и
`database.get_detailed_overdue_matches`) и отдаёт их сюда; здесь из них
собираются снимки дивизионов и текст сообщений. Фаза тура и «долг или нет»
решаются только в `services.debt_policy` — сводка их не пересчитывает, поэтому
она показывает те же туры и те же долги, что и остальные экраны админки.

В одном дивизионе может быть открыто несколько туров сразу, поэтому «текущий
тур» — это не один номер, а все открытые туры (и те, у которых дедлайн уже
прошёл, а закрыть их ещё не успели).
"""

from __future__ import annotations

import datetime as _dt
import html
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from services import debt_policy

# Долг, до эскалации которого осталось меньше стольких часов, помечается «скоро».
ESCALATION_SOON_HOURS = 12
# Сколько долгов и строк «Требует внимания» показывать, прежде чем свернуть в «…и ещё N».
MAX_DEBT_LINES = 15
MAX_ATTENTION_LINES = 12

PHASE_ICONS = {
    debt_policy.ROUND_OPEN: "🟢",
    debt_policy.ROUND_OVERDUE: "🔴",
    debt_policy.ROUND_SCHEDULED: "⚪",
    debt_policy.ROUND_CLOSED: "✅",
}


@dataclass
class RoundProgress:
    number: int
    phase: str
    deadline: _dt.datetime | None
    played: int
    total: int


@dataclass
class DivisionSnapshot:
    id: int
    name: str
    active_rounds: list[RoundProgress] = field(default_factory=list)
    next_round: RoundProgress | None = None
    rounds_total: int = 0
    rounds_closed: int = 0
    season_played: int = 0
    season_total: int = 0
    debts: list[dict] = field(default_factory=list)
    escalated: int = 0
    escalating_soon: int = 0
    warned: list[dict] = field(default_factory=list)
    at_limit: list[dict] = field(default_factory=list)

    @property
    def overdue_rounds(self) -> list[RoundProgress]:
        return [r for r in self.active_rounds if r.phase == debt_policy.ROUND_OVERDUE]

    @property
    def idle(self) -> bool:
        """Нет открытого тура, хотя впереди ещё есть неоткрытые."""
        return not self.active_rounds and self.next_round is not None


def _div_of(row: Mapping[str, Any]) -> int:
    return int(row.get("division_id") or 1)


def is_escalated(debt: Mapping[str, Any]) -> bool:
    """Эскалирован ли долг: трекер уже отметил стадию или срок эскалации прошёл."""
    row = debt.get("debt") or {}
    if row.get("state") == "escalated":
        return True
    return float(debt.get("hours_to_escalation") or 0) <= 0


def build_snapshots(
    divisions: Iterable[Mapping[str, Any]],
    rows: Mapping[str, Any],
    debts: Iterable[Mapping[str, Any]],
    now: _dt.datetime,
    max_warns: int,
) -> list[DivisionSnapshot]:
    """Снимок по каждому дивизиону из `divisions`, в их порядке.

    `rows` — результат `database.get_league_overview_rows`, `debts` — результат
    `database.get_detailed_overdue_matches` (без фильтра по дивизиону).
    """
    rounds_by_key = {(_div_of(r), int(r["round_number"])): r for r in rows.get("rounds", [])}
    counts_by_key = {(_div_of(c), int(c["round_number"])): c for c in rows.get("match_counts", [])}
    debts = list(debts)
    warned_users = list(rows.get("warned_users", []))

    snapshots = []
    for div in divisions:
        div_id = int(div["id"])
        snap = DivisionSnapshot(id=div_id, name=str(div.get("name") or f"Дивизион {div_id}"))

        numbers = sorted(
            {n for (d, n) in rounds_by_key if d == div_id and n > 0}
            | {n for (d, n) in counts_by_key if d == div_id}
        )
        for number in numbers:
            round_row = rounds_by_key.get((div_id, number))
            counts = counts_by_key.get((div_id, number)) or {}
            progress = RoundProgress(
                number=number,
                phase=debt_policy.round_phase(round_row, now),
                deadline=debt_policy.round_deadline(round_row),
                played=int(counts.get("played") or 0),
                total=int(counts.get("total") or 0),
            )
            snap.rounds_total += 1
            snap.season_played += progress.played
            snap.season_total += progress.total
            if progress.phase in (debt_policy.ROUND_OPEN, debt_policy.ROUND_OVERDUE):
                snap.active_rounds.append(progress)
            elif progress.phase == debt_policy.ROUND_CLOSED:
                snap.rounds_closed += 1
            elif snap.next_round is None:
                snap.next_round = progress

        snap.debts = sorted(
            (dict(d) for d in debts if _div_of(d) == div_id),
            key=lambda d: (-float(d.get("hours_overdue") or 0), d.get("round_number") or 0),
        )
        for debt in snap.debts:
            if is_escalated(debt):
                snap.escalated += 1
            elif float(debt.get("hours_to_escalation") or 0) <= ESCALATION_SOON_HOURS:
                snap.escalating_soon += 1

        snap.warned = [dict(u) for u in warned_users if _div_of(u) == div_id]
        snap.at_limit = [u for u in snap.warned if int(u["warn_count"]) >= max_warns - 1]
        snapshots.append(snap)
    return snapshots


# ── Форматирование ───────────────────────────────────────────────────────────

def progress_bar(done: int, total: int, width: int = 8) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * min(done, total) / total)
    return "▓" * filled + "░" * (width - filled)


def time_left(delta: _dt.timedelta) -> str:
    """«2 д 5 ч», «5 ч», «40 мин» — сколько осталось (или прошло)."""
    minutes = int(abs(delta.total_seconds()) // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} д {hours} ч" if hours else f"{days} д"
    if hours:
        return f"{hours} ч"
    return f"{max(mins, 1)} мин"


def _fmt_dt(value: _dt.datetime | None) -> str:
    return value.strftime("%d.%m %H:%M") if value else "—"


def _deadline_note(r: RoundProgress, now: _dt.datetime) -> str:
    if r.deadline is None:
        return "без дедлайна"
    if r.phase == debt_policy.ROUND_OVERDUE:
        return f"дедлайн прошёл {_fmt_dt(r.deadline)}"
    return f"до {_fmt_dt(r.deadline)} (ещё {time_left(r.deadline - now)})"


def _player(username: Any, team: Any) -> str:
    team_s = html.escape(str(team or "?"))
    return f"{team_s} (@{html.escape(str(username))})" if username else team_s


def _round_line(r: RoundProgress, now: _dt.datetime) -> str:
    icon = PHASE_ICONS.get(r.phase, "•")
    return (
        f"{icon} Тур {r.number} · {r.played}/{r.total} {progress_bar(r.played, r.total)}"
        f" · {_deadline_note(r, now)}"
    )


def _rounds_block(snap: DivisionSnapshot, now: _dt.datetime) -> list[str]:
    lines = [_round_line(r, now) for r in snap.active_rounds]
    if not snap.active_rounds:
        if snap.next_round is not None:
            lines.append(f"⏸ Открытых туров нет · следующий: Тур {snap.next_round.number}")
        elif snap.rounds_total:
            lines.append("🏁 Все туры закрыты")
        else:
            lines.append("⏸ Туров ещё нет")
    return lines


def _discipline_line(snap: DivisionSnapshot, max_warns: int) -> str:
    if snap.debts:
        extra = []
        if snap.escalated:
            extra.append(f"⚡ {snap.escalated} эскал.")
        if snap.escalating_soon:
            extra.append(f"⏳ {snap.escalating_soon} скоро")
        debts = f"🧾 Долги: {len(snap.debts)}" + (f" ({', '.join(extra)})" if extra else "")
    else:
        debts = "🧾 Долгов нет"
    if snap.warned:
        warns = f"⚠️ Варны: {len(snap.warned)}"
        if snap.at_limit:
            warns += f" (🟥 {len(snap.at_limit)} у лимита)"
    else:
        warns = "⚠️ Варнов нет"
    return f"{debts} · {warns}"


def _season_line(snap: DivisionSnapshot) -> str:
    pct = round(100 * snap.season_played / snap.season_total) if snap.season_total else 0
    return (
        f"📊 Сезон: {snap.season_played}/{snap.season_total} матчей ({pct}%)"
        f" · закрыто туров {snap.rounds_closed}/{snap.rounds_total}"
    )


def attention_items(snapshots: Iterable[DivisionSnapshot], max_warns: int) -> list[str]:
    """Что требует реакции админа — по всем дивизионам, самое срочное первым."""
    items: list[str] = []
    for snap in snapshots:
        tag = f"<b>{html.escape(snap.name)}</b>"
        if snap.escalated:
            items.append(f"⚡ {tag}: эскалировано долгов — {snap.escalated}")
    for snap in snapshots:
        tag = f"<b>{html.escape(snap.name)}</b>"
        for u in snap.at_limit:
            items.append(
                f"🟥 {tag}: {_player(u.get('username'), u.get('team_name'))} — "
                f"{u['warn_count']}/{max_warns} варнов"
            )
    for snap in snapshots:
        tag = f"<b>{html.escape(snap.name)}</b>"
        for r in snap.overdue_rounds:
            left = r.total - r.played
            items.append(f"🔴 {tag}: тур {r.number} — дедлайн прошёл, не сыграно {left}, тур не закрыт")
        if snap.idle:
            items.append(f"⏸ {tag}: нет открытого тура (следующий — {snap.next_round.number})")
    return items


def render_summary(snapshots: list[DivisionSnapshot], now: _dt.datetime, max_warns: int) -> str:
    lines = [f"📡 <b>Обзор лиги</b> · {now.strftime('%d.%m %H:%M')} МСК"]
    if not snapshots:
        lines.append("\nАктивных дивизионов нет.")
        return "\n".join(lines)
    for snap in snapshots:
        lines.append("")
        lines.append(f"🏆 <b>{html.escape(snap.name)}</b>")
        lines.extend(_rounds_block(snap, now))
        lines.append(_discipline_line(snap, max_warns))
        lines.append(_season_line(snap))

    items = attention_items(snapshots, max_warns)
    lines.append("")
    if items:
        lines.append("⚠️ <b>Требует внимания</b>")
        lines.extend(f"• {item}" for item in items[:MAX_ATTENTION_LINES])
        if len(items) > MAX_ATTENTION_LINES:
            lines.append(f"…и ещё {len(items) - MAX_ATTENTION_LINES}")
    else:
        lines.append("✅ Всё спокойно — срочного ничего нет.")
    return "\n".join(lines)


def _debt_line(debt: Mapping[str, Any]) -> str:
    p1 = _player(debt.get("p1_username"), debt.get("player1_team"))
    p2 = _player(debt.get("p2_username"), debt.get("player2_team"))
    overdue = int(float(debt.get("hours_overdue") or 0))
    to_esc = float(debt.get("hours_to_escalation") or 0)
    if is_escalated(debt):
        icon, tail = "⚡", "эскалирован"
    else:
        icon = "⏳" if to_esc <= ESCALATION_SOON_HOURS else "•"
        tail = f"эскалация через {time_left(_dt.timedelta(hours=to_esc))}"
    frozen = " · ❄️ заморожен" if debt.get("is_extended") else ""
    return f"{icon} Тур {debt.get('round_number')}: {p1} — {p2} · {overdue} ч · {tail}{frozen}"


def render_division(snap: DivisionSnapshot, now: _dt.datetime, max_warns: int) -> str:
    lines = [f"🏆 <b>{html.escape(snap.name)}</b> · {now.strftime('%d.%m %H:%M')} МСК", ""]
    lines.append("<b>Туры</b>")
    lines.extend(_rounds_block(snap, now))
    lines.append(_season_line(snap))

    lines.append("")
    if snap.debts:
        lines.append(f"<b>🧾 Долги ({len(snap.debts)})</b>")
        lines.extend(_debt_line(d) for d in snap.debts[:MAX_DEBT_LINES])
        if len(snap.debts) > MAX_DEBT_LINES:
            lines.append(f"…и ещё {len(snap.debts) - MAX_DEBT_LINES} — полный список в «Долги дивизиона»")
    else:
        lines.append("🧾 Долгов нет")

    lines.append("")
    if snap.warned:
        lines.append(f"<b>⚠️ Варны ({len(snap.warned)})</b>")
        for u in snap.warned:
            icon = "🟥" if int(u["warn_count"]) >= max_warns - 1 else "🟧"
            lines.append(f"{icon} {_player(u.get('username'), u.get('team_name'))} — {u['warn_count']}/{max_warns}")
    else:
        lines.append("⚠️ Варнов нет")
    return "\n".join(lines)
