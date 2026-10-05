"""Игроки с нулём монет и без открытых купонов — те, кому нужно пособие.

Показывает, кто сейчас в тупике: баланс ровно 0 и нет ни одного неразыгранного купона
(обычного `user_bets` или IRL `irl_bets`; долгосрочные `outright_bets` не считаются —
так же, как в `database.claim_bailout`). Для каждого указывает, может ли он забрать
пособие прямо сейчас или ещё на кулдауне.

Использование:

    python scripts/check_zero_balance.py                          # config.DB_PATH
    python scripts/check_zero_balance.py --db ../server_league.db # снимок боевой базы
    python scripts/check_zero_balance.py --all                    # + нулевые, у кого есть купоны

exit code 1, если такие игроки есть, 0 — если нет.

Скрипт **только читает**: соединение закрыто авторизатором SQLite, запись отклоняется
на уровне драйвера.
"""

import argparse
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
# Скрипт могут запустить скопированным в /tmp — тогда config.py ищем в текущей папке.
sys.path.insert(1, os.getcwd())

import config  # noqa: E402

_READ_ONLY_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_TRANSACTION,
})


def open_read_only(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"Базы нет: {db_path}")
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(
        lambda action, *_: sqlite3.SQLITE_OK if action in _READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY
    )
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def find_zero_balance(conn: sqlite3.Connection, cooldown_days: int) -> list[dict]:
    """Все кошельки с балансом 0 и сведения о купонах и последнем пособии."""
    irl_open = (
        "(SELECT COUNT(*) FROM irl_bets b WHERE b.user_id = w.user_id AND b.status = 'pending')"
        if has_table(conn, "irl_bets") else "0"
    )
    has_users = has_table(conn, "users")
    name_expr = "u.username" if has_users else "NULL"
    team_expr = "u.team_name" if has_users else "NULL"
    join_users = "LEFT JOIN users u ON u.telegram_id = w.user_id" if has_users else ""
    rows = conn.execute(
        f"""
        SELECT w.user_id,
               {name_expr} AS username,
               {team_expr} AS team_name,
               (SELECT COUNT(*) FROM user_bets b
                 WHERE b.user_id = w.user_id AND b.status = 'pending') AS open_bets,
               {irl_open} AS open_irl,
               (SELECT MAX(t.created_at) FROM coin_transactions t
                 WHERE t.user_id = w.user_id AND t.transaction_type = 'bailout') AS last_bailout,
               (SELECT datetime(MAX(t.created_at), ?) FROM coin_transactions t
                 WHERE t.user_id = w.user_id AND t.transaction_type = 'bailout') AS next_bailout,
               datetime('now', '+3 hours') AS now_msk
        FROM user_wallets w
        {join_users}
        WHERE w.balance = 0
        ORDER BY w.user_id
        """,
        (f"+{cooldown_days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def classify(row: dict) -> str:
    if row["open_bets"] or row["open_irl"]:
        return "open_bets"
    if row["next_bailout"] and row["next_bailout"] > row["now_msk"]:
        return "cooldown"
    return "stuck"


def main() -> int:
    parser = argparse.ArgumentParser(description="Игроки с 0 монет без купонов (read-only)")
    parser.add_argument("--db", default=config.DB_PATH, help="путь к league.db")
    parser.add_argument("--all", action="store_true", help="показать и нулевых с открытыми купонами")
    args = parser.parse_args()

    conn = open_read_only(args.db)
    try:
        # На сервере до выкладки пособия в config этих настроек ещё нет.
        rows = find_zero_balance(conn, getattr(config, "BAILOUT_COOLDOWN_DAYS", 7))
    finally:
        conn.close()

    groups: dict[str, list[dict]] = {"stuck": [], "cooldown": [], "open_bets": []}
    for row in rows:
        groups[classify(row)].append(row)

    print(f"База: {args.db}")
    print(f"Кошельков с 0 🪙: {len(rows)}")
    print(f"  без купонов, пособие можно забрать сейчас: {len(groups['stuck'])}")
    print(f"  без купонов, пособие уже брали (кулдаун):  {len(groups['cooldown'])}")
    print(f"  с открытыми купонами (ждут расчёта):       {len(groups['open_bets'])}")

    def show(title: str, items: list[dict]) -> None:
        if not items:
            return
        print(f"\n{title}")
        for r in items:
            who = f"@{r['username']}" if r["username"] else "—"
            team = r["team_name"] or "—"
            extra = ""
            if r["next_bailout"] and r["next_bailout"] > r["now_msk"]:
                extra = f"  следующее пособие с {r['next_bailout'][:16]}"
            if r["open_bets"] or r["open_irl"]:
                extra = f"  купонов: {r['open_bets']} + IRL {r['open_irl']}"
            print(f"  {r['user_id']:<12} {who:<24} {team}{extra}")

    show("Без купонов, можно забрать пособие:", groups["stuck"])
    show("Без купонов, кулдаун пособия:", groups["cooldown"])
    if args.all:
        show("С открытыми купонами:", groups["open_bets"])

    return 1 if (groups["stuck"] or groups["cooldown"]) else 0


if __name__ == "__main__":
    sys.exit(main())
