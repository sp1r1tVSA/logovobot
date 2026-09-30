"""Метрики OCR: таблица ocr_runs, services/ocr_metrics.py и stats распознавателя."""
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import database
from services import ocr_metrics
from services.ai.ai_recognizer import GEMINI_MODELS, recognize_match_screenshots_bytes


def _row(status="ok", *, source="cabinet", outcome="pending", model="m1", attempts=None,
         duration_ms=1000, ocr=(2, 1), final=(2, 1), match_status="confirmed", technical=0):
    return {
        "source": source,
        "status": status,
        "outcome": outcome,
        "model": model,
        "attempt_log": json.dumps(attempts) if attempts is not None else None,
        "duration_ms": duration_ms,
        "ocr_score1": ocr[0] if ocr else None,
        "ocr_score2": ocr[1] if ocr else None,
        "match_score1": final[0] if final else None,
        "match_score2": final[1] if final else None,
        "match_status": match_status,
        "match_is_technical": technical,
    }


class TestComputeStats:
    def test_empty(self):
        stats = ocr_metrics.compute_ocr_stats([])
        assert stats["total"] == 0
        assert stats["success_pct"] is None
        assert stats["exact_pct"] is None
        assert stats["avg_ms"] is None

    def test_success_outcomes_and_accuracy(self):
        rows = [
            _row(outcome="accepted", ocr=(2, 1), final=(2, 1)),              # exact
            _row(outcome="manual", ocr=(3, 1), final=(2, 1)),                # winner only
            _row(outcome="manual", ocr=(1, 1), final=(2, 1)),                # wrong
            _row(outcome="accepted", ocr=(3, 0), final=(3, 0), technical=1),  # ТП: skipped
            _row(outcome="pending", ocr=(1, 0), final=None, match_status="scheduled"),
            _row(status="failed", model=None, ocr=None, final=None),
            _row(status="error", model=None, ocr=None, final=None, duration_ms=None),
        ]
        stats = ocr_metrics.compute_ocr_stats(rows)
        assert stats["total"] == 7
        assert stats["ok"] == 5
        assert stats["success_pct"] == round(100 * 5 / 7, 1)
        assert stats["outcomes"] == {"accepted": 2, "manual": 2, "pending": 1}
        assert stats["accepted_pct"] == 50.0
        assert stats["manual_pct"] == 50.0
        assert stats["compared"] == 3
        assert stats["exact_pct"] == round(100 / 3, 1)
        assert stats["winner_pct"] == round(200 / 3, 1)
        assert stats["models"] == {"m1": {"runs": 5, "ok": 5}}
        assert stats["avg_ms"] == 1000

    def test_attempt_errors_and_retries(self):
        rows = [
            _row(attempts=[{"model": "a", "outcome": "http_429"}, {"model": "b", "outcome": "ok"}]),
            _row(attempts=[{"model": "a", "outcome": "ok"}]),
            _row(status="failed", attempts=[{"model": "a", "outcome": "timeout"},
                                            {"model": "b", "outcome": "http_429"}]),
            _row(attempts="not json"),
        ]
        rows[-1]["attempt_log"] = "{broken"
        stats = ocr_metrics.compute_ocr_stats(rows)
        assert stats["retried"] == 2
        assert stats["errors"] == {"http_429": 2, "timeout": 1}

    def test_format_report(self):
        empty = ocr_metrics.format_report({**ocr_metrics.compute_ocr_stats([]), "days": 1, "since": "x"})
        assert "Прогонов не было" in empty
        assert "сутки" in empty

        rows = [
            _row(outcome="accepted", attempts=[{"model": "m1", "outcome": "ok"}]),
            _row(status="failed", source="draft", model=None, ocr=None, final=None,
                 attempts=[{"model": "m1", "outcome": "http_503"}]),
        ]
        report = ocr_metrics.format_report(
            {**ocr_metrics.compute_ocr_stats(rows), "days": 7, "since": "2026-01-01 00:00:00"})
        assert "7 дн." in report
        assert "Прогонов: <b>2</b>" in report
        assert "черновики в группе" in report
        assert "http_503" in report
        assert "<b>Точность</b>" in report


class TestRunsInDatabase:
    def test_migration_031_applied(self):
        with database.transaction() as conn:
            row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?",
                               (database.MIGRATION_031_OCR_RUNS,)).fetchone()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ocr_runs)")}
        assert row is not None
        assert {"source", "status", "model", "attempts", "attempt_log", "duration_ms",
                "ocr_score1", "ocr_score2", "outcome", "resolved_at", "created_at"} <= cols

    def test_record_and_mark_by_id(self):
        stats = {"model": "gemini-x", "attempts": [{"model": "gemini-x", "outcome": "ok"}],
                 "duration_ms": 1234}
        run_id = ocr_metrics.record_run("cabinet", "ok", stats=stats, user_id=42, match_id=None,
                                        images=2, score1=3, score2=1)
        assert run_id
        assert ocr_metrics.mark_outcome("accepted", run_id=run_id) is True
        # Only a pending run changes: the second decision is ignored.
        assert ocr_metrics.mark_outcome("manual", run_id=run_id) is False

        rows = [r for r in database.get_ocr_runs_since("2000-01-01 00:00:00") if r["id"] == run_id]
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "accepted" and row["resolved_at"]
        assert row["model"] == "gemini-x" and row["attempts"] == 1
        assert row["duration_ms"] == 1234 and row["images"] == 2
        assert (row["ocr_score1"], row["ocr_score2"]) == (3, 1)
        assert row["match_score1"] is None  # no such match: LEFT JOIN

    def test_mark_latest_pending_by_match_and_source(self):
        old = ocr_metrics.record_run("draft", "ok", match_id=987654, score1=1, score2=0)
        new = ocr_metrics.record_run("draft", "ok", match_id=987654, score1=2, score2=0)
        cab = ocr_metrics.record_run("cabinet", "ok", match_id=987654, score1=2, score2=0)
        assert ocr_metrics.mark_outcome("rejected", match_id=987654, source="draft") is True
        by_id = {r["id"]: r["outcome"] for r in database.get_ocr_runs_since("2000-01-01 00:00:00")}
        assert by_id[new] == "rejected"
        assert by_id[old] == "pending"
        assert by_id[cab] == "pending"

    def test_mark_outcome_never_raises(self):
        assert ocr_metrics.mark_outcome("bogus", run_id=1) is False
        assert ocr_metrics.mark_outcome("accepted") is False

    def test_stats_for_days_reads_the_table(self):
        ocr_metrics.record_run("cabinet", "failed")
        stats = ocr_metrics.stats_for_days(1)
        assert stats["days"] == 1
        assert stats["total"] >= 1
        assert stats["by_status"].get("failed", 0) >= 1


def _gemini_reply(left=1, right=0):
    match = {"team1": "A", "team2": "B", "left_score": left, "right_score": right,
             "left_goals": ["X"] * left, "right_goals": ["Y"] * right,
             "left_assists": [], "right_assists": []}
    return json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps({"matches": [match]})}]}}]}
                      ).encode("utf-8")


class TestRecognizerStats:
    @patch("services.ai.ai_recognizer._get_gemini_opener")
    def test_attempts_model_and_duration(self, mock_get_opener):
        import services.ai.ai_recognizer as ar
        with ar._ocr_model_lock:
            ar._ocr_model_index = 0
        opener = MagicMock()
        mock_get_opener.return_value = opener

        def fake_open(req, timeout=30):
            if GEMINI_MODELS[0] in req.full_url:
                raise urllib.error.HTTPError(url="http://fake", code=429, msg="Too Many Requests",
                                             hdrs={}, fp=io.BytesIO(b"{}"))
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = _gemini_reply()
            return cm

        opener.open.side_effect = fake_open
        stats: dict = {}
        res = recognize_match_screenshots_bytes([b"img"], api_key="single_key", stats=stats)
        assert res is not None
        assert stats["attempts"][0] == {"model": GEMINI_MODELS[0], "outcome": "http_429"}
        assert stats["attempts"][-1]["outcome"] == "ok"
        assert stats["model"] == stats["attempts"][-1]["model"] != GEMINI_MODELS[0]
        assert isinstance(stats["duration_ms"], int)

    def test_no_key(self, monkeypatch):
        import services.ai.ai_recognizer as ar
        monkeypatch.setattr(ar, "get_ordered_ocr_keys", lambda api_key=None: [])
        stats: dict = {}
        assert recognize_match_screenshots_bytes([b"img"], stats=stats) is None
        assert stats["attempts"] == [{"model": "", "outcome": "no_key"}]
        assert stats["model"] is None
        assert "duration_ms" in stats
