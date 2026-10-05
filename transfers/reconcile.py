"""Сверка составов: одобренные заявки окна против живых `squad_players`.

Без Telegram и без записи. Одобрение состав не трогает, а `squad_players` могут править
руками в обход ТО, поэтому перед стартом тура полезно увидеть расхождения:

* `not_applied` — заявка одобрена, но к составу не применена;
* `missing`     — применена, а игрока нет в клубе, куда он ушёл;
* `extra`       — применена, а игрок всё ещё числится в клубе, который не должен его держать;
* `duplicate`   — игрок заявки значится больше чем в одном клубе, а итог по нему ещё не применён.

Ожидаемое место игрока — по его последней одобренной заявке, меняющей состав (по времени
решения, затем по номеру). Проверяются только игроки заявок этого окна: одинаковые имена в
остальных составах — не повод для тревоги.
"""

from __future__ import annotations

from dataclasses import dataclass

from club_registry import resolve_team_name
from transfers import repo, squad
from transfers.engine import norm_club, norm_player

KIND_ORDER = ("not_applied", "missing", "extra", "duplicate")
KIND_LABELS = {
    "not_applied": "Одобрена, но не применена к составу",
    "missing": "Нет в клубе, куда перешёл",
    "extra": "Остался в клубе, который покинул",
    "duplicate": "Числится в двух клубах",
}


@dataclass
class Issue:
    kind: str
    player: str
    text: str
    transfer_id: int | None = None


def _actual_clubs() -> dict[str, dict[str, str]]:
    """{норм. игрок: {норм. клуб: имя клуба как в составе}} по всем строкам `squad_players`."""
    where: dict[str, dict[str, str]] = {}
    for row in repo.list_squad_players():
        club = resolve_team_name(row["team_name"]) or row["team_name"]
        where.setdefault(norm_player(row["player_name"]), {})[norm_club(club)] = club
    return where


def _expected(t: dict) -> str | None:
    """Клуб, где игрок должен оказаться после заявки; None — ни в одном клубе лиги."""
    last = None
    for op, club in squad._steps(t):
        last = club if op == "add" else None
    return last


def reconcile(window_id: int) -> list[Issue]:
    """Расхождения окна, сгруппированные по виду и отсортированные по игроку."""
    approved = [t for t in repo.list_transfers(window_id, statuses=("approved",)) if squad.changes_squad(t)]
    approved.sort(key=lambda t: (t.get("decided_at") or t.get("created_at") or "", t["id"]))
    last_by_player: dict[str, dict] = {}
    for t in approved:
        last_by_player[norm_player(t["player_name"])] = t

    actual = _actual_clubs()
    issues: list[Issue] = []
    for key, t in last_by_player.items():
        name = t["player_name"]
        here = actual.get(key, {})
        if not t.get("squad_applied_at"):
            issues.append(Issue("not_applied", name, f"#{t['id']} {name} — {_route(t)}", t["id"]))
            if len(here) > 1:
                issues.append(Issue("duplicate", name, f"{name} — {', '.join(sorted(here.values()))}", t["id"]))
            continue
        expected = _expected(t)
        want = norm_club(expected) if expected else None
        if want and want not in here:
            issues.append(Issue("missing", name, f"#{t['id']} {name} должен быть в {expected}, его там нет",
                                t["id"]))
        for club_key, club in sorted(here.items()):
            if club_key != want:
                issues.append(Issue("extra", name, f"#{t['id']} {name} всё ещё числится в {club}", t["id"]))
    issues.sort(key=lambda i: (KIND_ORDER.index(i.kind), norm_player(i.player)))
    return issues


def _route(t: dict) -> str:
    return " → ".join(c for c in (t.get("from_club"), t.get("to_club")) if c) or (t.get("kind") or "")


def summary(issues: list[Issue]) -> dict[str, int]:
    out = {kind: 0 for kind in KIND_ORDER}
    for issue in issues:
        out[issue.kind] += 1
    return out
