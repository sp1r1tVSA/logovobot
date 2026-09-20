#!/usr/bin/env python3
"""
scripts/reset_season_data.py

Безопасный инструмент предсезонной очистки базы данных (Pre-season Clean Reset).

Используется перед формированием нового списка команд и генерацией расписания.
Удаляет турнирный «мусор» (старые матчи, туры, события, котировки, купоны ставок и лишние варны),
гарантированно сохраняя зарегистрированных пользователей, роли администраторов,
структуру дивизионов и топиков.

Режимы работы:
  1. Dry-run (по умолчанию):
     python3 scripts/reset_season_data.py
     Показывает аудит текущих данных и точное количество записей, которые будут удалены.
     База данных НЕ модифицируется.

  2. Очистка турнира и ставок (с сохранением команд):
     python3 scripts/reset_season_data.py --apply
     Автоматически создаёт бэкап league.db.bak-YYYYMMDD-HHMMSS и очищает
     матчи, туры, события, напоминания, рынки ставок и купоны.

  3. Полный сброс перед новым чемпионатом (с отвязкой команд и амнистией варнов):
     python3 scripts/reset_season_data.py --apply --reset-teams --reset-warns
     Дополнительно:
       - очищает составы (squad_players), рейтинги команд (team_ratings);
       - сбрасывает привязку клубов у игроков (users.team_name = NULL);
       - обнуляет предупреждения (users.warn_count = 0) и очищает историю (user_warns).
     Пользователи, их Telegram ID, роли и кошельки остаются нетронутыми.

  4. Очистка демо-аккаунтов:
     python3 scripts/reset_season_data.py --apply --clean-demo
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

import config
from time_utils import now_msk_str

DEMO_UIDS = [990101, 990102, 990103, 990104, 990105, 990106]


def get_table_count(cursor: sqlite3.Cursor, table_name: str, condition: str = "") -> int:
    try:
        query = f"SELECT COUNT(*) FROM {table_name}"
        if condition:
            query += f" WHERE {condition}"
        cursor.execute(query)
        row = cursor.fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cursor.fetchone() is not None


def perform_audit(cursor: sqlite3.Cursor, reset_teams: bool, reset_warns: bool, clean_demo: bool) -> dict[str, int]:
    audit = {}

    # Матчи и туры
    audit["matches"] = get_table_count(cursor, "matches")
    audit["rounds"] = get_table_count(cursor, "rounds")
    audit["match_events"] = get_table_count(cursor, "match_events")
    audit["round_reminders"] = get_table_count(cursor, "round_reminders")
    audit["round_content_posts"] = get_table_count(cursor, "round_content_posts")
    audit["debt_reminders"] = get_table_count(cursor, "debt_reminders")
    audit["pending_reports"] = get_table_count(cursor, "pending_reports")
    audit["cup_series"] = get_table_count(cursor, "cup_series")

    # Лайв-события
    audit["live_match_states"] = get_table_count(cursor, "live_match_states")
    audit["live_events"] = get_table_count(cursor, "live_events")
    audit["live_statistics"] = get_table_count(cursor, "live_statistics")

    # Ставки и рынки
    audit["bet_markets"] = get_table_count(cursor, "bet_markets")
    audit["markets"] = get_table_count(cursor, "markets")
    audit["market_selections"] = get_table_count(cursor, "market_selections")
    audit["odds_movement"] = get_table_count(cursor, "odds_movement")
    audit["odds_history"] = get_table_count(cursor, "odds_history")
    audit["user_bets"] = get_table_count(cursor, "user_bets")
    audit["bet_items"] = get_table_count(cursor, "bet_items")
    audit["saved_coupons"] = get_table_count(cursor, "saved_coupons")
    audit["predictions"] = get_table_count(cursor, "predictions")
    audit["prediction_snapshots"] = get_table_count(cursor, "prediction_snapshots")

    # Варны и дисциплина
    if reset_warns or reset_teams:
        audit["user_warns (история)"] = get_table_count(cursor, "user_warns")
        audit["users_with_warns (счётчик)"] = get_table_count(cursor, "users", "warn_count > 0")

    # Клубы и составы
    if reset_teams:
        audit["squad_players"] = get_table_count(cursor, "squad_players")
        audit["team_ratings"] = get_table_count(cursor, "team_ratings")
        audit["assigned_teams"] = get_table_count(cursor, "users", "team_name IS NOT NULL AND team_name != ''")

    # Демо-данные
    if clean_demo:
        ph = ",".join(str(u) for u in DEMO_UIDS)
        audit["demo_users"] = get_table_count(cursor, "users", f"telegram_id IN ({ph})")
        audit["demo_wallets"] = get_table_count(cursor, "user_wallets", f"user_id IN ({ph})")

    return audit


def execute_reset(
    db_path: str,
    reset_teams: bool = False,
    reset_warns: bool = False,
    clean_demo: bool = False,
    apply: bool = False
):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Если запрошен сброс команд, варны логично сбрасывать вместе с ними
    do_reset_warns = reset_warns or reset_teams

    print("=" * 65)
    print("🛠  ПРЕДСЕЗОННАЯ ОЧИСТКА БАЗЫ ДАННЫХ (Logovobot)")
    print("=" * 65)
    print(f"📁 База данных: {db_path}")
    print(f"⚙️  Режим: {'🔴 ИСПОЛНЕНИЕ (--apply)' if apply else '🟡 DRY-RUN (только чтение)'}")
    print(f"🛡  Сброс клубов игроков (--reset-teams): {'ДА' if reset_teams else 'НЕТ'}")
    print(f"⚠️  Сброс варнов игроков (--reset-warns): {'ДА' if do_reset_warns else 'НЕТ'}")
    print(f"👤 Очистка демо-аккаунтов (--clean-demo): {'ДА' if clean_demo else 'НЕТ'}")
    print("-" * 65)

    # Аудит
    audit = perform_audit(cursor, reset_teams=reset_teams, reset_warns=do_reset_warns, clean_demo=clean_demo)
    total_records = sum(audit.values())

    print("📊 Обнаруженные турнирные записи:")
    for tbl, cnt in audit.items():
        if cnt > 0:
            print(f"   • {tbl:<28}: {cnt:>5} записей")

    if total_records == 0:
        print("   ✓ База уже чиста: активных матчей, туров, маркетов и варнов не обнаружено.")
        conn.close()
        return

    print(f"\nИТОГО к очистке / обновлению: {total_records} записей.")

    # Безопасность пользователей
    user_count = get_table_count(cursor, "users")
    admin_count = get_table_count(cursor, "users", "role = 'admin'")
    div_count = get_table_count(cursor, "divisions")
    print(f"\n🔒 БУДУТ СОХРАНЕНЫ БЕЗ УДАЛЕНИЯ:")
    print(f"   • Пользователи: {user_count} (включая {admin_count} админов)")
    print(f"   • Дивизионы:    {div_count}")
    print(f"   • Балансы кошельков и уровни прогресса игроков")

    if not apply:
        print("\n" + "!" * 65)
        print("ℹ️  Это был ознакомительный запуск (DRY-RUN). Никаких изменений не внесено.")
        print("Для применения очистки запустите с флагом --apply:")
        flags = ["--apply"]
        if reset_teams:
            flags.append("--reset-teams")
        if do_reset_warns and not reset_teams:
            flags.append("--reset-warns")
        if clean_demo:
            flags.append("--clean-demo")
        print(f"   python3 scripts/reset_season_data.py {' '.join(flags)}")
        print("!" * 65)
        conn.close()
        return

    # Создание бэкапа перед модификацией
    ts = now_msk_str("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.bak-{ts}"
    try:
        shutil.copy2(db_path, backup_path)
        print(f"\n💾 Резервная копия базы успешно создана:\n   → {backup_path}")
    except Exception as e:
        conn.close()
        sys.exit(f"\n❌ Ошибка создания резервной копии базы данных: {e}\nОчистка отменена.")

    print("\n🚀 Выполнение очистки в атомарной транзакции...")

    try:
        cursor.execute("BEGIN TRANSACTION")

        # 1. Турнирные сущности
        for tbl in [
            "match_events",
            "round_reminders",
            "round_content_posts",
            "debt_reminders",
            "pending_reports",
            "live_events",
            "live_statistics",
            "live_match_states",
            "matches",
            "rounds",
            "cup_series",
        ]:
            if table_exists(cursor, tbl):
                cursor.execute(f"DELETE FROM {tbl}")

        # 2. Ставки и рынки
        for tbl in [
            "bet_items",
            "user_bets",
            "saved_coupons",
            "odds_movement",
            "odds_history",
            "market_selections",
            "markets",
            "bet_markets",
            "predictions",
            "prediction_snapshots",
        ]:
            if table_exists(cursor, tbl):
                cursor.execute(f"DELETE FROM {tbl}")

        # 3. Сброс команд (если запрошено)
        if reset_teams:
            if table_exists(cursor, "squad_players"):
                cursor.execute("DELETE FROM squad_players")
            if table_exists(cursor, "team_ratings"):
                cursor.execute("DELETE FROM team_ratings")
            if table_exists(cursor, "users"):
                cursor.execute("UPDATE users SET team_name = NULL")
            print("   ✓ Сброшены привязки команд у всех игроков и очищены составы клубов.")

        # 4. Сброс варнов и дисциплинарных взысканий
        if do_reset_warns:
            if table_exists(cursor, "user_warns"):
                cursor.execute("DELETE FROM user_warns")
            if table_exists(cursor, "users"):
                cursor.execute("UPDATE users SET warn_count = 0")
            print("   ✓ Сброшены предупреждения у всех игроков (warn_count = 0) и очищена история взысканий.")

        # 5. Удаление демо-аккаунтов (если запрошено)
        if clean_demo:
            ph = ",".join("?" for _ in DEMO_UIDS)
            cursor.execute(f"DELETE FROM users WHERE telegram_id IN ({ph})", DEMO_UIDS)
            cursor.execute(f"DELETE FROM user_wallets WHERE user_id IN ({ph})", DEMO_UIDS)
            print("   ✓ Удалены аккаунты виртуальных демо-соперников.")

        conn.commit()
        print("\n✅ БАЗА ДАННЫХ УСПЕШНО ОЧИЩЕНА!")
        print("   Все матчи, туры, рынки ставок и лишние варны удалены.")
        print("   Теперь можно задавать список команд и генерировать чистое расписание.")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Ошибка во время выполнения очистки: {e}")
        print(f"Транзакция откачена. База данных восстановлена в исходное состояние.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Скрипт предсезонной очистки базы данных Logovobot")
    parser.add_argument("--db", default=None, help="Путь к файлу базы данных SQLite (по умолчанию config.DB_PATH)")
    parser.add_argument("--apply", action="store_true", help="Применить изменения (по умолчанию dry-run)")
    parser.add_argument("--reset-teams", action="store_true", help="Сбросить привязку клубов у игроков (users.team_name = NULL), составы и рейтинги")
    parser.add_argument("--reset-warns", action="store_true", help="Сбросить все варны игроков (warn_count = 0) и очистить таблицу user_warns")
    parser.add_argument("--clean-demo", action="store_true", help="Удалить тестовых виртуальных соперников (ID 990101..990106)")

    args = parser.parse_args()

    if args.db:
        db_path = args.db
    else:
        db_path = str(getattr(config, "DB_PATH", PROJECT_ROOT / "league.db"))

    if not os.path.exists(db_path):
        sys.exit(f"❌ Файл базы данных не найден: {db_path}")

    execute_reset(
        db_path=db_path,
        reset_teams=args.reset_teams,
        reset_warns=args.reset_warns,
        clean_demo=args.clean_demo,
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
