"""
services/outright_engine.py

Модель долгосрочных рынков Logovo.bet: победитель дивизиона, победитель кубка,
лучший бомбардир дивизиона и всей лиги.

Модуль чистый — ни базы, ни сети: на вход таблица, оставшиеся матчи, сетка или
голы игроков, на выход вероятности. Сбор данных и запись цен — в
`services/outright_service.py`, SQL — в `database.py`.

* Лига — Монте-Карло оставшихся матчей: голы каждой пары тянутся из Пуассона с
  теми же λ, что и у линии матча, поэтому разница и забитые (тай-брейки таблицы)
  разыгрываются вместе с очками.
* Кубок — точный расчёт по сетке, без симуляции: серии независимы, и вероятность
  клуба выйти из узла сетки — сумма по возможным соперникам. Серия в игре
  доигрывается от своего текущего счёта.
* Бомбардир — Монте-Карло голов до конца сезона с разбросом темпа игрока
  (гамма-апостериор, см. `scorer_rate_posterior`). Равенство на первом месте
  рассчитывается по dead heat, поэтому «вероятность» здесь — ожидаемая доля
  выплаты, а не шанс быть единоличным лидером: при 1/k цене честна именно она.

Цена — та же конвенция, что у линии: `1 / (p · маржа)`, но с потолком выше
матчевого: у аутсайдера долгосрочного рынка 100+ — нормальная цена.
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, Sequence

import numpy as np

from services.poisson_odds import TARGET_MARGIN

OUTRIGHT_MARGIN = TARGET_MARGIN
OUTRIGHT_MIN_ODD = 1.01
OUTRIGHT_MAX_ODD = 201.0

LEAGUE_SIMULATIONS = 20_000
SCORER_SIMULATIONS = 20_000

# Сила бомбардира: голы за матч клуба, стянутые к `SCORER_PRIOR_RATE` весом
# `SCORER_PRIOR_MATCHES` матчей. После двух туров 6 голов не означают три за игру.
SCORER_PRIOR_RATE = 0.35
SCORER_PRIOR_MATCHES = 3.0
# Неизвестные бомбардиры клуба (ещё не забивали): их доля уходит в «другого игрока».
PHANTOM_SCORERS_PER_TEAM = 2
PHANTOM_SCORER_RATE = 0.18


def price(prob: float, margin: float = OUTRIGHT_MARGIN) -> float:
    """Десятичный коэффициент `1 / (p · margin)` в пределах [1.01, 201]."""
    if prob is None or prob <= 0:
        return OUTRIGHT_MAX_ODD
    odd = 1.0 / (prob * margin)
    return round(min(OUTRIGHT_MAX_ODD, max(OUTRIGHT_MIN_ODD, odd)), 2)


# ─── Лига ────────────────────────────────────────────────────────────────────

def league_max_points(table: dict[str, dict], remaining: dict[str, int]) -> dict[str, int]:
    """Максимум очков, которого клуб ещё может достичь."""
    return {team: int(row.get("points") or 0) + 3 * int(remaining.get(team, 0)) for team, row in table.items()}


def league_eliminated(table: dict[str, dict], remaining: dict[str, int]) -> set[str]:
    """Клубы, которые уже не догонят лидера даже при всех победах.

    Равенство по очкам не исключает: дальше решают разница и забитые.
    """
    if not table:
        return set()
    leader = max(int(r.get("points") or 0) for r in table.values())
    best = league_max_points(table, remaining)
    return {team for team, pts in best.items() if pts < leader}


def simulate_league_winner(
    table: dict[str, dict],
    fixtures: Sequence[tuple[str, str, float, float]],
    n: int = LEAGUE_SIMULATIONS,
    seed: int | None = None,
) -> dict[str, float]:
    """Вероятность занять первое место.

    `table` — {клуб: {points, goals_scored, goals_conceded, wins}}, `fixtures` —
    оставшиеся матчи (клуб1, клуб2, λ1, λ2). Матч с клубом вне таблицы
    пропускается. Порядок в таблице — как в `get_standings`: очки, разница,
    забитые, победы; полное равенство делится поровну.
    """
    teams = list(table)
    if not teams:
        return {}
    idx = {t: i for i, t in enumerate(teams)}
    size = len(teams)
    rng = np.random.default_rng(seed)

    pts = np.tile(np.array([float(table[t].get("points") or 0) for t in teams]), (n, 1))
    gf = np.tile(np.array([float(table[t].get("goals_scored") or 0) for t in teams]), (n, 1))
    ga = np.tile(np.array([float(table[t].get("goals_conceded") or 0) for t in teams]), (n, 1))
    wins = np.tile(np.array([float(table[t].get("wins") or 0) for t in teams]), (n, 1))

    for t1, t2, l1, l2 in fixtures:
        a, b = idx.get(t1), idx.get(t2)
        if a is None or b is None or a == b:
            continue
        g1 = rng.poisson(max(0.05, float(l1)), n)
        g2 = rng.poisson(max(0.05, float(l2)), n)
        w1 = g1 > g2
        w2 = g2 > g1
        draw = ~(w1 | w2)
        pts[:, a] += 3 * w1 + draw
        pts[:, b] += 3 * w2 + draw
        wins[:, a] += w1
        wins[:, b] += w2
        gf[:, a] += g1
        ga[:, a] += g2
        gf[:, b] += g2
        ga[:, b] += g1

    gd = gf - ga
    # Лексикографический ключ одним числом: очки ≫ разница ≫ забитые ≫ победы.
    key = pts * 1e10 + (gd + 5_000) * 1e6 + gf * 1e3 + wins
    best = key.max(axis=1, keepdims=True)
    leaders = key == best
    share = leaders / leaders.sum(axis=1, keepdims=True)
    probs = share.mean(axis=0)
    return {teams[i]: float(probs[i]) for i in range(size)}


# ─── Кубок ───────────────────────────────────────────────────────────────────

def bo3_win_prob(p_game: float, wins1: int = 0, wins2: int = 0) -> float:
    """P(первый клуб возьмёт серию до двух побед) при текущем счёте серии."""
    p = min(1.0, max(0.0, float(p_game)))
    q = 1.0 - p
    if wins1 >= 2:
        return 1.0
    if wins2 >= 2:
        return 0.0
    if wins1 == 1 and wins2 == 1:
        return p
    if wins1 == 1:
        return 1.0 - q * q
    if wins2 == 1:
        return p * p
    return p * p + 2 * p * p * q


def cup_bracket_complete(series_count: int, stages_left: int) -> bool:
    """Сетка сходится к финалу: на стадии ровно 2^(оставшихся стадий) серий.

    Стадия, на которую вступают новые клубы (1/64 общего кубка → 1/32), этому
    не отвечает — такую сетку просчитать нельзя, пока следующая не посеяна.
    """
    return series_count >= 1 and series_count == 2 ** stages_left


def cup_winner_probs(
    series: Sequence[dict],
    game_prob: Callable[[str, str], float],
) -> dict[str, float]:
    """Вероятность взять кубок каждым клубом текущей стадии.

    `series` — серии текущей стадии в порядке номеров: team1_name, team2_name,
    team1_wins, team2_wins, winner_name. Следующая стадия сводит победителей
    соседних серий (1-я со 2-й, 3-я с 4-й…), как `--from-winners`.
    `game_prob(a, b)` — P(a выиграет одну игру у b).
    """
    cache: dict[tuple[str, str], float] = {}

    def series_prob(a: str, b: str) -> float:
        if (a, b) not in cache:
            p = bo3_win_prob(game_prob(a, b))
            cache[(a, b)] = p
            cache[(b, a)] = 1.0 - p
        return cache[(a, b)]

    nodes: list[dict[str, float]] = []
    for s in series:
        t1, t2 = s.get("team1_name"), s.get("team2_name")
        winner = s.get("winner_name")
        if winner:
            nodes.append({winner: 1.0})
            continue
        if not t1 or not t2:
            # Пустое место в паре — клуб проходит без игры.
            nodes.append({(t1 or t2): 1.0} if (t1 or t2) else {})
            continue
        p1 = bo3_win_prob(game_prob(t1, t2), int(s.get("team1_wins") or 0), int(s.get("team2_wins") or 0))
        nodes.append({t1: p1, t2: 1.0 - p1})

    while len(nodes) > 1:
        merged = []
        for i in range(0, len(nodes), 2):
            left = nodes[i]
            right = nodes[i + 1] if i + 1 < len(nodes) else {}
            if not right:
                merged.append(dict(left))
                continue
            out: dict[str, float] = {}
            for a, pa in left.items():
                for b, pb in right.items():
                    if pa <= 0 or pb <= 0:
                        continue
                    w = series_prob(a, b)
                    out[a] = out.get(a, 0.0) + pa * pb * w
                    out[b] = out.get(b, 0.0) + pa * pb * (1.0 - w)
            merged.append(out)
        nodes = merged
    return nodes[0] if nodes else {}


# ─── Бомбардир ───────────────────────────────────────────────────────────────

def scorer_rate_posterior(goals: int, team_played: int) -> tuple[float, float]:
    """Гамма-апостериор темпа игрока (голы за матч клуба): (shape, scale).

    Априор — `SCORER_PRIOR_RATE` весом `SCORER_PRIOR_MATCHES` матчей. Темп в
    симуляции тянется из этого распределения, а не берётся точкой: 13 голов за
    5 матчей — это сильный бомбардир, но не гарантированные 2.6 гола до конца
    сезона, и без разброса темпа лидер получал бы 90%+ при трёх четвертях
    сезона впереди.
    """
    shape = float(goals) + SCORER_PRIOR_RATE * SCORER_PRIOR_MATCHES
    return shape, 1.0 / (max(0, int(team_played)) + SCORER_PRIOR_MATCHES)


def scorer_rate(goals: int, team_played: int) -> float:
    """Голы за матч клуба, стянутые к априорной ставке (среднее апостериора)."""
    shape, scale = scorer_rate_posterior(goals, team_played)
    return shape * scale


def simulate_top_scorer(
    players: Sequence[dict],
    n: int = SCORER_SIMULATIONS,
    seed: int | None = None,
) -> list[float]:
    """Ожидаемая доля выплаты dead heat для каждого игрока.

    `players` — {goals, remaining} и либо `rate` (голы за матч, точкой), либо
    `shape`/`scale` — гамма-распределение темпа, которое разыгрывается в каждой
    симуляции заново. Сумма долей — 1 (если игроки есть). Порядок — как на входе.
    """
    if not players:
        return []
    size = len(players)
    goals = np.array([float(p.get("goals") or 0) for p in players])
    remaining = np.array([float(max(0, int(p.get("remaining") or 0))) for p in players])
    has_shape = np.array([p.get("shape") is not None for p in players])
    shape = np.array([float(p.get("shape") or 1.0) for p in players])
    scale = np.array([float(p.get("scale") or 0.0) for p in players])
    rate = np.array([max(0.0, float(p.get("rate") or 0)) for p in players])
    rng = np.random.default_rng(seed)
    total = np.zeros(size)
    # Кусками: у бомбардира лиги сотни игроков, и матрица n×игроки целиком
    # заняла бы десятки мегабайт ради одной суммы по строкам.
    chunk = max(1, min(n, 2_000_000 // max(1, size)))
    done = 0
    while done < n:
        rows = min(chunk, n - done)
        drawn = rng.gamma(shape, np.maximum(scale, 1e-12), size=(rows, size))
        lam = np.where(has_shape, drawn, rate) * remaining
        final = goals + rng.poisson(lam)
        leaders = final == final.max(axis=1, keepdims=True)
        total += (leaders / leaders.sum(axis=1, keepdims=True)).sum(axis=0)
        done += rows
    return [float(x) for x in total / n]


def top_scorer_leaders(totals: Iterable[tuple[str, int]]) -> list[str]:
    """Ключи игроков, делящих первое место (пусто — никто не забивал)."""
    rows = [(k, int(g or 0)) for k, g in totals]
    if not rows:
        return []
    best = max(g for _, g in rows)
    if best <= 0:
        return []
    return [k for k, g in rows if g == best]


def dead_heat_factor(winners: int) -> float:
    """Доля ставки, которая играет при равенстве `winners` победителей."""
    return 1.0 / winners if winners > 0 else 0.0


def normalize(probs: dict[str, float]) -> dict[str, float]:
    total = sum(v for v in probs.values() if v > 0)
    if total <= 0 or math.isclose(total, 0.0):
        return {k: 0.0 for k in probs}
    return {k: max(0.0, v) / total for k, v in probs.items()}
