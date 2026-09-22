"""Предпросмотр цен общего кубка по сетке этапа — ничего не пишет.

Скрипт отвечает на вопрос «что увидит игрок в линии этапа» до того, как этап
открыт: сила каждой пары, цена игры (П1/П2, тоталы, ОЗ, фора) и цена серии
(проход, счёт серии, третья игра). Это единственный способ посмотреть на цены
конкретных пар до боевого прогона: `DIVISION_PLAYER_SEEDS` задан логинами
тренеров, а связь «клуб → тренер» живёт только в `users` на сервере, поэтому
локально сила любой пары — нейтральные 10.0.

Использование:

    python scripts/cup_line_preview.py                       # активный сезон, 1/64
    python scripts/cup_line_preview.py --stage 1/32
    python scripts/cup_line_preview.py --db /path/to/league.db --season 2

Скрипт не заводит ни игр, ни рынков: читается готовая сетка (`cup_series`), а
цены считаются функциями из `services/cup_strength.py` — теми же, которыми
линия заполняется. В конце скрипт сверяет число строк в затронутых таблицах и
падает, если хоть одна изменилась: обещание «read-only» здесь проверяемое.
"""

import argparse
import os
import sys
from decimal import Decimal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
import database  # noqa: E402
from constants import CUP_STAGES  # noqa: E402
from services import cup_strength  # noqa: E402

_WATCHED_TABLES = ("matches", "markets", "market_selections", "cup_series", "cup_stages", "bet_markets")


def _table_counts() -> dict[str, int]:
    with database.transaction() as conn:
        cursor = conn.cursor()
        counts = {}
        for table in _WATCHED_TABLES:
            counts[table] = int(cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return counts


def _schema_missing() -> list[str]:
    """Таблицы, которых в открытой базе нет — то есть база не инициализирована."""
    with database.transaction() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    present = {r["name"] for r in rows}
    return [t for t in _WATCHED_TABLES if t not in present]


def _fmt(value) -> str:
    if value is None:
        return "  —  "
    return f"{Decimal(str(value)):.2f}".rjust(5)


def _print_series(stage: str, season_id: int, series: dict) -> None:
    t1, t2 = series["team1_name"], series["team2_name"]
    s1 = cup_strength.cup_club_strength(t1, season_id=season_id)
    s2 = cup_strength.cup_club_strength(t2, season_id=season_id)
    code1 = cup_strength.division_code_for_club(t1, season_id=season_id)
    code2 = cup_strength.division_code_for_club(t2, season_id=season_id)
    game = cup_strength.cup_match_odds(t1, t2, season_id=season_id)
    bracket = cup_strength.best_of_three_odds(t1, t2, season_id=season_id)

    print(f"\nСерия {series['series_num']}: {t1} ({code1 or '?'}) — {t2} ({code2 or '?'})")
    print(f"  сила: {s1:5.2f} (сид/таблица {game['strength1']}) vs {s2:5.2f} ({game['strength2']})"
          f"   класс: {cup_strength.class_uplift(code1):+.1f} / {cup_strength.class_uplift(code2):+.1f}")
    print(f"  игра   λ: {game['lambda1']:.2f} — {game['lambda2']:.2f}   "
          f"P(победа в игре): {game['p_game_1']:.3f} — {game['p_game_2']:.3f}")
    go = game["odds"]
    print(f"  П1/П2: {_fmt(go['p1'])} / {_fmt(go['p2'])}   "
          f"ТБ2.5/ТМ2.5: {_fmt(go['tb25'])} / {_fmt(go['tm25'])}   "
          f"ОЗ да/нет: {_fmt(go['btts_yes'])} / {_fmt(go['btts_no'])}")
    print(f"  Ф1(-1.5)/Ф2(+1.5): {_fmt(go['h1_minus_1.5'])} / {_fmt(go['h2_plus_1.5'])}   "
          f"ИТБ1/ИТБ2: {_fmt(go['it1_over_1.5'])} / {_fmt(go['it2_over_1.5'])}")
    so = bracket["odds"]
    sp = bracket["probs"]
    print(f"  серия: проход {_fmt(so['p1'])} / {_fmt(so['p2'])}   "
          f"счёт 2:0 {_fmt(so['series_2_0'])}  2:1 {_fmt(so['series_2_1'])}  "
          f"1:2 {_fmt(so['series_1_2'])}  0:2 {_fmt(so['series_0_2'])}")
    print(f"  третья игра: да {_fmt(so['over_2.5'])} / нет {_fmt(so['under_2.5'])}"
          f"   P(3 игры) = {sp['third_game']:.3f}")
    coach1 = database.find_user_by_team(t1)
    coach2 = database.find_user_by_team(t2)
    missing = [name for name, coach in ((t1, coach1), (t2, coach2)) if not coach]
    if missing:
        print(f"  ⚠ без тренера: {', '.join(missing)} — серию некому сыграть, "
              f"приём результата через кабинет недоступен")


def main() -> int:
    parser = argparse.ArgumentParser(description="Предпросмотр линии общего кубка (только чтение)")
    parser.add_argument("--db", default=None, help="путь к league.db (по умолчанию — из окружения)")
    parser.add_argument("--stage", default="1/64", choices=list(CUP_STAGES))
    parser.add_argument("--season", type=int, default=None, help="id сезона; по умолчанию активный")
    args = parser.parse_args()

    if args.db:
        database.DB_PATH = args.db

    missing = _schema_missing()
    if missing:
        print("В базе %s нет таблиц %s. Чаще всего это неверный путь: /tmp/... из Git Bash "
              "на Windows читается относительно диска C, но как аргумент конвертируется в "
              "AppData\\Temp. Передавай windows-путь, например --db C:\\path\\to\\league.db"
              % (database.DB_PATH, missing))
        return 2

    before = _table_counts()
    if args.season is not None:
        season_id = args.season
    else:
        act = database.get_active_season()
        season_id = act["id"] if act else 1
    bracket = database.get_cup_bracket(args.stage, season_id=season_id)

    print(f"Сезон {season_id}, этап {args.stage}, база: {database.DB_PATH}")
    print(f"Лестница классов дивизионов: {config.CUP_DIVISION_CLASS}")
    if not bracket:
        print("Сетка этапа пуста — заведи пары через create_cup_series (или scripts/seed_cup_bracket.py).")
        return 1

    played = [s for s in bracket if s["winner_name"]]
    print(f"Серий в сетке: {len(bracket)}, из них уже решённых: {len(played)}")
    for series in bracket:
        _print_series(args.stage, season_id, series)

    after = _table_counts()
    changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
    if changed:
        print(f"\n✗ Таблицы изменились: {changed} — скрипт обязан быть только читающим.")
        return 2
    print(f"\nСтрок не изменилось ({len(before)} таблиц проверено) — превью ничего не записало.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
