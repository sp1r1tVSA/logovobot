#!/usr/bin/env python3
"""
purge_old_season.py

Скрипт полной и безопасной очистки данных старого сезона перед переходом на систему дивизионов.

Поддерживает:
- Dry-run (по умолчанию): подробный аудит и вывод таблицы всех записей, подлежащих удалению.
- Execute (--execute): атомарное удаление в единой транзакции с автоматическим rollback при ошибке.
- Полная сохранность пользователей, кошельков, балансов монет, финансовых транзакций, админов и структуры дивизионов.
- Валидация целостности: PRAGMA foreign_key_check, PRAGMA integrity_check, сверка балансов и пользователей.
"""

import os
import sys
import sqlite3
import argparse
import logging
from typing import Any, Dict, List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("purge_old_season")

# Списки таблиц
SEASONAL_PURGE_TABLES = [
    # 1. Зависимости ставок и рынков (нижний уровень)
    "bet_items",
    "market_selections",
    "odds_history",
    "odds_movement",
    "prediction_snapshots",
    "provider_matches",
    "risk_alerts",
    "live_events",
    "live_statistics",
    "live_match_states",
    "match_events",
    "pending_reports",
    "predictions",
    "bet_markets",
    "markets",
    
    # 2. Ставки и купоны пользователей
    "user_bets",
    "saved_coupons",
    "bet_audit_log",
    "notification_events",
    "favorites",
    
    # 3. Сезонные таблицы экономики и прогресса
    "season_reward_ledger",
    "season_rewards_catalog",
    "season_rules_config",
    "season_snapshots",
    "season_player_stats",
    
    # 4. Матчи, туры и кубки
    "matches",
    "round_reminders",
    "debt_reminders",
    "cup_series",
    "rounds",
    "rounds_v3",
    
    # 5. Команды и составы
    "team_ratings",
    "squad_players",
    "teams",
    
    # 6. Сезоны
    "seasons",
]

PRESERVED_TABLES = [
    "users",
    "user_wallets",
    "coin_transactions",
    "user_progression",
    "achievements_catalog",
    "user_achievements",
    "user_warns",
    "tournaments",
    "divisions",
    "division_topics",
    "division_admins",
    "system_config",
    "schema_migrations",
    "admin_audit_log",
    "sports_providers",
    "risk_limits_config",
    "user_notification_settings",
    "style_samples",
]


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    """Check if table exists in database."""
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cursor.fetchone() is not None


def get_table_count(cursor: sqlite3.Cursor, table_name: str) -> int:
    """Get total row count for table if it exists."""
    if not table_exists(cursor, table_name):
        return 0
    cursor.execute(f"SELECT COUNT(*) FROM \"{table_name}\"")
    return cursor.fetchone()[0]


def collect_audit_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Collect current counts of all seasonal and preserved entities."""
    cursor = conn.cursor()
    
    seasonal_counts = {}
    for tbl in SEASONAL_PURGE_TABLES:
        if table_exists(cursor, tbl):
            seasonal_counts[tbl] = get_table_count(cursor, tbl)
            
    preserved_counts = {}
    for tbl in PRESERVED_TABLES:
        if table_exists(cursor, tbl):
            preserved_counts[tbl] = get_table_count(cursor, tbl)
            
    # Финансовые показатели
    total_balance = 0
    if table_exists(cursor, "user_wallets"):
        cursor.execute("SELECT COALESCE(SUM(balance), 0) FROM user_wallets")
        total_balance = cursor.fetchone()[0]
        
    total_tx_volume = 0
    if table_exists(cursor, "coin_transactions"):
        cursor.execute("SELECT COALESCE(SUM(abs(amount)), 0) FROM coin_transactions")
        total_tx_volume = cursor.fetchone()[0]

    return {
        "seasonal": seasonal_counts,
        "preserved": preserved_counts,
        "total_balance": total_balance,
        "total_tx_volume": total_tx_volume,
    }


def print_audit_table(summary: Dict[str, Any], title: str = "СВОДКА ДАННЫХ"):
    """Format and print beautiful console table."""
    print("\n" + "=" * 80)
    print(f"📋 {title}")
    print("=" * 80)
    
    print("\n🗑️  СЕЗОННЫЕ ТАБЛИЦЫ (ПОДЛЕЖАТ ПОЛНОМУ УДАЛЕНИЮ):")
    print("-" * 80)
    print(f"{'Таблица':<35} | {'Записей к удалению':<20} | {'Статус':<15}")
    print("-" * 80)
    
    total_seasonal_rows = 0
    for tbl, cnt in summary["seasonal"].items():
        total_seasonal_rows += cnt
        status = "Будет очищена" if cnt > 0 else "Пусто"
        print(f"{tbl:<35} | {cnt:<20} | {status:<15}")
        
    print("-" * 80)
    print(f"ИТОГО СЕЗОННЫХ СТРОК: {total_seasonal_rows}")
    print("-" * 80)
    
    print("\n🛡️  ГЛОБАЛЬНЫЕ ТАБЛИЦЫ (СТРОГО СОХРАНЯЮТСЯ):")
    print("-" * 80)
    print(f"{'Таблица':<35} | {'Сохраняемых строк':<20} | {'Статус':<15}")
    print("-" * 80)
    
    for tbl, cnt in summary["preserved"].items():
        status = "Сохраняется ✅"
        print(f"{tbl:<35} | {cnt:<20} | {status:<15}")
        
    print("-" * 80)
    print(f"💰 Сумма балансов всех игроков: {summary['total_balance']:,} 🪙 (СОХРАНЯЕТСЯ)")
    print(f"📊 Объем финансовых транзакций: {summary['total_tx_volume']:,} 🪙 (СОХРАНЯЕТСЯ)")
    print("=" * 80 + "\n")


def execute_purge(conn: sqlite3.Connection, verbose: bool = False) -> Tuple[bool, str]:
    """
    Execute atomic purge of all old season data.
    Ensures rollback on any error and runs integrity checks.
    """
    cursor = conn.cursor()
    
    try:
        # Включаем внешние ключи для проверки связности
        cursor.execute("PRAGMA foreign_keys = ON;")
        
        # Кэшируем связки топиков и администраторов дивизионов в память перед очисткой
        saved_topics = []
        if table_exists(cursor, "division_topics"):
            cursor.execute("SELECT * FROM division_topics")
            saved_topics = [dict(r) for r in cursor.fetchall()]

        saved_div_admins = []
        if table_exists(cursor, "division_admins"):
            cursor.execute("SELECT * FROM division_admins")
            saved_div_admins = [dict(r) for r in cursor.fetchall()]
        
        # 1. Зависимости ставок и рынков
        dep_tables_first = [
            "bet_items",
            "market_selections",
            "odds_history",
            "odds_movement",
            "prediction_snapshots",
            "provider_matches",
            "risk_alerts",
            "live_events",
            "live_statistics",
            "live_match_states",
            "match_events",
            "pending_reports",
            "predictions",
            "bet_markets",
            "markets",
            "user_bets",
            "saved_coupons",
            "bet_audit_log",
            "notification_events",
            "favorites",
        ]
        
        for tbl in dep_tables_first:
            if table_exists(cursor, tbl):
                cursor.execute(f"DELETE FROM \"{tbl}\"")
                if verbose:
                    logger.info(f"Cleaned {tbl}")
                    
        # 2. Сезонные таблицы аналитики и наград
        season_stats_tables = [
            "season_reward_ledger",
            "season_rewards_catalog",
            "season_rules_config",
            "season_snapshots",
            "season_player_stats",
        ]
        for tbl in season_stats_tables:
            if table_exists(cursor, tbl):
                cursor.execute(f"DELETE FROM \"{tbl}\"")
                if verbose:
                    logger.info(f"Cleaned {tbl}")
                    
        # 3. Матчи, напоминания, туры и кубки
        match_tables = [
            "matches",
            "round_reminders",
            "debt_reminders",
            "cup_series",
            "rounds",
            "rounds_v3",
        ]
        for tbl in match_tables:
            if table_exists(cursor, tbl):
                cursor.execute(f"DELETE FROM \"{tbl}\"")
                if verbose:
                    logger.info(f"Cleaned {tbl}")
                    
        # 4. Команды и составы
        team_tables = [
            "team_ratings",
            "squad_players",
            "teams",
        ]
        for tbl in team_tables:
            if table_exists(cursor, tbl):
                cursor.execute(f"DELETE FROM \"{tbl}\"")
                if verbose:
                    logger.info(f"Cleaned {tbl}")
                    
        # 5. Отвязка дивизионов от удаляемого сезона (архитектура дивизионов DIV_1..DIV_5 сохраняется!)
        if table_exists(cursor, "divisions"):
            try:
                cursor.execute("UPDATE divisions SET season_id = NULL WHERE season_id IS NOT NULL")
                if verbose:
                    logger.info("Unlinked divisions from old season (season_id set to NULL)")
            except sqlite3.IntegrityError:
                # В схеме есть NOT NULL constraint на season_id (старая миграция без FK).
                # Оставляем существующие значения season_id без изменений!
                # Ни в коем случае НЕ делаем DROP TABLE divisions — это вызывает каскадное удаление division_topics!
                if verbose:
                    logger.info("divisions.season_id has NOT NULL constraint; preserved without dropping table")

        # 5.1 Защита от каскадного удаления: проверка и восстановление топиков и админов дивизионов
        if saved_topics and table_exists(cursor, "division_topics"):
            curr_topics = cursor.execute("SELECT COUNT(*) FROM division_topics").fetchone()[0]
            if curr_topics < len(saved_topics):
                logger.warning(
                    f"Обнаружена потеря записей division_topics ({curr_topics} вместо {len(saved_topics)})! "
                    "Выполняется автоматическое восстановление..."
                )
                for t in saved_topics:
                    cols = list(t.keys())
                    placeholders = ", ".join("?" for _ in cols)
                    col_names = ", ".join(f'"{c}"' for c in cols)
                    cursor.execute(
                        f'INSERT OR REPLACE INTO division_topics ({col_names}) VALUES ({placeholders})',
                        [t[c] for c in cols]
                    )
                if verbose:
                    logger.info(f"Restored {len(saved_topics)} division_topics")

        if saved_div_admins and table_exists(cursor, "division_admins"):
            curr_admins = cursor.execute("SELECT COUNT(*) FROM division_admins").fetchone()[0]
            if curr_admins < len(saved_div_admins):
                logger.warning(
                    f"Обнаружена потеря записей division_admins ({curr_admins} вместо {len(saved_div_admins)})! "
                    "Выполняется автоматическое восстановление..."
                )
                for a in saved_div_admins:
                    cols = list(a.keys())
                    placeholders = ", ".join("?" for _ in cols)
                    col_names = ", ".join(f'"{c}"' for c in cols)
                    cursor.execute(
                        f'INSERT OR REPLACE INTO division_admins ({col_names}) VALUES ({placeholders})',
                        [a[c] for c in cols]
                    )
                if verbose:
                    logger.info(f"Restored {len(saved_div_admins)} division_admins")

        # 6. Сезоны (удаляем все старые сезоны)
        if table_exists(cursor, "seasons"):
            cursor.execute("DELETE FROM seasons")
            if verbose:
                logger.info("Cleaned seasons")
                
        # 6. Сброс сезонных полей в таблице пользователей (аккаунты и роли СОХРАНЯЮТСЯ!)
        if table_exists(cursor, "users"):
            cursor.execute("""
                UPDATE users 
                SET team_name = NULL, 
                    league_name = NULL, 
                    division_id = 1
            """)
            if verbose:
                logger.info("Reset users season fields: team_name=NULL, league_name=NULL")
                
        # 7. Обнуление сезонных счетчиков в кошельках (БАЛАНС СОХРАНЯЕТСЯ 100%!)
        if table_exists(cursor, "user_wallets"):
            cursor.execute("""
                UPDATE user_wallets 
                SET bets_count = 0, 
                    bets_won = 0, 
                    total_wagered = 0, 
                    total_won = 0
            """)
            if verbose:
                logger.info("Reset user_wallets betting counters (balances preserved)")
                
        # 8. Сброс серий пари в user_progression (уровень и XP СОХРАНЯЮТСЯ!)
        # Серия входов сбрасывается вместе с last_active_date: иначе после
        # обнуления счётчика в базе остаётся «вчерашний» вход, и первый же заход
        # в новом сезоне продолжает прошлую серию вместо того, чтобы начать новую.
        if table_exists(cursor, "user_progression"):
            cursor.execute("""
                UPDATE user_progression 
                SET current_streak = 0, 
                    best_streak = 0,
                    login_streak = 0,
                    best_login_streak = 0,
                    last_active_date = NULL
            """)
            if verbose:
                logger.info("Reset user_progression streaks (level and XP preserved)")
                
        # 9. Очистка системного кэша медиа и временного конфига лабы
        if table_exists(cursor, "telegram_media_cache"):
            cursor.execute("DELETE FROM telegram_media_cache")
        if table_exists(cursor, "system_config"):
            cursor.execute("DELETE FROM system_config WHERE key LIKE 'lab_%'")
            
        return True, "Успешно очищено"
        
    except Exception as e:
        logger.error(f"Ошибка во время очистки: {e}")
        raise e


def verify_integrity(conn: sqlite3.Connection, summary_before: Dict[str, Any]) -> List[str]:
    """
    Run comprehensive post-purge verification suite:
    1. PRAGMA foreign_key_check
    2. PRAGMA integrity_check
    3. Verify all seasonal tables have 0 rows
    4. Verify users and wallets count match before
    5. Verify sum of balances matches before (0 coin leak)
    6. Verify coin_transactions count matches before
    7. Verify divisions architecture is preserved
    """
    cursor = conn.cursor()
    errors = []
    
    # 1. Foreign Key Check
    cursor.execute("PRAGMA foreign_key_check;")
    fk_violations = cursor.fetchall()
    if fk_violations:
        errors.append(f"❌ Нарушение Foreign Keys: обнаружено {len(fk_violations)} ошибок: {fk_violations[:5]}")
    else:
        logger.info("✅ PRAGMA foreign_key_check: OK (0 ошибок)")
        
    # 2. Integrity Check
    cursor.execute("PRAGMA integrity_check;")
    integrity_rows = cursor.fetchall()
    if not integrity_rows or integrity_rows[0][0] != "ok":
        errors.append(f"❌ PRAGMA integrity_check не прошёл: {integrity_rows}")
    else:
        logger.info("✅ PRAGMA integrity_check: OK")
        
    # 3. Проверка обнуления сезонных таблиц
    for tbl in SEASONAL_PURGE_TABLES:
        if table_exists(cursor, tbl):
            cnt = get_table_count(cursor, tbl)
            if cnt > 0:
                errors.append(f"❌ В сезонной таблице '{tbl}' остались записи ({cnt} шт.)")
                
    if not errors:
        logger.info("✅ Все сезонные таблицы полностью очищены (0 записей)")
        
    # 4. Проверка сохранения пользователей
    if table_exists(cursor, "users"):
        users_after = get_table_count(cursor, "users")
        users_before = summary_before["preserved"].get("users", 0)
        if users_after != users_before:
            errors.append(f"❌ Количество пользователей изменилось: было {users_before}, стало {users_after}")
        else:
            logger.info(f"✅ Пользователи полностью сохранены ({users_after} аккаунтов)")
            
    # 5. Проверка сохранения балансов монет (0 утечек)
    if table_exists(cursor, "user_wallets"):
        cursor.execute("SELECT COALESCE(SUM(balance), 0) FROM user_wallets")
        bal_after = cursor.fetchone()[0]
        bal_before = summary_before["total_balance"]
        if bal_after != bal_before:
            errors.append(f"❌ Сумма балансов изменилась! Было: {bal_before}, стало: {bal_after}")
        else:
            logger.info(f"✅ Балансы кошельков сохранены с точностью до 1 копейки: {bal_after:,} 🪙")
            
    # 6. Проверка финансовых транзакций
    if table_exists(cursor, "coin_transactions"):
        tx_after = get_table_count(cursor, "coin_transactions")
        tx_before = summary_before["preserved"].get("coin_transactions", 0)
        if tx_after != tx_before:
            errors.append(f"❌ Число транзакций изменилось: было {tx_before}, стало {tx_after}")
        else:
            logger.info(f"✅ История глобальных транзакций сохранена ({tx_after} записей)")
            
    # 7. Проверка дивизионов
    if table_exists(cursor, "divisions"):
        divs_count = get_table_count(cursor, "divisions")
        divs_before = summary_before["preserved"].get("divisions", 0)
        if divs_count == 0 or (divs_before > 0 and divs_count != divs_before):
            errors.append(f"❌ Архитектура дивизионов была повреждена: было {divs_before}, стало {divs_count}")
        else:
            logger.info(f"✅ Архитектура дивизионов сохранена ({divs_count} дивизионов готовы к новому сезону)")

    # 8. Проверка топиков дивизионов (строгое сохранение привязок к форуму Telegram)
    if table_exists(cursor, "division_topics"):
        topics_after = get_table_count(cursor, "division_topics")
        topics_before = summary_before["preserved"].get("division_topics", 0)
        if topics_after != topics_before:
            errors.append(
                f"❌ Таблица 'division_topics' изменилась: было {topics_before}, стало {topics_after}! "
                "Топики дивизионов должны быть строго сохранены."
            )
        else:
            logger.info(f"✅ Топики дивизионов полностью сохранены ({topics_after} топиков)")

    # 9. Проверка администраторов дивизионов
    if table_exists(cursor, "division_admins"):
        admins_after = get_table_count(cursor, "division_admins")
        admins_before = summary_before["preserved"].get("division_admins", 0)
        if admins_after != admins_before:
            errors.append(
                f"❌ Таблица 'division_admins' изменилась: было {admins_before}, стало {admins_after}!"
            )
        else:
            logger.info(f"✅ Администраторы дивизионов сохранены ({admins_after} записей)")

    return errors


def run(db_path: str, execute: bool, verbose: bool) -> int:
    """Main executor flow."""
    if not os.path.exists(db_path):
        logger.error(f"Файл базы данных не найден: {db_path}")
        return 1
        
    print("\n" + "=" * 80)
    print("🚀 LOGOVO.BET — ОЧИСТКА СТАРОГО СЕЗОНА")
    print(f"📁 База данных: {os.path.abspath(db_path)}")
    print(f"⚙️  Режим работы: {'🔴 EXECUTE (РЕАЛЬНОЕ УДАЛЕНИЕ)' if execute else '🟡 DRY-RUN (БЕЗ ИЗМЕНЕНИЙ)'}")
    print("=" * 80)
    
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    
    try:
        # Собираем начальное состояние
        summary_before = collect_audit_summary(conn)
        print_audit_table(summary_before, title="СОСТОЯНИЕ ДО ОЧИСТКИ")
        
        if not execute:
            print("=" * 80)
            print("ℹ️  РЕЖИМ DRY-RUN ЗАВЕРШЕН.")
            print("Никакие данные не были изменены или удалены.")
            print("Для выполнения реальной очистки запустите скрипт с флагом --execute:")
            print(f"   python {sys.argv[0]} --execute --db-path {db_path}")
            print("=" * 80 + "\n")
            return 0
            
        # Реальное удаление в транзакции
        print("⏳ Выполняется атомарное удаление старого сезона в транзакции...")
        with conn:
            execute_purge(conn, verbose=verbose)
            
        print("✅ Удаление завершено. Запуск валидации целостности...")
        
        # Валидация
        errors = verify_integrity(conn, summary_before)
        
        summary_after = collect_audit_summary(conn)
        print_audit_table(summary_after, title="СОСТОЯНИЕ ПОСЛЕ ОЧИСТКИ")
        
        if errors:
            print("=" * 80)
            print("❌ ОБНАРУЖЕНЫ ОШИБКИ ВАЛИДАЦИИ:")
            for err in errors:
                print(f"   {err}")
            print("=" * 80 + "\n")
            return 1
            
        print("=" * 80)
        print("🎉 ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ УСПЕШНО!")
        print("База данных очищена от старого сезона и полностью готова к запуску дивизионов.")
        print("=" * 80 + "\n")
        return 0
        
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")
        return 1
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Полная и безопасная очистка старого сезона Logovo.bet."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Выполнить реальное удаление данных (без этого флага работает в безопасном режиме dry-run)"
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="league.db",
        help="Путь к файлу SQLite базы данных (по умолчанию: league.db)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Подробный вывод процесса очистки"
    )
    
    args = parser.parse_args()
    exit_code = run(db_path=args.db_path, execute=args.execute, verbose=args.verbose)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
