#!/usr/bin/env python3
"""
scripts/merge_player_spellings.py

Склейка написаний одного футболиста в базе: OCR записывает голы, ассисты и
корону «Игрок матча» так, как прочитал экран, и один игрок оказывается под
несколькими именами — EMEGA / Emegha, KOKCU / Kökçü, YILDIZ / Yıldız.
Скрипт переписывает такие имена на написание из состава клуба (`squad_players`):

  - `match_events.player_name` — в пределах своего клуба;
  - `matches.mvp_player` — только когда имя подходит составу ровно одного из
    двух клубов матча.

Составы не меняются. Подбор идёт через `services.player_names.match_roster_name`,
тем же, чем склеиваются написания при показе, поэтому после прогона данные
совпадают с тем, что игроки уже видят в Mini App и на карточках.

Режимы:

  1. Dry-run (по умолчанию) — печатает план и ничего не пишет:
       python scripts/merge_player_spellings.py

  2. Применение (перед записью делается копия базы рядом с ней):
       python scripts/merge_player_spellings.py --apply

  3. Один клуб:
       python scripts/merge_player_spellings.py --apply --club "Челси"

  4. Без нечётких совпадений (только регистр/диакритика и алиасы):
       python scripts/merge_player_spellings.py --apply --no-fuzzy

  5. Другая база:
       python scripts/merge_player_spellings.py --db /path/to/league.db

Метод в плане: `key` — то же имя с точностью до регистра и диакритики,
`alias` — известный алиас или фамилия, `fuzzy` — похожее написание
(Emegha → EMEGA). Строки `fuzzy` стоит просмотреть глазами перед --apply.
Повторный запуск безопасен: уже склеенное в план не попадает.
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
    events, mvp = plan["events"], plan["mvp"]
    if events:
        print("Голы и ассисты (match_events):")
        width = max(len(i["team_name"]) for i in events)
        for i in events:
            mark = "  ⚠" if i["method"] == "fuzzy" else ""
            print(f"  {i['team_name']:<{width}}  {i['old_name']} → {i['new_name']}"
                  f"  [{i['method']}]  строк: {i['rows']}, событий: {i['total']}{mark}")
    if mvp:
        print("Короны «Игрок матча» (matches.mvp_player):")
        for i in mvp:
            mark = "  ⚠" if i["method"] == "fuzzy" else ""
            print(f"  матч #{i['match_id']}  {i['team_name']}  {i['old_name']} → {i['new_name']}"
                  f"  [{i['method']}]{mark}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Склейка написаний игроков на имя из состава клуба.")
    parser.add_argument("--apply", action="store_true", help="Записать изменения (по умолчанию dry-run).")
    parser.add_argument("--club", default=None, help="Только этот клуб.")
    parser.add_argument("--no-fuzzy", action="store_true", help="Не склеивать нечёткие совпадения.")
    parser.add_argument("--db", default=None, help=f"Путь к базе (по умолчанию {config.DB_PATH}).")
    parser.add_argument("--no-backup", action="store_true", help="Не делать копию базы перед --apply.")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db or config.DB_PATH)
    if not os.path.isfile(db_path):
        print(f"❌ База не найдена: {db_path}")
        return 1
    config.DB_PATH = database.DB_PATH = db_path

    print(f"База: {db_path}")
    print(f"Режим: {'APPLY' if args.apply else 'DRY-RUN'}"
          f"{'  клуб: ' + args.club if args.club else ''}"
          f"{'  без fuzzy' if args.no_fuzzy else ''}")
    print("-" * 60)

    plan = database.plan_player_spelling_merges(team_name=args.club, include_fuzzy=not args.no_fuzzy)
    if not plan["events"] and not plan["mvp"]:
        print("✅ Склеивать нечего.")
        return 0
    _print_plan(plan)
    print("-" * 60)

    if not args.apply:
        print(f"Итого: {len(plan['events'])} написаний в событиях, {len(plan['mvp'])} корон. "
              f"Ничего не записано — запустите с --apply.")
        return 0

    if not args.no_backup:
        backup_path = f"{db_path}.backup_before_player_merge_{now_msk_str('%Y%m%d_%H%M%S')}"
        database.backup_database(backup_path)
        print(f"💾 Копия базы: {backup_path}")

    updated = database.apply_player_spelling_merges(plan)
    print(f"✅ Обновлено строк: событий {updated['events']}, корон {updated['mvp']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
