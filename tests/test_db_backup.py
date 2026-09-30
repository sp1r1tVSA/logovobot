"""Бэкап базы: services/db_backup.py поверх database.backup_database."""
import gzip
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import database
from services import db_backup


def _touch(directory, name, size=10):
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    return path


class TestCreateBackup:
    def test_backup_is_a_gzipped_valid_database(self, tmp_path):
        with database.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, ?)",
                (770001, "backup_probe", "player"),
            )
        result = db_backup.create_backup(directory=str(tmp_path), keep=5)

        assert result["integrity"] == "ok"
        assert os.path.exists(result["path"])
        assert db_backup._NAME_RE.match(result["name"])
        assert result["size_bytes"] == os.path.getsize(result["path"])
        assert result["raw_bytes"] > 0

        raw = tmp_path / "restored.db"
        with gzip.open(result["path"], "rb") as src:
            raw.write_bytes(src.read())
        conn = sqlite3.connect(raw)
        try:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            # Self-contained: the copy is not left in WAL mode.
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
            row = conn.execute(
                "SELECT username FROM users WHERE telegram_id = 770001").fetchone()
            assert row == ("backup_probe",)
        finally:
            conn.close()

    def test_no_temp_files_left_behind(self, tmp_path):
        db_backup.create_backup(directory=str(tmp_path), keep=5)
        leftovers = [n for n in os.listdir(tmp_path) if not db_backup._NAME_RE.match(n)]
        assert leftovers == []

    def test_failed_integrity_raises_and_keeps_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(database, "backup_database",
                            lambda path: {"integrity": "*** broken", "pages": 1})
        with pytest.raises(db_backup.BackupError):
            db_backup.create_backup(directory=str(tmp_path), keep=5)
        assert os.listdir(tmp_path) == []

    def test_second_backup_in_same_second_gets_a_suffix(self, tmp_path):
        first = db_backup._target_name(str(tmp_path), "20260101-120000")
        _touch(str(tmp_path), first)
        second = db_backup._target_name(str(tmp_path), "20260101-120000")
        assert first == "league-20260101-120000.db.gz"
        assert second == "league-20260101-120000-2.db.gz"
        assert db_backup._NAME_RE.match(second)

    def test_create_rotates(self, tmp_path):
        for day in range(1, 5):
            _touch(str(tmp_path), f"league-202601{day:02d}-000000.db.gz")
        result = db_backup.create_backup(directory=str(tmp_path), keep=2)
        names = [b.name for b in db_backup.list_backups(str(tmp_path))]
        assert len(names) == 2
        assert names[0] == result["name"]
        assert len(result["removed"]) == 3


class TestListAndRotate:
    def test_list_is_newest_first_and_ignores_foreign_files(self, tmp_path):
        d = str(tmp_path)
        _touch(d, "league-20260102-080000.db.gz")
        _touch(d, "league-20260103-080000.db.gz")
        _touch(d, "league-20260101-080000.db.gz")
        _touch(d, "notes.txt")
        _touch(d, "league-20260104-080000.db.gz.part")
        _touch(d, "league-latest.db.gz")
        names = [b.name for b in db_backup.list_backups(d)]
        assert names == [
            "league-20260103-080000.db.gz",
            "league-20260102-080000.db.gz",
            "league-20260101-080000.db.gz",
        ]
        latest = db_backup.latest_backup(d)
        assert latest.created_at == datetime(2026, 1, 3, 8, 0, 0)

    def test_missing_directory_is_empty(self, tmp_path):
        assert db_backup.list_backups(str(tmp_path / "nope")) == []
        assert db_backup.latest_backup(str(tmp_path / "nope")) is None

    def test_rotate_keeps_newest(self, tmp_path):
        d = str(tmp_path)
        for day in range(1, 6):
            _touch(d, f"league-202601{day:02d}-000000.db.gz")
        removed = db_backup.rotate_backups(keep=2, directory=d)
        assert sorted(removed) == [f"league-202601{day:02d}-000000.db.gz" for day in (1, 2, 3)]
        assert [b.name for b in db_backup.list_backups(d)] == [
            "league-20260105-000000.db.gz", "league-20260104-000000.db.gz"]

    def test_rotate_never_removes_the_last_copy(self, tmp_path):
        d = str(tmp_path)
        _touch(d, "league-20260101-000000.db.gz")
        _touch(d, "league-20260102-000000.db.gz")
        db_backup.rotate_backups(keep=0, directory=d)
        assert [b.name for b in db_backup.list_backups(d)] == ["league-20260102-000000.db.gz"]


class TestBackupDue:
    def test_disabled_interval_is_never_due(self, tmp_path):
        assert db_backup.backup_due(0, str(tmp_path)) is False

    def test_no_backups_is_due(self, tmp_path):
        assert db_backup.backup_due(24, str(tmp_path)) is True

    def test_due_by_age_of_newest_file(self, tmp_path):
        d = str(tmp_path)
        _touch(d, "league-20260101-120000.db.gz")
        base = datetime(2026, 1, 1, 12, 0, 0)
        assert db_backup.backup_due(24, d, now=base + timedelta(hours=23)) is False
        assert db_backup.backup_due(24, d, now=base + timedelta(hours=24)) is True


def test_human_size():
    assert db_backup.human_size(None) == "—"
    assert db_backup.human_size(512) == "512 Б"
    assert db_backup.human_size(2048) == "2.0 КБ"
    assert db_backup.human_size(5 * 1024 ** 2) == "5.0 МБ"
