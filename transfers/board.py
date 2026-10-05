"""Доска «ищу / продаю»: лоты тренеров без конкретного покупателя.

Лот — объявление клуба: «продаю игрока» (`sell`) или «ищу» (`buy`, игрок или описание в
`note`). Сам по себе он ничего не стоит — ни бюджета, ни слота. Отклик на лот — обычная
сделка (`requests.create_deal`) с клубом лота, связанная с ним строкой
`transfer_board_responses`; дальше она идёт своим путём: подтверждение второй стороны,
решение ответственного.

Лот «живой», пока он открыт, его окно открыто и это не продажа, по которой уже одобрена
сделка. Это считается при чтении, поэтому одобрению не нужен хук: проданный лот пропадает с
доски сам. Доска работает только в открытом окне (в черновике сделки не принимаются).
"""

from __future__ import annotations

import database
from transfers import repo, sanctions, service
from transfers import requests as req_mod
from transfers.engine import format_k, norm_player, parse_money_k
from transfers.service import InputError

SIDES = ("buy", "sell")
SIDE_LABELS = {"buy": "Ищу", "sell": "Продаю"}
MAX_OPEN_PER_CLUB = 3
NOTE_MAX = 200


# ─── Чтение ──────────────────────────────────────────────────────────────────

def _open_window() -> dict:
    window = repo.get_active_window()
    if window is None or window.get("status") != "open":
        raise InputError("Доска работает, пока трансферное окно открыто.")
    return window


def _board_window() -> dict | None:
    window = repo.get_active_window()
    return window if window and window.get("status") == "open" else None


def is_live(lot: dict, window: dict | None = None) -> bool:
    window = window if window is not None else _board_window()
    return (bool(window) and lot["status"] == "open" and lot["window_id"] == window["id"]
            and not (lot["side"] == "sell" and (lot.get("responses_approved") or 0) > 0))


def live_lots(window: dict | None = None) -> list[dict]:
    """Живые лоты открытого окна, новые сверху. Окно не открыто — пусто."""
    window = window if window is not None else _board_window()
    if not window:
        return []
    return [lot for lot in repo.list_lots(window["id"]) if is_live(lot, window)]


def _club_lots(lots: list[dict], club: str | None) -> list[dict]:
    return [lot for lot in lots if req_mod._same_club(lot["club_name"], club)]


def _blocked(user_id: int, club: str | None) -> bool:
    return sanctions.for_user(user_id, club) is not None


def serialize(lot: dict, viewer_club: str | None = None, *, can_act: bool = False) -> dict:
    mine = req_mod._same_club(lot["club_name"], viewer_club)
    return {
        "id": lot["id"],
        "side": lot["side"],
        "side_label": SIDE_LABELS.get(lot["side"], lot["side"]),
        "club": lot["club_name"],
        "player": lot["player_name"],
        "ovr": lot["ovr"],
        "price_k": lot["price_k"],
        "price": format_k(lot["price_k"]) if lot["price_k"] is not None else None,
        "note": lot["note"],
        "created_at": lot["created_at"],
        "mine": mine,
        "can_respond": can_act and not mine,
        "responses": {"pending": lot.get("responses_pending") or 0,
                      "approved": lot.get("responses_approved") or 0},
    }


def list_board(viewer_id: int) -> dict:
    window = _board_window()
    club = req_mod._coach_club_or_none(viewer_id)
    lots = live_lots(window)
    can_act = bool(window) and bool(club) and not _blocked(viewer_id, club)
    my_open = len(_club_lots(lots, club)) if club else 0
    return {
        "open": bool(window),
        "club": club,
        "my_open": my_open,
        "max_open": MAX_OPEN_PER_CLUB,
        "can_post": can_act and my_open < MAX_OPEN_PER_CLUB,
        "lots": [serialize(lot, club, can_act=can_act) for lot in lots],
    }


# ─── Лоты ────────────────────────────────────────────────────────────────────

def _clean_note(note) -> str | None:
    text = " ".join(str(note or "").split())
    if len(text) > NOTE_MAX:
        raise InputError(f"Комментарий длиннее {NOTE_MAX} символов.")
    return text or None


def _optional_price(value) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    amount = parse_money_k(value)
    if amount is None:
        raise InputError("Не понял сумму: пример <code>12,5</code> (в млн).")
    return amount


def create_lot(user_id: int, *, side: str, player=None, ovr=None, price=None, note=None) -> dict:
    """Новый лот клуба тренера. Продажа — с игроком, поиск — с игроком или описанием."""
    if side not in SIDES:
        raise InputError("Укажите, ищете вы игрока или продаёте.")
    club = req_mod.coach_club(user_id)
    name = req_mod._clean_player(player) if norm_player(" ".join(str(player or "").split())) else None
    note_text = _clean_note(note)
    if side == "sell" and not name:
        raise InputError("Укажите игрока, которого продаёте.")
    if side == "buy" and not name and not note_text:
        raise InputError("Укажите игрока или опишите, кого ищете.")
    ovr_v = req_mod._parse_ovr(ovr, required=False)
    price_k = _optional_price(price)

    with database.transaction():
        window = _open_window()
        if _blocked(user_id, club):
            raise InputError("Ваш клуб под трансферной санкцией — доска недоступна.")
        mine = _club_lots(live_lots(window), club)
        if len(mine) >= MAX_OPEN_PER_CLUB:
            raise InputError(f"У клуба уже {MAX_OPEN_PER_CLUB} лота на доске — снимите один.")
        key = norm_player(name) if name else None
        if key and any(lot["side"] == side and lot["norm_name"] == key for lot in mine):
            raise InputError(f"Лот «{SIDE_LABELS[side]}: {name}» уже висит на доске.")
        lot_id = repo.insert_lot(window["id"], club, user_id, side, player_name=name,
                                 ovr=ovr_v, price_k=price_k, note=note_text)
        return repo.get_lot(lot_id)


def _get(lot_id) -> dict:
    lot = repo.get_lot(req_mod._parse_id(lot_id))
    if lot is None:
        raise InputError("Лот не найден.")
    return lot


def close_lot(user_id: int, lot_id) -> dict:
    """Снять свой лот (лот клуба тренера)."""
    lot = _get(lot_id)
    if not req_mod._same_club(lot["club_name"], req_mod._coach_club_or_none(user_id)):
        raise InputError("Снять лот может только его клуб.")
    if not repo.close_lot(lot["id"], "author", int(user_id)):
        raise InputError("Лот уже снят.")
    return repo.get_lot(lot["id"])


def remove_lot(actor_id: int, lot_id) -> dict:
    """Снять чужой лот — только ответственный за трансферы."""
    if not service.is_transfer_manager(actor_id):
        raise InputError("Снимать чужие лоты может только ответственный за трансферы.")
    lot = _get(lot_id)
    if not repo.close_lot(lot["id"], "manager", int(actor_id)):
        raise InputError("Лот уже снят.")
    return repo.get_lot(lot["id"])


# ─── Отклик ──────────────────────────────────────────────────────────────────

def respond(user_id: int, lot_id, *, role: str, other_club=None, player=None, price=None, ovr=None) -> dict:
    """Отклик на лот: сделка с его клубом. Продаёт лот — тренер покупает, ищет — продаёт ему."""
    lot = _get(lot_id)
    if not is_live(lot):
        raise InputError("Лот уже снят с доски.")
    expected = "buy" if lot["side"] == "sell" else "sell"
    if role != expected:
        raise InputError("На лот «Продаю» отвечают покупкой, на «Ищу» — продажей.")
    if other_club and not req_mod._same_club(service.resolve_club(other_club), lot["club_name"]):
        raise InputError(f"Отклик идёт клубу лота — {lot['club_name']}.")
    if req_mod._same_club(lot["club_name"], req_mod._coach_club_or_none(user_id)):
        raise InputError("Это лот вашего клуба.")
    if lot["side"] == "sell" and norm_player(" ".join(str(player or "").split())) != lot["norm_name"]:
        raise InputError(f"В лоте продаётся {lot['player_name']} — отклик на другого игрока не принимается.")
    with database.transaction():
        deal = req_mod.create_deal(user_id, role=role, other_club=lot["club_name"], player=player,
                                   price=price, ovr=ovr)
        repo.link_board_response(deal["id"], lot["id"])
        return deal
