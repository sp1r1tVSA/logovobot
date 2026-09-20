"""
services/integrity_engine.py

Детектор договорных матчей («индекс подозрительности» ставки).

Чистая детерминированная логика: модуль не открывает соединений к БД и не ходит
в сеть — весь контекст ему передаёт `services/background_sync.scan_integrity_job`
готовыми словарями из `database.py`. Это делает движок целиком покрываемым
юнит-тестами и держит правило CLAUDE.md «весь SQL живёт в database.py».

Единица анализа — **нога ставки** (`bet_items`), а не купон целиком: в экспрессе
из пяти матчей договорным может быть ровно один.

Оценка складывается из двух проходов:

* онлайн — сразу после размещения: насколько выбранный исход невероятен по
  модели и насколько ставка не похожа на обычное поведение этого игрока;
* постматчевый — после подтверждения счёта: зашла ли нога, насколько невероятен
  фактический счёт и не зашли ли туда же другие.

Ничего не блокируется: движок только считает балл, объясняет его по признакам и
отдаёт наружу. Решение принимает супер-админ на экране `/admin_bets`.
"""

import math
import re
from typing import Any

# ─── Веса признаков ──────────────────────────────────────────────────────────
# Правятся разработчиками, а не эксплуатацией, поэтому это константы модуля, а
# не строки в risk_limits_config (та таблица про денежные лимиты).

WEIGHTS_ONLINE: dict[str, float] = {
    "improbability": 22.0,    # насколько исход невероятен по модели
    "stake_z": 18.0,          # сумма против собственной истории игрока
    "bankroll_share": 15.0,   # доля банка в ставке
    "odds_edge": 15.0,        # взятая цена против «честной»
    "market_novelty": 10.0,   # игрок раньше этот рынок не брал
    "timing": 10.0,           # ставка сразу после открытия линии
    "dormancy": 10.0,         # игрок молчал и вдруг вернулся
}

WEIGHTS_POST: dict[str, float] = {
    "score_improbability": 18.0,  # невероятность фактического счёта
    "co_movement": 12.0,          # тот же исход взяли и другие
    "payout_ratio": 10.0,         # во сколько раз выросла ставка
}

ONLINE_TOTAL = sum(WEIGHTS_ONLINE.values())   # 100
POST_TOTAL = sum(WEIGHTS_POST.values())       # 40

# В итоговом балле онлайн-часть урезается: «критично» дело должно получать
# только после того, как результат матча подтвердил подозрение.
ONLINE_CAP = 60.0

# Семейства признаков для правила двух семейств.
MODEL_FAMILY = ("improbability", "odds_edge")
BEHAVIOR_FAMILY = ("stake_z", "bankroll_share", "market_novelty", "timing", "dormancy")
FAMILY_MIN_RATIO = 0.25

# Холодный старт: у игрока с такой историей распределения ещё нет.
MIN_HISTORY = 5
COLD_START_FEATURES = ("stake_z", "market_novelty", "dormancy")

# Ниже этого балла дело не показывается в ленте (строка всё равно пишется —
# иначе джоба пересчитывала бы одни и те же ноги на каждом проходе).
CASE_MIN_SCORE = 30.0

SEVERITY_BANDS = ((85.0, "critical"), (70.0, "high"), (50.0, "medium"))

# Человеческие подписи признаков для карточки.
FEATURE_LABELS: dict[str, str] = {
    "improbability": "Невероятность исхода по модели",
    "stake_z": "Сумма против обычной для игрока",
    "bankroll_share": "Доля банка в ставке",
    "odds_edge": "Цена выгоднее честной",
    "market_novelty": "Новый для игрока рынок",
    "timing": "Ставка сразу после открытия линии",
    "dormancy": "Игрок долго не ставил",
    "score_improbability": "Невероятность фактического счёта",
    "co_movement": "Тот же исход взяли другие",
    "payout_ratio": "Кратность выплаты",
}


# ─── Вероятность выбранного исхода по модели ─────────────────────────────────

_TOTAL_RE = re.compile(r"^(over|under|tb|tm)_?(\d+)(?:[._](\d))?$")
_IND_TOTAL_RE = re.compile(r"^it([12])_(over|under)_(\d+(?:\.\d+)?)$")
_HANDICAP_RE = re.compile(r"^h([12])_(minus|plus)_(\d+(?:\.\d+)?)$")
_CORRECT_SCORE_RE = re.compile(r"^cs_(\d+)_(\d+)$")


def _parse_total(key: str) -> tuple[str, float] | None:
    """over_2.5 / tb25 / tm_2_5 → ('over'|'under', 2.5)."""
    m = _TOTAL_RE.match(key)
    if not m:
        return None
    side = "over" if m.group(1) in ("over", "tb") else "under"
    whole, frac = m.group(2), m.group(3)
    if frac is None:
        line = float(f"{whole[:-1]}.{whole[-1]}") if len(whole) > 1 else float(whole)
    else:
        line = float(f"{whole}.{frac}")
    return side, line


def _grid(pred: dict[str, Any]) -> list[tuple[int, int, float]]:
    """correct_scores {'2:1': 0.08} → [(2, 1, 0.08), …]."""
    out: list[tuple[int, int, float]] = []
    for key, prob in (pred.get("correct_scores") or {}).items():
        try:
            g1, g2 = key.split(":")
            out.append((int(g1), int(g2), float(prob)))
        except (ValueError, AttributeError):
            continue
    return out


def _grid_sum(pred: dict[str, Any], predicate) -> float | None:
    """Сумма вероятностей всех счетов, удовлетворяющих предикату."""
    cells = _grid(pred)
    if not cells:
        return None
    total = sum(p for g1, g2, p in cells)
    if total <= 0:
        return None
    hit = sum(p for g1, g2, p in cells if predicate(g1, g2))
    # Сетка усечена по числу голов, поэтому нормируем на её собственную массу.
    return max(0.0, min(1.0, hit / total))


def model_probability(selection_key: str | None, pred: dict[str, Any]) -> float | None:
    """
    Модельная вероятность конкретного исхода.

    Прямые рынки берутся из ансамбля, остальные (двойной шанс, инд. тоталы,
    форы, точный счёт) — суммированием сетки `correct_scores`. Неизвестный
    ключ даёт None: признак просто не участвует, а не подставляет догадку.
    """
    if not selection_key:
        return None
    key = str(selection_key).strip().lower()

    home = pred.get("home_probability")
    draw = pred.get("draw_probability")
    away = pred.get("away_probability")
    goals = pred.get("goals_markets") or {}

    direct: dict[str, Any] = {
        "p1": home, "1": home, "home": home,
        "x": draw, "draw": draw,
        "p2": away, "2": away, "away": away,
        "btts_yes": goals.get("btts_yes"), "both_yes": goals.get("btts_yes"),
        "btts_no": goals.get("btts_no"), "both_no": goals.get("btts_no"),
    }
    if key in direct and direct[key] is not None:
        return float(direct[key])

    if key in ("1x", "x1") and home is not None and draw is not None:
        return float(home) + float(draw)
    if key == "12" and home is not None and away is not None:
        return float(home) + float(away)
    if key in ("x2", "2x") and draw is not None and away is not None:
        return float(draw) + float(away)

    total = _parse_total(key)
    if total:
        side, line = total
        name = f"{'over' if side == 'over' else 'under'}_{line:g}".replace(".", "_")
        # goals_markets хранит ключи вида over_2_5
        val = goals.get(name)
        if val is not None:
            return float(val)
        if side == "over":
            return _grid_sum(pred, lambda a, b, l=line: (a + b) > l)
        return _grid_sum(pred, lambda a, b, l=line: (a + b) < l)

    m = _IND_TOTAL_RE.match(key)
    if m:
        side_idx, side, line = int(m.group(1)), m.group(2), float(m.group(3))
        if side == "over":
            return _grid_sum(pred, lambda a, b: (a if side_idx == 1 else b) > line)
        return _grid_sum(pred, lambda a, b: (a if side_idx == 1 else b) < line)

    m = _HANDICAP_RE.match(key)
    if m:
        team_idx, sign, line = int(m.group(1)), m.group(2), float(m.group(3))
        shift = -line if sign == "minus" else line
        if team_idx == 1:
            return _grid_sum(pred, lambda a, b: (a + shift) > b)
        return _grid_sum(pred, lambda a, b: (b + shift) > a)

    m = _CORRECT_SCORE_RE.match(key)
    if m:
        g1, g2 = int(m.group(1)), int(m.group(2))
        return _grid_sum(pred, lambda a, b: a == g1 and b == g2)

    return None


def score_probability(pred: dict[str, Any], goals1: int, goals2: int) -> float | None:
    """Модельная вероятность именно этого счёта."""
    if goals1 is None or goals2 is None:
        return None
    return _grid_sum(pred, lambda a, b: a == int(goals1) and b == int(goals2))


# ─── Вспомогательное ─────────────────────────────────────────────────────────

def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _parse_ts(value: Any) -> float | None:
    """'2026-09-20 12:30:00' → unix-секунды. Работает и с ISO-строкой с 'T'."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    import datetime
    text = str(value).strip().replace("T", " ")
    if "." in text:
        text = text.split(".", 1)[0]
    if "+" in text:
        text = text.split("+", 1)[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(
                tzinfo=datetime.timezone.utc
            ).timestamp()
        except ValueError:
            continue
    return None


def _mean_std(values: list[int]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(var)


def _overround(odds: list[float]) -> float:
    """Маржа рынка: Σ(1/k) − 1. Пустой или битый рынок → 0."""
    usable = [float(o) for o in odds if o and float(o) > 1.0 and math.isfinite(float(o))]
    if len(usable) < 2:
        return 0.0
    return max(0.0, sum(1.0 / o for o in usable) - 1.0)


def _true_implied(odd: float | None, overround: float) -> float | None:
    """Вероятность, очищенная от маржи."""
    try:
        val = float(odd)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val) or val <= 1.0:
        return None
    return (1.0 / val) / (1.0 + max(0.0, overround))


# ─── Признаки ────────────────────────────────────────────────────────────────

def compute_online_features(
    item: dict[str, Any],
    profile: dict[str, Any],
    pred: dict[str, Any] | None,
    market_odds: list[float] | None = None,
) -> dict[str, float | None]:
    """
    Онлайн-признаки в диапазоне [0, 1]. None — признак посчитать не из чего;
    такой признак получает нулевой вес и балл не поднимает.
    """
    feats: dict[str, float | None] = {}

    p_model = model_probability(item.get("selection_key") or item.get("outcome_type"), pred or {})

    # 1. Невероятность выбранного исхода.
    feats["improbability"] = None if p_model is None else _clip(1.0 - p_model)

    # 2. Сумма против собственной истории.
    amounts = [a for a in (profile.get("amounts") or []) if a]
    amount = float(item.get("amount") or 0)
    if len(amounts) >= MIN_HISTORY and amount > 0:
        mean, std = _mean_std(amounts)
        if std <= 0:
            # Игрок всегда ставит одинаково: любое отклонение заметно, но
            # нулевая дисперсия не должна давать бесконечный z.
            std = max(1.0, mean * 0.25)
        feats["stake_z"] = _clip((amount - mean) / std / 4.0)
    else:
        feats["stake_z"] = None

    # 3. Доля банка.
    balance_before = item.get("balance_before")
    try:
        balance_before = float(balance_before) if balance_before is not None else None
    except (TypeError, ValueError):
        balance_before = None
    if balance_before and balance_before > 0 and amount > 0:
        feats["bankroll_share"] = _clip(amount / balance_before)
    else:
        feats["bankroll_share"] = None

    # 4. Взятая цена против честной.
    odd = item.get("odds_at_placement") or item.get("odd")
    implied = _true_implied(odd, _overround(market_odds or []))
    if p_model is None or implied is None:
        feats["odds_edge"] = None
    else:
        feats["odds_edge"] = _clip((p_model - implied) / 0.25)

    # 5. Новый рынок.
    market_key = item.get("market_key")
    counts = profile.get("market_counts") or {}
    if not market_key or profile.get("bets_count", 0) < MIN_HISTORY:
        feats["market_novelty"] = None
    else:
        seen = int(counts.get(market_key, 0))
        feats["market_novelty"] = 1.0 if seen == 0 else (0.5 if seen < 3 else 0.0)

    # 6. Скорость: ставка в первые минуты после открытия линии.
    opened = _parse_ts(item.get("bets_opened_at"))
    placed = _parse_ts(item.get("placed_at"))
    if opened is None or placed is None or placed < opened:
        feats["timing"] = None
    else:
        delay = placed - opened
        if delay <= 600:
            feats["timing"] = 1.0
        elif delay >= 21600:
            feats["timing"] = 0.0
        else:
            feats["timing"] = _clip(1.0 - (delay - 600) / (21600 - 600))

    # 7. Спящий игрок внезапно вернулся.
    last_bet = _parse_ts(profile.get("last_bet_at"))
    if last_bet is None or placed is None or placed < last_bet:
        feats["dormancy"] = None
    else:
        days = (placed - last_bet) / 86400.0
        if days < 3:
            feats["dormancy"] = 0.0
        elif days >= 30:
            feats["dormancy"] = 1.0
        else:
            feats["dormancy"] = _clip((days - 3) / 27.0)

    return feats


def compute_post_features(
    item: dict[str, Any],
    pred: dict[str, Any] | None,
    volume: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    """Постматчевые признаки в диапазоне [0, 1]."""
    feats: dict[str, float | None] = {}

    p_score = score_probability(pred or {}, item.get("player1_score"), item.get("player2_score"))
    if p_score is None:
        feats["score_improbability"] = None
    elif p_score < 0.01:
        feats["score_improbability"] = 1.0
    else:
        feats["score_improbability"] = _clip((0.05 - p_score) / 0.04)

    vol = volume or {}
    match_amount = float(vol.get("match_amount") or 0)
    if match_amount > 0:
        feats["co_movement"] = _clip(float(vol.get("selection_amount") or 0) / match_amount)
    else:
        feats["co_movement"] = None

    amount = float(item.get("amount") or 0)
    payout = float(item.get("actual_payout") or 0)
    if amount > 0 and payout > amount:
        ratio = _clip(payout / amount, 1.0, 15.0)
        feats["payout_ratio"] = _clip(math.log(ratio) / math.log(15.0))
    else:
        feats["payout_ratio"] = None

    return feats


# ─── Сборка дела ─────────────────────────────────────────────────────────────

def _weigh(
    values: dict[str, float | None],
    base_weights: dict[str, float],
    dropped: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """
    Признаки → строки разбора с эффективным весом и вкладом в баллах.

    `dropped` — признаки, снятые осознанно (холодный старт). Их вес
    перераспределяется на остальные, чтобы шкала осталась прежней. Признаки,
    которые просто не из чего посчитать, веса не перераспределяют: недостаток
    данных должен балл понижать, а не раздувать.
    """
    kept_total = sum(w for name, w in base_weights.items() if name not in dropped)
    factor = (sum(base_weights.values()) / kept_total) if kept_total > 0 else 0.0

    rows: list[dict[str, Any]] = []
    for name, base in base_weights.items():
        value = values.get(name)
        if name in dropped:
            weight = 0.0
        else:
            weight = round(base * factor, 2)
        points = 0.0 if value is None else round(value * weight, 2)
        rows.append({
            "name": name,
            "label": FEATURE_LABELS.get(name, name),
            "value": None if value is None else round(float(value), 4),
            "weight": weight,
            "points": points,
            "available": value is not None and weight > 0,
        })
    return rows


def _family_ratio(rows: list[dict[str, Any]], family: tuple[str, ...]) -> float:
    """Доля набранного от максимума семейства. Нулевой максимум → 0."""
    members = [r for r in rows if r["name"] in family]
    ceiling = sum(r["weight"] for r in members)
    if ceiling <= 0:
        return 0.0
    return sum(r["points"] for r in members) / ceiling


def severity_for(total_score: float) -> str:
    for threshold, name in SEVERITY_BANDS:
        if total_score >= threshold:
            return name
    return "low"


def score_case(
    item: dict[str, Any],
    profile: dict[str, Any],
    pred: dict[str, Any] | None,
    market_odds: list[float] | None = None,
    volume: dict[str, Any] | None = None,
    resolved: bool = False,
) -> dict[str, Any]:
    """
    Полная оценка одной ноги ставки.

    `resolved=False` — онлайн-проход (только поведение и модель).
    `resolved=True` — постматчевый: добавляются признаки результата.

    Возвращает готовую к записи структуру. Балл может оказаться нулевым —
    строка всё равно нужна, иначе джоба пересчитывала бы эту ногу вечно.
    """
    low_confidence = int(profile.get("bets_count") or 0) < MIN_HISTORY
    dropped = COLD_START_FEATURES if low_confidence else ()

    online_values = compute_online_features(item, profile, pred, market_odds)
    online_rows = _weigh(online_values, WEIGHTS_ONLINE, dropped)
    online_score = round(sum(r["points"] for r in online_rows), 2)

    gate: str | None = None

    # Правило двух семейств: одинокий выброс — это просто крупная ставка.
    model_ok = _family_ratio(online_rows, MODEL_FAMILY) >= FAMILY_MIN_RATIO
    behavior_ok = _family_ratio(online_rows, BEHAVIOR_FAMILY) >= FAMILY_MIN_RATIO
    if not (model_ok and behavior_ok):
        gate = "families"

    post_rows: list[dict[str, Any]] = []
    post_score = 0.0
    if resolved:
        if str(item.get("item_status") or "").lower() != "won":
            # Нога не зашла — знать результат заранее было нечего.
            gate = "lost"
        else:
            post_values = compute_post_features(item, pred, volume)
            post_rows = _weigh(post_values, WEIGHTS_POST)
            post_score = round(sum(r["points"] for r in post_rows), 2)

    if gate:
        total_score = 0.0
    else:
        total_score = round(min(online_score, ONLINE_CAP) + post_score, 2)

    return {
        "bet_id": item.get("bet_id"),
        "bet_item_id": item.get("bet_item_id"),
        "user_id": item.get("user_id"),
        "match_id": item.get("match_id"),
        "division_id": item.get("division_id"),
        "season_id": item.get("season_id"),
        "online_score": online_score,
        "post_score": post_score,
        "total_score": total_score,
        "severity": severity_for(total_score),
        "stage": "resolved" if resolved else "online",
        "low_confidence": low_confidence,
        "gate": gate,
        "reportable": total_score >= CASE_MIN_SCORE and gate is None,
        "features": {
            "online": online_rows,
            "post": post_rows,
            "gate": gate,
            "model_probability": model_probability(
                item.get("selection_key") or item.get("outcome_type"), pred or {}
            ),
            "low_confidence": low_confidence,
        },
    }
