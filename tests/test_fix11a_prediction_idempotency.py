"""
FIX-11A — predictions идемпотентны: (match_id, model_version) = одна строка.

Исторически `idx_predictions_match` был обычным (не уникальным) индексом, а
`save_ai_prediction` делал простой INSERT. Каждый повторный прогноз или превью
матча добавлял в `predictions` ещё одну строку того же прогноза, и
`resolve_ai_predictions`/`correct_ai_predictions` потом считали Brier по всему
набору дублей.

Здесь проверяются оба уровня защиты:
  * `save_ai_prediction` — атомарная идемпотентность самой записи (работает и
    тогда, когда уникального индекса в базе ещё нет, то есть как в проде сегодня);
  * миграция 019 — `UNIQUE(match_id, model_version)` на уровне схемы, которая
    отказывается создавать индекс, если исторические дубли мешают, и при этом
    ничего не удаляет.
"""
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest

import database
from services.intelligence_engine import IntelligenceEngine

MODEL = "ensemble_v1"


def _save(match_id, model_version=MODEL, home=0.55, draw=0.25, away=0.20):
    return database.save_ai_prediction(
        match_id=match_id, division_id=1, season_id=1,
        model_version=model_version, feature_version="features_v1",
        home_prob=home, draw_prob=draw, away_prob=away, confidence=0.65,
        key_factors=["fixture"]
    )


class TestPredictionIdempotency(unittest.TestCase):
    """Один ключ (match_id, model_version) — одна запись, при любом числе вызовов."""

    def setUp(self):
        self.db_dir = tempfile.mkdtemp(prefix="fix11a-")
        self._orig_db_path = database.DB_PATH
        database.close_thread_connection()
        database.DB_PATH = os.path.join(self.db_dir, "league.db")
        database.init_db()

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        shutil.rmtree(self.db_dir, ignore_errors=True)

    # ─── helpers ────────────────────────────────────────────────────────────
    def _make_match(self, match_id, t1="Фикстия A", t2="Фикстия B"):
        with database.transaction() as conn:
            conn.execute(
                """INSERT INTO matches (id, division_id, season_id, round_number,
                                        player1_team, player2_team, status)
                   VALUES (?, 1, 1, 1, ?, ?, 'open')""",
                (match_id, t1, t2)
            )

    def _rows(self, match_id, model_version=None):
        sql = "SELECT * FROM predictions WHERE match_id = ?"
        params = [match_id]
        if model_version is not None:
            sql += " AND model_version = ?"
            params.append(model_version)
        with database.transaction() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]

    def _index_names(self):
        with database.transaction() as conn:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'predictions'"
            )}

    def _legacy_duplicate_state(self, match_id, copies=3):
        """Вернуть базу в состояние прода: уникального индекса нет, дубли есть."""
        with database.transaction() as conn:
            conn.execute("DROP INDEX IF EXISTS uniq_predictions_match_model")
            conn.execute("DELETE FROM schema_migrations WHERE version = '019_prediction_one_row_per_model'")
            for _ in range(copies):
                conn.execute(
                    """INSERT INTO predictions (match_id, division_id, season_id, model_version,
                                                feature_version, home_probability, draw_probability,
                                                away_probability, confidence, created_at)
                       VALUES (?, 1, 1, ?, 'features_v1', 0.40, 0.30, 0.30, 0.50,
                               datetime('now', '+3 hours'))""",
                    (match_id, MODEL)
                )

    # ─── A. запись через save_ai_prediction ─────────────────────────────────
    def test_first_save_creates_exactly_one_row(self):
        self._make_match(9501)
        pid = _save(9501)
        rows = self._rows(9501)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], pid)

    def test_second_save_reuses_the_existing_row(self):
        self._make_match(9502)
        first = _save(9502, home=0.55)
        second = _save(9502, home=0.90)

        self.assertEqual(second, first, "повторный save вернул id другой строки")
        rows = self._rows(9502)
        self.assertEqual(len(rows), 1, "повторный save создал вторую строку")
        self.assertAlmostEqual(rows[0]["home_probability"], 0.55,
                               msg="повторный save перезаписал исторические поля прогноза")

    def test_different_matches_stay_separate_rows(self):
        self._make_match(9503)
        self._make_match(9504)
        a, b = _save(9503), _save(9504)
        self.assertNotEqual(a, b)
        self.assertEqual(len(self._rows(9503)), 1)
        self.assertEqual(len(self._rows(9504)), 1)

    def test_different_model_versions_coexist(self):
        self._make_match(9505)
        _save(9505, model_version="ensemble_v1")
        _save(9505, model_version="ensemble_v2")
        self.assertEqual(len(self._rows(9505)), 2)
        self.assertEqual(len(self._rows(9505, "ensemble_v1")), 1)
        self.assertEqual(len(self._rows(9505, "ensemble_v2")), 1)

    def test_get_ai_prediction_returns_the_saved_row(self):
        self._make_match(9506)
        pid = _save(9506, home=0.61, draw=0.21, away=0.18)
        again = _save(9506, home=0.10, draw=0.10, away=0.80)

        stored = database.get_ai_prediction(9506, model_version=MODEL)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["id"], pid)
        self.assertEqual(stored["id"], again)
        self.assertAlmostEqual(stored["home_probability"], 0.61)
        self.assertEqual(stored["key_factors"], ["fixture"])

    def test_resolved_prediction_does_not_become_unresolved(self):
        self._make_match(9507)
        _save(9507)
        self.assertEqual(database.resolve_ai_predictions(9507, 2, 0), 1)
        before = self._rows(9507)[0]
        self.assertIsNotNone(before["resolved_at"])

        _save(9507, home=0.30)

        after = self._rows(9507)[0]
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["resolved_at"], before["resolved_at"])
        self.assertEqual(after["actual_result"], before["actual_result"])
        self.assertEqual(after["brier_score"], before["brier_score"])

    def test_resolve_after_repeated_saves_returns_one_prediction(self):
        """Brier считается по одной строке, а не по набору дублей того же прогноза."""
        self._make_match(9508)
        for _ in range(5):
            _save(9508)
        self.assertEqual(len(self._rows(9508)), 1)
        self.assertEqual(database.resolve_ai_predictions(9508, 2, 0), 1)

    # ─── B. производственные точки входа ────────────────────────────────────
    def test_repeated_prediction_call_stores_one_row(self):
        self._make_match(9510)
        IntelligenceEngine.get_match_prediction(9510)
        IntelligenceEngine.get_match_prediction(9510)
        rows = self._rows(9510, MODEL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feature_version"], "features_v1")

    def test_repeated_preview_call_stores_one_row(self):
        self._make_match(9511)
        IntelligenceEngine.get_match_preview(9511)
        IntelligenceEngine.get_match_preview(9511)
        self.assertEqual(len(self._rows(9511, MODEL)), 1)

    def test_preview_then_prediction_store_one_row(self):
        self._make_match(9512)
        IntelligenceEngine.get_match_preview(9512)
        IntelligenceEngine.get_match_prediction(9512)
        self.assertEqual(len(self._rows(9512, MODEL)), 1)

    # ─── C. конкуренция ─────────────────────────────────────────────────────
    def _race(self, match_id, threads=8):
        barrier = threading.Barrier(threads)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                _save(match_id)
            except Exception as exc:  # noqa: BLE001 — выводим в assertion message
                errors.append(exc)
            finally:
                database.close_thread_connection()

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join(30)
        self.assertEqual(errors, [], f"гонка породила ошибку: {errors}")
        return self._rows(match_id, MODEL)

    def test_concurrent_saves_do_not_duplicate(self):
        self._make_match(9520)
        self.assertEqual(len(self._race(9520)), 1)

    def test_concurrent_saves_do_not_duplicate_without_unique_index(self):
        """Идемпотентность обеспечивает не только индекс: без него работает одношаговая защита."""
        self._make_match(9521)
        self._legacy_duplicate_state(9521, copies=0)
        self.assertNotIn("uniq_predictions_match_model", self._index_names())
        self.assertEqual(len(self._race(9521)), 1)

    # ─── D. уровень схемы ───────────────────────────────────────────────────
    def test_unique_index_blocks_a_raw_duplicate_insert(self):
        self._make_match(9530)
        self.assertIn("uniq_predictions_match_model", self._index_names())
        _save(9530)
        with self.assertRaises(sqlite3.IntegrityError):
            with database.transaction() as conn:
                conn.execute(
                    """INSERT INTO predictions (match_id, division_id, season_id, model_version,
                                                feature_version, home_probability, draw_probability,
                                                away_probability, confidence, created_at)
                       VALUES (9530, 1, 1, ?, 'features_v1', 0.5, 0.3, 0.2, 0.6,
                               datetime('now', '+3 hours'))""",
                    (MODEL,)
                )

    def test_schema_migrations_row_is_recorded(self):
        with database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = '019_prediction_one_row_per_model'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_existing_index_is_not_recreated_on_the_next_start(self):
        """Повторный init_db() — обычное дело: приложение перезапускают не один раз."""
        database.init_db()
        self.assertIn("uniq_predictions_match_model", self._index_names())

    # ─── E. заблокированная миграция не трогает данные ──────────────────────
    def test_migration_refuses_to_index_over_legacy_duplicates_and_deletes_nothing(self):
        self._make_match(9540)
        self._legacy_duplicate_state(9540, copies=3)
        ids_before = [r["id"] for r in self._rows(9540)]

        with self.assertLogs("database", level="ERROR") as logs:
            database.init_db()

        self.assertNotIn("uniq_predictions_match_model", self._index_names())
        with database.transaction() as conn:
            guard = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = '019_prediction_one_row_per_model'"
            ).fetchone()
        self.assertIsNone(guard, "миграция отметилась выполненной, не создав индекс")

        self.assertEqual([r["id"] for r in self._rows(9540)], ids_before,
                         "исторические predictions изменились или удалились")
        self.assertEqual(len(ids_before), 3)
        joined = "\n".join(logs.output)
        self.assertIn("019_prediction_one_row_per_model", joined)
        self.assertIn("not applied", joined)

    def test_saves_stay_idempotent_while_the_migration_is_blocked(self):
        self._make_match(9541)
        self._legacy_duplicate_state(9541, copies=3)
        with self.assertLogs("database", level="ERROR"):
            database.init_db()

        existing = [r["id"] for r in self._rows(9541)]
        returned = _save(9541, home=0.77)

        self.assertIn(returned, existing)
        self.assertEqual([r["id"] for r in self._rows(9541)], existing,
                         "пока миграция заблокирована, повторный save не должен ни создавать, ни менять строки")
        self.assertEqual(len(self._race(9541, threads=6)), 3,
                         "даже в гонке без индекса дубли не добавляются")

    def test_migration_creates_the_index_once_duplicates_are_gone(self):
        """Разблокировка: как только ключи становятся уникальными, следующий старт создаёт индекс."""
        self._make_match(9550)
        self._legacy_duplicate_state(9550, copies=2)
        with self.assertLogs("database", level="ERROR"):
            database.init_db()
        self.assertNotIn("uniq_predictions_match_model", self._index_names())

        with database.transaction() as conn:
            conn.execute("DELETE FROM predictions WHERE match_id = 9550")
        database.init_db()
        self.assertIn("uniq_predictions_match_model", self._index_names())


if __name__ == "__main__":
    unittest.main()
