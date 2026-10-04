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
        raw_rows = _dump_rows(stats.get("rows"))
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
            raw_rows=raw_rows,
        )
    except Exception as e:
        logger.warning("Could not record OCR run (%s/%s): %s", source, status, e)
        return None


# A table has at most ~11 rows a side; the cap only guards against a runaway answer.
_MAX_RAW_ROWS_CHARS = 6000


def _dump_rows(rows) -> str | None:
    """JSON of the model's transcribed table rows, or None when there were none."""
    if not rows:
        return None
    text = json.dumps(rows, ensure_ascii=False, default=str)
    return text if len(text) <= _MAX_RAW_ROWS_CHARS else None


def format_run_rows(run: dict | None) -> str:
    """HTML for `/ocr_stats raw <match_id>`: what the model read for one run."""
    if not run:
        return "🔍 Для этого матча прогонов OCR нет."
    esc = html.escape
    lines = [
        f"🔍 <b>Прогон OCR #{run.get('id')}</b> · матч {run.get('match_id')} · "
        f"{esc(str(run.get('created_at') or ''))} МСК",
        f"Источник: {esc(str(run.get('source')))} · статус: {esc(STATUS_LABELS.get(run.get('status'), str(run.get('status'))))}"
        f" · модель: {esc(str(run.get('model') or '—'))}",
    ]
    if run.get("ocr_score1") is not None and run.get("ocr_score2") is not None:
        lines.append(f"Счёт, как прочитан: {run['ocr_score1']} : {run['ocr_score2']} (хозяева : гости)")
    raw = run.get("raw_rows")
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict):
        lines.append("")
        lines.append("Строк таблицы нет: прогон до сохранения строк, скриншот без таблицы или чтение не удалось.")
        return "\n".join(lines)
    titles = {"left": "Левая таблица (экран слева)", "right": "Правая таблица (экран справа)"}
    for side in ("left", "right"):
        rows = data.get(side)
        lines.append("")
        lines.append(f"<b>{titles[side]}</b> — цифры как на экране, слева направо")
        if not isinstance(rows, list) or not rows:
            lines.append("—")
            continue
        for row in rows:
            if isinstance(row, dict):
                name, digits = row.get("name") or row.get("player"), row.get("digits")
            elif isinstance(row, (list, tuple)) and len(row) == 3:
                name, digits = row[0], list(row[1:])
            else:
                lines.append(f"<code>{esc(str(row))}</code>")
                continue
            shown = " ".join(str(d) for d in digits) if isinstance(digits, (list, tuple)) else str(digits)
            lines.append(f"• {esc(str(name))}: <code>{esc(shown)}</code>")
    return "\n".join(lines)


def _load_rows(raw) -> dict | None:
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _compact_rows(rows) -> str:
    """One line of a table as the model read it: «Openda 1 0 · Nakamura 0 1»."""
    parts = []
    for row in rows or []:
        if isinstance(row, dict):
            name, digits = row.get("name") or row.get("player"), row.get("digits")
        elif isinstance(row, (list, tuple)) and len(row) == 3:
            name, digits = row[0], list(row[1:])
        else:
            continue
        shown = " ".join(str(d) for d in digits) if isinstance(digits, (list, tuple)) else str(digits)
        parts.append(f"{name} {shown}")
    return " · ".join(parts)


def find_assist_gaps(run: dict) -> list[dict]:
    """Sides of one run that scored but have no assist in the table as the model read it.

    Goals vs assists is decided exactly as the recognizer does it (`rows_to_events`), so
    the verdict matches what the coach saw on the confirmation card.
    """
    from services.ai.ai_recognizer import rows_to_events

    data = _load_rows(run.get("raw_rows"))
    score = data.get("score") if data else None
    if not isinstance(score, (list, tuple)) or len(score) != 2:
        return []
    gaps = []
    for side, value in zip(("left", "right"), score):
        rows = data.get(side)
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value <= 0 or not isinstance(rows, list) or not rows:
            continue
        goals, assists, _ = rows_to_events(rows, side, value)
        if goals and not assists:
            gaps.append({"side": side, "score": value, "rows": _compact_rows(rows)})
    return gaps


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

    with_rows = 0
    assist_gaps: list[dict] = []
    for r in ok_rows:
        if not r.get("raw_rows"):
            continue
        with_rows += 1
        for gap in find_assist_gaps(r):
            assist_gaps.append({"run_id": r.get("id"), "match_id": r.get("match_id"), **gap})

    return {
        "total": total,
        "with_rows": with_rows,
        "assist_gaps": assist_gaps,
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
        lines.append("")

    lines.extend(_format_assist_diagnostics(stats))
    return "\n".join(lines).rstrip()


_MAX_GAPS_SHOWN = 5
_MAX_GAP_ROWS_CHARS = 220


def _format_assist_diagnostics(stats: dict) -> list[str]:
    """The «lost assists» block of /ocr_stats: where the model's table reading has none."""
    esc = html.escape
    with_rows = stats.get("with_rows") or 0
    lines = ["<b>Диагностика ассистов</b>"]
    if not with_rows:
        lines.append("Строки таблиц сохраняются с последнего обновления — появятся после новых прогонов.")
        return lines
    gaps = stats.get("assist_gaps") or []
    lines.append(
        f"Строки таблиц сохранены для {with_rows} прогонов; "
        f"команда забила, а ассистов не прочитано: <b>{len(gaps)}</b>"
    )
    for gap in gaps[:_MAX_GAPS_SHOWN]:
        side = "слева" if gap["side"] == "left" else "справа"
        rows = gap["rows"]
        if len(rows) > _MAX_GAP_ROWS_CHARS:
            rows = rows[:_MAX_GAP_ROWS_CHARS].rstrip() + "…"
        lines.append(f"• матч {gap['match_id']}, {side}, голов {gap['score']}: <code>{esc(rows)}</code>")
    if len(gaps) > _MAX_GAPS_SHOWN:
        lines.append(f"…и ещё {len(gaps) - _MAX_GAPS_SHOWN}")
    if gaps:
        lines.append("<i>Полные строки матча: /ocr_stats raw &lt;номер матча&gt;</i>")
    return lines
