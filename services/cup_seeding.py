"""
services/cup_seeding.py

Сетка кубка дивизиона из бота: 1/8 — по парам, присланным текстом, поздние
стадии — из победителей предыдущей (серия 1 со 2-й, 3-я с 4-й, …).

Это та же сетка, что заводит `scripts/seed_cup_bracket.py --division N`, но для
админа дивизиона, у которого нет доступа к серверу. Общий кубок сюда не входит:
у него ограничения по дивизионам на 1/64 и стадия вступления 1/32 — его сидит
скрипт.

Авто-жеребьёвки нет: пары присылает владелец, порядок строк = номера серий.
Проверка собирает все проблемы сразу, а запись — `database.create_cup_series`,
который ещё раз сверяет состав дивизиона и не даёт завести стадию дважды.
"""

from __future__ import annotations

import re

import database

# Кубок дивизиона: 16 клубов, 1/8 → 1/4 → 1/2 → финал; стадия → число серий.
DIVISION_CUP_SERIES: dict[str, int] = {"1/8": 8, "1/4": 4, "1/2": 2, "final": 1}
DIVISION_CUP_STAGES: tuple[str, ...] = tuple(DIVISION_CUP_SERIES)

# «Клуб — Клуб», «Клуб - Клуб», «Клуб; Клуб», «Клуб vs Клуб». Дефис без пробелов
# вокруг не разделитель: «Аль-Наср» — одно имя.
PAIR_SPLIT = re.compile(r"\s+[—–-]\s+|\s*;\s*|\s+vs\.?\s+", re.IGNORECASE)

MODE_PAIRS = "pairs"
MODE_WINNERS = "winners"


def parse_pairs_text(text: str) -> list[tuple[str, str]]:
    """Пары из текста: одна на строку, «1. Клуб — Клуб»; ValueError со всеми ошибками."""
    pairs: list[tuple[str, str]] = []
    problems: list[str] = []
    for lineno, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^\d+[.)]\s*", "", line)
        parts = [p.strip() for p in PAIR_SPLIT.split(line) if p.strip()]
        if len(parts) != 2:
            problems.append(f"строка {lineno}: «{line}» — нужна пара «Клуб — Клуб»")
            continue
        pairs.append((parts[0], parts[1]))
    if problems:
        raise ValueError("\n".join(problems))
    if not pairs:
        raise ValueError("Ни одной пары — пришлите по паре «Клуб — Клуб» на строку.")
    return pairs


def winners_pairs(stage: str, division_id: int, season_id: int | None = None) -> list[tuple[str, str]]:
    """Пары стадии из победителей предыдущей стадии того же кубка дивизиона."""
    if stage not in DIVISION_CUP_STAGES or DIVISION_CUP_STAGES.index(stage) == 0:
        raise ValueError(f"У стадии {stage} нет предыдущей — победителей брать неоткуда.")
    prev = DIVISION_CUP_STAGES[DIVISION_CUP_STAGES.index(stage) - 1]
    bracket = database.get_cup_bracket(prev, season_id=season_id, division_id=division_id)
    if not bracket:
        raise ValueError(f"Сетка {prev} не заведена — победителей брать неоткуда.")
    pending = [str(s["series_num"]) for s in bracket if not s.get("winner_name")]
    if pending:
        raise ValueError(f"На {prev} ещё не решены серии: {', '.join(pending)}.")
    if len(bracket) % 2:
        raise ValueError(f"На {prev} нечётное число серий ({len(bracket)}) — пары не составить.")
    winners = [s["winner_name"] for s in bracket]
    return [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]


def validate_pairs(stage: str, pairs: list[tuple[str, str]], division_id: int,
                   season_id: int | None = None) -> list[tuple[str, str]]:
    """Каноническая сетка или ValueError со списком всех проблем сразу.

    Состав — `database.get_division_teams` (ростер сезона ∪ зарегистрированные
    тренеры ∪ клубы матчей), тот же, по которому проверяет `create_cup_series`.
    """
    if stage not in DIVISION_CUP_SERIES:
        raise ValueError(f"В кубке дивизиона нет стадии {stage}: он идёт {' → '.join(DIVISION_CUP_STAGES)}.")
    roster = {t.lower(): t for t in database.get_division_teams(division_id, season_id=season_id)}
    if not roster:
        raise ValueError("У дивизиона нет клубов — кубок не из кого собрать.")

    problems: list[str] = []
    seen: dict[str, int] = {}
    canonical: list[tuple[str, str]] = []
    for index, (raw1, raw2) in enumerate(pairs, start=1):
        clubs: list[str] = []
        for raw in (raw1, raw2):
            club = (database.resolve_team_name(raw) or str(raw)).strip()
            key = club.lower()
            if key not in roster:
                problems.append(f"пара {index}: «{raw}» — нет такого клуба в дивизионе")
                continue
            club = roster[key]
            if seen.get(key) == index:
                problems.append(f"пара {index}: «{club}» играет сам с собой")
            elif key in seen:
                problems.append(f"«{club}» встречается дважды: в парах {seen[key]} и {index}")
            else:
                seen[key] = index
            clubs.append(club)
        if len(clubs) == 2 and clubs[0].lower() != clubs[1].lower():
            canonical.append((clubs[0], clubs[1]))

    expected = DIVISION_CUP_SERIES[stage]
    if len(pairs) != expected:
        problems.append(f"на {stage} нужно {expected} пар, а прислано {len(pairs)}")
    if problems:
        raise ValueError("\n".join(problems))
    return canonical


def next_seed(division_id: int, season_id: int | None = None) -> tuple[str, str] | None:
    """(стадия, способ), которую сейчас можно завести, или None.

    Первая по порядку стадия без серий: 1/8 — по парам, дальше — из
    победителей, и только когда предыдущая стадия решена целиком.
    """
    for index, stage in enumerate(DIVISION_CUP_STAGES):
        bracket = database.get_cup_bracket(stage, season_id=season_id, division_id=division_id)
        if bracket:
            continue
        if index == 0:
            return stage, MODE_PAIRS
        prev = DIVISION_CUP_STAGES[index - 1]
        prev_bracket = database.get_cup_bracket(prev, season_id=season_id, division_id=division_id)
        if prev_bracket and all(s.get("winner_name") for s in prev_bracket):
            return stage, MODE_WINNERS
        return None
    return None


def seed(stage: str, pairs: list[tuple[str, str]], division_id: int,
         season_id: int | None = None) -> list[int]:
    """Записать сетку; ValueError, если она не прошла проверку или уже заведена."""
    canonical = validate_pairs(stage, pairs, division_id, season_id=season_id)
    return database.create_cup_series(stage, canonical, season_id=season_id, division_id=division_id)
