"""Сид сетки кубка — общего или кубка дивизиона: пары владельца → `cup_series`.

Пары приходят живым текстом («Ман Сити», «МЮ», «Реад МАдрид»), а линия, `users.team_name`
и резолвер OCR работают с каноном из `config.DIVISION_CLUBS`. Значит прежде чем
что-то записать, сетка проверяется целиком:

  * каждое имя нормализуется резолвером (и не схлопнулось в другое);
  * клуб встречается ровно один раз — иначе в сетке он проходит и выбывает сразу;
  * для 1/64 оба клуба каждой пары — из Д4 или Д5, и всего ровно 32 разных клуба;
  * для 1/32 в сетке ровно победители 1/64 и все клубы Д1–Д3: выбывший клуб
    или клуб из нерешённой серии в неё не попадает;
  * `WinnerOf("Клуб", "Клуб")` вместо имени — победитель этой серии предыдущей
    стадии, берётся из базы в момент сида (серия обязана быть решена);
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
    python scripts/seed_cup_bracket.py --stage 1/32 --apply
    python scripts/seed_cup_bracket.py --division 3 --pairs-file d3.txt --apply
    python scripts/seed_cup_bracket.py --division 3 --stage 1/4 --from-winners --apply

`--division` — id дивизиона (его показывает панель /cup) или код `DIV_3`.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
import database  # noqa: E402
from club_registry import resolve_team_name  # noqa: E402
from constants import CUP_STAGES  # noqa: E402


@dataclass(frozen=True)
class WinnerOf:
    """Место в паре за победителем серии `team1 — team2` предыдущей стадии.

    Жеребьёвку присылают, пока серия ещё идёт («Милан или Лейпциг»), а сетка
    хранит имя клуба, не ссылку: сидить заглушку — значит завести игры, рынки и
    ставки на клуб, которого нет. Имя подставляется при сиде из решённой серии.
    """

    team1: str
    team2: str

    def __str__(self) -> str:
        return f"победитель {self.team1} — {self.team2}"


# Пары — от владельца, в том порядке, в котором они присланы: позиция в
# списке есть номер серии. Правки вносит владелец, авто-жеребьёвки нет.
# 1/64 — как прислали; 1/32 — уже в каноне («Фулхєм», «Нєшвилл», «Буринам»
# резолвер не узнаёт).
PAIRS: dict[str, list[tuple]] = {
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
    "1/32": [
        ("Аль-Ахли", "Болонья"),
        ("Лос Анджелес", "Вест Хэм"),
        ("Валенсия", "Монако"),
        ("Фенербахче", "Айнтрахт"),
        ("Атлетико Мадрид", "Лидс"),
        ("Арсенал", "Трабзонспор"),
        ("Ювентус", "Бетис"),
        ("Бернли", "ПСВ"),
        ("Будё Глимт", "Вулверхэмптон"),
        ("Аль-Кадисия", "Брайтон"),
        ("Барселона", "Ривер Плейт"),
        ("Порту", "Реал Сосьедад"),
        ("Аль-Хиляль", "Интер Милан"),
        ("Штутгарт", "Лилль"),
        ("Кристал Пэлас", "Фулхэм"),
        ("Париж", "Кельн"),
        ("Бенфика", "Галатасарай"),
        ("Бурирам", "Ницца"),
        ("Вильярреал", "Лион"),
        ("Ноттингем Форест", "Манчестер Сити"),
        ("Бешикташ", "Ланс"),
        ("Торино", "Наполи"),
        ("ПСЖ", "Хоффенхайм"),
        ("Майнц", "Фиорентина"),
        ("Брентфорд", "Сандерленд"),
        ("Аль-Иттихад", "Сельта"),
        ("Комо", "Нэшвилл"),
        ("Спортинг", "Аякс"),
        ("Ренн", "Лацио"),
        ("Бавария", "Эвертон"),
        ("Вольфсбург", "Марсель"),
        ("Борнмут", WinnerOf("Милан", "Лейпциг")),
    ],
}

# Пары кубков дивизионов: код дивизиона → стадия → пары. 1/8 — жеребьёвка
# владельца в присланном порядке (позиция = номер серии), уже в каноне: «Реал»,
# «АТБ», «Буринам» резолвер не узнаёт. Поздние стадии — `--from-winners`.
DIVISION_PAIRS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "DIV_1": {
        "1/8": [
            ("Майнц", "Кельн"),
            ("Нэшвилл", "Лацио"),
            ("Вольфсбург", "Порту"),
            ("Лидс", "Марсель"),
            ("Айнтрахт", "Фиорентина"),
            ("Ницца", "Лилль"),
            ("Будё Глимт", "Вест Хэм"),
            ("Бернли", "Ренн"),
        ],
    },
    "DIV_2": {
        "1/8": [
            ("Монако", "Лос Анджелес"),
            ("Торино", "Ланс"),
            ("Ривер Плейт", "Вулверхэмптон"),
            ("Валенсия", "ПСВ"),
            ("Бенфика", "Спортинг"),
            ("Сельта", "Аль-Кадисия"),
            ("Бурирам", "Аякс"),
            ("Фулхэм", "Хоффенхайм"),
        ],
    },
    "DIV_3": {
        "1/8": [
            ("Штутгарт", "Брентфорд"),
            ("Сандерленд", "Вильярреал"),
            ("Аль-Иттихад", "Париж"),
            ("Болонья", "Трабзонспор"),
            ("Аль-Ахли", "Борнмут"),
            ("Фенербахче", "Лион"),
            ("Ноттингем Форест", "Кристал Пэлас"),
            ("Реал Сосьедад", "Комо"),
        ],
    },
    "DIV_4": {
        "1/8": [
            ("Аталанта", "Аль-Хиляль"),
            ("Брайтон", "Астон Вилла"),
            ("Лейпциг", "Бешикташ"),
            ("Эвертон", "Ньюкасл"),
            ("Милан", "Байер"),
            ("Боруссия Дортмунд", "Интер Милан"),
            ("Интер Майами", "Байя"),
            ("Бетис", "Атлетик Бильбао"),
        ],
    },
    "DIV_5": {
        "1/8": [
            ("Бавария", "Арсенал"),
            ("Наполи", "Манчестер Юнайтед"),
            ("ПСЖ", "Ювентус"),
            ("Манчестер Сити", "Аль-Наср"),
            ("Тоттенхэм", "Рома"),
            ("Ливерпуль", "Галатасарай"),
            ("Барселона", "Челси"),
            ("Атлетико Мадрид", "Реал Мадрид"),
        ],
    },
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

# Стадия общего кубка, на которой вступают клубы старших дивизионов: её сетка —
# это ровно победители предыдущей стадии плюс все клубы этих дивизионов.
_STAGE_ENTRY_DIVISIONS: dict[str, set[str]] = {
    "1/32": {"DIV_1", "DIV_2", "DIV_3"},
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


def _previous_bracket(stage: str, season_id: int | None) -> tuple[str | None, list[dict]]:
    """(предыдущая стадия, её серии) общего кубка; (None, []) у первой стадии."""
    if stage not in CUP_STAGES or CUP_STAGES.index(stage) == 0:
        return None, []
    prev = CUP_STAGES[CUP_STAGES.index(stage) - 1]
    return prev, database.get_cup_bracket(prev, season_id=season_id)


def _winner_of(ref: WinnerOf, prev: str | None, bracket: list[dict]) -> tuple[str | None, str | None]:
    """(победитель, None) или (None, почему его нет) для ссылки на серию."""
    if prev is None:
        return None, f"«{ref}»: у стадии нет предыдущей — брать победителя неоткуда"
    wanted = {(resolve_team_name(t) or t).strip().lower() for t in (ref.team1, ref.team2)}
    for series in bracket:
        if {series["team1_name"].lower(), series["team2_name"].lower()} == wanted:
            if not series.get("winner_name"):
                return None, (f"«{ref}»: серия {prev} #{series['series_num']} ещё не решена "
                              f"({series['team1_wins']}:{series['team2_wins']})")
            return series["winner_name"], None
    return None, f"«{ref}»: такой серии на {prev} нет"


def _check_entrants(stage: str, prev: str | None, bracket: list[dict],
                    names: dict[str, str]) -> list[str]:
    """Сетка стадии вступления = победители `prev` + все клубы вступающих дивизионов."""
    entry = _STAGE_ENTRY_DIVISIONS.get(stage)
    if not entry or prev is None:
        return []
    if not bracket:
        return [f"сетка {prev} не заведена — не из кого собрать {stage}"]
    pending = [str(s["series_num"]) for s in bracket if not s.get("winner_name")]
    if pending:
        return [f"на {prev} ещё не решены серии: {', '.join(pending)}"]

    expected = {s["winner_name"].lower(): s["winner_name"] for s in bracket}
    for code in sorted(entry):
        for club in config.DIVISION_CLUBS.get(code, []):
            expected[club.strip().lower()] = club.strip()
    losers = {
        (s["team2_name"] if s["winner_name"].lower() == s["team1_name"].lower() else s["team1_name"]).lower()
        for s in bracket
    }
    problems: list[str] = []
    for key, club in names.items():
        if key in losers:
            problems.append(f"«{club}» выбыл на {prev} — в {stage} ему не место")
        elif key not in expected:
            problems.append(f"«{club}» не прошёл {prev} и не из {'/'.join(sorted(entry))}")
    missing = [club for key, club in expected.items() if key not in names]
    if missing:
        problems.append(f"в сетке {stage} не хватает: {', '.join(missing)}")
    return problems


def validate_pairs(
    stage: str,
    pairs: list[tuple] | None = None,
    division_code: str | None = None,
    season_id: int | None = None,
) -> list[tuple[str, str]]:
    """Каноническая сетка или ValueError со списком всех проблем сразу.

    Все находки одним сообщением, а не по одной за запуск: проверять присланные
    пары «по одной» — значит гонять владельца по кругу, пока список не сойдётся.

    `pairs` — готовый список (файл, победители); без него берётся `PAIRS` или,
    для кубка дивизиона `division_code`, `DIVISION_PAIRS`. `season_id` — сезон,
    в котором ищется предыдущая стадия (`WinnerOf`, стадия вступления).
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
    names: dict[str, str] = {}
    canonical: list[tuple[str, str]] = []

    prev: str | None = None
    prev_bracket: list[dict] = []
    needs_prev = stage in _STAGE_ENTRY_DIVISIONS or any(
        isinstance(raw, WinnerOf) for pair in raw_pairs for raw in pair
    )
    if division_code is None and needs_prev:
        prev, prev_bracket = _previous_bracket(stage, season_id)

    for index, pair in enumerate(raw_pairs, start=1):
        clubs: list[str] = []
        for raw in pair:
            if isinstance(raw, WinnerOf):
                if division_code is not None:
                    problems.append(f"пара {index}: «{raw}» — ссылка на серию только в общем кубке")
                    continue
                club, why = _winner_of(raw, prev, prev_bracket)
                if club is None:
                    problems.append(f"пара {index}: {why}")
                    continue
            else:
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
                names[club.lower()] = club
            clubs.append(club)
        if len(clubs) == 2:
            canonical.append((clubs[0], clubs[1]))

    if canonical and allowed and stage == "1/64" and len(seen) != 32:
        problems.append(f"на 1/64 должно играть 32 клуба, а в списке их {len(seen)}")
    if division_code is None:
        problems += _check_entrants(stage, prev, prev_bracket, names)
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
        canonical = validate_pairs(stage, pairs, division_code, season_id=season_id)
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
