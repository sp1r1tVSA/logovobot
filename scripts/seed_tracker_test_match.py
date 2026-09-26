#!/usr/bin/env python3
"""
scripts/seed_tracker_test_match.py

Создаёт один настоящий несыгранный матч в `matches`, привязанный к
конкретному telegram_id, — чтобы Logovo Tracker мог реально запустить по
нему трансляцию (`/api/tracker/session/start` и дальше tick/event/finish).

Зачем это нужно: встроенный dev-бэкдор в `_fetch_open_matches`
(api/routes_tracker.py, telegram_id 777777 + TRACKER_DEV_PIN_ENABLED=true)
отдаёт в лобби три ЗАГЛУШКИ (id 9991-9993) — они существуют только в ответе
API и не лежат в таблице `matches`. Открыть их в лобби можно, но «Начать
трансляцию» упадёт с match_not_found, потому что `_load_match_for_user`
всегда ищет матч по id в реальной таблице. Этот скрипт создаёт настоящую
строку, поэтому она подменяет собой заглушки в списке (заглушки показываются
только когда `not rows`) и её можно реально стартовать.

Использование (на сервере, где лежит league.db):
  python scripts/seed_tracker_test_match.py
  python scripts/seed_tracker_test_match.py --user-id 777777 --own-team "Манчестер Сити" --opponent "Ливерпуль"
  python scripts/seed_tracker_test_match.py --clean   # удалить ранее созданный тестовый матч
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import database

MARKER = "[tracker-test]"


def seed(user_id: int, own_team: str, opponent: str, division_id: int, season_id: int) -> int:
    database.init_db()
    with database.transaction() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO users (telegram_id, username, team_name, division_id, warn_count, role, registered_at) "
            "VALUES (?, ?, ?, ?, 0, 'player', datetime('now', '+3 hours')) "
            "ON CONFLICT(telegram_id) DO UPDATE SET team_name = excluded.team_name",
            (user_id, f"tracker_test_{user_id}", own_team, division_id),
        )

        cursor.execute(
            "INSERT INTO matches "
            "(season_id, division_id, round_number, tournament_type, player1_id, player2_id, "
            " player1_team, player2_team, status, played_at) "
            "VALUES (?, ?, 1, 'friendly', ?, NULL, ?, ?, 'scheduled', NULL)",
            (season_id, division_id, user_id, own_team, f"{opponent} {MARKER}"),
        )
        match_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO live_match_states "
            "(match_id, season_id, division_id, status, period, minute, home_score, away_score, "
            " provider, last_updated_at) "
            "VALUES (?, ?, ?, 'SCHEDULED', 'pre_match', 0, 0, 0, 'tracker', datetime('now', '+3 hours'))",
            (match_id, season_id, division_id),
        )

    return match_id


def clean(user_id: int) -> int:
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM matches WHERE player1_id = ? AND player2_team LIKE ?",
            (user_id, f"%{MARKER}%"),
        )
        ids = [r["id"] for r in cursor.fetchall()]
        for match_id in ids:
            cursor.execute("DELETE FROM live_events WHERE match_id = ?", (match_id,))
            cursor.execute("DELETE FROM live_match_states WHERE match_id = ?", (match_id,))
            cursor.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        return len(ids)


def main():
    parser = argparse.ArgumentParser(description="Создать/удалить тестовый матч для Logovo Tracker")
    parser.add_argument("--user-id", type=int, default=777777, help="telegram_id владельца матча (по умолчанию dev-пользователь 777777)")
    parser.add_argument("--own-team", type=str, default=None, help="Название своего клуба (по умолчанию уникальное тестовое имя с telegram_id, чтобы не столкнуться с именем реального клуба)")
    parser.add_argument("--opponent", type=str, default="Ливерпуль", help="Название клуба соперника")
    parser.add_argument("--division-id", type=int, default=1)
    parser.add_argument("--season-id", type=int, default=1)
    parser.add_argument("--clean", action="store_true", help="Удалить ранее созданные этим скриптом тестовые матчи")
    args = parser.parse_args()

    if args.clean:
        n = clean(args.user_id)
        print(f"Удалено тестовых матчей: {n}")
        return

    own_team = args.own_team or f"Тест-клуб {args.user_id}"
    match_id = seed(args.user_id, own_team, args.opponent, args.division_id, args.season_id)
    print(f"Создан тестовый матч id={match_id}: {own_team} vs {args.opponent} (owner telegram_id={args.user_id})")
    print("Теперь он появится в лобби приложения вместо заглушек 9991-9993, и трансляцию по нему можно реально запускать.")


if __name__ == "__main__":
    main()
