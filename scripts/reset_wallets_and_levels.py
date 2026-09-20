#!/usr/bin/env python3
"""
Reset registered players' wallet balances and progression levels to the
project defaults (config.INITIAL_WALLET_BALANCE coins, level 1 / 0 XP).

Dry-run by default — prints exactly what WOULD change. Pass --apply to execute
everything in one atomic transaction.

Targets, by default, только зарегистрированных игроков — строки `users`
с непустым `team_name` (коуч с клубом). Расширяется флагами --all-users,
--all-wallets, --division, --user-id, --exclude-user-id.

--all-wallets идёт не от `users`, а от самих кошельков: берёт все user_id из
`user_wallets` ∪ `user_progression`, включая «сирот» — id без строки в `users`
(так бывает после чистки ростера: коуч удалён, а его кошелёк с монетами остался).
Это единственный режим, который вычищает монеты у таких id.

Что делает --apply:
  * user_wallets.balance  → config.INITIAL_WALLET_BALANCE
  * коррекция баланса пишется в coin_transactions как 'balance_reset'
    (amount = дельта, может быть отрицательной; balance_after = новый баланс)
  * user_progression: level → 1, current_xp → 0, total_xp_earned → 0,
    equipped_title → 'Новичок' (звание выводится из уровня в add_user_xp,
    иначе игрок 1-го уровня остался бы «Легендой Логова»)

Чего НЕ делает:
  * не создаёт кошельки и прогресс тем, у кого их нет — такой игрок получит
    дефолтные значения сам при первом входе (get_or_create_wallet /
    get_or_create_progression)
  * не трогает счётчики ставок, серии и квесты/достижения — только по флагам
    --reset-stats / --reset-streaks / --reset-quests (или --reset-achievements)
  * не отменяет и не рассчитывает ставки

Порядок запуска на сервере (бота лучше остановить — иначе он может принять
ставку между отчётом и применением):

    cd /path/to/bot
    cp league.db "league.db.bak-$(date +%F-%H%M)"
    python3 scripts/reset_wallets_and_levels.py
    python3 scripts/reset_wallets_and_levels.py --apply

Путь к БД берётся из config.DB_PATH (env LEAGUE_SQLITE_PATH).
"""
import argparse
import sys
from pathlib import Path

# Add project root to sys.path for standalone execution
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
import database

TARGET_BALANCE = config.INITIAL_WALLET_BALANCE

# Дефолты прогресса — те же, что ставит database.get_or_create_progression().
DEFAULT_LEVEL = 1
DEFAULT_XP = 0
DEFAULT_TITLE = "Новичок"
# Серия = 0, а не 1: last_active_date тут сбрасывается в NULL, то есть входов
# ещё не было, и «день подряд» считать не с чего.
DEFAULT_STREAK = 0
DEFAULT_SHIELDS = 1


def _id_filters(args: argparse.Namespace, id_column: str) -> tuple[list[str], list]:
    """--user-id / --exclude-user-id — общие для обоих режимов выборки."""
    where: list[str] = []
    params: list = []

    if args.user_id:
        placeholders = ",".join("?" for _ in args.user_id)
        where.append(f"{id_column} IN ({placeholders})")
        params.extend(args.user_id)

    if args.exclude_user_id:
        placeholders = ",".join("?" for _ in args.exclude_user_id)
        where.append(f"{id_column} NOT IN ({placeholders})")
        params.extend(args.exclude_user_id)

    return where, params


def build_target_query(args: argparse.Namespace) -> tuple[str, list]:
    """Return (WHERE clause, params) selecting the users to reset."""
    where, params = _id_filters(args, "u.telegram_id")

    if not args.user_id and not args.all_users:
        # «Зарегистрированный игрок» = коуч с клубом.
        where.append("u.team_name IS NOT NULL AND TRIM(u.team_name) != ''")

    if args.division is not None:
        where.append("u.division_id = ?")
        params.append(args.division)

    return (" AND ".join(where) if where else "1=1"), params


def build_wallet_query(args: argparse.Namespace) -> tuple[str, list]:
    """WHERE для режима --all-wallets: фильтр по id + опционально дивизион."""
    where, params = _id_filters(args, "i.user_id")

    if args.division is not None:
        # У «сирот» строки в users нет, поэтому фильтр по дивизиону их отбросит.
        where.append("u.division_id = ?")
        params.append(args.division)

    return (" AND ".join(where) if where else "1=1"), params


def fetch_targets(args: argparse.Namespace) -> list[dict]:
    if args.all_wallets:
        return _fetch_from_wallets(args)
    return _fetch_from_users(args)


def _fetch_from_users(args: argparse.Namespace) -> list[dict]:
    where_sql, params = build_target_query(args)
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT u.telegram_id, u.username, u.team_name, u.division_id,
                   1 AS user_exists,
                   w.user_id AS wallet_exists, w.balance,
                   w.total_wagered, w.total_won, w.bets_count, w.bets_won,
                   p.user_id AS prog_exists, p.level, p.current_xp,
                   p.total_xp_earned, p.equipped_title
            FROM users u
            LEFT JOIN user_wallets w ON w.user_id = u.telegram_id
            LEFT JOIN user_progression p ON p.user_id = u.telegram_id
            WHERE {where_sql}
            ORDER BY u.division_id, u.team_name, u.telegram_id
        """, params)
        return [dict(r) for r in cursor.fetchall()]


def _fetch_from_wallets(args: argparse.Namespace) -> list[dict]:
    """Выборка от кошельков и прогресса — ловит id, которых нет в users."""
    where_sql, params = build_wallet_query(args)
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            WITH ids(user_id) AS (
                SELECT user_id FROM user_wallets
                UNION
                SELECT user_id FROM user_progression
            )
            SELECT i.user_id AS telegram_id, u.username, u.team_name, u.division_id,
                   u.telegram_id AS user_exists,
                   w.user_id AS wallet_exists, w.balance,
                   w.total_wagered, w.total_won, w.bets_count, w.bets_won,
                   p.user_id AS prog_exists, p.level, p.current_xp,
                   p.total_xp_earned, p.equipped_title
            FROM ids i
            LEFT JOIN users u ON u.telegram_id = i.user_id
            LEFT JOIN user_wallets w ON w.user_id = i.user_id
            LEFT JOIN user_progression p ON p.user_id = i.user_id
            WHERE {where_sql}
            ORDER BY w.balance DESC, i.user_id
        """, params)
        return [dict(r) for r in cursor.fetchall()]


def fetch_pending_bets(user_ids: list[int]) -> dict[int, dict]:
    """Pending coupons per user: their stake is already deducted, and a later
    settlement would credit a payout on top of the reset balance."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT user_id, COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS staked
            FROM user_bets
            WHERE status = 'pending' AND user_id IN ({placeholders})
            GROUP BY user_id
        """, user_ids)
        return {r["user_id"]: dict(r) for r in cursor.fetchall()}


def fetch_achievements_counts(user_ids: list[int]) -> dict[int, int]:
    """Количество открытых квестов/достижений на каждого пользователя."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" for _ in user_ids)
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT user_id, COUNT(*) AS cnt
            FROM user_achievements
            WHERE user_id IN ({placeholders})
            GROUP BY user_id
        """, user_ids)
        return {r["user_id"]: r["cnt"] for r in cursor.fetchall()}


def describe(row: dict) -> str:
    who = f"@{row['username']}" if row.get("username") else str(row["telegram_id"])
    club = row.get("team_name") or "без клуба"
    div = row.get("division_id")
    orphan = "" if row.get("user_exists") else ", НЕТ строки в users"
    return (f"{who} [{club}] (див. {div if div is not None else '—'}, "
            f"id {row['telegram_id']}{orphan})")


def apply_reset(rows: list[dict], args: argparse.Namespace) -> dict:
    """Reset every target in ONE transaction: either all of it lands, or none."""
    stats = {
        "wallets": 0,
        "progressions": 0,
        "coins_removed": 0,
        "coins_added": 0,
        "achievements_cleared": 0,
    }

    with database.transaction() as conn:
        cursor = conn.cursor()
        for row in rows:
            uid = row["telegram_id"]

            if row["wallet_exists"] is not None:
                old_balance = row["balance"] or 0
                delta = TARGET_BALANCE - old_balance

                if args.reset_stats:
                    cursor.execute("""
                        UPDATE user_wallets
                        SET balance = ?, total_wagered = 0, total_won = 0,
                            bets_count = 0, bets_won = 0, last_bonus_at = NULL,
                            updated_at = datetime('now', '+3 hours')
                        WHERE user_id = ?
                    """, (TARGET_BALANCE, uid))
                else:
                    cursor.execute("""
                        UPDATE user_wallets
                        SET balance = ?, updated_at = datetime('now', '+3 hours')
                        WHERE user_id = ?
                    """, (TARGET_BALANCE, uid))

                if delta != 0:
                    # Ledger-запись, чтобы правка баланса не выглядела как утечка монет.
                    cursor.execute("""
                        INSERT INTO coin_transactions
                            (user_id, amount, transaction_type, reference_type, balance_after, created_at)
                        VALUES (?, ?, 'balance_reset', 'admin_script', ?, datetime('now', '+3 hours'))
                    """, (uid, delta, TARGET_BALANCE))
                    if delta < 0:
                        stats["coins_removed"] += -delta
                    else:
                        stats["coins_added"] += delta
                stats["wallets"] += 1

            if row["prog_exists"] is not None:
                if args.reset_streaks:
                    cursor.execute("""
                        UPDATE user_progression
                        SET level = ?, current_xp = ?, total_xp_earned = ?,
                            equipped_title = ?, current_streak = ?, best_streak = ?,
                            login_streak = ?, best_login_streak = ?,
                            streak_shields = ?, last_active_date = NULL,
                            updated_at = datetime('now', '+3 hours')
                        WHERE user_id = ?
                    """, (DEFAULT_LEVEL, DEFAULT_XP, DEFAULT_XP, DEFAULT_TITLE,
                          DEFAULT_STREAK, DEFAULT_STREAK,
                          DEFAULT_STREAK, DEFAULT_STREAK, DEFAULT_SHIELDS, uid))
                else:
                    cursor.execute("""
                        UPDATE user_progression
                        SET level = ?, current_xp = ?, total_xp_earned = ?,
                            equipped_title = ?, updated_at = datetime('now', '+3 hours')
                        WHERE user_id = ?
                    """, (DEFAULT_LEVEL, DEFAULT_XP, DEFAULT_XP, DEFAULT_TITLE, uid))
                stats["progressions"] += 1

            if getattr(args, "reset_quests", False):
                cursor.execute("DELETE FROM user_achievements WHERE user_id = ?", (uid,))
                stats["achievements_cleared"] += cursor.rowcount

    return stats


def verify(rows: list[dict], args: argparse.Namespace) -> list[str]:
    """Read the affected rows back and report anything that is still off."""
    problems: list[str] = []
    ids = [r["telegram_id"] for r in rows]
    if not ids:
        return problems

    placeholders = ",".join("?" for _ in ids)
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT user_id, balance FROM user_wallets
            WHERE user_id IN ({placeholders}) AND balance != ?
        """, [*ids, TARGET_BALANCE])
        for r in cursor.fetchall():
            problems.append(f"кошелёк {r['user_id']}: баланс {r['balance']}, ожидался {TARGET_BALANCE}")

        cursor.execute(f"""
            SELECT user_id, level, current_xp, total_xp_earned FROM user_progression
            WHERE user_id IN ({placeholders})
              AND (level != ? OR current_xp != ? OR total_xp_earned != ?)
        """, [*ids, DEFAULT_LEVEL, DEFAULT_XP, DEFAULT_XP])
        for r in cursor.fetchall():
            problems.append(
                f"прогресс {r['user_id']}: ур. {r['level']}, XP {r['current_xp']}/{r['total_xp_earned']}"
            )

        if getattr(args, "reset_quests", False):
            cursor.execute(f"""
                SELECT user_id, COUNT(*) AS cnt FROM user_achievements
                WHERE user_id IN ({placeholders})
                GROUP BY user_id
            """, ids)
            for r in cursor.fetchall():
                if r["cnt"] > 0:
                    problems.append(f"квесты {r['user_id']}: осталось {r['cnt']} записей в user_achievements")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Сброс балансов и уровней игроков до дефолта ({TARGET_BALANCE} 🪙, ур. 1)"
    )
    parser.add_argument("--apply", action="store_true",
                        help="Реально применить изменения (по умолчанию dry-run)")
    parser.add_argument("--all-users", action="store_true",
                        help="Все строки users, включая тех, у кого нет клуба")
    parser.add_argument("--all-wallets", action="store_true",
                        help="Идти от кошельков и прогресса, а не от users: захватывает "
                             "и «сирот» — id без строки в users")
    parser.add_argument("--division", type=int, default=None,
                        help="Только указанный дивизион (по division_id)")
    parser.add_argument("--user-id", type=int, action="append", default=[],
                        help="Только конкретный telegram_id (можно указать несколько раз)")
    parser.add_argument("--exclude-user-id", type=int, action="append", default=[],
                        help="Исключить telegram_id, например тестовый кошелёк "
                             "(можно указать несколько раз)")
    parser.add_argument("--reset-stats", action="store_true",
                        help="Дополнительно обнулить счётчики ставок и таймер дневного бонуса")
    parser.add_argument("--reset-streaks", action="store_true",
                        help="Дополнительно сбросить серии входов и щиты до дефолта")
    parser.add_argument("--reset-quests", "--reset-achievements", action="store_true",
                        dest="reset_quests",
                        help="Дополнительно сбросить все полученные квесты и достижения (очищает user_achievements)")
    parser.add_argument("--allow-pending", action="store_true",
                        help="Не прерываться, если у игроков есть нерассчитанные купоны")
    args = parser.parse_args()

    print(f"База: {config.DB_PATH}")
    print(f"Целевой баланс: {TARGET_BALANCE} 🪙 (config.INITIAL_WALLET_BALANCE)")
    print(f"Целевой прогресс: ур. {DEFAULT_LEVEL}, XP {DEFAULT_XP}, звание «{DEFAULT_TITLE}»")
    if args.all_wallets:
        source = "user_wallets ∪ user_progression (включая id без строки в users)"
    elif args.all_users or args.user_id:
        source = "users — все попавшие под фильтр"
    else:
        source = "users с непустым team_name"
    print(f"Выборка: {source}")

    rows = fetch_targets(args)
    if not rows:
        print("\nПод выборку не попал ни один игрок — нечего делать.")
        return 0

    pending = fetch_pending_bets([r["telegram_id"] for r in rows])
    ach_counts = fetch_achievements_counts([r["telegram_id"] for r in rows]) if args.reset_quests else {}

    mode = "ПРИМЕНЕНИЕ" if args.apply else "DRY-RUN (ничего не изменено)"
    print(f"\n=== {mode} ===")
    print(f"Игроков в выборке: {len(rows)}\n")

    no_wallet: list[dict] = []
    no_prog: list[dict] = []
    delta_sum = 0

    for row in rows:
        line = f"  • {describe(row)}"
        if row["wallet_exists"] is None:
            no_wallet.append(row)
            line += "\n      баланс: кошелька нет — получит дефолт при первом входе"
        else:
            old_balance = row["balance"] or 0
            delta = TARGET_BALANCE - old_balance
            delta_sum += delta
            mark = "без изменений" if delta == 0 else f"{delta:+d}"
            line += f"\n      баланс: {old_balance} → {TARGET_BALANCE} ({mark})"

        if row["prog_exists"] is None:
            no_prog.append(row)
            line += "\n      прогресс: записи нет — будет создана с дефолтом при первом входе"
        else:
            line += (f"\n      прогресс: ур. {row['level']} / XP {row['total_xp_earned']}"
                     f" «{row['equipped_title']}» → ур. {DEFAULT_LEVEL} / XP {DEFAULT_XP}"
                     f" «{DEFAULT_TITLE}»")

        if args.reset_quests:
            ac_cnt = ach_counts.get(row["telegram_id"], 0)
            line += f"\n      квесты: {ac_cnt} получено → 0 (будут очищены)"

        p = pending.get(row["telegram_id"])
        if p:
            line += f"\n      ⚠ нерассчитанных купонов: {p['cnt']} на {p['staked']} 🪙"

        print(line)

    if pending and not args.allow_pending:
        total_cnt = sum(p["cnt"] for p in pending.values())
        print(f"\n❌ ОСТАНОВЛЕНО: нерассчитанных купонов у игроков выборки: {total_cnt}.")
        print("   Ставка уже списана, а выплата придёт ПОСЛЕ сброса — баланс уедет выше дефолта.")
        print("   Дождитесь расчёта тура либо запустите с --allow-pending, если это устраивает.")
        return 2

    print("\n=== ИТОГИ ===")
    print(f"Кошельков к обновлению: {len(rows) - len(no_wallet)}")
    print(f"Записей прогресса к обновлению: {len(rows) - len(no_prog)}")
    print(f"Итоговое изменение монет в обороте: {delta_sum:+d} 🪙")
    if no_wallet:
        print(f"Без кошелька (пропущены): {len(no_wallet)}")
    if no_prog:
        print(f"Без записи прогресса (пропущены): {len(no_prog)}")
    orphans = [r for r in rows if not r.get("user_exists")]
    if orphans:
        print(f"Из них без строки в users («сироты»): {len(orphans)}")

    if not args.apply:
        print("\nЭто был dry-run. Для применения запустите с флагом --apply")
        return 0

    stats = apply_reset(rows, args)
    print(f"\nОбновлено кошельков: {stats['wallets']}")
    print(f"Обновлено записей прогресса: {stats['progressions']}")
    print(f"Изъято монет: {stats['coins_removed']} 🪙, начислено: {stats['coins_added']} 🪙")
    if args.reset_quests:
        print(f"Сброшено записей квестов/достижений: {stats['achievements_cleared']}")

    problems = verify(rows, args)
    if problems:
        print("\n❌ ПРОВЕРКА НЕ ПРОЙДЕНА:")
        for p in problems:
            print(f"  • {p}")
        return 1

    print("\n✅ Проверка пройдена: у всех затронутых игроков дефолтный баланс и уровень.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
