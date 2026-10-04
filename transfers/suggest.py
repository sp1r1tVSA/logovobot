"""Автоподбор имени клуба и игрока для форм заявок в Mini App.

Только чтение: клубы берутся из реестра, игроки — из `squad_players` и пула `transfer_players`.
Результат — подсказка; сервер при создании заявки проверяет введённое имя сам.
"""
from __future__ import annotations

import difflib

import club_registry
import config
from transfers import repo
from transfers.engine import norm_club, norm_player
from transfers.requests import URN_CLUB, _coach_club_or_none

LIMIT = 8
MAX_QUERY = 60
_TYPO_CUTOFF = 0.75


def _rank(key: str, query: str) -> int | None:
    """0 — начало имени, 1 — начало слова, 2 — вхождение; `None` — не подходит."""
    if not key:
        return None
    if key.startswith(query):
        return 0
    if any(word.startswith(query) for word in key.split()):
        return 1
    if query in key:
        return 2
    return None


def _typo(key: str, query: str) -> bool:
    """Опечатка: запрос близок к целому имени или к одному из его слов."""
    if len(query) < 4:
        return False
    return any(difflib.SequenceMatcher(None, query, part).ratio() >= _TYPO_CUTOFF
               for part in [key, *key.split()])


def clubs(user_id: int, query: str, limit: int = LIMIT) -> list[dict]:
    """Клубы реестра, подходящие под запрос: по имени и по псевдонимам («Ман Сити», «МЮ»)."""
    q = norm_club(query[:MAX_QUERY])
    if not q:
        return []
    own = norm_club(_coach_club_or_none(user_id))
    registry = [c for c in (getattr(config, "CLUB_REGISTRY", None) or [])
                if norm_club(c) not in (own, norm_club(URN_CLUB))]
    found: dict[str, int] = {}
    for name in registry:
        rank = _rank(norm_club(name), q)
        if rank is not None:
            found[name] = rank
    for alias, canon in club_registry.TEAM_ALIASES.items():
        rank = _rank(alias, q)
        if rank is not None and canon in registry:
            found[canon] = min(found.get(canon, 3), rank + 1)
    if not found:
        for name in registry:
            if _typo(norm_club(name), q):
                found[name] = 4
    ranked = sorted(found.items(), key=lambda kv: (kv[1], norm_club(kv[0])))
    return [{"name": name} for name, _ in ranked[:limit]]


def players(user_id: int, query: str, *, club: str | None = None, own: bool = False,
            limit: int = LIMIT) -> list[dict]:
    """Игроки под запрос. Сначала состав нужного клуба (`club` или свой при `own`), затем вся лига."""
    q = norm_player(query[:MAX_QUERY])
    if not q:
        return []
    scope = norm_club(_coach_club_or_none(user_id) if own else club)

    rows: dict[str, dict] = {}   # ключ имени игрока → лучшая запись
    known: dict[str, str] = {}   # имя команды из squad_players → ключ клуба (резолв один раз)
    for r in repo.list_squad_players():
        team = r["team_name"]
        if team not in known:
            known[team] = norm_club(club_registry.resolve_team_name(team) or team)
        key = norm_player(r["player_name"])
        in_scope = bool(scope) and known[team] == scope
        prev = rows.get(key)
        if prev is None or (in_scope and not prev["in_scope"]):
            rows[key] = {"name": r["player_name"], "club": team, "in_scope": in_scope}
    for r in repo.list_pool_players():
        rows.setdefault(norm_player(r["player_name"]),
                        {"name": r["player_name"], "club": r.get("last_club"), "in_scope": False})

    found = []
    for key, rec in rows.items():
        rank = _rank(key, q)
        if rank is not None:
            found.append((not rec["in_scope"], rank, key, rec))
    if not found:
        found = [(not rec["in_scope"], 4, key, rec) for key, rec in rows.items() if _typo(key, q)]
    found.sort(key=lambda x: x[:3])
    return [{"name": rec["name"], "club": rec["club"]} for *_, rec in found[:limit]]
