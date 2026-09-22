"""Готовность боевой базы к запуску общего кубка — инвентаризация и репетиция.

Скрипт отвечает на два вопроса до того, как на сервере что-то записано:

1. **Что в базе сейчас** (всегда, строго только чтение — соединение открыто с
   `mode=ro`, записать через него SQLite не даст):
   * схема: есть ли `cup_stages`, применены ли миграции 022/023, нет ли уже
     кубковых серий и матчей, о которые споткнётся уникальный ключ
     `(stage, series_num)` — он НЕ включает сезон;
   * активный сезон;
   * каноническая сетка 1/64 из `scripts/seed_cup_bracket.py` и тренер каждого
     из 32 клубов по `users.team_name` (логин, дивизион, варны) — клуб без
     тренера играть серию не сможет.

2. **`--rehearse`: пройдёт ли запуск целиком.** База копируется во временный
   файл (backup API — копия согласована с WAL), `database.DB_PATH` переводится
   на копию, и на ней выполняется ровно то, что потом сделают на сервере:
   `init_db()` (миграции) → `create_cup_series` → `provision_cup_stage_line` →
   `open_cup_stage_bets` → `generate_stage_markets`. Печатается линия по сериям
   и проверяется, что у кубка нет ничьей. Копия удаляется; исходный файл не
   открывается на запись ни разу.

Использование:

    python scripts/cup_prod_inventory.py --db C:\\path\\server_league.db
    python scripts/cup_prod_inventory.py --db C:\\path\\server_league.db --rehearse
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import database  # noqa: E402
from scripts.seed_cup_bracket import validate_pairs  # noqa: E402

STAGE = "1/64"
_MIGRATIONS = (database.MIGRATION_022_CUP_GENERAL, database.MIGRATION_023_CUP_TOPICS)


def _ro_connect(path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}  # table — из кода


def inventory(path: str) -> int:
    """Отчёт по живой базе. Возвращает число блокирующих проблем."""
    blockers = 0
    conn = _ro_connect(path)
    try:
        tables = _tables(conn)
        print(f"База: {path} (только чтение)")

        print("\n── Схема")
        applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        last = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"]
        print(f"  последняя миграция: {last}")
        for m in _MIGRATIONS:
            print(f"  {m}: {'применена' if m in applied else 'применится при деплое (init_db)'}")
        print(f"  cup_stages: {'есть' if 'cup_stages' in tables else 'нет — создаст миграция 022'}")

        series_rows = conn.execute(
            "SELECT stage, COUNT(*) AS n FROM cup_series GROUP BY stage"
        ).fetchall() if "cup_series" in tables else []
        match_cols = _columns(conn, "matches")
        cup_matches = conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE tournament_type = 'cup'"
        ).fetchone()["n"] if "tournament_type" in match_cols else 0
        if series_rows:
            detail = ", ".join(f"{r['stage']}: {r['n']}" for r in series_rows)
            print(f"  ✗ в cup_series уже есть серии ({detail}). Ключ (stage, series_num) без "
                  f"сезона — сид 1/64 упрётся в UNIQUE, старые серии сначала убрать.")
            blockers += 1
        else:
            print("  cup_series: пусто — сид 1/64 ляжет без конфликтов ключа")
        print(f"  кубковых матчей в matches: {cup_matches}")
        if "cup_series" in tables:
            dups = conn.execute(
                "SELECT stage, series_num, COUNT(*) AS n FROM cup_series "
                "GROUP BY stage, series_num HAVING n > 1"
            ).fetchall()
            if dups:
                print(f"  ✗ дубли (stage, series_num): {[tuple(d) for d in dups]} — "
                      f"миграция 022 оставит по одной строке")

        print("\n── Сезон")
        season = conn.execute(
            "SELECT id, name, status FROM seasons WHERE status = 'active' "
            "AND name NOT LIKE '%TEST%' AND name NOT LIKE '%LAB%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if season:
            print(f"  активный: #{season['id']} «{season['name']}»")
        else:
            print("  ✗ активного сезона нет — сид возьмёт последний по id")
            blockers += 1

        print(f"\n── Сетка {STAGE} (scripts/seed_cup_bracket.py)")
        try:
            pairs = validate_pairs(STAGE)
        except ValueError as e:
            print(f"  ✗ {e}")
            return blockers + 1
        print(f"  пар: {len(pairs)}, клубов: {len({c for p in pairs for c in p})} — проверка пройдена")

        coaches = {}
        for r in conn.execute(
            "SELECT u.telegram_id, u.username, u.team_name, u.warn_count, d.code "
            "FROM users u LEFT JOIN divisions d ON d.id = u.division_id "
            "WHERE u.team_name IS NOT NULL AND TRIM(u.team_name) != ''"
        ):
            coaches[r["team_name"].strip().lower()] = r

        print("\n── Тренеры клубов")
        missing = []
        by_div: dict[str, int] = {}
        for num, (t1, t2) in enumerate(pairs, start=1):
            cells = []
            for club in (t1, t2):
                c = coaches.get(club.lower())
                if c is None:
                    missing.append(club)
                    cells.append(f"{club} [нет тренера]")
                    continue
                by_div[c["code"] or "?"] = by_div.get(c["code"] or "?", 0) + 1
                warns = f", варнов {c['warn_count']}" if c["warn_count"] else ""
                cells.append(f"{club} (@{c['username'] or c['telegram_id']}, {c['code']}{warns})")
            print(f"  {num:>2}. {cells[0]} — {cells[1]}")
        print(f"  тренеров найдено: {32 - len(missing)}/32, по дивизионам: {by_div}")
        if missing:
            print(f"  ⚠ без тренера ({len(missing)}): {', '.join(missing)}")
            print("    Сид это не блокирует, но серию такого клуба сыграть некому — "
                  "привяжи клуб (scripts/bind_clubs_to_players.py) или замени пару.")
    finally:
        conn.close()
    return blockers


def _copy_db(src: str, dst: str) -> None:
    """Согласованная копия с учётом WAL: backup API, источник открыт только на чтение."""
    source = _ro_connect(src)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def rehearse(path: str) -> int:
    """Полный запуск 1/64 на временной копии. Возвращает 0, если всё прошло."""
    from services import betting_engine  # после выбора DB_PATH — модуль читает её лениво

    workdir = tempfile.mkdtemp(prefix="cup_rehearsal_")
    copy_path = os.path.join(workdir, "league.db")
    original_path = database.DB_PATH
    print(f"\n══ Репетиция на копии {copy_path}")
    try:
        _copy_db(path, copy_path)
        database.close_thread_connection()
        database.DB_PATH = copy_path

        database.init_db()
        with database.transaction() as conn:
            applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        for m in _MIGRATIONS:
            print(f"  {m}: {'✓' if m in applied else '✗ не применилась'}")
        if not all(m in applied for m in _MIGRATIONS):
            return 1

        season_id = int(database.get_active_season())
        pairs = validate_pairs(STAGE)
        ids = database.create_cup_series(STAGE, pairs, season_id=season_id)
        print(f"  create_cup_series: {len(ids)} серий")

        database.provision_cup_stage_line(STAGE, season_id=season_id)
        stage = database.get_cup_stage(STAGE, season_id=season_id)
        rows = database.get_cup_stage_matches(STAGE, season_id=season_id)
        headers = sum(1 for r in rows if r["is_series_header"])
        print(f"  provision_cup_stage_line: этап #{stage['id']}, заголовков {headers}, "
              f"игр {len(rows) - headers}")

        ok, message = database.open_cup_stage_bets(stage["id"])
        print(f"  open_cup_stage_bets: {'✓' if ok else '✗'} {message}")
        if not ok:
            return 1
        betting_engine.generate_stage_markets(STAGE, season_id=season_id)

        line = database.get_cup_stage_line(STAGE, season_id=season_id)
        problems = []
        print("\n  Линия (серия: проход П1/П2 | игра 1 П1/П2):")
        for s in line["series"]:
            h = (s.get("header") or {}).get("odds") or {}
            g1 = next((g for g in s.get("games", []) if g.get("game_num_in_series") == 1), None)
            g = (g1 or {}).get("odds") or {}
            print(f"  {s['series_num']:>2}. {s['team1_name']} — {s['team2_name']}: "
                  f"{h.get('p1', '—')}/{h.get('p2', '—')} | {g.get('p1', '—')}/{g.get('p2', '—')}")
            for tile in [s.get("header"), *s.get("games", [])]:
                if not tile:
                    problems.append(f"серия {s['series_num']}: нет тайла")
                    continue
                if "x" in (tile.get("odds") or {}):
                    problems.append(f"матч {tile['match_id']}: в линии есть ничья")
                if not (tile.get("odds") or {}).get("p1"):
                    problems.append(f"матч {tile['match_id']}: нет П1")
            if len(s.get("games", [])) != 3:
                problems.append(f"серия {s['series_num']}: игр {len(s.get('games', []))}, а не 3")

        with database.transaction() as conn:
            draws = conn.execute(
                "SELECT COUNT(*) FROM market_selections ms "
                "JOIN markets m ON m.id = ms.market_id "
                "JOIN matches mt ON mt.id = m.match_id "
                "WHERE mt.tournament_type = 'cup' AND ms.selection_key = 'x'"
            ).fetchone()[0]
        if draws:
            problems.append(f"в рынках кубка {draws} исходов «ничья»")

        if problems:
            print("\n  ✗ Репетиция нашла проблемы:\n    - " + "\n    - ".join(problems))
            return 1
        print(f"\n  ✓ Репетиция прошла: {len(line['series'])} серий в линии, ничьей нет.")
        return 0
    finally:
        database.close_thread_connection()
        database.DB_PATH = original_path
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"  копия удалена: {not os.path.exists(copy_path)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Готовность боевой базы к запуску кубка")
    parser.add_argument("--db", required=True, help="путь к снапшоту боевой базы")
    parser.add_argument("--rehearse", action="store_true",
                        help="прогнать запуск 1/64 на временной копии")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Файла {args.db} нет.")
        return 2

    size_before = os.path.getsize(args.db)
    blockers = inventory(args.db)
    rc = 1 if blockers else 0
    if args.rehearse:
        rc = rehearse(args.db) or rc
    if os.path.getsize(args.db) != size_before:
        print("✗ Размер исходной базы изменился — такого быть не должно.")
        return 2
    print(f"\nИтог: {'готово к запуску' if rc == 0 else 'есть проблемы — см. выше'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
