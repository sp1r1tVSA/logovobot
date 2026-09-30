"""Сводка /health: services/bot_health.py."""
from datetime import timedelta

import pytest

import config
import database
from services import bot_health, db_backup, job_health
from time_utils import now_msk


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    job_health.reset()
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path))
    yield
    job_health.reset()


def test_db_health_keys():
    info = database.get_db_health(True)
    assert info["quick_check"] == "ok"
    assert info["journal_mode"].lower() == "wal"
    assert info["page_count"] > 0
    assert info["migrations"] > 0
    assert info["last_migration"]


def test_collect_and_format_do_not_leak_keys(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEYS", ["secret-ocr-key-123"])
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "secret-openrouter-456")
    job_health.record_component("api_server", True, "порт 8080")
    report = bot_health.collect()
    assert report["keys"]["gemini_ocr"] == 1
    assert report["keys"]["openrouter"] is True
    assert report["backups"]["count"] == 0

    text = bot_health.format_report(report)
    assert "Состояние бота" in text
    assert "api_server" in text
    assert "secret" not in text


def _report(**overrides):
    now = now_msk()
    report = {
        "now": now,
        "started_at": now - timedelta(hours=5),
        "db": {"quick_check": "ok", "wal_bytes": 0, "size_bytes": 1, "page_count": 1,
               "freelist_count": 0, "journal_mode": "wal", "migrations": 31,
               "last_migration": "031_ocr_runs"},
        "db_error": None,
        "backups": {"dir": "/b", "count": 1, "total_bytes": 1, "interval_hours": 24, "keep": 14,
                    "due": False, "telegram": False,
                    "latest": db_backup.BackupInfo("/b/x", "x", 1, now - timedelta(hours=3))},
        "disk": {"total": 10 * 1024 ** 3, "used": 0, "free": 10 * 1024 ** 3},
        "keys": {"gemini_ocr": 2, "gemini_chat": 1, "gemini_smm": 0, "openrouter": False,
                 "sports": False},
        "settings": {"lockdown": False, "group_id_set": True, "webapp_https": True},
        "runtime": {"jobs": [], "components": []},
        "ocr": {"total": 0},
        "process": {"python": "3.11", "rss_bytes": None, "pid": 1},
    }
    report.update(overrides)
    return report


def test_healthy_report_has_no_warnings():
    report = _report()
    assert bot_health.warnings(report) == []
    assert "Проблем не найдено" in bot_health.format_report(report)


def test_warnings():
    now = now_msk()
    report = _report(
        db={"quick_check": "corrupt", "wal_bytes": 10 ** 9},
        disk={"total": 1, "used": 0, "free": 1},
        keys={"gemini_ocr": 0, "gemini_chat": 0, "gemini_smm": 0, "openrouter": False,
              "sports": False},
        runtime={
            "jobs": [
                {"name": "bad_job", "status": "failing", "interval": 60, "last_ok": None,
                 "failures": 3, "last_error": "RuntimeError: x"},
                {"name": "slow_job", "status": "stale", "interval": 60, "last_ok": None,
                 "failures": 0, "last_error": None},
            ],
            "components": [{"name": "api_server", "ok": False, "detail": "", "updated_at": now}],
        },
        ocr={"total": 10, "ok": 4, "success_pct": 40.0},
    )
    report["backups"]["latest"] = db_backup.BackupInfo("/b/x", "x", 1, now - timedelta(days=3))
    problems = bot_health.warnings(report)
    joined = " | ".join(problems)
    for needle in ("quick_check", "WAL", "bad_job", "slow_job", "api_server",
                   "старше двух интервалов", "1 ГБ", "Gemini", "40.0%"):
        assert needle in joined, needle
    text = bot_health.format_report(report)
    assert "RuntimeError: x" in text


def test_missing_backup_warns_only_when_auto_is_on():
    report = _report()
    report["backups"]["latest"] = None
    assert "бэкапов ещё нет" in bot_health.warnings(report)
    report["backups"]["interval_hours"] = 0
    assert bot_health.warnings(report) == []
    assert "выключено" in bot_health.format_report(report)
