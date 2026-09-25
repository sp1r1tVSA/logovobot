"""
services/outright_service.py

Долгосрочные рынки Logovo.bet: сбор данных, цены и авторасчёт.

Задача `refresh_outrights` (фоновая, раз в пару минут) проходит по рынкам
активного сезона: победитель каждого дивизиона, общий кубок и кубки
дивизионов, бомбардир каждого дивизиона и всей лиги. Рынок пересчитывается,
только когда изменился отпечаток его состояния (`database.get_outright_fingerprint`)
или версия модели — иначе прогон почти бесплатный. Пока цена не пересчитана,
`place_outright_bet` отказывает с `OUTRIGHT_REPRICING`, поэтому старую цену
после подтверждённого матча не поймать.

Сид симуляции берётся из отпечатка: одинаковое состояние даёт одинаковую цену,
и цена не «дрожит» между прогонами.

Модель — `services/outright_engine.py`, SQL — `database.py`.
"""

from __future__ import annotations

import logging
import threading

import database
from constants import CUP_STAGE_ORDER
from services import outright_engine as engine
from services.betting_engine import _get_team_strength_score
from services.cup_strength import _split_draw, class_uplift, grid_probs
from services.poisson_odds import calculate_match_lambdas

logger = logging.getLogger(__name__)

# Меняется при любой правке модели — все рынки пересчитаются на следующем прогоне.
MODEL_VERSION = 1

REFRESH_INTERVAL_SECONDS = 120

# Рынок бомбардира: сколько игроков показывать отдельной строкой.
SCORER_MIN_PROBABILITY = 0.005
SCORER_MAX_SELECTIONS = 40
OTHER_PLAYER_NAME = "Другой игрок"

_refresh_lock = threading.Lock()


class _Context:
    """Кэш одного прогона: таблицы, матчи и сила клубов грузятся по разу."""

    def __init__(self, season_id: int):
        self.season_id = season_id
        self.divisions = [d for d in database.get_divisions(is_active=True)]
        self._standings: dict[int, list[dict]] = {}
        self._league: dict[int, dict] = {}
        self._strength: dict[str, float] = {}
        self._club_div: dict[str, int | None] = {}

    def division(self, division_id: int | None) -> dict | None:
        return next((d for d in self.divisions if d["id"] == division_id), None)

    def standings(self, division_id: int) -> list[dict]:
        if division_id not in self._standings:
            self._standings[division_id] = database.get_standings(division_id=division_id,
                                                                  season_id=self.season_id)
        return self._standings[division_id]

    def league(self, division_id: int) -> dict:
        if division_id not in self._league:
            self._league[division_id] = database.get_outright_league_fixtures(division_id, self.season_id)
        return self._league[division_id]

    def club_division(self, club: str) -> int | None:
        if club not in self._club_div:
            self._club_div[club] = database.get_team_division_id(club, season_id=self.season_id)
        return self._club_div[club]

    def league_strength(self, division_id: int, club: str) -> float:
        return float(_get_team_strength_score(self.standings(division_id), club))

    def cup_strength(self, club: str) -> float:
        """Та же сила, что у кубковой линии: сид + таблица своего дивизиона + класс."""
        if club not in self._strength:
            div = self.club_division(club)
            standings = self.standings(div) if div is not None else []
            try:
                base = float(_get_team_strength_score(standings, club))
            except Exception:
                logger.warning("Outright cup strength fell back to neutral for %s", club)
                base = 10.0
            code = ((self.division(div) or {}).get("code") or "").strip().upper() or None
            self._strength[club] = base + class_uplift(code)
        return self._strength[club]


# ─── Лига ────────────────────────────────────────────────────────────────────

def _league_table(ctx: _Context, division_id: int) -> dict[str, dict]:
    return {row["team_name"]: row for row in ctx.standings(division_id) if row.get("team_name")}


def _league_finished(ctx: _Context, division_id: int) -> bool:
    """Чемпионат дивизиона доигран: несыгранных матчей нет, все туры закрыты.

    Страховка от пустого расписания в начале сезона: каждый клуб должен сыграть
    хотя бы круг.
    """
    info = ctx.league(division_id)
    table = _league_table(ctx, division_id)
    if info["fixtures"] or info["open_rounds"] or not table:
        return False
    return min(int(r.get("played") or 0) for r in table.values()) >= len(table) - 1


def _remaining_by_team(fixtures) -> dict[str, int]:
    out: dict[str, int] = {}
    for t1, t2 in fixtures:
        out[t1] = out.get(t1, 0) + 1
        out[t2] = out.get(t2, 0) + 1
    return out


def price_division_winner(ctx: _Context, division: dict, fingerprint: str) -> list[dict]:
    div_id = division["id"]
    table = _league_table(ctx, div_id)
    if not table:
        return []
    fixtures = ctx.league(div_id)["fixtures"]
    priced = []
    for t1, t2 in fixtures:
        l1, l2 = calculate_match_lambdas(ctx.league_strength(div_id, t1), ctx.league_strength(div_id, t2))
        priced.append((t1, t2, l1, l2))
    probs = engine.simulate_league_winner(table, priced, seed=_seed(fingerprint))
    eliminated = engine.league_eliminated(table, _remaining_by_team(fixtures))
    ranked = sorted(table, key=lambda t: -probs.get(t, 0.0))
    return [{
        "key": database.outright_club_key(team),
        "name": team,
        "team_name": team,
        "division_id": div_id,
        "probability": probs.get(team, 0.0),
        "model_odds": engine.price(probs.get(team, 0.0)),
        "eliminated": team in eliminated or probs.get(team, 0.0) <= 0,
        "sort_order": i,
    } for i, team in enumerate(ranked)]


def league_winner_factors(ctx: _Context, division_id: int) -> dict[str, float]:
    """Победитель доигранного дивизиона по порядку `get_standings`; полное равенство — dead heat."""
    rows = ctx.standings(division_id)
    if not rows:
        return {}

    def key(r):
        gd = (r.get("goals_scored") or 0) - (r.get("goals_conceded") or 0)
        return (r.get("points") or 0, gd, r.get("goals_scored") or 0, r.get("wins") or 0)

    best = max(key(r) for r in rows)
    leaders = [database.outright_club_key(r["team_name"]) for r in rows if key(r) == best]
    return {k: engine.dead_heat_factor(len(leaders)) for k in leaders}


# ─── Кубок ───────────────────────────────────────────────────────────────────

def _cup_current_stage(stages: list[dict]) -> dict | None:
    """Самая поздняя стадия, сетку которой можно довести до финала."""
    last_order = max(CUP_STAGE_ORDER.values())
    for st in sorted(stages, key=lambda s: -s["stage_order"]):
        if engine.cup_bracket_complete(len(st["series"]), last_order - st["stage_order"]):
            return st
    return None


def price_cup_winner(ctx: _Context, cup_division: int | None) -> tuple[list[dict], dict | None]:
    """Исходы рынка кубка и стадия, по которой он посчитан (None — считать не по чему)."""
    stages = database.get_outright_cup_bracket(cup_division, ctx.season_id)
    stage = _cup_current_stage(stages)
    if stage is None:
        return [], None

    def game_prob(a: str, b: str) -> float:
        p1, px, p2 = grid_probs(ctx.cup_strength(a), ctx.cup_strength(b))
        return _split_draw(p1, px, p2)[0]

    probs = engine.cup_winner_probs(stage["series"], game_prob)
    clubs: list[str] = []
    for s in stage["series"]:
        for name in (s["team1_name"], s["team2_name"]):
            if name and name not in clubs:
                clubs.append(name)
    ranked = sorted(clubs, key=lambda c: -probs.get(c, 0.0))
    selections = []
    for i, club in enumerate(ranked):
        canon = database.resolve_team_name(club) or club
        p = probs.get(club, 0.0)
        selections.append({
            "key": database.outright_club_key(club),
            "name": canon,
            "team_name": canon,
            "division_id": ctx.club_division(canon),
            "probability": p,
            "model_odds": engine.price(p),
            "eliminated": p <= 0,
            "sort_order": i,
        })
    return selections, stage


def cup_winner_factors(stage: dict | None) -> dict[str, float]:
    if not stage or stage["stage"] != "final" or len(stage["series"]) != 1:
        return {}
    winner = stage["series"][0].get("winner_name")
    return {database.outright_club_key(winner): 1.0} if winner else {}


# ─── Бомбардир ───────────────────────────────────────────────────────────────

def _scorer_scope(ctx: _Context, division_id: int | None) -> list[int]:
    """Дивизионы рынка бомбардира. У лиги — только те, где есть клубы: пустой
    дивизион никогда не «доиграется» и навсегда задержал бы расчёт."""
    if division_id is not None:
        return [division_id]
    return [d["id"] for d in ctx.divisions if ctx.standings(d["id"])]


def price_top_scorer(ctx: _Context, division_id: int | None, fingerprint: str,
                     existing_keys: set[str]) -> list[dict]:
    """Исходы рынка бомбардира дивизиона (или лиги при `division_id=None`).

    Игроки клуба разыгрывают его оставшиеся матчи; ещё не забивавшие игроки
    клуба — фантомы, их доля и доля всех игроков вне списка уходит в «Другого
    игрока». Уже выставленный исход остаётся в списке всегда: на него могут
    быть ставки.
    """
    totals = database.get_outright_scorer_totals(division_id, ctx.season_id)
    if not totals:
        return []
    played: dict[str, int] = {}
    remaining: dict[str, int] = {}
    for div in _scorer_scope(ctx, division_id):
        for row in ctx.standings(div):
            key = database.outright_club_key(row["team_name"])
            played[key] = int(row.get("played") or 0)
            remaining.setdefault(key, 0)
        for club, n in _remaining_by_team(ctx.league(div)["fixtures"]).items():
            key = database.outright_club_key(club)
            remaining[key] = remaining.get(key, 0) + n

    players = []
    for t in totals:
        club_key = t["key"].split("|", 1)[0]
        shape, scale = engine.scorer_rate_posterior(t["goals"], played.get(club_key, 0))
        players.append({"goals": t["goals"], "remaining": remaining.get(club_key, 0),
                        "shape": shape, "scale": scale})
    phantoms = [{"goals": 0, "rate": engine.PHANTOM_SCORER_RATE, "remaining": n}
                for n in remaining.values() for _ in range(engine.PHANTOM_SCORERS_PER_TEAM) if n > 0]
    shares = engine.simulate_top_scorer(players + phantoms, seed=_seed(fingerprint))
    player_shares = shares[:len(totals)]
    other = sum(shares[len(totals):])

    leader_goals = max(t["goals"] for t in totals)
    ranked = sorted(range(len(totals)), key=lambda i: -player_shares[i])
    named = set()
    for i in ranked:
        if len(named) >= SCORER_MAX_SELECTIONS or player_shares[i] < SCORER_MIN_PROBABILITY:
            break
        named.add(i)
    named |= {i for i, t in enumerate(totals) if t["key"] in existing_keys}

    selections = []
    for order, i in enumerate(ranked):
        t = totals[i]
        if i not in named:
            other += player_shares[i]
            continue
        club_key = t["key"].split("|", 1)[0]
        dead = remaining.get(club_key, 0) == 0 and t["goals"] < leader_goals
        p = 0.0 if dead else player_shares[i]
        selections.append({
            "key": t["key"],
            "name": t["player_name"],
            "team_name": t["team_name"],
            "division_id": t["division_id"],
            "probability": p,
            "model_odds": engine.price(p),
            "eliminated": dead or p <= 0,
            "sort_order": order,
        })
    selections.append({
        "key": database.OUTRIGHT_OTHER_KEY,
        "name": OTHER_PLAYER_NAME,
        "team_name": None,
        "division_id": None,
        "probability": other,
        "model_odds": engine.price(other),
        "eliminated": other <= 0,
        "sort_order": len(selections),
    })
    return selections


def top_scorer_factors(ctx: _Context, division_id: int | None, selection_keys: set[str]) -> dict[str, float]:
    """Доли dead heat по ключам исходов; неназванные лидеры складываются в «Другого игрока»."""
    totals = database.get_outright_scorer_totals(division_id, ctx.season_id)
    leaders = engine.top_scorer_leaders((t["key"], t["goals"]) for t in totals)
    if not leaders:
        return {}
    share = engine.dead_heat_factor(len(leaders))
    out: dict[str, float] = {}
    for key in leaders:
        target = key if key in selection_keys else database.OUTRIGHT_OTHER_KEY
        out[target] = out.get(target, 0.0) + share
    return out


# ─── Прогон ──────────────────────────────────────────────────────────────────

def _seed(fingerprint: str) -> int:
    return int(fingerprint[:12], 16) if fingerprint else 0


def _market_specs(ctx: _Context) -> list[dict]:
    specs = []
    for d in ctx.divisions:
        specs.append({"type": "division_winner", "scope": f"D{d['id']}", "division_id": d["id"],
                      "title": f"Победитель — {d['name']}"})
    specs.append({"type": "cup_winner", "scope": "CUP", "division_id": None, "title": "Победитель общего кубка"})
    for d in ctx.divisions:
        specs.append({"type": "cup_winner", "scope": f"CUP_D{d['id']}", "division_id": d["id"],
                      "title": f"Победитель кубка — {d['name']}"})
    for d in ctx.divisions:
        specs.append({"type": "division_top_scorer", "scope": f"D{d['id']}", "division_id": d["id"],
                      "title": f"Лучший бомбардир — {d['name']}"})
    specs.append({"type": "league_top_scorer", "scope": "LEAGUE", "division_id": None,
                  "title": "Лучший бомбардир лиги"})
    return specs


def _settle_by_keys(market: dict, factors: dict[str, float]) -> None:
    by_key = {s["selection_key"]: s["id"] for s in market["selections"]}
    ids = {by_key[k]: f for k, f in factors.items() if k in by_key}
    if not ids:
        logger.warning("Outright market #%s finished but its winner has no selection; left for an admin",
                       market["id"])
        return
    ok, res = database.settle_outright_market(market["id"], ids)
    if ok:
        logger.info("Outright market #%s («%s») settled automatically: %s", market["id"], market["title"], res)
    else:
        logger.warning("Outright market #%s auto-settle failed: %s", market["id"], res)


def refresh_outrights(force: bool = False) -> dict:
    """Пересчитать цены изменившихся рынков и рассчитать доигранные. Синхронная — зовётся из потока."""
    if not _refresh_lock.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        return _refresh(force)
    finally:
        _refresh_lock.release()


def _refresh(force: bool) -> dict:
    season = database.get_active_season()
    if not season:
        return {"skipped": "no_season"}
    ctx = _Context(season["id"])
    existing = {(m["market_type"], m["scope_key"]): m for m in database.get_outright_markets(ctx.season_id)}
    summary = {"priced": 0, "unchanged": 0, "settled": 0, "failed": 0}

    for spec in _market_specs(ctx):
        market = existing.get((spec["type"], spec["scope"]))
        if market and market["status"] in ("settled", "voided"):
            continue
        try:
            fp = database.get_outright_fingerprint(ctx.season_id, spec["type"], spec["division_id"])
            fresh = market and market["model_fingerprint"] == fp and market["model_version"] == MODEL_VERSION
            if fresh and not force:
                summary["unchanged"] += 1
            else:
                selections, stage = _price(ctx, spec, fp, market)
                if not selections:
                    continue
                database.sync_outright_market(ctx.season_id, spec["type"], spec["scope"], spec["division_id"],
                                              spec["title"], fp, MODEL_VERSION, selections)
                summary["priced"] += 1
            if _auto_settle(ctx, spec):
                summary["settled"] += 1
        except Exception:
            summary["failed"] += 1
            logger.exception("Outright market %s/%s failed to refresh", spec["type"], spec["scope"])
    if summary["priced"] or summary["settled"] or summary["failed"]:
        logger.info("Outrights refreshed: %s", summary)
    return summary


def _price(ctx: _Context, spec: dict, fp: str, market: dict | None) -> tuple[list[dict], dict | None]:
    mtype = spec["type"]
    if mtype == "division_winner":
        return price_division_winner(ctx, ctx.division(spec["division_id"]), fp), None
    if mtype == "cup_winner":
        return price_cup_winner(ctx, spec["division_id"])
    keys = {s["selection_key"] for s in (market or {}).get("selections", [])}
    return price_top_scorer(ctx, spec["division_id"], fp, keys), None


def _auto_settle(ctx: _Context, spec: dict) -> bool:
    market = next((m for m in database.get_outright_markets(ctx.season_id)
                   if m["market_type"] == spec["type"] and m["scope_key"] == spec["scope"]), None)
    if not market or market["status"] in ("settled", "voided"):
        return False
    mtype = spec["type"]
    if mtype == "division_winner":
        if not _league_finished(ctx, spec["division_id"]):
            return False
        factors = league_winner_factors(ctx, spec["division_id"])
    elif mtype == "cup_winner":
        stages = database.get_outright_cup_bracket(spec["division_id"], ctx.season_id)
        final = next((s for s in stages if s["stage"] == "final"), None)
        factors = cup_winner_factors(final)
    else:
        if not all(_league_finished(ctx, d) for d in _scorer_scope(ctx, spec["division_id"])):
            return False
        factors = top_scorer_factors(ctx, spec["division_id"],
                                     {s["selection_key"] for s in market["selections"]})
    if not factors:
        return False
    _settle_by_keys(market, factors)
    return True


async def refresh_outrights_job(context) -> None:
    """Фоновая задача JobQueue: прогон в потоке, чтобы Монте-Карло не держал event loop."""
    import asyncio
    try:
        await asyncio.to_thread(refresh_outrights)
    except Exception:
        logger.exception("Outright refresh job failed")
