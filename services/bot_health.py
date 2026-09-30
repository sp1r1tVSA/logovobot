"""Сводка /health: процесс, база, бэкапы, диск, ключи, компоненты и джобы.

`collect()` синхронный (читает базу и файловую систему) — вызывать через
`asyncio.to_thread`. `format_report()` — чистая функция над его результатом.

Секреты никогда не попадают в отчёт: по ключам показывается только, сколько их
задано.
"""
from __future__ import annotations

import html
import logging
import os
import platform
import shutil
from datetime import datetime, timedelta

import config
import database
from services import db_backup, job_health, ocr_metrics
from time_utils import now_msk

logger = logging.getLogger(__name__)

# Предупреждения в отчёте.
DISK_FREE_WARN_BYTES = 1024 ** 3          # < 1 ГБ свободно
WAL_WARN_BYTES = 256 * 1024 ** 2          # WAL > 256 МБ — чекпоинт не успевает
OCR_SUCCESS_WARN_PCT = 60.0               # доля распознанных за сутки
OCR_MIN_RUNS_FOR_WARN = 5

_STATUS_ICON = {"ok": "🟢", "running": "🔵", "waiting": "⚪", "stale": "🟠", "failing": "🔴"}


def _rss_bytes() -> int | None:
    """Resident memory of this process (Linux /proc; None elsewhere)."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs), None
    except Exception as e:
        logger.warning("health: %s failed: %s", getattr(fn, "__name__", fn), e)
        return None, f"{type(e).__name__}: {e}"


def collect(now: datetime | None = None) -> dict:
    now = now or now_msk()
    report: dict = {"now": now, "started_at": job_health.STARTED_AT}

    report["db"], report["db_error"] = _safe(database.get_db_health, True)

    backups = db_backup.list_backups()
    report["backups"] = {
        "dir": db_backup.backup_dir(),
        "count": len(backups),
        "latest": backups[0] if backups else None,
        "total_bytes": sum(b.size_bytes for b in backups),
        "interval_hours": config.BACKUP_INTERVAL_HOURS,
        "keep": config.BACKUP_KEEP,
        "due": db_backup.backup_due(),
        "telegram": bool(config.BACKUP_TELEGRAM_CHAT_ID),
    }

    disk_target = os.path.dirname(os.path.abspath(config.DB_PATH)) or "."
    usage, _ = _safe(shutil.disk_usage, disk_target)
    report["disk"] = (
        {"total": usage.total, "used": usage.used, "free": usage.free} if usage else None
    )

    report["keys"] = {
        "gemini_ocr": len(config.GEMINI_API_KEYS or []),
        "gemini_chat": len(config.GEMINI_CHAT_API_KEYS or []),
        "gemini_smm": len(config.GEMINI_SMM_API_KEYS or []),
        "openrouter": bool(config.OPENROUTER_API_KEY),
        "sports": bool(config.SPORTS_API_KEY),
    }
    lockdown, _ = _safe(config.is_global_lockdown_enabled)
    report["settings"] = {
        "lockdown": bool(lockdown),
        "group_id_set": bool(config.GROUP_ID),
        "webapp_https": str(config.WEBAPP_URL or "").startswith("https://"),
    }

    report["runtime"] = job_health.snapshot(now)
    report["ocr"], _ = _safe(ocr_metrics.stats_for_days, 1)
    report["process"] = {
        "rss_bytes": _rss_bytes(),
        "python": platform.python_version(),
        "pid": os.getpid(),
    }
    return report


def warnings(report: dict) -> list[str]:
    """Plain-text problems worth a glance, most important first."""
    out: list[str] = []
    if report.get("db_error"):
        out.append("база недоступна")
    db = report.get("db") or {}
    if db.get("quick_check") not in (None, "ok"):
        out.append("quick_check базы не «ok»")
    if (db.get("wal_bytes") or 0) > WAL_WARN_BYTES:
        out.append("WAL-файл разросся")
    jobs = (report.get("runtime") or {}).get("jobs") or []
    failing = [j["name"] for j in jobs if j["status"] == "failing"]
    stale = [j["name"] for j in jobs if j["status"] == "stale"]
    if failing:
        out.append("падают джобы: " + ", ".join(failing))
    if stale:
        out.append("не запускаются джобы: " + ", ".join(stale))
    down = [c["name"] for c in (report.get("runtime") or {}).get("components") or [] if not c["ok"]]
    if down:
        out.append("не работают: " + ", ".join(down))
    backups = report.get("backups") or {}
    if backups.get("interval_hours", 0) > 0 and backups.get("latest") is None:
        out.append("бэкапов ещё нет")
    elif backups.get("interval_hours", 0) > 0 and backups.get("latest") is not None:
        age = report["now"] - backups["latest"].created_at
        if age > timedelta(hours=backups["interval_hours"] * 2):
            out.append("последний бэкап старше двух интервалов")
    disk = report.get("disk")
    if disk and disk["free"] < DISK_FREE_WARN_BYTES:
        out.append("на диске меньше 1 ГБ")
    keys = report.get("keys") or {}
    if not keys.get("gemini_ocr"):
        out.append("нет ключей Gemini для OCR")
    ocr = report.get("ocr") or {}
    if (ocr.get("total") or 0) >= OCR_MIN_RUNS_FOR_WARN and (ocr.get("success_pct") or 0) < OCR_SUCCESS_WARN_PCT:
        out.append(f"OCR за сутки распознал {ocr['success_pct']}%")
    return out


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m %H:%M") if value else "—"


def _fmt_age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def _fmt_interval(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч"
    if seconds % 60 == 0:
        return f"{seconds // 60} мин"
    return f"{seconds} с"


def format_report(report: dict) -> str:
    esc = html.escape
    now = report["now"]
    hs = db_backup.human_size
    lines: list[str] = ["🩺 <b>Состояние бота</b>", ""]

    problems = warnings(report)
    if problems:
        lines.append("⚠️ <b>Внимание:</b>")
        lines.extend(f"• {esc(p)}" for p in problems)
    else:
        lines.append("✅ Проблем не найдено")
    lines.append("")

    proc = report.get("process") or {}
    lines.append(
        f"⏱ Аптайм: <b>{_fmt_age(now - report['started_at'])}</b> "
        f"(с {_fmt_dt(report['started_at'])} МСК)"
    )
    mem = f", память {hs(proc['rss_bytes'])}" if proc.get("rss_bytes") else ""
    lines.append(f"🐍 Python {esc(proc.get('python', '?'))}{mem}")
    lines.append("")

    db = report.get("db")
    lines.append("🗄 <b>База</b>")
    if db:
        free_pages = db.get("freelist_count") or 0
        lines.append(
            f"Размер {hs(db.get('size_bytes'))}, WAL {hs(db.get('wal_bytes'))}, "
            f"режим {esc(str(db.get('journal_mode')))}"
        )
        lines.append(
            f"quick_check: <b>{esc(str(db.get('quick_check')))}</b>, "
            f"свободных страниц {free_pages} из {db.get('page_count')}"
        )
        lines.append(
            f"Миграций {db.get('migrations')}, последняя "
            f"<code>{esc(str(db.get('last_migration')))}</code>"
        )
    else:
        lines.append(f"❌ {esc(report.get('db_error') or 'нет данных')}")
    lines.append("")

    b = report["backups"]
    lines.append("💾 <b>Бэкапы</b>")
    if b["interval_hours"] > 0:
        lines.append(f"Авто: раз в {b['interval_hours']:g} ч, хранится {b['keep']}"
                     + (", копия в Telegram" if b["telegram"] else ""))
    else:
        lines.append("Авто: выключено (BACKUP_INTERVAL_HOURS=0)")
    if b["latest"]:
        latest = b["latest"]
        lines.append(
            f"Последний: {_fmt_dt(latest.created_at)} ({_fmt_age(now - latest.created_at)} назад), "
            f"{hs(latest.size_bytes)}"
        )
        lines.append(f"Всего {b['count']} на {hs(b['total_bytes'])}")
    else:
        lines.append("Бэкапов нет")
    disk = report.get("disk")
    if disk:
        lines.append(f"Диск: свободно {hs(disk['free'])} из {hs(disk['total'])}")
    lines.append("")

    keys = report["keys"]
    s = report["settings"]
    lines.append("🔑 <b>Ключи и настройки</b>")
    lines.append(
        f"Gemini: OCR {keys['gemini_ocr']}, чат {keys['gemini_chat']}, SMM {keys['gemini_smm']}; "
        f"OpenRouter {'✅' if keys['openrouter'] else '—'}, спорт-API {'✅' if keys['sports'] else '—'}"
    )
    lines.append(
        f"Локдаун {'🔒 включён' if s['lockdown'] else 'выключен'}, "
        f"GROUP_ID {'✅' if s['group_id_set'] else '—'}, "
        f"Mini App {'https ✅' if s['webapp_https'] else 'не https'}"
    )
    lines.append("")

    runtime = report.get("runtime") or {}
    components = runtime.get("components") or []
    if components:
        lines.append("🧩 <b>Компоненты</b>")
        for c in components:
            detail = f" — {esc(c['detail'])}" if c["detail"] else ""
            lines.append(f"{'🟢' if c['ok'] else '🔴'} {esc(c['name'])}{detail}")
        lines.append("")

    jobs = runtime.get("jobs") or []
    lines.append(f"⚙️ <b>Фоновые задачи</b> ({len(jobs)})")
    for j in jobs:
        icon = _STATUS_ICON.get(j["status"], "⚪")
        row = f"{icon} <code>{esc(j['name'])}</code> · {_fmt_interval(j['interval'])}"
        if j["last_ok"]:
            row += f" · ок {_fmt_age(now - j['last_ok'])} назад"
        if j["failures"]:
            row += f" · падений {j['failures']}"
        lines.append(row)
        if j["status"] == "failing" and j["last_error"]:
            lines.append(f"   └ <code>{esc(j['last_error'])}</code>")
    lines.append("")

    ocr = report.get("ocr")
    lines.append("🔍 <b>OCR за 24 ч</b>")
    if ocr and ocr.get("total"):
        lines.append(
            f"Прогонов {ocr['total']}, распознано {ocr['ok']} ({ocr['success_pct']}%)"
            + (f", в среднем {ocr['avg_ms'] / 1000:.1f} с" if ocr.get("avg_ms") else "")
        )
    else:
        lines.append("Прогонов не было")
    lines.append("")
    lines.append("Подробнее: /ocr_stats · /backup · /audit")
    return "\n".join(lines)
