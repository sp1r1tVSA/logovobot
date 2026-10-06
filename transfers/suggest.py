"""Автоподбор имени клуба и игрока для форм заявок в Mini App.

Только чтение: клубы берутся из реестра, игроки — из `squad_players` и пула `transfer_players`.
Результат — подсказка; сервер при создании заявки проверяет введённое имя сам.
"""
from __future__ import annotations

import difflib

import club_registry
import config
from transfers import repo
from transfers.engine import name_covers, norm_club, norm_player
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
    """Игроки под запрос.

    Если `own=True`, возвращаются строго игроки клуба тренера.
    Если передан `club`, возвращаются строго игроки указанного клуба.
    В обоих случаях игроки других клубов и свободного пула в выдачу НЕ попадают.
    Если `own=False` и `club=None`, поиск идёт по всей лиге и пулу свободных.
    """
    q = norm_player(query[:MAX_QUERY])
    if not q:
        return []

    target_scope: str | None = None
    if own:
        coach_club = _coach_club_or_none(user_id)
        if not coach_club:
            return []
        target_scope = norm_club(coach_club)
    elif club and club.strip():
        cq = club_registry.resolve_club_query(club.strip())
        resolved = cq.canonical or club_registry.resolve_team_name(club.strip()) or club.strip()
        target_scope = norm_club(resolved)
        if not target_scope:
            return []

    rows: dict[str, dict] = {}   # ключ имени игрока → лучшая запись
    known: dict[str, str] = {}   # имя команды из squad_players → ключ клуба (резолв один раз)
    for r in repo.list_squad_players():
        team = r["team_name"]
        if team not in known:
            known[team] = norm_club(club_registry.resolve_team_name(team) or team)
        if target_scope is not None:
            if known[team] != target_scope:
                continue
            key = norm_player(r["player_name"])
            rows[key] = {"name": r["player_name"], "club": team, "in_scope": True}
        else:
            key = norm_player(r["player_name"])
            rows.setdefault(key, {"name": r["player_name"], "club": team, "in_scope": False})

    if target_scope is None:
        for r in repo.list_pool_players():
            rows.setdefault(norm_player(r["player_name"]),
                            {"name": r["player_name"], "club": r.get("last_club"), "in_scope": False})

    found = []
    for key, rec in rows.items():
        rank = _rank(key, q)
        if rank is not None:
            found.append((rank, key, rec))
    if not found:
        found = [(4, key, rec) for key, rec in rows.items() if _typo(key, q)]
    found.sort(key=lambda x: x[:2])
    top = [rec for *_, rec in found[:limit]]
    index = _card_index()
    return [{"name": rec["name"], "club": rec["club"], "cards": _cards_for(index, rec)} for rec in top]


def _card_index() -> dict:
    """Карточки Renderz: `{"by_club": {клуб: {имя: [карты]}}, "by_name": {имя: [карты]}}`.

    Только выбранные версии (без дубля продаваемая/непродаваемая). Нет таблицы или она пуста — пусто.
    """
    by_club: dict[str, dict[str, list]] = {}
    by_name: dict[str, list] = {}
    try:
        cards = repo.list_player_cards()
    except Exception:
        return {"by_club": by_club, "by_name": by_name}
    for c in cards:
        card = {"ovr": c["ovr"], "tradable": bool(c["tradable"]), "program": c["program"],
                "position": c["position"]}
        by_name.setdefault(c["norm_name"], []).append(card)
        club = norm_club(club_registry.resolve_team_name(c["club"]) or c["club"]) if c["club"] else ""
        by_club.setdefault(club, {}).setdefault(c["norm_name"], []).append(card)
    return {"by_club": by_club, "by_name": by_name}


def _cards_for(index: dict, rec: dict) -> list[dict]:
    """Версии карточки игрока подсказки, по убыванию OVR. Неоднозначность — пустой список.

    В `squad_players` имена короткие («SAKA», «C. RONALDO»), на Renderz — полные («Bukayo Saka»),
    поэтому внутри клуба совпадением считается полное имя, покрывающее короткое (`name_covers`).
    Два игрока клуба под одну фамилию — не угадываем и OVR не подставляем.
    """
    key = norm_player(rec["name"])
    cards = index["by_name"].get(key)
    if cards is None and rec.get("club"):
        club = norm_club(club_registry.resolve_team_name(rec["club"]) or rec["club"])
        hits = [cs for full, cs in index["by_club"].get(club, {}).items() if name_covers(rec["name"], full)]
        cards = hits[0] if len(hits) == 1 else None
    return sorted(cards or [], key=lambda c: -c["ovr"])
