"""
tests/test_fix_irl_wrong_results.py

scripts/fix_irl_wrong_results.py: исправление IRL-матчей, рассчитанных с исходом,
который противоречит счёту (баг радио-кнопок «Рассчитать матч»).
"""

import itertools
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

import config
import database
from time_utils import now_msk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fix_irl_wrong_results as fix  # noqa: E402

_seq = itertools.count(910000)
P1, P2, P3 = 930001, 930002, 930003


def _user(uid, balance=5000):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                     (uid, f"fx{uid}"))
    database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = ? WHERE user_id = ?", (balance, uid))


def _balance(uid):
    return database.get_or_create_wallet(uid)["balance"]


def _settled_match(result, goals, bets):
    """Открытый матч Portugal — Norway, ставки `{uid: outcome}`, расчёт с исходом `result`."""
    kickoff = (now_msk() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    mid, _ = database.create_irl_draft(next(_seq), 39, "UEFA Nations League", "Portugal", "Norway",
                                       kickoff, 1.63, 4.51, 4.94)
    assert database.publish_irl_match(mid)[0]
    for uid, outcome in bets.items():
        assert database.place_irl_bet(uid, mid, outcome, 100)[0]
    home, away = goals if goals else (None, None)
    assert database.settle_irl_match(mid, result, home, away)[0]
    return mid


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _mine(fixes, mid):
    return next((f for f in fixes if f.match["id"] == mid), None)


@pytest.fixture(autouse=True)
def _wallets():
    for uid in (P1, P2, P3):
        _user(uid)


def test_finds_match_whose_result_contradicts_score():
    mid = _settled_match("away", (2, 1), {P1: "home", P2: "away"})
    conn = _conn()
    fixes, _ = fix.find_wrong(conn)
    conn.close()
    f = _mine(fixes, mid)
    assert f is not None and f.correct == "home"
    assert {(c.user_id, c.to_status) for c in f.changes} == {(P1, "won"), (P2, "lost")}


def test_correct_match_is_left_alone():
    mid = _settled_match("home", (2, 1), {P1: "home"})
    conn = _conn()
    fixes, unverifiable = fix.find_wrong(conn)
    conn.close()
    assert _mine(fixes, mid) is None
    assert all(m["id"] != mid for m in unverifiable)


def test_apply_moves_money_and_is_idempotent():
    mid = _settled_match("away", (2, 1), {P1: "home", P2: "away"})
    assert (_balance(P1), _balance(P2)) == (4900, 5000 + 494 - 100)

    conn = _conn()
    fixes, _ = fix.find_wrong(conn)
    fix.apply_fix(conn, _mine(fixes, mid))
    conn.close()

    assert _balance(P1) == 4900 + 163
    assert _balance(P2) == 4900
    m = database.get_irl_match(mid)
    assert m["result"] == "home" and m["status"] == "settled"
    with database.transaction() as c:
        statuses = {r["user_id"]: (r["status"], r["actual_payout"]) for r in c.execute(
            "SELECT user_id, status, actual_payout FROM irl_bets WHERE irl_match_id = ?", (mid,))}
        types = {r["transaction_type"] for r in c.execute(
            "SELECT transaction_type FROM coin_transactions WHERE reference_type = 'irl_bet' "
            "AND user_id IN (?, ?)", (P1, P2))}
    assert statuses[P1] == ("won", 163) and statuses[P2] == ("lost", 0)
    assert fix.IRL_TX_REVERT in types

    conn = _conn()
    fixes, _ = fix.find_wrong(conn)
    conn.close()
    assert _mine(fixes, mid) is None
    assert (_balance(P1), _balance(P2)) == (5063, 4900)


def test_lost_bet_on_another_outcome_stays_lost():
    mid = _settled_match("away", (2, 1), {P3: "draw"})
    conn = _conn()
    fixes, _ = fix.find_wrong(conn)
    f = _mine(fixes, mid)
    assert f.changes == []
    fix.apply_fix(conn, f)
    conn.close()
    assert database.get_irl_match(mid)["result"] == "home"
    assert _balance(P3) == 4900


def test_match_without_score_needs_manual_override():
    mid = _settled_match("away", None, {P1: "home"})
    conn = _conn()
    fixes, unverifiable = fix.find_wrong(conn)
    assert _mine(fixes, mid) is None
    assert any(m["id"] == mid for m in unverifiable)

    fixes, _ = fix.find_wrong(conn, {mid: "home"})
    f = _mine(fixes, mid)
    assert f.correct == "home" and [c.to_status for c in f.changes] == ["won"]
    conn.close()


def test_dry_run_writes_nothing(capsys):
    mid = _settled_match("away", (2, 1), {P1: "home"})
    before = _balance(P1)
    assert fix.main(["--db", config.DB_PATH]) == 0
    out = capsys.readouterr().out
    assert f"#{mid}" in out and "DRY-RUN" in out
    assert _balance(P1) == before
    assert database.get_irl_match(mid)["result"] == "away"


def test_apply_via_cli_and_bad_override(capsys):
    mid = _settled_match("away", (2, 1), {P1: "home"})
    assert fix.main(["--db", config.DB_PATH, "--apply"]) == 0
    assert database.get_irl_match(mid)["result"] == "home"
    with pytest.raises(SystemExit):
        fix.main(["--db", config.DB_PATH, "--set", "1=nope"])
