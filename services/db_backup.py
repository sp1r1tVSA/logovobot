"""Бэкап базы: сжатая, проверенная копия league.db с ротацией.

Копию снимает `database.backup_database` через SQLite backup API — это безопасно
при работающем боте, в отличие от копирования файла WAL-базы. Здесь — всё, что
вокруг: временный файл, gzip, атомарное переименование, ротация и список копий.

Файлы называются `league-YYYYmmdd-HHMMSS.db.gz` по московскому времени, так что
лексикографический порядок совпадает с хронологическим. Всё синхронное — вызывать
через `asyncio.to_thread`.
"""
from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import config
import database
from time_utils import now_msk

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "league-"
BACKUP_SUFFIX = ".db.gz"
_NAME_RE = re.compile(r"^league-(\d{8}-\d{6})(?:-\d+)?\.db\.gz$")

# Два бэкапа одновременно (джоба + /backup) удвоили бы нагрузку и могли бы
# поделить одно имя файла; второй просто ждёт первого.
_lock = threading.Lock()


class BackupError(RuntimeError):
    """The copy was made but is not trustworthy (quick_check failed)."""


@dataclass(frozen=True)
class BackupInfo:
    path: str
    name: str
    size_bytes: int
    created_at: datetime


def backup_dir() -> str:
    return config.BACKUP_DIR


def _parse_created(name: str) -> datetime | None:
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        # Naive MSK, like now_msk(): the stamp in the name is Moscow time.
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def list_backups(directory: str | None = None) -> list[BackupInfo]:
    """Backups in the directory, newest first. Foreign files are ignored."""
    directory = directory or backup_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out: list[BackupInfo] = []
    for name in names:
        created = _parse_created(name)
        if created is None:
            continue
        path = os.path.join(directory, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        out.append(BackupInfo(path=path, name=name, size_bytes=size, created_at=created))
    out.sort(key=lambda b: b.name, reverse=True)
    return out


def latest_backup(directory: str | None = None) -> BackupInfo | None:
    backups = list_backups(directory)
    return backups[0] if backups else None


def backup_due(interval_hours: float | None = None, directory: str | None = None,
               now: datetime | None = None) -> bool:
    """True when auto-backup is on and the newest copy is older than the interval.

    The age is read from the files themselves, so a restart neither skips a
    backup nor makes an extra one.
    """
    interval = config.BACKUP_INTERVAL_HOURS if interval_hours is None else interval_hours
    if interval <= 0:
        return False
    last = latest_backup(directory)
    if last is None:
        return True
    return (now or now_msk()) - last.created_at >= timedelta(hours=interval)


def _target_name(directory: str, stamp: str) -> str:
    name = f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    n = 1
    while os.path.exists(os.path.join(directory, name)):
        n += 1
        name = f"{BACKUP_PREFIX}{stamp}-{n}{BACKUP_SUFFIX}"
    return name


def rotate_backups(keep: int | None = None, directory: str | None = None) -> list[str]:
    """Delete all but the newest `keep` backups; returns the removed names."""
    keep = config.BACKUP_KEEP if keep is None else keep
    removed: list[str] = []
    for info in list_backups(directory)[max(1, keep):]:
        try:
            os.remove(info.path)
            removed.append(info.name)
        except OSError as e:
            logger.warning("Could not remove old backup %s: %s", info.name, e)
    return removed


def create_backup(directory: str | None = None, keep: int | None = None) -> dict:
    """Snapshot, verify, compress and rotate. Returns a summary dict.

    The raw copy is made in a temp file next to the target, checked with
    `PRAGMA quick_check`, gzipped into `<name>.part` and only then renamed, so
    a crash never leaves a half-written file that looks like a valid backup.
    A copy that fails the check raises BackupError and is not kept.
    """
    directory = directory or backup_dir()
    os.makedirs(directory, exist_ok=True)
    with _lock:
        started = now_msk()
        fd, raw_path = tempfile.mkstemp(prefix=".backup-", suffix=".db", dir=directory)
        os.close(fd)
        part_path = None
        try:
            check = database.backup_database(raw_path)
            if check["integrity"].lower() != "ok":
                raise BackupError(f"quick_check: {check['integrity']}")
            raw_size = os.path.getsize(raw_path)
            name = _target_name(directory, started.strftime("%Y%m%d-%H%M%S"))
            final_path = os.path.join(directory, name)
            part_path = final_path + ".part"
            with open(raw_path, "rb") as src, gzip.open(part_path, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            os.replace(part_path, final_path)
            part_path = None
        finally:
            for leftover in (raw_path, part_path):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
        removed = rotate_backups(keep, directory)
        duration = (now_msk() - started).total_seconds()
    size = os.path.getsize(final_path)
    logger.info("DB backup %s: %d bytes (raw %d) in %.1fs, rotated %d",
                name, size, raw_size, duration, len(removed))
    return {
        "path": final_path,
        "name": name,
        "size_bytes": size,
        "raw_bytes": raw_size,
        "integrity": check["integrity"],
        "duration_s": round(duration, 2),
        "removed": removed,
        "created_at": started,
    }


def human_size(num: int | float | None) -> str:
    if num is None:
        return "—"
    num = float(num)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if abs(num) < 1024 or unit == "ГБ":
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ГБ"
