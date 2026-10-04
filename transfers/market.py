"""Рынок: урна (можно выкупить сразу) и каталог игроков справочника (можно предложить сделку).

Только чтение. Свободных агентов в системе как списка нет — они подаются по шаблону,
поэтому каталог — это справочник `transfer_players` (последняя известная карта игрока).
"""
from __future__ import annotations

import club_registry

from transfers import repo, requests as req_mod, suggest
from transfers.engine import format_k, norm_club, norm_player

CATALOG_LIMIT = 60
MAX_QUERY = suggest.MAX_QUERY
SORTS = ("ovr", "price", "price_desc", "name")
DEFAULT_SORT = "ovr"


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _matches_query(name: str, query: str) -> bool:
    key = norm_player(name)
    return suggest._rank(key, query) is not None or suggest._typo(key, query)


def _sorted(items: list[dict], sort: str, *, name_key: str, price_key: str) -> list[dict]:
    if sort == "name":
        return sorted(items, key=lambda i: norm_player(i[name_key]))
    if sort == "price":
        return sorted(items, key=lambda i: (i.get(price_key) is None, i.get(price_key) or 0))
    if sort == "price_desc":
        return sorted(items, key=lambda i: -(i.get(price_key) or 0))
    return sorted(items, key=lambda i: (-(i.get("ovr") or 0), norm_player(i[name_key])))


def market(user_id: int, *, q: str = "", ovr_min=None, ovr_max=None, club: str = "",
           sort: str = DEFAULT_SORT, limit: int = CATALOG_LIMIT) -> dict:
    """Рынок окна с фильтрами: запрос по имени, диапазон OVR, клуб игрока, сортировка."""
    sort = sort if sort in SORTS else DEFAULT_SORT
    query = norm_player((q or "")[:MAX_QUERY])
    lo, hi = _int_or_none(ovr_min), _int_or_none(ovr_max)
    club_key = norm_club(club_registry.resolve_team_name(club) or club) if (club or "").strip() else ""
    own = norm_club(req_mod._coach_club_or_none(user_id))
    urn_key = norm_club(req_mod.URN_CLUB)

    def passes(name: str, ovr, club_name) -> bool:
        if lo is not None and (ovr is None or ovr < lo):
            return False
        if hi is not None and (ovr is None or ovr > hi):
            return False
        if club_key and norm_club(club_name) != club_key:
            return False
        return not query or _matches_query(name, query)

    urn = req_mod.urn_items(user_id)
    urn_items = [i for i in urn["items"] if passes(i["player_name"], i.get("ovr"), i.get("from_club"))]
    urn_names = {norm_player(i["player_name"]) for i in urn["items"]}

    catalog = []
    for row in repo.list_catalog_players():
        holder = norm_club(row["last_club"])
        if (holder and holder in (own, urn_key)) or row["norm_name"] in urn_names:
            continue
        if not passes(row["player_name"], row["ovr"], row["last_club"]):
            continue
        price_k = row["price_k"]
        catalog.append({
            "player_name": row["player_name"], "club": row["last_club"], "ovr": row["ovr"],
            "price_k": price_k, "price": format_k(price_k) if price_k else None,
            "banned": bool(row["banned"]), "ban_reason": row["ban_reason"] if row["banned"] else None,
        })
    total = len(catalog)
    catalog = _sorted(catalog, sort, name_key="player_name", price_key="price_k")[:max(int(limit), 1)]
    urn_items = _sorted(urn_items, sort, name_key="player_name", price_key="buy_price_k")

    clubs = sorted({r["last_club"] for r in repo.list_catalog_players()
                    if r["last_club"] and norm_club(r["last_club"]) not in (own, urn_key)}
                   | {i["from_club"] for i in urn["items"] if i.get("from_club")}, key=str.lower)
    return {"window": urn["window"], "can_buy": urn["can_buy"], "own_club": req_mod._coach_club_or_none(user_id),
            "urn": urn_items, "catalog": catalog, "catalog_total": total, "clubs": clubs, "sort": sort}
