"""Сид сетки общего кубка: пары владельца → `cup_series`.

Пары приходят живым текстом («Ман Сити», «МЮ», «Реад МАдрид»), а линия, `users.team_name`
и резолвер OCR работают с каноном из `config.DIVISION_CLUBS`. Значит прежде чем
что-то записать, сетка проверяется целиком:

  * каждое имя нормализуется резолвером (и не схлопнулось в другое);
  * клуб встречается ровно один раз — иначе в сетке он проходит и выбывает сразу;
  * для 1/64 оба клуба каждой пары — из Д4 или Д5, и всего ровно 32 разных клуба;
  * стадия уже заведена — повторный сид не должен удваивать сетку.

По умолчанию скрипт ничего не пишет (dry-run) и печатает каноническую сетку.
Запись — только с `--apply`.

Использование:

    python scripts/seed_cup_bracket.py --stage 1/64
    python scripts/seed_cup_bracket.py --stage 1/64 --apply
    python scripts/seed_cup_bracket.py --stage 1/64 --apply --db C:/path/league.db
"""

import argparse
import os
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


def validate_pairs(stage: str) -> list[tuple[str, str]]:
    """Каноническая сетка или ValueError со списком всех проблем сразу.

    Все находки одним сообщением, а не по одной за запуск: проверять присланные
    пары «по одной» — значит гонять владельца по кругу, пока список не сойдётся.
    """
    raw_pairs = PAIRS.get(stage)
    if not raw_pairs:
        raise ValueError(
            f"Для стадии {stage} пары не заданы в scripts/seed_cup_bracket.py (PAIRS)."
        )

    divisions = _canonical_divisions()
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

    if problems:
        raise ValueError("Сетка не прошла проверку:\n  - " + "\n  - ".join(problems))
    return canonical


def main() -> int:
    parser = argparse.ArgumentParser(description="Сид сетки общего кубка")
    parser.add_argument("--db", default=None, help="путь к league.db")
    parser.add_argument("--stage", default="1/64", choices=list(CUP_STAGES))
    parser.add_argument("--season", type=int, default=None, help="id сезона; по умолчанию активный")
    parser.add_argument("--apply", action="store_true", help="записать сетку (по умолчанию только проверка)")
    args = parser.parse_args()

    if args.db:
        database.DB_PATH = args.db

    try:
        canonical = validate_pairs(args.stage)
    except ValueError as e:
        print(f"✗ {e}")
        return 1

    print(f"Стадия {args.stage}, серий: {len(canonical)}, клубов: {len({c for p in canonical for c in p})}")
    for num, (t1, t2) in enumerate(canonical, start=1):
        print(f"  {num:>2}. {t1} — {t2}")

    season_id = args.season
    if season_id is None:
        act = database.get_active_season()
        season_id = act["id"] if act else 1
    print(f"Сезон: {season_id}, база: {database.DB_PATH}")

    existing = database.get_cup_bracket(args.stage, season_id=season_id)
    if existing:
        print(f"Сетка {args.stage} уже заведена ({len(existing)} сер.) — сид останавливается, "
              f"серии правятся поштучно.")
        return 0

    if not args.apply:
        print("DRY-RUN: ничего не записано. Повтори с --apply.")
        return 0

    ids = database.create_cup_series(args.stage, canonical, season_id=season_id)
    print(f"✓ Заведено серий: {len(ids)} (ids {ids[0]}…{ids[-1]}). "
          f"Дальше: панель /cup → «завести игры» → «открыть ставки».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
