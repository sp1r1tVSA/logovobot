"""Состав клубов: применить одобренный трансфер к `squad_players`, откатить, отменить.

Без Telegram. Одобрение состав не трогает — ответственный применяет трансфер
отдельной кнопкой (`apply`). Каждое изменение пишется в `transfer_squad_ops`,
поэтому откат (`rollback`) возвращает ровно то, что сделало применение, а не то,
что «должно было» случиться: если игрока в составе уже не было, операции нет и
откатывать нечего.

Жёсткое правило ядра: нельзя убрать игрока из клуба, если от его исходного
состава (снимок на открытие окна) после этого останется меньше `min_core_players`.
Правило проверяется при применении по живому составу — заявки подаются и
одобряются раньше, и состав мог измениться.

Доплата за спешл состав не меняет. Клуб вне турнира («Урна», «вне турнира») в
`squad_players` не ведётся — для него шаг пропускается.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import database
from transfers import repo, service
from transfers.approval import REJECT_REASON_MAX, STATUS_WORDS, _manager_only
from transfers.engine import ACTIVE_STATUSES, WindowSettings, core_remaining, norm_club, norm_player
from transfers.service import InputError

# Что делает вид заявки с составом: (операция, поле с клубом), по порядку.
_PLAN = {
    "deal": (("remove", "from_club"), ("add", "to_club")),
    "free_agent": (("add", "to_club"),),
    "urn_sale": (("remove", "from_club"),),
    "urn_buy": (("add", "to_club"),),
}


@dataclass
class SquadResult:
    transfer: dict
    lines: list[str] = field(default_factory=list)   # что сделано
    notes: list[str] = field(default_factory=list)   # что пропущено и почему


def changes_squad(t: dict) -> bool:
    """Меняет ли этот вид заявки состав вообще (доплата — нет)."""
    return t.get("kind") in _PLAN


def needs_apply(t: dict) -> bool:
    """Одобрена, состав меняет и ещё не применена."""
    return t.get("status") == "approved" and changes_squad(t) and not t.get("squad_applied_at")


def _steps(t: dict) -> list[tuple[str, str]]:
    """(операция, клуб) только для клубов лиги — по ним ведётся `squad_players`."""
    league = {norm_club(c) for c in service.league_clubs()}
    return [(op, t[field_]) for op, field_ in _PLAN.get(t["kind"], ())
            if t.get(field_) and norm_club(t[field_]) in league]


def preview(t: dict) -> list[str]:
    """Что сделает применение, человеческим текстом — для экрана ответственного."""
    name = t.get("player_name") or ""
    return [f"➖ {name} уходит из {club}" if op == "remove" else f"➕ {name} приходит в {club}"
            for op, club in _steps(t)]


def _get(transfer_id) -> dict:
    t = repo.get_transfer(int(transfer_id))
    if t is None:
        raise InputError("Заявка не найдена.")
    return t


def _approved(t: dict) -> None:
    if t["status"] != "approved":
        raise InputError(f"Заявка #{t['id']} {STATUS_WORDS.get(t['status'], t['status'])} — "
                         "состав меняется только по одобренной.")


def _find(rows: list[dict], player: str) -> dict | None:
    key = norm_player(player)
    return next((r for r in rows if norm_player(r["player_name"]) == key), None)


def _check_core(window_id: int, club: str, rows: list[dict], player: str) -> None:
    """Жёсткое правило: уход не должен опустить ядро клуба ниже минимума."""
    snapshot = [r["player_name"] for r in repo.get_core_snapshot(window_id, club)]
    if not snapshot:
        return
    squad = [r["player_name"] for r in rows]
    before = core_remaining(snapshot, squad, [])
    after = core_remaining(snapshot, squad, [player])
    window = repo.get_window(window_id)
    minimum = WindowSettings.from_row(window).min_core_players if window else 0
    if after < before and after < minimum:
        raise InputError(f"В {club} останется {after} игроков исходного состава, нужно минимум {minimum} — "
                         f"{player} убрать нельзя.")


def apply(manager_id: int, transfer_id) -> SquadResult:
    """Применить одобренный трансфер к составам клубов одной транзакцией."""
    _manager_only(manager_id)
    with database.transaction():
        t = _get(transfer_id)
        _approved(t)
        if t["squad_applied_at"]:
            raise InputError(f"Состав по заявке #{t['id']} уже изменён.")
        if not changes_squad(t):
            raise InputError("Доплата состав не меняет — применять нечего.")
        result = SquadResult(t)
        player, position = t["player_name"], None
        for op, club in _steps(t):
            rows = repo.squad_rows(club)
            row = _find(rows, player)
            if op == "remove":
                if row is None:
                    result.notes.append(f"{player} уже нет в составе {club} — убирать нечего.")
                    continue
                _check_core(t["window_id"], club, rows, player)
                repo.squad_delete(row["id"])
                repo.insert_squad_op(t["id"], "remove", row["team_name"], row["player_name"],
                                     row["position"], int(manager_id))
                position = row["position"]
                result.lines.append(f"➖ {player} убран из {club}")
            else:
                if row is not None:
                    result.notes.append(f"{player} уже есть в составе {club} — добавлять не нужно.")
                    continue
                team = repo.squad_insert(club, player, position)
                if team is None:
                    result.notes.append(f"{player} уже есть в составе {club} — добавлять не нужно.")
                    continue
                repo.insert_squad_op(t["id"], "add", team, player, position, int(manager_id))
                result.lines.append(f"➕ {player} добавлен в {club}")
        repo.mark_squad_applied(t["id"])
        result.transfer = repo.get_transfer(t["id"])
        return result


def _rollback_locked(t: dict, manager_id: int) -> SquadResult:
    """Откат внутри открытой транзакции — общий для `rollback` и `cancel`."""
    result = SquadResult(t)
    ops = repo.list_squad_ops(t["id"])
    for op in ops:
        key, club_key = norm_player(op["player_name"]), norm_club(op["team_name"])
        for later in repo.later_squad_ops(op["id"], t["id"]):
            if norm_player(later["player_name"]) == key and norm_club(later["team_name"]) == club_key:
                raise InputError(f"Сначала откатите заявку #{later['transfer_id']}: "
                                 f"она после этой тронула {op['player_name']} в {op['team_name']}.")
    for op in reversed(ops):
        rows = repo.squad_rows(op["team_name"])
        row = _find(rows, op["player_name"])
        if op["op"] == "add":
            if row is None:
                result.notes.append(f"{op['player_name']} уже нет в составе {op['team_name']}.")
                continue
            repo.squad_delete(row["id"])
            result.lines.append(f"➖ {op['player_name']} убран из {op['team_name']}")
        else:
            if row is not None or repo.squad_insert(op["team_name"], op["player_name"], op["position"]) is None:
                result.notes.append(f"{op['player_name']} уже есть в составе {op['team_name']}.")
                continue
            result.lines.append(f"➕ {op['player_name']} возвращён в {op['team_name']}")
    repo.mark_squad_ops_reverted(t["id"])
    repo.clear_squad_applied(t["id"])
    result.transfer = repo.get_transfer(t["id"])
    return result


def rollback(manager_id: int, transfer_id) -> SquadResult:
    """Вернуть составы к состоянию до применения; заявка остаётся одобренной."""
    _manager_only(manager_id)
    with database.transaction():
        t = _get(transfer_id)
        _approved(t)
        if not t["squad_applied_at"]:
            raise InputError(f"Состав по заявке #{t['id']} не менялся — откатывать нечего.")
        return _rollback_locked(t, manager_id)


def cancel(manager_id: int, transfer_id, reason: str | None = None) -> tuple[dict, SquadResult | None]:
    """Отменить одобренную заявку. Применённый состав откатывается тут же.

    Бюджет и слоты считаются при чтении по активным заявкам, поэтому освобождаются
    сами. Продажу в урну с уже заявленным выкупом отменять нельзя.
    """
    _manager_only(manager_id)
    text = " ".join((reason or "").split())[:REJECT_REASON_MAX] or None
    with database.transaction():
        t = _get(transfer_id)
        _approved(t)
        if t["kind"] == "urn_sale":
            buys = [b for b in repo.list_transfers(t["window_id"], statuses=ACTIVE_STATUSES, kinds=("urn_buy",))
                    if b["urn_item_id"] == t["id"]]
            if buys:
                raise InputError(f"Игрока из урны уже выкупают — сначала отмените заявку #{buys[0]['id']}.")
        rolled = _rollback_locked(t, manager_id) if t["squad_applied_at"] else None
        if not repo.set_transfer_status(t["id"], "cancelled", expected=("approved",),
                                        actor_id=int(manager_id), reason=text):
            raise InputError("Заявку уже решили.")
        partner = repo.get_swap_partner(t)
        if partner is not None and partner["status"] == "approved":
            # Обмен отменяется целиком: вторая половина без первой не имеет смысла.
            partner_rolled = _rollback_locked(partner, manager_id) if partner["squad_applied_at"] else None
            if not repo.set_transfer_status(partner["id"], "cancelled", expected=("approved",),
                                            actor_id=int(manager_id), reason=text):
                raise InputError("Вторую половину обмена уже решили.")
            if partner_rolled is not None:
                rolled = rolled or SquadResult(t)
                rolled.lines.extend(partner_rolled.lines)
                rolled.notes.extend(partner_rolled.notes)
        return repo.get_transfer(t["id"]), rolled
