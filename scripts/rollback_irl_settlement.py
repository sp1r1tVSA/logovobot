"""
scripts/rollback_irl_settlement.py

Скрипт для отката ошибочного расчёта IRL-матча (например, Spain — Czechia,
где по ошибке был выбран исход П2 вместо П1).

Поддерживает:
1. --mode rollback:
   - Списывает ошибочно выплаченные выигрыши со счетов игроков (user_wallets.balance, total_won, bets_won).
   - Записывает транзакцию компенсации в coin_transactions (отрицательная сумма).
   - Возвращает все ставки на этот матч в статус 'pending' (actual_payout = NULL).
   - Возвращает матч в статус 'closed' (result = NULL).
   - Удаляет зависшие уведомления из notification_events.
   - В результате матч в админке снова можно рассчитать начисто с правильным исходом.

2. --mode resettle:
   - Делает откат ошибочных выплат (по исходу away).
   - Сразу рассчитывает матч по верному исходу (по умолчанию --result home, --score 3:1).
   - Начисляет честные выигрыши игрокам, поставившим на home (Испания).
   - Фиксирует статус 'won' / 'lost' и обновляет матч до 'settled'.

3. Безопасность:
   - По умолчанию работает в режиме DRY-RUN (без записи).
   - Для применения изменений передайте флаг --apply.

Примеры использования:
  # Просмотр ситуации (dry-run):
  python scripts/rollback_irl_settlement.py --db server_league.db

  # Откат в статус 'closed' (чтобы перерассчитать в админ-панели):
  python scripts/rollback_irl_settlement.py --db server_league.db --mode rollback --apply

  # Автоматический перерасчет в пользу Испании (П1, 3:1):
  python scripts/rollback_irl_settlement.py --db server_league.db --mode resettle --result home --score 3:1 --apply
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Корень репозитория
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from time_utils import now_msk_str

SQL_NOW = "datetime('now', '+3 hours')"
IRL_TX_WIN = "irl_bet_won"
IRL_TX_REVERT = "irl_bet_won_reverted"


def find_default_db() -> str:
    candidates = [
        PROJECT_ROOT / "server_league.db",
        PROJECT_ROOT.parent / "server_league.db",
        PROJECT_ROOT / "league.db",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(PROJECT_ROOT / "league.db")


def parse_args():
    parser = argparse.ArgumentParser(description="Откат или перерасчёт ставок на IRL-матч")
    parser.add_argument("--db", default=None, help="Путь к файлу базы данных SQLite")
    parser.add_argument("--match-id", type=int, default=None, help="ID матча в irl_matches (если не указан, автопоиск)")
    parser.add_argument("--mode", choices=["rollback", "resettle", "info"], default="rollback",
                        help="Режим: rollback (вернуть в closed для перерасчёта руками), resettle (рассчитать заново), info (только инфо)")
    parser.add_argument("--result", choices=["home", "draw", "away"], default="home",
                        help="Правильный исход для режима resettle (default: home)")
    parser.add_argument("--score", default="3:1", help="Счёт матча, например 3:1 (для режима resettle)")
    parser.add_argument("--apply", action="store_true", help="Применить изменения (по умолчанию DRY-RUN)")
    return parser.parse_args()


def get_match(conn: sqlite3.Connection, match_id: int | None):
    conn.row_factory = sqlite3.Row
    if match_id is not None:
        row = conn.execute("SELECT * FROM irl_matches WHERE id = ?", (match_id,)).fetchone()
        return dict(row) if row else None

    # Поиск матча Spain vs Czechia или последнего рассчитанного
    rows = conn.execute("""
        SELECT * FROM irl_matches
        WHERE status = 'settled'
        ORDER BY id DESC
    """).fetchall()

    if not rows:
        # Попробуем найти по названию команд
        rows = conn.execute("""
            SELECT * FROM irl_matches
            WHERE home LIKE '%Spain%' OR home LIKE '%Испан%' OR away LIKE '%Czech%' OR away LIKE '%Чех%'
            ORDER BY id DESC
        """).fetchall()

    if not rows:
        return None

    if len(rows) == 1:
        return dict(rows[0])

    # Если несколько, приоритет тому, где Spain — Czechia
    for r in rows:
        if "spain" in r["home"].lower() or "испан" in r["home"].lower():
            return dict(r)

    return dict(rows[0])


def main():
    args = parse_args()
    db_path = args.db or find_default_db()

    if not os.path.exists(db_path):
        print(f"❌ База данных не найдена по пути: {db_path}")
        print("Укажите путь через флаг --db, например: --db C:\\Users\\...\\server_league.db")
        sys.exit(1)

    print(f"📁 Подключение к базе данных: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    match = get_match(conn, args.match_id)
    if not match:
        print("❌ Не удалось найти подходящий IRL-матч.")
        sys.exit(1)

    match_id = match["id"]
    home = match["home"]
    away = match["away"]
    status = match["status"]
    current_result = match["result"]
    home_goals = match["home_goals"]
    away_goals = match["away_goals"]

    print("=" * 60)
    print(f"⚽ Матч #{match_id}: {home} — {away}")
    print(f"   Текущий статус: {status}")
    print(f"   Записанный исход: {current_result} (Счёт: {home_goals}:{away_goals})")
    print(f"   Время начала: {match['kickoff_at']} МСК")
    print("=" * 60)

    # Получаем ставки на этот матч
    bets = conn.execute("""
        SELECT b.*, u.username, u.team_name, w.balance, w.total_won, w.bets_won
        FROM irl_bets b
        LEFT JOIN users u ON u.telegram_id = b.user_id
        LEFT JOIN user_wallets w ON w.user_id = b.user_id
        WHERE b.irl_match_id = ?
        ORDER BY b.id ASC
    """, (match_id,)).fetchall()

    print(f"📊 Всего ставок на матч: {len(bets)}")
    if not bets:
        print("   На этот матч не было ставок игроков.")
    else:
        for b in bets:
            uname = f"@{b['username']}" if b["username"] else f"id:{b['user_id']}"
            tname = f" [{b['team_name']}]" if b["team_name"] else ""
            print(f"   • Ставка #{b['id']} от {uname}{tname}: исход={b['outcome']}, "
                  f"сумма={b['amount']} 🪙, кэф={b['odd']}, статус={b['status']}, "
                  f"выплата={b['actual_payout']}")

    if args.mode == "info":
        return

    # Разбор счёта для resettle
    resettle_home_goals, resettle_away_goals = home_goals, away_goals
    if args.score and ":" in args.score:
        parts = args.score.split(":")
        try:
            resettle_home_goals = int(parts[0].strip())
            resettle_away_goals = int(parts[1].strip())
        except ValueError:
            pass

    # Анализ действий
    revert_actions = []
    payout_actions = []

    for b in bets:
        user_id = b["user_id"]
        uname = f"@{b['username']}" if b["username"] else f"id:{user_id}"
        actual_payout = int(b["actual_payout"] or 0)

        # 1. Если ставка ошибочно выиграла (например, исход away при победе Испании):
        if b["status"] == "won" and actual_payout > 0:
            revert_actions.append({
                "bet_id": b["id"],
                "user_id": user_id,
                "username": uname,
                "amount_to_deduct": actual_payout,
                "curr_balance": b["balance"] or 0,
            })

        # 2. Если режим resettle, и ставка на правильный исход:
        if args.mode == "resettle":
            if b["outcome"] == args.result:
                pot_win = int(b["potential_win"])
                payout_actions.append({
                    "bet_id": b["id"],
                    "user_id": user_id,
                    "username": uname,
                    "amount_to_credit": pot_win,
                    "curr_balance": b["balance"] or 0,
                })

    print("\n📋 ПЛАН ДЕЙСТВИЙ:")
    if revert_actions:
        print(f"🔻 Списание ошибочно начисленных выигрышей ({len(revert_actions)} ставок):")
        for act in revert_actions:
            new_bal = act["curr_balance"] - act["amount_to_deduct"]
            print(f"   - {act['username']}: -{act['amount_to_deduct']} 🪙 (Баланс: {act['curr_balance']} -> {new_bal})")
    else:
        print("🔻 Нет ошибочно выплаченных выигрышей для списания.")

    if args.mode == "resettle":
        if payout_actions:
            print(f"🔺 Начисление верных выигрышей по исходу '{args.result}' ({len(payout_actions)} ставок):")
            for act in payout_actions:
                new_bal = act["curr_balance"] + act["amount_to_credit"]
                print(f"   + {act['username']}: +{act['amount_to_credit']} 🪙 (Баланс: {act['curr_balance']} -> {new_bal})")
        else:
            print(f"🔺 Нет ставок на верный исход '{args.result}'.")

    if args.mode == "rollback":
        print(f"\n🔄 Статус матча #{match_id} будет изменён на 'closed' (не рассчитан).")
        print("   Все ставки на этот матч будут возвращены в статус 'pending'.")
        print("   После этого вы сможете рассчитать матч в веб-панели админа с верным исходом!")
    elif args.mode == "resettle":
        print(f"\n✅ Статус матча #{match_id} будет изменён на 'settled' с исходом '{args.result}' ({resettle_home_goals}:{resettle_away_goals}).")

    if not args.apply:
        print("\n" + "=" * 60)
        print("⚠️ ВНИМАНИЕ: Запуск в режиме DRY-RUN (изменения НЕ применены).")
        print("Чтобы применить изменения, добавьте флаг --apply:")
        if args.mode == "rollback":
            print(f"  python scripts/rollback_irl_settlement.py --db \"{db_path}\" --match-id {match_id} --mode rollback --apply")
        else:
            print(f"  python scripts/rollback_irl_settlement.py --db \"{db_path}\" --match-id {match_id} --mode resettle --result {args.result} --score {resettle_home_goals}:{resettle_away_goals} --apply")
        print("=" * 60)
        return

    # Применение изменений в транзакции
    print("\n⏳ Применение изменений в базе данных...")
    cursor = conn.cursor()

    try:
        # 1. Откат ошибочных выплат
        for act in revert_actions:
            u_id = act["user_id"]
            deduct = act["amount_to_deduct"]
            b_id = act["bet_id"]

            cursor.execute("""
                UPDATE user_wallets
                SET balance = balance - ?,
                    total_won = MAX(0, total_won - ?),
                    bets_won = MAX(0, bets_won - 1),
                    updated_at = datetime('now', '+3 hours')
                WHERE user_id = ?
            """, (deduct, deduct, u_id))

            cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
            w_row = cursor.fetchone()
            bal_after = w_row["balance"] if w_row else None

            cursor.execute("""
                INSERT INTO coin_transactions
                    (user_id, amount, transaction_type, reference_id, reference_type, balance_after, created_at)
                VALUES (?, ?, ?, ?, 'irl_bet', ?, datetime('now', '+3 hours'))
            """, (u_id, -deduct, IRL_TX_REVERT, b_id, bal_after))

        # 2. Очистка старых уведомлений по ставкам этого матча
        # Это необходимо, чтобы при новом расчёте INSERT OR IGNORE не блокировался старыми записями
        for b in bets:
            cursor.execute("""
                DELETE FROM notification_events
                WHERE source_event_id LIKE ?
            """, (f"ibet_{b['id']}%",))

        # 3. Отправка уведомления о корректировке игрокам, у которых аннулирован ошибочный выигрыш
        for act in revert_actions:
            rev_title = "⚠️ Корректировка ставки на реальный футбол"
            rev_body = (
                f"Матч <b>{home} — {away}</b> завершился со счётом <b>{resettle_home_goals}:{resettle_away_goals}</b> (Победа: {home}).\n"
                f"Ошибочно начисленный выигрыш по исходу {away} (−{act['amount_to_deduct']} 🪙) был аннулирован."
            )
            cursor.execute("""
                INSERT OR IGNORE INTO notification_events
                    (user_id, event_type, source_event_id, title, body, priority, status, created_at)
                SELECT ?, 'BET_SETTLED', ?, ?, ?, 'high', 'pending', datetime('now', '+3 hours')
                WHERE EXISTS (SELECT 1 FROM users WHERE telegram_id = ?)
            """, (act["user_id"], f"ibet_{act['bet_id']}_rev", rev_title, rev_body, act["user_id"]))

        if args.mode == "rollback":
            # Возврат всех ставок в pending
            cursor.execute("""
                UPDATE irl_bets
                SET status = 'pending', actual_payout = NULL, settled_at = NULL
                WHERE irl_match_id = ?
            """, (match_id,))

            # Возврат матча в closed
            cursor.execute("""
                UPDATE irl_matches
                SET status = 'closed', result = NULL, settled_by = NULL, settled_at = NULL
                WHERE id = ?
            """, (match_id,))

        elif args.mode == "resettle":
            # Начисление честных выигрышей
            for act in payout_actions:
                u_id = act["user_id"]
                credit = act["amount_to_credit"]
                b_id = act["bet_id"]

                cursor.execute("""
                    UPDATE user_wallets
                    SET balance = balance + ?,
                        total_won = total_won + ?,
                        bets_won = bets_won + 1,
                        updated_at = datetime('now', '+3 hours')
                    WHERE user_id = ?
                """, (credit, credit, u_id))

                cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (u_id,))
                w_row = cursor.fetchone()
                bal_after = w_row["balance"] if w_row else None

                cursor.execute("""
                    INSERT INTO coin_transactions
                        (user_id, amount, transaction_type, reference_id, reference_type, balance_after, created_at)
                    VALUES (?, ?, ?, ?, 'irl_bet', ?, datetime('now', '+3 hours'))
                """, (u_id, credit, IRL_TX_WIN, b_id, bal_after))

            # Обновление статусов ставок и постановка уведомлений победителям
            for b in bets:
                if b["outcome"] == args.result:
                    payout = int(b["potential_win"])
                    cursor.execute("""
                        UPDATE irl_bets
                        SET status = 'won', actual_payout = ?, settled_at = datetime('now', '+3 hours')
                        WHERE id = ?
                    """, (payout, b["id"]))

                    win_title = f"✅ Ставка на реальный матч выиграла: +{payout} 🪙"
                    win_body = (
                        f"{home} — {away}: <b>Победа {home}</b> @ {float(b['odd']):.2f}\n"
                        f"(Произведён корректный перерасчёт матча)"
                    )
                    cursor.execute("""
                        INSERT OR IGNORE INTO notification_events
                            (user_id, event_type, source_event_id, title, body, priority, status, created_at)
                        SELECT ?, 'BET_SETTLED', ?, ?, ?, 'high', 'pending', datetime('now', '+3 hours')
                        WHERE EXISTS (SELECT 1 FROM users WHERE telegram_id = ?)
                    """, (b["user_id"], f"ibet_{b['id']}", win_title, win_body, b["user_id"]))
                else:
                    cursor.execute("""
                        UPDATE irl_bets
                        SET status = 'lost', actual_payout = 0, settled_at = datetime('now', '+3 hours')
                        WHERE id = ?
                    """, (b["id"],))

            # Обновление матча
            cursor.execute("""
                UPDATE irl_matches
                SET status = 'settled', result = ?, home_goals = ?, away_goals = ?,
                    settled_at = datetime('now', '+3 hours')
                WHERE id = ?
            """, (args.result, resettle_home_goals, resettle_away_goals, match_id))

        conn.commit()
        print("✅ УСПЕШНО! Все изменения сохранены в базе данных.")
    except Exception as e:
        conn.rollback()
        print(f"❌ ОШИБКА: Изменения отменены (rollback). Причина: {e}")
        raise


if __name__ == "__main__":
    main()
