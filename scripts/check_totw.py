"""Проверка публикации символических сборных (TOTW) по блокам из 5 туров.

Для каждого дивизиона и каждого блока (1–5, 6–10, …) активного сезона показывает:

  * сыгран ли блок по правилу джоба (все матчи лиги подтверждены, отменённые не
    считаются, тур без матчей блок не закрывает) и что именно его держит;
  * стоит ли отметка публикации `round_content_posts('totw')` и когда она поставлена;
  * привязан ли топик ТАБЛИЦЫ/АНАЛИТИКА, без которого джоб ничего не отправит.

Вердикты:

  ОК              блок сыгран, сборная опубликована после последнего матча
  ЖДЁМ            блок ещё не сыгран — в строке видно, какие матчи его держат
  НЕ ВЫЛОЖЕНА     блок сыгран, отметки нет: джоб должен отправить в ближайшие 15 минут
  НЕТ ТОПИКА      блок сыгран, отметки нет, а топика ТАБЛИЦЫ/АНАЛИТИКА нет — джоб молчит
  РАНО            отметка стоит, но блок доигран позже неё (или не доигран вовсе):
                  сборную выложили по неполным цифрам, а настоящую джоб уже не отправит

Использование:

    python scripts/check_totw.py                          # все дивизионы, exit 1 при находках
    python scripts/check_totw.py --db server_league.db    # снимок боевой базы
    python scripts/check_totw.py --division 3
    python scripts/check_totw.py --block 1-5

Скрипт **только читает**: соединение закрыто авторизатором SQLite, записать в базу
нельзя даже случайно. Сеть и Telegram не трогаются.
"""

import argparse
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

MAX_HELD_LINES = 6
BLOCK_SIZE = 5  # database.TOTW_BLOCK_SIZE; здесь не импортируем database, он пишет в базу.

_READ_ONLY_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_TRANSACTION,
})

OK, WAIT, NOT_POSTED, NO_TOPIC, EARLY = "ОК", "ЖДЁМ", "НЕ ВЫЛОЖЕНА", "НЕТ ТОПИКА", "РАНО"
PROBLEMS = {NOT_POSTED, NO_TOPIC, EARLY}


def open_read_only(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"Базы нет: {db_path}")
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(
        lambda action, *_: sqlite3.SQLITE_OK if action in _READ_ONLY_ACTIONS else sqlite3.SQLITE_DENY
    )
    return conn


def active_season_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM seasons WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else 1


def division_rows(conn: sqlite3.Connection, only: int | None) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT id, code, name FROM divisions ORDER BY id").fetchall()
    return [r for r in rows if only is None or r["id"] == only]


def round_completion(conn: sqlite3.Connection, division_id: int, season_id: int) -> dict[int, tuple[int, int]]:
    """{тур: (матчей, подтверждено)} — то же правило, что database._round_completion."""
    rows = conn.execute(
        """
        SELECT round_number,
               COUNT(id) AS total,
               SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed
        FROM matches
        WHERE division_id = ?
          AND (tournament_type IS NULL OR tournament_type = 'league')
          AND (season_id = ? OR season_id IS NULL)
          AND round_number > 0
          AND status != 'cancelled'
        GROUP BY round_number
        """,
        (division_id, season_id),
    ).fetchall()
    return {int(r["round_number"]): (int(r["total"] or 0), int(r["confirmed"] or 0)) for r in rows}


def blockers(conn: sqlite3.Connection, division_id: int, season_id: int, start: int, end: int) -> list[str]:
    """Неподтверждённые матчи блока и пустые туры — то, что держит публикацию."""
    lines = []
    completion = round_completion(conn, division_id, season_id)
    for rn in range(start, end + 1):
        if completion.get(rn, (0, 0))[0] == 0:
            lines.append(f"тур {rn}: матчей нет вовсе")
    rows = conn.execute(
        """
        SELECT m.id, m.round_number, m.player1_team, m.player2_team, m.is_extended, m.frozen_at,
               d.state AS debt_state
        FROM matches m
        LEFT JOIN match_debts d ON d.match_id = m.id
        WHERE m.division_id = ?
          AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
          AND (m.season_id = ? OR m.season_id IS NULL)
          AND m.round_number BETWEEN ? AND ?
          AND m.status NOT IN ('confirmed', 'cancelled')
        ORDER BY m.round_number, m.id
        """,
        (division_id, season_id, start, end),
    ).fetchall()
    for r in rows:
        extra = []
        if r["debt_state"]:
            extra.append(f"долг: {r['debt_state']}")
        if r["is_extended"]:
            extra.append("продлён")
        if r["frozen_at"]:
            extra.append("заморожен")
        tail = f" ({', '.join(extra)})" if extra else ""
        lines.append(
            f"тур {r['round_number']}: матч {r['id']} {r['player1_team']} — {r['player2_team']}{tail}"
        )
    return lines


def last_played_at(conn: sqlite3.Connection, division_id: int, season_id: int, start: int, end: int) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(played_at) AS last FROM matches
        WHERE division_id = ?
          AND (tournament_type IS NULL OR tournament_type = 'league')
          AND (season_id = ? OR season_id IS NULL)
          AND round_number BETWEEN ? AND ?
          AND status = 'confirmed'
        """,
        (division_id, season_id, start, end),
    ).fetchone()
    return row["last"] if row else None


def has_topic(conn: sqlite3.Connection, division_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM division_topics
        WHERE division_id = ? AND topic_type IN ('tables', 'analytics')
          AND message_thread_id IS NOT NULL AND group_chat_id IS NOT NULL
        LIMIT 1
        """,
        (division_id,),
    ).fetchone()
    return row is not None


def posted_markers(conn: sqlite3.Connection, division_id: int) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT round_number, message_id, posted_at FROM round_content_posts "
        "WHERE division_id = ? AND content_type = 'totw'",
        (division_id,),
    ).fetchall()
    return {int(r["round_number"]): r for r in rows}


def check_division(conn, division: sqlite3.Row, season_id: int, only_block: tuple[int, int] | None) -> list[dict]:
    div_id = division["id"]
    completion = round_completion(conn, div_id, season_id)
    if not completion:
        return []
    markers = posted_markers(conn, div_id)
    topic = has_topic(conn, div_id)
    out = []
    # Календарь заведён на весь сезон, так что max(completion) — не «сколько сыграно»;
    # блоки без единого сыгранного матча ниже отсеиваются по `last`.
    for start in range(1, max(completion) + 1, BLOCK_SIZE):
        end = start + BLOCK_SIZE - 1
        if only_block and (start, end) != only_block:
            continue
        complete = all(
            completion.get(rn, (0, 0))[0] > 0 and completion[rn][0] == completion[rn][1]
            for rn in range(start, end + 1)
        )
        marker = markers.get(end)
        last = last_played_at(conn, div_id, season_id, start, end)
        held = [] if complete else blockers(conn, div_id, season_id, start, end)

        if marker is not None:
            premature = not complete or (last is not None and marker["posted_at"] < last)
            verdict = EARLY if premature else OK
        elif complete:
            verdict = NOT_POSTED if topic else NO_TOPIC
        else:
            if last is None:
                continue  # календарь заведён вперёд, но в блоке ещё ничего не сыграно
            verdict = WAIT
        out.append({
            "division": division, "start": start, "end": end, "verdict": verdict,
            "marker": marker, "last": last, "held": held,
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка публикации символических сборных (read-only)")
    parser.add_argument("--db", default=os.getenv("LEAGUE_SQLITE_PATH") or os.path.join(BASE_DIR, "league.db"))
    parser.add_argument("--division", type=int, help="id дивизиона")
    parser.add_argument("--block", help="блок, например 1-5")
    args = parser.parse_args()

    only_block = None
    if args.block:
        from services.totw_service import parse_round_range

        only_block = parse_round_range(args.block)
        if not only_block:
            raise SystemExit(f"Не понял блок: {args.block!r}")

    conn = open_read_only(args.db)
    season_id = active_season_id(conn)
    print(f"База: {args.db} · сезон id={season_id}\n")

    problems = 0
    for division in division_rows(conn, args.division):
        results = check_division(conn, division, season_id, only_block)
        print(f"■ {division['name']} ({division['code']}, id={division['id']})")
        if not results:
            print("  туров с матчами нет\n")
            continue
        for r in results:
            mark = r["marker"]
            posted = f"отметка {mark['posted_at']} (msg {mark['message_id']})" if mark else "отметки нет"
            last = f", последний матч {r['last']}" if r["last"] else ""
            print(f"  туры {r['start']}–{r['end']}: {r['verdict']} · {posted}{last}")
            for line in r["held"][:MAX_HELD_LINES]:
                print(f"      держит: {line}")
            if len(r["held"]) > MAX_HELD_LINES:
                print(f"      …и ещё {len(r['held']) - MAX_HELD_LINES}")
            if r["verdict"] in PROBLEMS:
                problems += 1
        print()

    print(f"Проблемных блоков: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
