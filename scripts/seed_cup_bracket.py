"""Сид сетки кубка — общего или кубка дивизиона: пары владельца → `cup_series`.

Пары приходят живым текстом («Ман Сити», «МЮ», «Реад МАдрид»), а линия, `users.team_name`
и резолвер OCR работают с каноном из `config.DIVISION_CLUBS`. Значит прежде чем
что-то записать, сетка проверяется целиком:

  * каждое имя нормализуется резолвером (и не схлопнулось в другое);
  * клуб встречается ровно один раз — иначе в сетке он проходит и выбывает сразу;
  * для 1/64 оба клуба каждой пары — из Д4 или Д5, и всего ровно 32 разных клуба;
  * в кубке дивизиона — только клубы этого дивизиона и ровно столько серий,
    сколько их на стадии (1/8 — 8, 1/4 — 4, 1/2 — 2, финал — 1);
  * стадия уже заведена — повторный сид не должен удваивать сетку.

Откуда пары: список `PAIRS` / `DIVISION_PAIRS` в этом файле, `--pairs-file`
(одна пара на строку: «Клуб — Клуб») или `--from-winners` — победители
предыдущей стадии того же кубка в порядке серий, 1-я со 2-й, 3-я с 4-й и т. д.

По умолчанию скрипт ничего не пишет (dry-run) и печатает каноническую сетку.
Запись — только с `--apply`.

Использование:

    python scripts/seed_cup_bracket.py --stage 1/64
    python scripts/seed_cup_bracket.py --stage 1/64 --apply
    python scripts/seed_cup_bracket.py --stage 1/64 --apply --db C:/path/league.db
    python scripts/seed_cup_bracket.py --division 3 --pairs-file d3.txt --apply
    python scripts/seed_cup_bracket.py --division 3 --stage 1/4 --from-winners --apply

`--division` — id дивизиона (его показывает панель /cup) или код `DIV_3`.
"""

import argparse
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
import database  # noqa: E402
from club_registry import resolve_team_name  # noqa: E402
from constants import CUP_STAGES  # noqa: E402

# Пары 1/64 — от владельца, в том порядке, в котором они присланы: позиция в
# списке есть номер серии. Правки вносит владелец, авто-жеребьёвки нет.
PAIRS: dict[str, list[tuple[str, str]]] = {
    "1/64": [
        ("Интер Милан", "Челси"),
        ("Байер", "Ман Сити"),
        ("Реад МАдрид", "Ювентус"),
        ("Милан", "Лейпциг"),
        ("Аталанта", "Атлетико МАдрид"),
        ("Бетис", "Ливерпуль"),
        ("Астон Вилла", "Брайтон"),
        ("Галатасарай", "Рома"),
        ("Аль-Наср", "Барселона"),
        ("Эвертон", "Интер Майми"),
        ("Тоттенхэм", "Аль-Хиляль"),
        ("Боруссия Дортмунд", "ПСЖ"),
        ("Арсенал", "Атлетик Бильбао"),
        ("МЮ", "Бавария"),
        ("Ньюкасл", "Наполи"),
        ("Бешикташ", "Байя"),
    ],
}

# Пары кубков дивизионов: код дивизиона → стадия → пары. Обычно пары приходят
# файлом (`--pairs-file`), а поздние стадии — из победителей (`--from-winners`);
# список здесь — для тех, кто хочет держать жеребьёвку в репозитории.
DIVISION_PAIRS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "DIV_1": {},
    "DIV_2": {},
    "DIV_3": {},
    "DIV_4": {},
    "DIV_5": {},
}

# Кубок дивизиона: 16 клубов, 1/8 → 1/4 → 1/2 → финал; стадия → число серий.
DIVISION_CUP_SERIES: dict[str, int] = {"1/8": 8, "1/4": 4, "1/2": 2, "final": 1}
DIVISION_CUP_STAGES: tuple[str, ...] = tuple(DIVISION_CUP_SERIES)

# «Клуб — Клуб», «Клуб - Клуб», «Клуб; Клуб», «Клуб vs Клуб». Дефис без пробелов
# вокруг не разделитель: «Аль-Наср» — одно имя.
_PAIR_SPLIT = re.compile(r"\s+[—–-]\s+|\s*;\s*|\s+vs\.?\s+", re.IGNORECASE)

# Стадия, где играют только низшие дивизионы (регланамент кубка).
_STAGE_DIVISION_RESTRICTION: dict[str, set[str]] = {
    "1/64": {"DIV_4", "DIV_5"},
}


def _canonical_divisions() -> dict[str, str]:
    """клуб → код дивизиона из `config.DIVISION_CLUBS` (без обращения к БД)."""
    mapping: dict[str, str] = {}
    for code, clubs in config.DIVISION_CLUBS.items():
        for club in clubs:
            mapping[club.strip().lower()] = code
    return mapping


def parse_pairs_file(path: str) -> list[tuple[str, str]]:
    """Пары из текстового файла: одна на строку, пустые и `#`-строки пропускаются."""
    pairs: list[tuple[str, str]] = []
    problems: list[str] = []
    with open(path, encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            # «1. Клуб — Клуб» — номер серии в начале строки не мешает.
            text = re.sub(r"^\d+[.)]\s*", "", text)
            parts = [p.strip() for p in _PAIR_SPLIT.split(text) if p.strip()]
            if len(parts) != 2:
                problems.append(f"строка {lineno}: «{text}» — нужна пара «Клуб — Клуб»")
                continue
            pairs.append((parts[0], parts[1]))
    if problems:
        raise ValueError("Файл пар не разобран:\n  - " + "\n  - ".join(problems))
    if not pairs:
        raise ValueError(f"В файле {path} нет ни одной пары.")
    return pairs


def resolve_division(value: str) -> dict:
    """Дивизион по id или коду (`3`, `DIV_3`); ValueError, если такого нет."""
    text = str(value).strip()
    division = None
    if text.isdigit():
        division = database.get_division(int(text))
    if division is None:
        division = database.get_division_by_code(text)
    if division is None:
        raise ValueError(f"Дивизион «{value}» не найден — передай id из панели /cup или код DIV_N.")
    return division


def winners_pairs(stage: str, season_id: int | None, division_id: int | None) -> list[tuple[str, str]]:
    """Пары стадии из победителей предыдущей: серия 1 с серией 2, 3 с 4, …"""
    order = DIVISION_CUP_STAGES if division_id is not None else CUP_STAGES
    if stage not in order or order.index(stage) == 0:
        raise ValueError(f"У стадии {stage} нет предыдущей — победителей брать неоткуда.")
    prev = order[order.index(stage) - 1]
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


def validate_pairs(
    stage: str,
    pairs: list[tuple[str, str]] | None = None,
    division_code: str | None = None,
) -> list[tuple[str, str]]:
    """Каноническая сетка или ValueError со списком всех проблем сразу.

    Все находки одним сообщением, а не по одной за запуск: проверять присланные
    пары «по одной» — значит гонять владельца по кругу, пока список не сойдётся.

    `pairs` — готовый список (файл, победители); без него берётся `PAIRS` или,
    для кубка дивизиона `division_code`, `DIVISION_PAIRS`.
    """
    if division_code is not None and stage not in DIVISION_CUP_SERIES:
        raise ValueError(
            f"В кубке дивизиона нет стадии {stage}: он идёт {' → '.join(DIVISION_CUP_STAGES)}."
        )
    raw_pairs = pairs
    if raw_pairs is None:
        source = PAIRS if division_code is None else DIVISION_PAIRS.get(division_code, {})
        raw_pairs = source.get(stage)
    if not raw_pairs:
        where = "PAIRS" if division_code is None else f"DIVISION_PAIRS[{division_code!r}]"
        raise ValueError(
            f"Для стадии {stage} пары не заданы в scripts/seed_cup_bracket.py ({where}) "
            f"— передай --pairs-file или --from-winners."
        )

    divisions = _canonical_divisions()
    if division_code is not None:
        allowed = {division_code}
    else:
        allowed = _STAGE_DIVISION_RESTRICTION.get(stage)
    problems: list[str] = []
    seen: dict[str, int] = {}
    canonical: list[tuple[str, str]] = []

    for index, pair in enumerate(raw_pairs, start=1):
        clubs: list[str] = []
        for raw in pair:
            club = (resolve_team_name(raw) or "").strip()
            if not club:
                problems.append(f"пара {index}: «{raw}» не резолвится ни в один клуб лиги")
                continue
            if club.lower() not in divisions:
                problems.append(f"пара {index}: «{club}» нет в config.DIVISION_CLUBS")
            elif allowed and divisions[club.lower()] not in allowed:
                problems.append(
                    f"пара {index}: «{club}» играет в {divisions[club.lower()]}, "
                    f"а на {stage} допущены {'/'.join(sorted(allowed))}"
                )
            if club.lower() in seen:
                problems.append(
                    f"«{club}» встречается дважды: в парах {seen[club.lower()]} и {index}"
                )
            else:
                seen[club.lower()] = index
            clubs.append(club)
        if len(clubs) == 2:
            canonical.append((clubs[0], clubs[1]))

    if canonical and allowed and stage == "1/64" and len(seen) != 32:
        problems.append(f"на 1/64 должно играть 32 клуба, а в списке их {len(seen)}")
    if division_code is not None and len(raw_pairs) != DIVISION_CUP_SERIES[stage]:
        problems.append(
            f"на {stage} кубка дивизиона {DIVISION_CUP_SERIES[stage]} сер., а в списке пар {len(raw_pairs)}"
        )

    if problems:
        raise ValueError("Сетка не прошла проверку:\n  - " + "\n  - ".join(problems))
    return canonical


def main() -> int:
    parser = argparse.ArgumentParser(description="Сид сетки кубка (общего или дивизиона)")
    parser.add_argument("--db", default=None, help="путь к league.db")
    parser.add_argument("--division", default=None,
                        help="кубок дивизиона: id дивизиона или код DIV_N; без него — общий кубок")
    parser.add_argument("--stage", default=None, choices=list(CUP_STAGES),
                        help="стадия; по умолчанию 1/64 для общего кубка и 1/8 для кубка дивизиона")
    parser.add_argument("--season", type=int, default=None, help="id сезона; по умолчанию активный")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pairs-file", default=None, help="файл с парами: одна «Клуб — Клуб» на строку")
    source.add_argument("--from-winners", action="store_true",
                        help="пары из победителей предыдущей стадии: 1-я серия со 2-й, 3-я с 4-й, …")
    parser.add_argument("--apply", action="store_true", help="записать сетку (по умолчанию только проверка)")
    args = parser.parse_args()

    if args.db:
        database.DB_PATH = args.db

    division_id: int | None = None
    division_code: str | None = None
    if args.division is not None:
        try:
            division = resolve_division(args.division)
        except ValueError as e:
            print(f"✗ {e}")
            return 1
        division_id = int(division["id"])
        division_code = (division.get("code") or "").strip().upper() or None
        if division_code not in config.DIVISION_CLUBS:
            print(f"✗ У дивизиона #{division_id} код {division.get('code')!r} — "
                  f"его ростера нет в config.DIVISION_CLUBS.")
            return 1
    stage = args.stage or ("1/64" if division_id is None else DIVISION_CUP_STAGES[0])

    season_id = args.season
    if season_id is None:
        act = database.get_active_season()
        season_id = act["id"] if act else 1

    try:
        if args.from_winners:
            pairs = winners_pairs(stage, season_id, division_id)
        elif args.pairs_file:
            pairs = parse_pairs_file(args.pairs_file)
        else:
            pairs = None
        canonical = validate_pairs(stage, pairs, division_code)
    except (OSError, ValueError) as e:
        print(f"✗ {e}")
        return 1

    print(f"{database.cup_scope_label(division_id)}, стадия {stage}, серий: {len(canonical)}, "
          f"клубов: {len({c for p in canonical for c in p})}")
    for num, (t1, t2) in enumerate(canonical, start=1):
        print(f"  {num:>2}. {t1} — {t2}")
    print(f"Сезон: {season_id}, база: {database.DB_PATH}")

    existing = database.get_cup_bracket(stage, season_id=season_id, division_id=division_id)
    if existing:
        print(f"Сетка {stage} уже заведена ({len(existing)} сер.) — сид останавливается, "
              f"серии правятся поштучно.")
        return 0

    if not args.apply:
        print("DRY-RUN: ничего не записано. Повтори с --apply.")
        return 0

    try:
        ids = database.create_cup_series(stage, canonical, season_id=season_id, division_id=division_id)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    print(f"✓ Заведено серий: {len(ids)} (ids {ids[0]}…{ids[-1]}). "
          f"Дальше: панель /cup → «завести игры» → «открыть ставки».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
