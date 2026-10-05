"""Решение ответственного по заявке: одобрить или отклонить.

Без Telegram: хендлеры зовут `approve` / `reject`, а сообщения шлют сами.
Решает один `TRANSFER_MANAGER_ID` — админ из `ADMIN_IDS` заявки не одобряет.

Перед одобрением заявка проверяется заново: с подачи могли измениться состав
и бюджеты. Жёсткие блокировки одобрение останавливают, предупреждения — нет
(это и есть решение ответственного), но обновляются в заявке. Окно к этому
времени может быть закрыто — `pending_manager` ответственный решает и после
закрытия, поэтому статус окна не проверяется.

Состав клуба здесь не меняется: «применить к составу» — отдельная кнопка (`transfers/squad.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

import database
from transfers import repo, service
from transfers.engine import Issue
from transfers.requests import _check, _request_from_row, _warnings
from transfers.service import InputError

REJECT_REASON_MAX = 300
# Окно решают отдельно от заявки: после закрытия ответственный всё равно вправе решить.
_WINDOW_STATE_BLOCKS = ("WINDOW_CLOSED", "WINDOW_DRAFT")

STATUS_WORDS = {
    "pending_counterparty": "ждёт вторую сторону", "pending_manager": "ждёт решения",
    "approved": "уже одобрена", "rejected": "уже отклонена",
    "withdrawn": "отозвана", "cancelled": "отменена",
}


@dataclass
class Decision:
    transfer: dict
    warnings: list[dict]
    partner: dict | None = None     # вторая половина обмена, решённая вместе с заявкой


def _manager_only(manager_id: int | None) -> None:
    if not service.is_transfer_manager(manager_id):
        raise InputError("Заявки решает только ответственный за трансферы.")


def _pending(transfer_id) -> dict:
    t = repo.get_transfer(int(transfer_id))
    if t is None:
        raise InputError("Заявка не найдена.")
    if t["status"] != "pending_manager":
        raise InputError(f"Заявка #{t['id']} {STATUS_WORDS.get(t['status'], t['status'])}.")
    return t


def _legs(t: dict) -> list[dict]:
    """Заявка и вторая половина обмена (если есть); обе обязаны ждать решения."""
    partner = repo.get_swap_partner(t)
    if partner is None:
        return [t]
    if partner["status"] != "pending_manager":
        raise InputError(f"Вторая половина обмена #{partner['id']} {STATUS_WORDS.get(partner['status'], partner['status'])}.")
    return [t, partner]


def _recheck(t: dict, window: dict) -> tuple[list[Issue], list[dict]]:
    urn_item = repo.get_transfer(t["urn_item_id"]) if t["urn_item_id"] else None
    ev = _check(window, _request_from_row(t), exclude_id=t["id"], urn_item=urn_item,
                user_ids=(t["from_user"], t["to_user"], t["initiator_id"]))
    return [b for b in ev.blocks if b.code not in _WINDOW_STATE_BLOCKS], _warnings(ev)


def _remember_player(t: dict) -> None:
    """Справочник игроков копится из одобренного: последняя карта и цена."""
    kind = t["kind"]
    if kind in ("deal", "urn_buy"):
        repo.upsert_player(t["player_name"], last_club=t["to_club"], ovr=t["ovr"], price_k=t["price_k"])
    elif kind == "surcharge":
        repo.upsert_player(t["player_name"], last_club=t["to_club"], ovr=t["ovr"])
    elif kind == "free_agent":
        repo.upsert_player(t["player_name"], last_club=t["to_club"], ovr=t["ovr"], price_k=t["price_k"])


def approve(manager_id: int, transfer_id) -> Decision:
    """`pending_manager` → `approved`, проверив заявку заново."""
    _manager_only(manager_id)
    with database.transaction():
        t = _pending(transfer_id)
        window = repo.get_window(t["window_id"])
        if window is None:
            raise InputError("Окно заявки не найдено.")
        legs = _legs(t)
        results = []
        for leg in legs:
            blocks, warnings = _recheck(leg, window)
            if blocks:
                raise InputError("Одобрить нельзя:\n" + "\n".join(f"• {b.message}" for b in blocks))
            results.append((leg, warnings))
        for leg, warnings in results:
            if not repo.set_transfer_status(leg["id"], "approved", expected=("pending_manager",),
                                            actor_id=int(manager_id)):
                raise InputError("Заявку уже решили.")
            repo.set_transfer_warnings(leg["id"], warnings)
            _remember_player(leg)
        partner = repo.get_transfer(legs[1]["id"]) if len(legs) > 1 else None
        return Decision(repo.get_transfer(t["id"]), [w for _, ws in results for w in ws], partner)


def reject(manager_id: int, transfer_id, reason: str | None = None) -> dict:
    """`pending_manager` → `rejected`; причина необязательна и идёт тренерам."""
    _manager_only(manager_id)
    text = " ".join((reason or "").split())[:REJECT_REASON_MAX] or None
    with database.transaction():
        t = _pending(transfer_id)
        for leg in _legs(t):
            if not repo.set_transfer_status(leg["id"], "rejected", expected=("pending_manager",),
                                            actor_id=int(manager_id), reason=text):
                raise InputError("Заявку уже решили.")
        return repo.get_transfer(t["id"])
