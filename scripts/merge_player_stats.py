#!/usr/bin/env python3
"""
scripts/merge_player_stats.py

Ручное объединение статистики двух (и более) имён, за которыми стоит ОДИН игрок:
голы и ассисты (`match_events.player_name`) и короны «Игрок матча»
(`matches.mvp_player`) переписываются на одно имя, поэтому топ бомбардиров,
карточка игрока и таблицы считают их вместе.

Это не `merge_player_spellings.py`: тот сам склеивает написания вроде
EMEGA / Emegha с составом клуба. Здесь имена разные (например, имя и фамилия),
никакой резолвер их не свяжет, и пару называет человек. Имена сравниваются
точно — с точностью до регистра и пробелов по краям, без нечётких совпадений.

Режимы:

  1. Dry-run (по умолчанию) — печатает план и ничего не пишет:
       python scripts/merge_player_stats.py --from ABDE --to EZZALZULI

  2. Применение (перед записью делается копия базы рядом с ней):
       python scripts/merge_player_stats.py --from ABDE --to EZZALZULI --apply

  3. Только один клуб (иначе — во всех, где имя встречается):
       python scripts/merge_player_stats.py --from ABDE --to EZZALZULI --club "Бетис" --apply

  4. Заодно привести состав клуба: строка `--from` в `squad_players` переименовывается
     в `--to`, а если `--to` в клубе уже есть — лишняя строка удаляется:
       python scripts/merge_player_stats.py --from ABDE --to EZZALZULI --squad --apply

  5. Несколько исходных имён и другая база:
       python scripts/merge_player_stats.py --from ABDE --from "Abde" --to EZZALZULI --db /path/to/league.db

`--to` — имя, под которым игрок останется. Повторный запуск безопасен:
после склейки писать больше нечего.
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
import database
from time_utils import now_msk_str


def _print_plan(plan: dict) -> None:
    events, mvp, squad = plan["events"], plan["mvp"], plan["squad"]
    target = plan["target"]
    if events:
        print("Голы и ассисты (match_events):")
        width = max(len(i["team_name"]) for i in events)
        for i in events:
            print(f"  {i['team_name']:<{width}}  {i['old_name']} → {target}"
                  f"  голов: {i['goals']}, ассистов: {i['assists']}, строк: {i['rows']}")
        print(f"  Всего: голов {sum(i['goals'] for i in events)}, "
              f"ассистов {sum(i['assists'] for i in events)}")
    if mvp:
        print("Короны «Игрок матча» (matches.mvp_player):")
        for i in mvp:
            print(f"  матч #{i['match_id']}  {i['old_name']} → {target}")
    if squad:
        print("Состав (squad_players):")
        for i in squad:
            what = "переименовать" if i["action"] == "rename" else "удалить дубль (игрок уже в составе)"
            print(f"  {i['team_name']}  {i['old_name']} — {what}")


def _print_final_totals(target: str) -> None:
    """Goals/assists under the target name after the merge, per club."""
    with database.transaction() as conn:
        rows = conn.execute("""
            SELECT team_name,
                   COALESCE(SUM(CASE WHEN event_type = 'goal' THEN count END), 0) AS goals,
                   COALESCE(SUM(CASE WHEN event_type = 'assist' THEN count END), 0) AS assists
            FROM match_events WHERE player_name = ? GROUP BY team_name ORDER BY team_name
        """, (target,)).fetchall()
    for r in rows:
        print(f"  {target} ({r['team_name']}): ⚽ {r['goals']}, 🅰️ {r['assists']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Объединение статистики двух имён одного игрока.")
    parser.add_argument("--from", dest="sources", action="append", required=True, metavar="NAME",
                        help="Имя, которое переписывается (можно повторять).")
    parser.add_argument("--to", dest="target", required=True, metavar="NAME",
                        help="Имя, под которым игрок остаётся.")
    parser.add_argument("--club", default=None, help="Только этот клуб.")
    parser.add_argument("--squad", action="store_true", help="Привести и состав клуба (squad_players).")
    parser.add_argument("--apply", action="store_true", help="Записать изменения (по умолчанию dry-run).")
    parser.add_argument("--db", default=None, help=f"Путь к базе (по умолчанию {config.DB_PATH}).")
    parser.add_argument("--no-backup", action="store_true", help="Не делать копию базы перед --apply.")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db or config.DB_PATH)
    if not os.path.isfile(db_path):
        print(f"❌ База не найдена: {db_path}")
        return 1
    config.DB_PATH = database.DB_PATH = db_path

    print(f"База: {db_path}")
    print(f"Режим: {'APPLY' if args.apply else 'DRY-RUN'}  {' + '.join(args.sources)} → {args.target}"
          f"{'  клуб: ' + args.club if args.club else ''}{'  со составом' if args.squad else ''}")
    print("-" * 60)

    try:
        plan = database.plan_player_merge(args.sources, args.target, team_name=args.club,
                                          include_squad=args.squad)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    if not (plan["events"] or plan["mvp"] or plan["squad"]):
        print("✅ Склеивать нечего: этих имён нет в событиях и коронах.")
        return 0
    _print_plan(plan)
    print("-" * 60)

    if not args.apply:
        print("Ничего не записано — запустите с --apply.")
        return 0

    if not args.no_backup:
        backup_path = f"{db_path}.backup_before_stats_merge_{now_msk_str('%Y%m%d_%H%M%S')}"
        database.backup_database(backup_path)
        print(f"💾 Копия базы: {backup_path}")

    done = database.apply_player_merge(plan)
    print(f"✅ Обновлено: событий {done['events']}, корон {done['mvp']}, строк состава {done['squad']}.")
    print("Итог по клубам:")
    _print_final_totals(plan["target"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
