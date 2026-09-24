"""
services/ai/pick_review.py

Сверка «ИИ-прогноза» с сыгранными матчами: насколько оценки модели точнее
вероятности, заложенной в линию.

Исходы, показанные ИИ, лежат в журнале `ai_pick_log` (одна строка на исход,
последняя оценка до матча). Итог каждого считается здесь, тем же
`market_settler.evaluate_market_selection`, что рассчитывает купоны, — поэтому
исправленный счёт сразу меняет сверку. Возвраты и аннулирования (фора в ноль,
неизвестный рынок) в сверку не идут.

Сравнение честное: для одних и тех же исходов есть два числа — шанс по ИИ и
шанс по линии без маржи, — и у обоих считается Brier score, средний квадрат
промаха (0 — идеально, 0.25 — монетка на 50%). Кто ниже, тот точнее.
«Ценные» (шанс ИИ × кэф > 1) дополнительно проверяются рублём: сколько дала бы
ставка по 1 🪙 на каждый.
"""

from __future__ import annotations

import logging

import database
from services.ai.bet_picks import MARKET_GROUPS
from services.market_settler import evaluate_market_selection

logger = logging.getLogger(__name__)

MIN_SAMPLE = 30           # меньше исходов — вывод по Brier ещё шум
EVEN_MARGIN = 0.005       # разница Brier меньше этой — «на равных»
RECENT_LIMIT = 30
BUCKETS = (
    (0, 50, "до 50%"),
    (50, 60, "50–60%"),
    (60, 70, "60–70%"),
    (70, 80, "70–80%"),
    (80, 101, "80% и выше"),
)


def _outcome(row: dict) -> str:
    from services.settlement_engine import _cup_winner_side

    s1, s2 = row["player1_score"], row["player2_score"]
    try:
        return evaluate_market_selection(
            row["market_key"], row["selection_key"], s1, s2,
            match_status="finished",
            ht_score1=row.get("ht_score1"), ht_score2=row.get("ht_score2"),
            winner_side=_cup_winner_side(row, s1, s2),
        )
    except (ValueError, TypeError):
        logger.warning("AI review: cannot evaluate selection %s", row.get("selection_id"))
        return "voided"


def _stats(rows: list[dict]) -> dict:
    """Сводка по исходам с известным итогом (`won` 1/0 уже проставлен)."""
    n = len(rows)
    if not n:
        return {"count": 0, "won": 0, "hit_rate": None, "avg_ai": None, "avg_line": None,
                "brier_ai": None, "brier_line": None, "roi": None}
    won = sum(r["won"] for r in rows)
    ai = [r["probability"] / 100.0 for r in rows]
    line = [r["line_probability"] / 100.0 for r in rows]
    ys = [r["won"] for r in rows]
    profit = sum((r["odds"] - 1.0) if r["won"] else -1.0 for r in rows)
    return {
        "count": n,
        "won": won,
        "hit_rate": round(won / n * 100, 1),
        "avg_ai": round(sum(ai) / n * 100, 1),
        "avg_line": round(sum(line) / n * 100, 1),
        "brier_ai": round(sum((p - y) ** 2 for p, y in zip(ai, ys)) / n, 4),
        "brier_line": round(sum((p - y) ** 2 for p, y in zip(line, ys)) / n, 4),
        # Ставка по 1 🪙 на каждый исход по кэфу на момент прогноза.
        "roi": round(profit / n * 100, 1),
    }


def _verdict(total: dict) -> str:
    if total["count"] < MIN_SAMPLE:
        return "few"
    diff = total["brier_line"] - total["brier_ai"]
    if abs(diff) < EVEN_MARGIN:
        return "even"
    return "ai" if diff > 0 else "line"


def _by_market(settled: list[dict]) -> list[dict]:
    """Та же сводка по группам рынков вкладки (Исход, Тотал, Фора…), в их порядке.

    У каждой группы свой вердикт по тому же порогу MIN_SAMPLE: по одной группе
    выборка набирается медленнее, и честнее сказать «мало данных», чем судить
    по десятку исходов.
    """
    groups: dict[str, list[dict]] = {}
    for r in settled:
        gid = next((g for g, (_label, keys) in MARKET_GROUPS.items() if r["market_key"] in keys), "other")
        groups.setdefault(gid, []).append(r)
    order = [*MARKET_GROUPS, "other"]
    result = []
    for gid in order:
        part = groups.get(gid)
        if not part:
            continue
        stats = _stats(part)
        label = MARKET_GROUPS[gid][0] if gid in MARKET_GROUPS else "Прочее"
        result.append({"group": gid, "label": label, "verdict": _verdict(stats), **stats})
    return result


def summarize(rows: list[dict]) -> dict:
    """Чистая часть: журнал со счётом → сводка. Отдельно от БД ради тестов."""
    settled, voided = [], 0
    for row in rows:
        outcome = _outcome(row)
        if outcome not in ("won", "lost"):
            voided += 1
            continue
        settled.append({**row, "won": 1 if outcome == "won" else 0})

    total = _stats(settled)
    value = [r for r in settled if r["probability"] / 100.0 * r["odds"] > 1.0]
    buckets = []
    for lo, hi, label in BUCKETS:
        part = [r for r in settled if lo <= r["probability"] < hi]
        if part:
            buckets.append({"label": label, **_stats(part)})
    models: dict[str, list[dict]] = {}
    for r in settled:
        models.setdefault(r.get("model") or "—", []).append(r)

    recent = [{
        "selection_id": r["selection_id"],
        "match_id": r["match_id"],
        "division_name": r.get("division_name"),
        "round_number": r.get("round_number"),
        "team1": r.get("team1"),
        "team2": r.get("team2"),
        "score": f"{r['player1_score']}:{r['player2_score']}",
        "market_name": r.get("market_name"),
        "selection_name": r.get("selection_name"),
        "odds": r["odds"],
        "probability": r["probability"],
        "line_probability": r["line_probability"],
        "won": bool(r["won"]),
        "model": r.get("model"),
    } for r in settled[:RECENT_LIMIT]]

    return {
        "total": total,
        "verdict": _verdict(total),
        "min_sample": MIN_SAMPLE,
        "value": _stats(value),
        "buckets": buckets,
        "markets": _by_market(settled),
        "models": [{"model": m, **_stats(part)} for m, part in
                   sorted(models.items(), key=lambda kv: -len(kv[1]))],
        "voided": voided,
        "recent": recent,
    }


def get_review(division_ids: list[int] | None) -> dict:
    rows, pending = database.get_ai_pick_log(division_ids)
    return {**summarize(rows), "pending": pending}
