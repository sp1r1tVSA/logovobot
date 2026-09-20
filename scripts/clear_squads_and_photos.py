#!/usr/bin/env python3
"""
scripts/clear_squads_and_photos.py

Скрипт для быстрого обнуления загруженных составов и фотографий составов в базе данных.

Что делает:
  1. Очищает таблицу `squad_players` (списки футболистов команд).
  2. Обнуляет `users.squad_photo_id = NULL` (удаляет привязку скриншотов составов).
  3. Создает автоматический бэкап базы перед применением (--apply).

Использование:
  # 1. Просмотр текущей статистики (Dry-Run, без изменений):
  python3 scripts/clear_squads_and_photos.py

  # 2. Применить полный сброс для всех клубов:
  python3 scripts/clear_squads_and_photos.py --apply

  # 3. Сбросить только конкретный дивизион:
  python3 scripts/clear_squads_and_photos.py --apply --division 1

  # 4. Сбросить только конкретный клуб:
  python3 scripts/clear_squads_and_photos.py --apply --club "Реал Мадрид"

  # 5. Указать нестандартный путь к БД:
  python3 scripts/clear_squads_and_photos.py --apply --db /path/to/league.db
"""

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Setup project root import path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from time_utils import now_msk_str

try:
    import config
    DEFAULT_DB_PATH = getattr(config, "DB_PATH", "league.db")
except Exception:
    DEFAULT_DB_PATH = "league.db"


def create_backup(db_path: str) -> str:
    """Создает резервную копию базы данных с текущим timestamp."""
    timestamp = now_msk_str("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.backup_before_squad_clear_{timestamp}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def main():
    parser = argparse.ArgumentParser(description="Сброс загруженных составов и фотографий составов.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально применить изменения в базе данных. По умолчанию включен dry-run."
    )
    parser.add_argument(
        "--db",
        type=str,
        default=DEFAULT_DB_PATH,
        help=f"Путь к файлу базы данных SQLite (по умолчанию: {DEFAULT_DB_PATH})"
    )
    parser.add_argument(
        "--division",
        type=int,
        default=None,
        help="Очистить составы только указанного дивизиона (например: --division 1)"
    )
    parser.add_argument(
        "--club",
        type=str,
        default=None,
        help="Очистить состав только указанного клуба (например: --club 'Реал Мадрид')"
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Не создавать бэкап базы перед очисткой."
    )

    args = parser.parse_args()
    db_path = os.path.abspath(args.db)

    if not os.path.isfile(db_path):
        print(f"❌ Ошибка: файл базы данных не найден по пути: {db_path}")
        sys.exit(1)

    print("=" * 60)
    print("🧹 СКРИПТ ОБНУЛЕНИЯ СОСТАВОВ И ФОТОГРАФИЙ (SQUAD RESET)")
    print("=" * 60)
    print(f"📁 База данных: {db_path}")
    print(f"⚙️  Режим: {'🔴 APPLY (РЕАЛЬНОЕ ПРИМЕНЕНИЕ)' if args.apply else '🟡 DRY-RUN (БЕЗОПАСНЫЙ ПРОСМОТР)'}")
    if args.division is not None:
        print(f"🎯 Фильтр дивизиона: Дивизион {args.division}")
    if args.club is not None:
        print(f"🎯 Фильтр клуба: {args.club}")
    print("-" * 60)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # 1. Проверяем текущее количество записей в squad_players
        squad_sql = "SELECT COUNT(*) as cnt, COUNT(DISTINCT team_name) as teams_cnt FROM squad_players"
        squad_params = []
        if args.club:
            squad_sql += " WHERE LOWER(team_name) = LOWER(?)"
            squad_params.append(args.club.strip())
        elif args.division is not None:
            squad_sql += " WHERE team_name IN (SELECT team_name FROM users WHERE division_id = ? AND team_name IS NOT NULL)"
            squad_params.append(args.division)

        cursor.execute(squad_sql, squad_params)
        squad_row = cursor.fetchone()
        players_cnt = squad_row["cnt"] if squad_row else 0
        squad_teams_cnt = squad_row["teams_cnt"] if squad_row else 0

        # 2. Проверяем количество фото составов в users
        photo_sql = "SELECT COUNT(*) as cnt FROM users WHERE squad_photo_id IS NOT NULL AND squad_photo_id != ''"
        photo_params = []
        if args.club:
            photo_sql += " AND LOWER(team_name) = LOWER(?)"
            photo_params.append(args.club.strip())
        elif args.division is not None:
            photo_sql += " AND division_id = ?"
            photo_params.append(args.division)

        cursor.execute(photo_sql, photo_params)
        photo_row = cursor.fetchone()
        photos_cnt = photo_row["cnt"] if photo_row else 0

        # Выводим сводку
        print("📊 ТЕКУЩЕЕ СОСТОЯНИЕ:")
        print(f"  • Игроков в squad_players к удалению: {players_cnt} (у {squad_teams_cnt} клубов)")
        print(f"  • Фотографий squad_photo_id к обнулению: {photos_cnt}")
        print("-" * 60)

        if not args.apply:
            print("ℹ️  Это был ознакомительный запуск (DRY-RUN). Никакие данные НЕ удалены.")
            print("👉 Чтобы применить очистку на сервере, запустите:")
            extra_flags = ""
            if args.division is not None:
                extra_flags += f" --division {args.division}"
            if args.club:
                extra_flags += f" --club '{args.club}'"
            print(f"   python3 scripts/clear_squads_and_photos.py --apply{extra_flags}")
            return

        if players_cnt == 0 and photos_cnt == 0:
            print("ℹ️  Составы и фотографии уже пусты. Ничего удалять не требуется.")
            return

        # Создаем бэкап
        if not args.no_backup:
            backup_file = create_backup(db_path)
            print(f"💾 Создан бэкап базы: {backup_file}")

        # Выполняем очистку
        # 1. Удаление squad_players
        del_squad_sql = "DELETE FROM squad_players"
        del_squad_params = []
        if args.club:
            del_squad_sql += " WHERE LOWER(team_name) = LOWER(?)"
            del_squad_params.append(args.club.strip())
        elif args.division is not None:
            del_squad_sql += " WHERE team_name IN (SELECT team_name FROM users WHERE division_id = ? AND team_name IS NOT NULL)"
            del_squad_params.append(args.division)

        cursor.execute(del_squad_sql, del_squad_params)
        deleted_players = cursor.rowcount

        # 2. Обнуление squad_photo_id в users
        upd_photo_sql = "UPDATE users SET squad_photo_id = NULL WHERE squad_photo_id IS NOT NULL"
        upd_photo_params = []
        if args.club:
            upd_photo_sql += " AND LOWER(team_name) = LOWER(?)"
            upd_photo_params.append(args.club.strip())
        elif args.division is not None:
            upd_photo_sql += " AND division_id = ?"
            upd_photo_params.append(args.division)

        cursor.execute(upd_photo_sql, upd_photo_params)
        cleared_photos = cursor.rowcount

        conn.commit()

        print("✅ УСПЕШНО ВЫПОЛНЕНО:")
        print(f"  • Удалено футболистов из squad_players: {deleted_players}")
        print(f"  • Сброшено фото составов у тренеров: {cleared_photos}")
        print("=" * 60)

    except Exception as e:
        conn.rollback()
        print(f"❌ Ошибка при выполнении: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
