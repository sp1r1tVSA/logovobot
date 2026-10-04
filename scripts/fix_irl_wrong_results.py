#!/usr/bin/env python3
"""
scripts/fix_irl_wrong_results.py

Находит рассчитанные IRL-матчи, у которых записанный исход противоречит счёту, и
исправляет выплаты. Причина — баг формы «Рассчитать матч» в Mini App: все три
радио-кнопки (П1 / Х / П2) читались как одно поле, и на сервер всегда уходил `away`
(исправлено вместе с проверкой «исход ↔ счёт» в `handle_panel_irl_settle`).

Что делает с каждым найденным матчем (одна транзакция на матч):
  • ставка на верный исход, помеченная `lost`  → `won`, игроку начисляется `potential_win`;
  • ставка на неверный исход, помеченная `won` → `lost`, выплата списывается обратно;
  • `refunded` и `pending` не трогает; матч получает верный `result`;
  • каждая выплата — своя строка `coin_transactions` (`irl_bet_won` / `irl_bet_won_reverted`),
    игрокам уходит уведомление о корректировке.
Повторный запуск безопасен: действие определяется статусом ставки, поэтому у уже
исправленного матча делать нечего.

Баланс игрока может уйти в минус, если он уже потратил неверный выигрыш — такие случаи
скрипт помечает в плане, но не пропускает: это долг, а не повод оставить выплату.

Матчи без записанного счёта (админ оставил поля пустыми) проверить нечем: они выводятся
отдельным списком, а исправить такой матч можно только явным `--set ID=home|draw|away`.

Режимы:

  1. Dry-run (по умолчанию) — только план, база открыта на чтение:
       python scripts/fix_irl_wrong_results.py --db ..\\server_league.db

  2. Применение:
       python scripts/fix_irl_wrong_results.py --db ..\\server_league.db --apply

  3. Матч без счёта — исход задаёт человек:
       python scripts/fix_irl_wrong_results.py --db ... --set 7=home --apply

Перед `--apply` сделайте `/backup`.
"""

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

IRL_TX_WIN = "irl_bet_won"
IRL_TX_REVERT = "irl_bet_won_reverted"
OUTCOME_LABEL = {"home": "П1", "draw": "Ничья", "away": "П2"}


def result_from_score(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


@dataclass
class Change:
    bet_id: int
    user_id: int
    username: str
    outcome: str
    delta: int            # + начислить, − списать
    balance: int          # баланс сейчас
    to_status: str        # won | lost


@dataclass
class MatchFix:
    match: dict
    correct: str
    changes: list = field(default_factory=list)

    @property
    def label(self) -> str:
        m = self.match
        return f"#{m['id']} {m['home']} — {m['away']}"


def _connect(db_path: str, write: bool) -> sqlite3.Connection:
    if write:
        conn = sqlite3.connect(db_path)
    else:
        conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def find_wrong(conn: sqlite3.Connection, overrides: dict[int, str] | None = None):
    """(исправления, матчи без счёта). `overrides` — исход, заданный вручную по id матча."""
    overrides = overrides or {}
    fixes, unverifiable = [], []
    rows = conn.execute("SELECT * FROM irl_matches WHERE status = 'settled' ORDER BY id").fetchall()
    for row in rows:
        match = dict(row)
        if match["id"] in overrides:
            correct = overrides[match["id"]]
        elif match["home_goals"] is None or match["away_goals"] is None:
            unverifiable.append(match)
            continue
        else:
            correct = result_from_score(match["home_goals"], match["away_goals"])
        if correct == match["result"]:
            continue
        fixes.append(MatchFix(match, correct, plan_changes(conn, match["id"], correct)))
    return fixes, unverifiable


def plan_changes(conn: sqlite3.Connection, match_id: int, correct: str) -> list[Change]:
    bets = conn.execute("""
        SELECT b.*, u.username, COALESCE(w.balance, 0) AS balance
        FROM irl_bets b
        LEFT JOIN users u ON u.telegram_id = b.user_id
        LEFT JOIN user_wallets w ON w.user_id = b.user_id
        WHERE b.irl_match_id = ? ORDER BY b.id
    """, (match_id,)).fetchall()
    changes = []
    for b in bets:
        name = f"@{b['username']}" if b["username"] else f"id:{b['user_id']}"
        if b["outcome"] == correct and b["status"] == "lost":
            changes.append(Change(b["id"], b["user_id"], name, b["outcome"],
                                  int(b["potential_win"]), int(b["balance"]), "won"))
        elif b["outcome"] != correct and b["status"] == "won":
            changes.append(Change(b["id"], b["user_id"], name, b["outcome"],
                                  -int(b["actual_payout"] or b["potential_win"]),
                                  int(b["balance"]), "lost"))
    return changes


def print_plan(fixes, unverifiable) -> None:
    for fix in fixes:
        m = fix.match
        print("=" * 64)
        print(f"⚽ {fix.label}  счёт {m['home_goals']}:{m['away_goals']}")
        print(f"   записано: {OUTCOME_LABEL.get(m['result'], m['result'])}  →  верно: {OUTCOME_LABEL[fix.correct]}")
        if not fix.changes:
            print("   ставок для исправления нет — поменяется только исход матча")
        running: dict[int, int] = {}
        for c in fix.changes:
            bal = running.get(c.user_id, c.balance)
            after = bal + c.delta
            running[c.user_id] = after
            warn = "  ⚠️ баланс уйдёт в минус" if after < 0 else ""
            sign = "+" if c.delta > 0 else "−"
            print(f"   • ставка #{c.bet_id} {c.username} на {OUTCOME_LABEL[c.outcome]}: "
                  f"{sign}{abs(c.delta)} 🪙 → {c.to_status}  (баланс {bal} → {after}){warn}")
    if unverifiable:
        print("=" * 64)
        print("❓ Рассчитаны без записанного счёта — проверить нечем:")
        for m in unverifiable:
            print(f"   #{m['id']} {m['home']} — {m['away']}: исход {OUTCOME_LABEL.get(m['result'], m['result'])}"
                  "  (если неверен — запустите с --set ID=home|draw|away)")


def apply_fix(conn: sqlite3.Connection, fix: MatchFix) -> None:
    """Исправляет один матч целиком или не трогает вовсе."""
    m = fix.match
    with conn:
        for c in fix.changes:
            if c.delta > 0:
                conn.execute("""
                    UPDATE user_wallets SET balance = balance + ?, total_won = total_won + ?,
                        bets_won = bets_won + 1, updated_at = datetime('now', '+3 hours')
                    WHERE user_id = ?
                """, (c.delta, c.delta, c.user_id))
                conn.execute("""
                    UPDATE irl_bets SET status = 'won', actual_payout = ?,
                        settled_at = datetime('now', '+3 hours')
                    WHERE id = ? AND status = 'lost'
                """, (c.delta, c.bet_id))
                tx_type = IRL_TX_WIN
                title = f"✅ Корректировка: ставка на реальный матч выиграла +{c.delta} 🪙"
                body = (f"{m['home']} — {m['away']}: верный исход — {OUTCOME_LABEL[fix.correct]}.\n"
                        "Исход был записан ошибочно, ставка пересчитана.")
            else:
                conn.execute("""
                    UPDATE user_wallets SET balance = balance - ?, total_won = MAX(0, total_won - ?),
                        bets_won = MAX(0, bets_won - 1), updated_at = datetime('now', '+3 hours')
                    WHERE user_id = ?
                """, (-c.delta, -c.delta, c.user_id))
                conn.execute("""
                    UPDATE irl_bets SET status = 'lost', actual_payout = 0,
                        settled_at = datetime('now', '+3 hours')
                    WHERE id = ? AND status = 'won'
                """, (c.bet_id,))
                tx_type = IRL_TX_REVERT
                title = f"⚠️ Корректировка: выигрыш по ставке на реальный матч аннулирован −{-c.delta} 🪙"
                body = (f"{m['home']} — {m['away']}: верный исход — {OUTCOME_LABEL[fix.correct]}.\n"
                        "Исход был записан ошибочно, выплата снята.")
            balance = conn.execute("SELECT balance FROM user_wallets WHERE user_id = ?",
                                   (c.user_id,)).fetchone()
            conn.execute("""
                INSERT INTO coin_transactions
                    (user_id, amount, transaction_type, reference_id, reference_type, balance_after, created_at)
                VALUES (?, ?, ?, ?, 'irl_bet', ?, datetime('now', '+3 hours'))
            """, (c.user_id, c.delta, tx_type, c.bet_id, balance["balance"] if balance else None))
            conn.execute("""
                INSERT OR IGNORE INTO notification_events
                    (user_id, event_type, source_event_id, title, body, priority, status, created_at)
                SELECT ?, 'BET_SETTLED', ?, ?, ?, 'high', 'pending', datetime('now', '+3 hours')
                WHERE EXISTS (SELECT 1 FROM users WHERE telegram_id = ?)
            """, (c.user_id, f"ibet_{c.bet_id}_fix", title, body, c.user_id))
        conn.execute("UPDATE irl_matches SET result = ? WHERE id = ? AND status = 'settled'",
                     (fix.correct, m["id"]))


def parse_overrides(items: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for item in items:
        mid, _, res = item.partition("=")
        if not mid.strip().isdigit() or res.strip() not in OUTCOME_LABEL:
            raise SystemExit(f"❌ --set ждёт ID=home|draw|away, получено: {item!r}")
        out[int(mid)] = res.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Исправление IRL-матчей с исходом, не совпадающим со счётом")
    parser.add_argument("--db", required=True, help="Путь к файлу SQLite (league.db / server_league.db)")
    parser.add_argument("--set", action="append", default=[], metavar="ID=ИСХОД",
                        help="Задать исход вручную (для матча без счёта); можно повторять")
    parser.add_argument("--apply", action="store_true", help="Записать изменения (по умолчанию dry-run)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"❌ База не найдена: {args.db}")
        return 1
    overrides = parse_overrides(args.set)

    conn = _connect(args.db, write=args.apply)
    try:
        fixes, unverifiable = find_wrong(conn, overrides)
        print(f"📁 {args.db}")
        if not fixes and not unverifiable:
            print("✅ Все рассчитанные IRL-матчи совпадают со счётом.")
            return 0
        print_plan(fixes, unverifiable)
        if not fixes:
            return 0
        if not args.apply:
            print("\n⚠️ DRY-RUN: ничего не записано. Для применения добавьте --apply.")
            return 0
        for fix in fixes:
            apply_fix(conn, fix)
            print(f"✅ {fix.label}: исправлен ({len(fix.changes)} ставок)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
