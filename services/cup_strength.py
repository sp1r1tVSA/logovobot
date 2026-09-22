"""
services/cup_strength.py

Сила клуба и цены общего кубка.

Лиговый индекс силы для кубка не годится дважды. Во-первых, `get_standings`
возвращает таблицу одного дивизиона, поэтому в паре Д4×Д5 одного из клубов в смеси
просто нет — он проваливается в предсезонный сид, и цена строится на половинном
знании. Во-вторых, индекс нормирован внутри дивизиона и разницу классов между
дивизионами не выражает вовсе: «первый снизу Д5» и «первый снизу Д1» получили бы
одну силу.

Здесь сила собирается из тех же кусков, что и в лиге (`_get_team_strength_score`:
предсезонный сид + место в таблице своего дивизиона), но к ней добавляется
`config.CUP_DIVISION_CLASS`, а `home_advantage` выключен: порядок в кубковой паре
задаёт жеребьёвка, и «написан слева» не имеет права делать клуб фаворитом.

Ничьих в кубке нет, поэтому вероятность выиграть игру — P(забил больше) плюс
половина вероятности ничьей: послематчевые считаются жребием, своей модели у них
нет. Основное время при этом остаётся основой для тоталов, ОЗ и форы: «ТМ2.5»
проигрывает при 2:2 независимо от того, кто взял серию. Тот же сдвиг — причина, по
которой рынка `X` в кубке нет: исход, который не наступает, нельзя ни выставить,
ни рассчитать.
"""

import logging
from typing import Optional

import config
import database
from services.betting_engine import _get_team_strength_score
from services.poisson_odds import (
    TARGET_MARGIN,
    MAX_GOALS_GRID,
    calculate_match_lambdas,
    generate_poisson_score_grid,
)

logger = logging.getLogger(__name__)

# Кубок играет без преимущества площадки — см. модуль-документ.
CUP_HOME_ADVANTAGE = 0.0

# Клубу без сид-рейтинга и без строки в таблице — нейтраль лиги, чтобы надбавка за
# класс оставалась единственным мнением о таком клубе.
_NEUTRAL_STRENGTH_POINTS = 10.0


def division_code_for_club(club: str, season_id: int | None = None) -> str | None:
    """Код дивизиона клуба (`DIV_1`..`DIV_5`) — ключ `config.CUP_DIVISION_CLASS`."""
    div_id = database.get_team_division_id(club, season_id=season_id)
    if div_id is None:
        return None
    division = database.get_division(div_id)
    if not division:
        return None
    return (division.get("code") or "").strip().upper() or None


def class_uplift(division_code: str | None) -> float:
    """Надбавка за класс дивизиона; 0.0 — дивизион неизвестен.

    Незнакомый дивизион не намеренно не «сильнейший» и не «слабейший»: клуб без
    привязки получает силу своего дивизиона без поправки, и это видно в прайсе,
    а не спрятано за произвольным числом.
    """
    if not division_code:
        return 0.0
    return float(config.CUP_DIVISION_CLASS.get(division_code, 0.0))


def cup_club_strength(club: str, season_id: int | None = None) -> float:
    """Сила клуба в s-поинтах: смесь сида и таблицы СВОЕГО дивизиона + класс."""
    target_season_id = season_id
    if target_season_id is None:
        act = database.get_active_season()
        target_season_id = act["id"] if act else 1

    div_id = database.get_team_division_id(club, season_id=target_season_id)
    standings: list[dict] = []
    if div_id is not None:
        try:
            standings = database.get_standings(division_id=div_id, season_id=target_season_id)
        except Exception as e:
            logger.warning("Could not load standings for cup club %s: %s", club, e)
    else:
        logger.debug("Cup club %s has no division; strength falls back to seed only", club)

    try:
        base = float(_get_team_strength_score(standings, club))
    except Exception as e:
        logger.warning("Cup strength fell back to neutral for %s: %s", club, e)
        base = _NEUTRAL_STRENGTH_POINTS

    code = division_code_for_club(club, season_id=target_season_id)
    return base + class_uplift(code)


def grid_probs(s1: float, s2: float) -> tuple[float, float, float]:
    """(P1, PX, P2) из совместной сетки Пуассона без преимущества поля."""
    l1, l2 = calculate_match_lambdas(s1, s2, home_advantage=CUP_HOME_ADVANTAGE)
    grid = generate_poisson_score_grid(l1, l2, max_goals=MAX_GOALS_GRID)
    p1 = px = p2 = 0.0
    for i in range(MAX_GOALS_GRID):
        for j in range(MAX_GOALS_GRID):
            mass = grid[i][j]
            if i > j:
                p1 += mass
            elif i == j:
                px += mass
            else:
                p2 += mass
    return p1, px, p2


def _split_draw(p1: float, px: float, p2: float) -> tuple[float, float]:
    """Вероятности выиграть игру: ничья разыгрывается послематчевыми как жребий."""
    return p1 + 0.5 * px, p2 + 0.5 * px


def cup_match_odds(club1: str, club2: str, season_id: int | None = None,
                   margin: float = TARGET_MARGIN) -> dict:
    """Цены одного кубкового матча: П1/П2 без `X`, плюс всё, что не зависит от ничьей.

    Тоталы, ОЗ, индивидуальные тоталы и фора считаются по основному времени — то
    есть по исходной сетке, где ничья есть. `X`, `1X`, `X2` и `12` не отдаются:
    первые три не наступают, четвёртый наступает всегда и при марже 7.5% был бы
    подарком игроку (рыночная вероятность 1.0 против заложенной 0.93).
    """
    s1 = cup_club_strength(club1, season_id=season_id)
    s2 = cup_club_strength(club2, season_id=season_id)
    l1, l2 = calculate_match_lambdas(s1, s2, home_advantage=CUP_HOME_ADVANTAGE)
    grid = generate_poisson_score_grid(l1, l2, max_goals=MAX_GOALS_GRID)
    max_g = MAX_GOALS_GRID

    p1 = px = p2 = 0.0
    total_counts: dict[int, float] = {}
    btts_yes = btts_no = 0.0
    it1_over = it2_over = 0.0
    h1_minus = h2_plus = h1_plus = h2_minus = 0.0
    for i in range(max_g):
        for j in range(max_g):
            mass = grid[i][j]
            if i > j:
                p1 += mass
            elif i == j:
                px += mass
            else:
                p2 += mass
            total_counts[i + j] = total_counts.get(i + j, 0.0) + mass
            if i > 0 and j > 0:
                btts_yes += mass
            else:
                btts_no += mass
            if i >= 2:
                it1_over += mass
            if j >= 2:
                it2_over += mass
            if i - j >= 2:
                h1_minus += mass
            # Ф2(+1.5) — дополнение к Ф1(-1.5): `(s2 + 1.5) > s1` из
            # services/market_settler.py означает `i - j <= 1`, а не зеркало.
            if i - j <= 1:
                h2_plus += mass
            if i - j >= -1:
                h1_plus += mass
            if j - i >= 2:
                h2_minus += mass

    def total_over(line: float) -> float:
        return sum(mass for goals, mass in total_counts.items() if goals > line)

    game1, game2 = _split_draw(p1, px, p2)
    return {
        "team1_name": club1,
        "team2_name": club2,
        "strength1": round(s1, 2),
        "strength2": round(s2, 2),
        "lambda1": round(l1, 3),
        "lambda2": round(l2, 3),
        # Основное время — для тоталов/ОЗ/форы.
        "p_main_1": round(p1, 4),
        "p_main_x": round(px, 4),
        "p_main_2": round(p2, 4),
        # Игра без ничьих — для П1/П2 и для развёртки серии.
        "p_game_1": round(game1, 4),
        "p_game_2": round(game2, 4),
        "odds": {
            "p1": _odd(game1, margin),
            "p2": _odd(game2, margin),
            "tb15": _odd(total_over(1.5), margin),
            "tm15": _odd(1.0 - total_over(1.5), margin),
            "tb25": _odd(total_over(2.5), margin),
            "tm25": _odd(1.0 - total_over(2.5), margin),
            "tb35": _odd(total_over(3.5), margin),
            "tm35": _odd(1.0 - total_over(3.5), margin),
            "btts_yes": _odd(btts_yes, margin),
            "btts_no": _odd(btts_no, margin),
            "it1_over_1.5": _odd(it1_over, margin),
            "it1_under_1.5": _odd(1.0 - it1_over, margin),
            "it2_over_1.5": _odd(it2_over, margin),
            "it2_under_1.5": _odd(1.0 - it2_over, margin),
            "h1_minus_1.5": _odd(h1_minus, margin),
            "h2_plus_1.5": _odd(h2_plus, margin),
            "h1_plus_1.5": _odd(h1_plus, margin),
            "h2_minus_1.5": _odd(h2_minus, margin),
        },
    }


def best_of_three_odds(club1: str, club2: str, season_id: int | None = None,
                       margin: float = TARGET_MARGIN) -> dict:
    """Цены серии Bo3 по вероятности выиграть отдельную игру.

    Развёртка стандартная для «до двух побед»: P(2:0) = p², P(2:1) = 2p²(1-p)
    (порядок «своя-чужая» фиксирован, поэтому коэффициент 2 перед вторым членом —
    это два разных счёта 2:1 и не 1:2), «проход» = сумма двух соседних исходов,
    «третья игра» = 2p(1-p).
    """
    match = cup_match_odds(club1, club2, season_id=season_id, margin=margin)
    p = match["p_game_1"]
    q = match["p_game_2"]

    p_2_0 = p * p
    p_2_1 = 2 * p * p * q
    p_1_2 = 2 * q * q * p
    p_0_2 = q * q
    qualify1 = p_2_0 + p_2_1
    qualify2 = p_1_2 + p_0_2
    third_game = p_1_2 + p_2_1

    return {
        "team1_name": club1,
        "team2_name": club2,
        "p_game_1": p,
        "p_game_2": q,
        "probs": {
            "series_2_0": round(p_2_0, 4),
            "series_2_1": round(p_2_1, 4),
            "series_1_2": round(p_1_2, 4),
            "series_0_2": round(p_0_2, 4),
            "qualify_1": round(qualify1, 4),
            "qualify_2": round(qualify2, 4),
            "third_game": round(third_game, 4),
        },
        "odds": {
            "p1": _odd(qualify1, margin),
            "p2": _odd(qualify2, margin),
            "series_2_0": _odd(p_2_0, margin),
            "series_2_1": _odd(p_2_1, margin),
            "series_1_2": _odd(p_1_2, margin),
            "series_0_2": _odd(p_0_2, margin),
            "over_2.5": _odd(third_game, margin),
            "under_2.5": _odd(1.0 - third_game, margin),
        },
    }


def _odd(prob: float, margin: float = TARGET_MARGIN) -> float:
    """Десятичный коэффициент с маржей: `1 / (p * margin)`.

    Конвенция взята у лиги (`poisson_odds._to_odd`) и она не косметическая: при
    перевёрнутой записи (`margin / p`) сумма воображаемых вероятностей полного
    покрытия равна `1 / margin` = 93%, то есть каждый купон кубка возвращал бы
    игроку 107.5% в среднем — маржа работала бы против дома.

    Вероятность 0.0 отдаётся как `None`, а не как бесконечность: выборка с
    `odds_value IS NULL` физически не вставляется в
    `market_selections.odds_value REAL NOT NULL`, поэтому исходы с нулевой
    вероятностью из росписи выпадают — вызывающий обязан пропустить такой ключ.
    """
    if prob is None or prob <= 0.0:
        return None
    return round(max(1.01, min(25.0, 1.0 / (prob * margin))), 2)
