"""Бэкфилл двух оборванных проводок прогнозной модели.

До появления детектора договорных матчей `EloEngine.update_ratings_post_match` и
`database.resolve_ai_predictions` не вызывались нигде в проде. Последствия:

  1. `team_ratings.elo_rating` у всех клубов навсегда 1500.0, и Elo-подмодель
     (35% веса ансамбля) отдавала константу — то есть треть прогноза была шумом.
  2. `predictions.resolved_at` / `actual_result` / `is_correct` / `brier_score`
     пустые, поэтому `ModelPerformanceService` всегда отвечал
     `insufficient_sample` и калибровать модель было не по чему.

Обе проводки теперь вызываются из `confirm_and_finalize_match` и из
`admin_set_match_score`, но это чинит только будущие матчи. Уже сыгранные надо
прогнать один раз этим скриптом — иначе рейтинги так и останутся на 1500.0, а
Brier не посчитается.

Скрипт идёт по `matches` со `status='confirmed'` строго в хронологическом порядке
`played_at` (матчи без даты идут последними, по id): Elo — путь, а не множество,
и порядок обработки меняет результат.
Технические результаты (ТП/ТН) пропускает сам `_apply_elo_after_match` — это
административный вердикт, а не сыгранный матч.

Повторный запуск безопасен: `elo_applied_matches` хранит дельты каждого матча и
откатывает их перед новым применением, а `resolve_ai_predictions` трогает только
строки с пустым `resolved_at`.

Использование:

    python scripts/backfill_model_wiring.py                 # сухой прогон (по умолчанию)
    python scripts/backfill_model_wiring.py --apply         # записать в базу
    python scripts/backfill_model_wiring.py --db /path/league.db --apply
    python scripts/backfill_model_wiring.py --limit 50      # первые 50 матчей
"""

import argparse
import os
import sys

# Запуск идёт из корня репозитория, но скрипт должны звать и как scripts/... с VPS.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Бэкфилл Elo-рейтингов и разрешения прогнозов по сыгранным матчам."
    )
    parser.add_argument(
        "--db",
        help="Путь к league.db. По умолчанию — тот же, что читает config.DB_PATH."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Записать изменения. Без этого флага — только отчёт, база не трогается."
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Обработать не больше N матчей (для пробного прогона на боевой базе)."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # DB_PATH резолвится на импорте config, поэтому путь подставляем до импорта.
    if args.db:
        os.environ["LEAGUE_SQLITE_PATH"] = os.path.abspath(args.db)
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "backfill-script")

    import config  # noqa: E402
    import database  # noqa: E402

    if not os.path.exists(config.DB_PATH):
        print(f"❌ База не найдена: {config.DB_PATH}")
        return 1

    print(f"База: {config.DB_PATH}")
    print("Режим: ЗАПИСЬ" if args.apply else "Режим: сухой прогон (--apply для записи)")
    print()

    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT m.id, m.player1_team, m.player2_team,
               m.player1_score, m.player2_score, m.is_technical,
               m.played_at, m.division_id,
               (SELECT COUNT(*) FROM elo_applied_matches e WHERE e.match_id = m.id) AS elo_done,
               (SELECT COUNT(*) FROM predictions p
                 WHERE p.match_id = m.id AND p.resolved_at IS NULL) AS preds_pending
          FROM matches m
         WHERE m.status = 'confirmed'
           AND m.player1_score IS NOT NULL
           AND m.player2_score IS NOT NULL
         ORDER BY (m.played_at IS NULL) ASC, m.played_at ASC, m.id ASC
        """
    )
    matches = [dict(row) for row in cursor.fetchall()]

    if args.limit:
        matches = matches[:args.limit]

    if not matches:
        print("Подтверждённых матчей со счётом нет — бэкфиллить нечего.")
        return 0

    technical = sum(1 for m in matches if m["is_technical"])
    elo_pending = sum(1 for m in matches if not m["elo_done"] and not m["is_technical"])
    preds_pending = sum(m["preds_pending"] for m in matches)

    print(f"Подтверждённых матчей:      {len(matches)}")
    print(f"  из них технических:       {technical} (Elo не двигают)")
    print(f"  ждут применения Elo:      {elo_pending}")
    print(f"Неразрешённых прогнозов:    {preds_pending}")
    print()

    if not args.apply:
        print("Сухой прогон завершён. Повторите с --apply, чтобы записать.")
        return 0

    elo_applied = 0
    elo_skipped = 0
    preds_resolved = 0
    failures = 0

    for m in matches:
        match_id = m["id"]
        try:
            if database._apply_elo_after_match(match_id, m["player1_score"], m["player2_score"]):
                elo_applied += 1
            else:
                elo_skipped += 1
        except Exception as e:
            failures += 1
            print(f"  ⚠️ матч #{match_id}: Elo не применён — {e}")

        try:
            preds_resolved += database.resolve_ai_predictions(
                match_id, m["player1_score"], m["player2_score"]
            )
        except Exception as e:
            failures += 1
            print(f"  ⚠️ матч #{match_id}: прогнозы не разрешены — {e}")

    print()
    print(f"✅ Elo применён:            {elo_applied}")
    print(f"   пропущено (ТП/ТН и пр.): {elo_skipped}")
    print(f"✅ Прогнозов разрешено:     {preds_resolved}")
    if failures:
        print(f"⚠️ Ошибок:                  {failures}")

    cursor.execute(
        "SELECT COUNT(*) AS n, MIN(elo_rating) AS lo, MAX(elo_rating) AS hi FROM team_ratings"
    )
    row = cursor.fetchone()
    if row and row["n"]:
        print()
        print(f"team_ratings: {row['n']} клубов, Elo от {row['lo']:.1f} до {row['hi']:.1f}")
        if row["lo"] == row["hi"]:
            print("⚠️ Все рейтинги совпадают — Elo-подмодель всё ещё отдаёт константу.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
