"""
services/irl_betting.py

IRL-ставки — чистая логика: выбор топ-матча дня, проверка ставки, исход расчёта.

Без сети и без БД: на вход идут уже разобранные данные, на выходе — решение.
Деньги и статусы двигает `database.py` (`place_irl_bet`, `settle_irl_match`),
данные приносит адаптер провайдера — здесь только правила, поэтому всё
проверяется напрямую, без моков.

Имена команд сравниваются как есть от провайдера и НЕ проходят через
`resolve_team_name`: реальные «Реал» и «Интер» — другие сущности, чем клубы лиги.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

import config

OUTCOMES = ("home", "draw", "away")

# Матч сыгран не раньше, чем через столько минут после начала: 90 + добавленное
# + перерыв. Статус FT раньше этого срока — сбой или ранний флаг провайдера.
MIN_MINUTES_BEFORE_FT = 100
# Матч, не завершившийся через столько часов после начала, уходит админам на ручной расчёт.
MANUAL_SETTLE_AFTER_HOURS = 6

# Статусы API-Sports (fixture.status.short).
FINISHED_MAIN_TIME = ("FT",)
FINISHED_EXTRA = ("AET", "PEN")        # fulltime-счёт — это 90 минут
VOID_STATUSES = ("PST", "CANC", "ABD", "AWD", "WO")

# Места клубной таблицы, дающие бонус к баллам матча.
TABLE_BONUS_TOP_N = 6

MIN_ODD = 1.01


# ─── Названия ────────────────────────────────────────────────────────────────

def normalize_name(name: object) -> str:
    """Регистр, пробелы и знаки не важны: «Paris Saint-Germain» == «paris saint germain»."""
    text = re.sub(r"[^\w]+", " ", str(name or "").casefold().replace("ё", "е"), flags=re.UNICODE)
    return " ".join(text.split())


# ─── Выбор матча ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """Матч-кандидат на день: то, что нужно для отбора, без привязки к провайдеру."""
    fixture_id: int | str
    league_id: int
    home: str
    away: str
    kickoff: datetime                      # naive, МСК
    odd_home: Optional[float] = None
    odd_draw: Optional[float] = None
    odd_away: Optional[float] = None
    league_name: str = ""
    # Места в таблице заполняются только для клубных лиг; у сборных их нет.
    home_rank: Optional[int] = None
    away_rank: Optional[int] = None


def valid_odds(*odds: object) -> bool:
    """Все цены — конечные числа выше 1.00 (иначе матч без коэффициентов букмекера)."""
    for odd in odds:
        try:
            value = float(odd)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value < MIN_ODD:
            return False
    return True


def has_complete_odds(c: Candidate) -> bool:
    return valid_odds(c.odd_home, c.odd_draw, c.odd_away)


def top_team_count(c: Candidate, top_teams: Iterable[str]) -> int:
    """Сколько из двух команд входит в список топ-команд (0, 1 или 2)."""
    top = {normalize_name(t) for t in top_teams if normalize_name(t)}
    return sum(1 for team in (c.home, c.away) if normalize_name(team) in top)


def table_bonus(c: Candidate) -> float:
    """Бонус за места клубов в таблице, строго меньше 1 — на счёт «топ-команд» не влияет.

    Первое место даёт 1.0, место `TABLE_BONUS_TOP_N` — 1/N, ниже — ноль;
    берётся среднее двух команд, и неизвестное место считается нулём.
    """
    total = 0.0
    for rank in (c.home_rank, c.away_rank):
        if isinstance(rank, int) and 1 <= rank <= TABLE_BONUS_TOP_N:
            total += (TABLE_BONUS_TOP_N + 1 - rank) / TABLE_BONUS_TOP_N
    return total / 2 * 0.99


def odds_gap(c: Candidate) -> float:
    """Разница кэфов на 1 и 2: чем меньше, тем равнее матч."""
    return abs(float(c.odd_home) - float(c.odd_away))  # type: ignore[arg-type]


def _rank_key(c: Candidate, top_teams: Iterable[str]) -> tuple:
    # Детерминированный порядок: баллы ↓, равенство матча ↑, время ↑, id ↑.
    return (
        -(top_team_count(c, top_teams) + table_bonus(c)),
        odds_gap(c),
        c.kickoff,
        str(c.fixture_id),
    )


def pick_top_matches(
    candidates: Iterable[Candidate],
    now: datetime,
    priority: Optional[list[int]] = None,
    top_teams: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> list[Candidate]:
    """Топ-матч(и) дня.

    Берётся первый по приоритету турнир, где есть подходящие матчи (не начались,
    полный набор коэффициентов выбранного букмекера). Внутри него — матч с
    наибольшим числом топ-команд; матчи с тем же числом идут вторым и далее, но не
    больше `limit`. Если топ-команд нет вовсе, берётся один лучший матч, а не пара
    случайных. Пустой список — штатный исход («сегодня IRL-ставок нет»), не ошибка.
    """
    priority = list(config.IRL_COMPETITION_PRIORITY if priority is None else priority)
    top_teams = list(config.IRL_TOP_TEAMS if top_teams is None else top_teams)
    limit = max(1, int(config.IRL_MAX_MATCHES_PER_DAY if limit is None else limit))

    eligible = [c for c in candidates if has_complete_odds(c) and c.kickoff > now]
    for league_id in priority:
        in_league = [c for c in eligible if c.league_id == league_id]
        if not in_league:
            continue
        ranked = sorted(in_league, key=lambda c: _rank_key(c, top_teams))
        best = top_team_count(ranked[0], top_teams)
        if best == 0:
            return ranked[:1]
        return [c for c in ranked if top_team_count(c, top_teams) == best][:limit]
    return []


# ─── Ставка ──────────────────────────────────────────────────────────────────

def normalize_outcome(value: object) -> Optional[str]:
    """`home|draw|away` (а также 1/X/2 и П1/Х/П2) → канонический ключ или None."""
    key = str(value or "").strip().casefold()
    aliases = {
        "home": "home", "1": "home", "п1": "home", "h": "home",
        "draw": "draw", "x": "draw", "х": "draw", "ничья": "draw", "d": "draw",
        "away": "away", "2": "away", "п2": "away", "a": "away",
    }
    return aliases.get(key)


def parse_stake(amount: object, max_bet: Optional[int] = None) -> tuple[bool, int | dict]:
    """Сумма ставки → `(True, int)` или `(False, {"error", "message"})`."""
    max_bet = int(config.IRL_MAX_BET if max_bet is None else max_bet)
    try:
        if isinstance(amount, bool) or (isinstance(amount, float) and not amount.is_integer()):
            raise ValueError
        value = int(amount)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, {"error": "INVALID_AMOUNT", "message": "Сумма ставки должна быть целым числом."}
    if value <= 0:
        return False, {"error": "INVALID_AMOUNT", "message": "Сумма ставки должна быть больше нуля."}
    if value > max_bet:
        return False, {"error": "MAX_BET_EXCEEDED", "max_bet": max_bet,
                       "message": f"Максимальная ставка на реальный матч — {max_bet:,} 🪙."}
    return True, value


def potential_win(amount: int, odd: float, max_payout: Optional[int] = None) -> int:
    """Выигрыш с учётом потолка выплаты (полная выплата, ставка включена)."""
    win = int(round(amount * odd))
    return min(win, int(max_payout)) if max_payout else win


def odd_for(match: dict, outcome: str) -> Optional[float]:
    """Цена исхода из строки `irl_matches` (None, если её нет или она негодная)."""
    value = match.get({"home": "odd_home", "draw": "odd_draw", "away": "odd_away"}[outcome])
    return round(float(value), 2) if valid_odds(value) else None


def betting_open(status: str, kickoff: object, now: datetime) -> bool:
    """Ставка принимается, пока матч опубликован и ещё не начался."""
    from time_utils import parse_msk
    start = parse_msk(kickoff)
    return status == "open" and start is not None and now < start


# ─── Расчёт ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SettleDecision:
    action: str                            # "result" | "void" | "wait"
    result: Optional[str] = None           # home | draw | away — при action == "result"
    reason: str = ""


def result_from_score(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


def settle_decision(
    status_short: object,
    home_goals: Optional[int],
    away_goals: Optional[int],
    kickoff: datetime,
    now: datetime,
) -> SettleDecision:
    """Что делать с матчем по ответу провайдера.

    `home_goals`/`away_goals` — счёт основного времени (`score.fulltime` у
    API-Sports): после допвремени и пенальти он остаётся счётом 90 минут. Матч
    ждёт, пока провайдер не отдаст финал; `FT` раньше `MIN_MINUTES_BEFORE_FT`
    минут от начала — тоже ждёт (ранний или сбойный флаг).
    """
    status = str(status_short or "").strip().upper()
    if status in VOID_STATUSES:
        return SettleDecision("void", reason=f"Матч не состоялся ({status})")
    if status not in FINISHED_MAIN_TIME + FINISHED_EXTRA:
        return SettleDecision("wait", reason=f"Матч не завершён ({status or 'нет статуса'})")
    if status in FINISHED_MAIN_TIME and now - kickoff < timedelta(minutes=MIN_MINUTES_BEFORE_FT):
        return SettleDecision("wait", reason="Слишком рано для финального счёта")
    if not isinstance(home_goals, int) or not isinstance(away_goals, int) \
            or isinstance(home_goals, bool) or isinstance(away_goals, bool) \
            or home_goals < 0 or away_goals < 0:
        return SettleDecision("wait", reason="Провайдер не вернул счёт основного времени")
    return SettleDecision("result", result=result_from_score(home_goals, away_goals))


def needs_manual_settlement(kickoff: datetime, now: datetime) -> bool:
    """Матч давно идёт к концу, а расчёта нет — пора звать админа."""
    return now - kickoff >= timedelta(hours=MANUAL_SETTLE_AFTER_HOURS)


# ─── Батчи одновременных матчей ──────────────────────────────────────────────

@dataclass(frozen=True)
class MatchBatch:
    """Группа матчей, проходящих одновременно (в один тайм-слот)."""
    slot_time: str                        # e.g. "2026-10-10 19:30"
    time_label: str                       # e.g. "19:30 МСК"
    kickoff: datetime                     # naive MSK
    matches: list[dict]                   # список матчей
    fixture_ids: list[str]                # ID матчей у провайдера для батч-запроса

    @property
    def is_simultaneous(self) -> bool:
        """True, если в батче 2 или более параллельных матчей."""
        return len(self.matches) > 1

    @property
    def count(self) -> int:
        return len(self.matches)


def group_matches_into_batches(
    matches: Iterable[dict],
    tolerance_minutes: int = 0,
) -> list[MatchBatch]:
    """Группирует матчи по времени начала в одновременные батчи.

    Матчи сортируются по kickoff_at. При `tolerance_minutes == 0` матчи объединяются
    по точному времени начала (например, 19:30 МСК).
    """
    from time_utils import parse_msk

    parsed: list[tuple[datetime, dict]] = []
    for m in matches:
        raw_ko = m.get("kickoff_at")
        ko = raw_ko if isinstance(raw_ko, datetime) else parse_msk(raw_ko)
        if ko is not None:
            parsed.append((ko, m))

    parsed.sort(key=lambda item: (item[0], item[1].get("id", 0)))
    if not parsed:
        return []

    batches: list[MatchBatch] = []
    cur_kickoff: Optional[datetime] = None
    cur_matches: list[dict] = []

    for ko, m in parsed:
        if cur_kickoff is None:
            cur_kickoff = ko
            cur_matches = [m]
        elif tolerance_minutes > 0 and (ko - cur_kickoff).total_seconds() <= tolerance_minutes * 60:
            cur_matches.append(m)
        elif tolerance_minutes == 0 and ko == cur_kickoff:
            cur_matches.append(m)
        else:
            slot_str = cur_kickoff.strftime("%Y-%m-%d %H:%M")
            label_str = cur_kickoff.strftime("%H:%M МСК")
            fids = [str(x["provider_fixture_id"]) for x in cur_matches if x.get("provider_fixture_id")]
            batches.append(MatchBatch(
                slot_time=slot_str,
                time_label=label_str,
                kickoff=cur_kickoff,
                matches=cur_matches,
                fixture_ids=fids,
            ))
            cur_kickoff = ko
            cur_matches = [m]

    if cur_kickoff is not None and cur_matches:
        slot_str = cur_kickoff.strftime("%Y-%m-%d %H:%M")
        label_str = cur_kickoff.strftime("%H:%M МСК")
        fids = [str(x["provider_fixture_id"]) for x in cur_matches if x.get("provider_fixture_id")]
        batches.append(MatchBatch(
            slot_time=slot_str,
            time_label=label_str,
            kickoff=cur_kickoff,
            matches=cur_matches,
            fixture_ids=fids,
        ))

    return batches
