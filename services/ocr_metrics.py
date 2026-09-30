"""Метрики OCR скриншотов результата: запись прогонов и сводка для /ocr_stats.

Каждый вызов `recognize_match_screenshots_bytes` из ЛС (`cabinet`) или из
черновиков в группе (`draft`) — одна строка `ocr_runs`: чем закончилось
распознавание (`status`), какая модель ответила, сколько было попыток и с какими
ошибками, сколько заняло и какой счёт прочитан. Что было дальше (`outcome`) —
игрок подтвердил счёт ИИ, ушёл во ввод вручную или админ отклонил черновик —
дописывается отдельно.

Точность не хранится: она считается на чтении сравнением прочитанного счёта с
итоговым счётом матча, так что исправленный админом результат сразу учитывается.

Запись никогда не бросает исключение — метрики не должны ломать приём результата.
"""
from __future__ import annotations

import html
import json
import logging
from collections import Counter
from datetime import timedelta

import database
from time_utils import now_msk

logger = logging.getLogger(__name__)

SOURCES = ("cabinet", "draft")

# status прогона: ok — счёт прочитан и показан; остальные — почему нет.
STATUS_LABELS = {
    "ok": "счёт распознан",
    "failed": "не распознано",
    "error": "ошибка",
    "shootout": "серия пенальти",
    "goals_exceed": "голов больше счёта",
    "no_teams": "не определены команды",
    "no_match": "матч не найден",
}

OUTCOME_LABELS = {
    "accepted": "принято",
    "manual": "ввели вручную",
    "rejected": "отклонено",
    "pending": "без решения",
}

# Итог матча, с которым имеет смысл сравнивать прочитанный счёт.
_FINAL_MATCH_STATUSES = ("confirmed", "finished")


def record_run(
    source: str,
    status: str,
    *,
    stats: dict | None = None,
    user_id: int | None = None,
    match_id: int | None = None,
    images: int = 0,
    score1: int | None = None,
    score2: int | None = None,
) -> int | None:
    """Store a run from the recognizer's `stats` dict. Returns the run id or None."""
    stats = stats or {}
    attempts = stats.get("attempts") or []
    try:
        return database.record_ocr_run(
            source,
            status,
            user_id=user_id,
            match_id=match_id,
            images=int(images or 0),
            model=stats.get("model"),
            attempts=len(attempts),
            attempt_log=json.dumps(attempts, ensure_ascii=False) if attempts else None,
            duration_ms=stats.get("duration_ms"),
            ocr_score1=score1,
            ocr_score2=score2,
        )
    except Exception as e:
        logger.warning("Could not record OCR run (%s/%s): %s", source, status, e)
        return None


def mark_outcome(outcome: str, *, run_id: int | None = None, match_id: int | None = None,
                 source: str | None = None) -> bool:
    """Close the pending run: accepted / manual / rejected. Never raises."""
    if run_id is None and match_id is None:
        return False
    try:
        return database.set_ocr_run_outcome(outcome, run_id=run_id, match_id=match_id, source=source)
    except Exception as e:
        logger.warning("Could not mark OCR outcome %s (run=%s match=%s): %s",
                       outcome, run_id, match_id, e)
        return False


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


def _attempts(row: dict) -> list[dict]:
    raw = row.get("attempt_log")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def compute_ocr_stats(rows: list[dict]) -> dict:
    """Pure summary of `get_ocr_runs_since` rows."""
    total = len(rows)
    by_status = Counter(r.get("status") or "?" for r in rows)
    by_source = Counter(r.get("source") or "?" for r in rows)

    models: dict[str, dict] = {}
    errors: Counter = Counter()
    retried = 0
    for r in rows:
        attempts = _attempts(r)
        if len(attempts) > 1:
            retried += 1
        for a in attempts:
            outcome = str(a.get("outcome") or "?")
            if outcome != "ok":
                errors[outcome] += 1
        model = r.get("model")
        if model:
            m = models.setdefault(model, {"runs": 0, "ok": 0})
            m["runs"] += 1
            if r.get("status") == "ok":
                m["ok"] += 1

    durations = [float(r["duration_ms"]) for r in rows if r.get("duration_ms") is not None]

    ok_rows = [r for r in rows if r.get("status") == "ok"]
    outcomes = Counter(r.get("outcome") or "pending" for r in ok_rows)
    decided = outcomes["accepted"] + outcomes["manual"] + outcomes["rejected"]

    compared = exact = winner_ok = 0
    for r in ok_rows:
        s1, s2 = r.get("ocr_score1"), r.get("ocr_score2")
        f1, f2 = r.get("match_score1"), r.get("match_score2")
        if None in (s1, s2, f1, f2) or r.get("match_status") not in _FINAL_MATCH_STATUSES:
            continue
        if r.get("match_is_technical"):
            continue  # ТП/ТН: счёт назначен, а не сыгран — сравнивать не с чем
        compared += 1
        if (s1, s2) == (f1, f2):
            exact += 1
        if (s1 > s2) - (s1 < s2) == (f1 > f2) - (f1 < f2):
            winner_ok += 1

    def _share(part: int, whole: int) -> float | None:
        return round(100.0 * part / whole, 1) if whole else None

    return {
        "total": total,
        "ok": len(ok_rows),
        "success_pct": _share(len(ok_rows), total),
        "by_status": dict(by_status.most_common()),
        "by_source": dict(by_source.most_common()),
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1]["runs"])),
        "errors": dict(errors.most_common()),
        "retried": retried,
        "avg_ms": round(sum(durations) / len(durations)) if durations else None,
        "p95_ms": _percentile(durations, 95),
        "outcomes": dict(outcomes),
        "accepted_pct": _share(outcomes["accepted"], decided),
        "manual_pct": _share(outcomes["manual"], decided),
        "compared": compared,
        "exact_pct": _share(exact, compared),
        "winner_pct": _share(winner_ok, compared),
    }


def stats_for_days(days: int) -> dict:
    since = (now_msk() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    stats = compute_ocr_stats(database.get_ocr_runs_since(since))
    stats["days"] = days
    stats["since"] = since
    return stats


SOURCE_LABELS = {"cabinet": "ЛС (кабинет)", "draft": "черновики в группе"}


def _pct(value) -> str:
    return "—" if value is None else f"{value:g}%"


def format_report(stats: dict) -> str:
    """HTML for /ocr_stats."""
    esc = html.escape
    days = stats.get("days")
    period = "сутки" if days == 1 else f"{days} дн."
    lines = [f"🔍 <b>Метрики OCR за {period}</b>", f"<i>с {esc(str(stats.get('since', '')))} МСК</i>", ""]
    total = stats.get("total") or 0
    if not total:
        lines.append("Прогонов не было.")
        return "\n".join(lines)

    lines.append(f"Прогонов: <b>{total}</b>, счёт распознан: <b>{stats['ok']}</b> ({_pct(stats['success_pct'])})")
    if stats.get("avg_ms") is not None:
        p95 = stats.get("p95_ms")
        lines.append(
            f"Время: в среднем {stats['avg_ms'] / 1000:.1f} с"
            + (f", p95 {p95 / 1000:.1f} с" if p95 is not None else "")
        )
    if stats.get("retried"):
        lines.append(f"С повторными попытками: {stats['retried']}")
    lines.append("")

    lines.append("<b>Чем закончилось</b>")
    for status, count in stats["by_status"].items():
        lines.append(f"• {esc(STATUS_LABELS.get(status, status))}: {count}")
    lines.append("")

    if len(stats["by_source"]) > 1 or "draft" in stats["by_source"]:
        lines.append("<b>Откуда</b>")
        for source, count in stats["by_source"].items():
            lines.append(f"• {esc(SOURCE_LABELS.get(source, source))}: {count}")
        lines.append("")

    if stats["ok"]:
        out = stats["outcomes"]
        lines.append("<b>Что сделали с распознанным счётом</b>")
        for key in ("accepted", "manual", "rejected", "pending"):
            if out.get(key):
                lines.append(f"• {OUTCOME_LABELS[key]}: {out[key]}")
        if stats.get("accepted_pct") is not None:
            lines.append(f"Принято без правки: <b>{_pct(stats['accepted_pct'])}</b> из решённых")
        lines.append("")

    if stats.get("compared"):
        lines.append(
            f"<b>Точность</b> (сверка с итогом {stats['compared']} матчей): "
            f"счёт {_pct(stats['exact_pct'])}, победитель {_pct(stats['winner_pct'])}"
        )
        lines.append("")

    if stats["models"]:
        lines.append("<b>Модели</b>")
        for model, m in stats["models"].items():
            share = round(100.0 * m["ok"] / m["runs"], 1) if m["runs"] else 0
            lines.append(f"• <code>{esc(model)}</code>: {m['runs']} (распознано {share:g}%)")
        lines.append("")

    if stats["errors"]:
        lines.append("<b>Неудачные попытки</b>")
        for outcome, count in list(stats["errors"].items())[:8]:
            lines.append(f"• <code>{esc(outcome)}</code>: {count}")
    return "\n".join(lines).rstrip()
