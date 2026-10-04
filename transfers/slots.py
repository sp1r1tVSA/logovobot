"""Доп. слоты за монеты: покупка в магазине Mini App.

Без Telegram. Цену и потолок задаёт ответственный в настройках окна
(`slot_price_coins`, `max_extra_slots`; 0 — докупка выключена). Потолок —
на клуб за всё окно, покупки и продажи вместе. Купленный слот попадает в
`transfer_slot_purchases`, а сам лимит клуба считается при чтении
(`engine.compute_ledger`), поэтому счётчиков тут нет. Монеты списываются и
покупка записывается одной транзакцией: не хватило монет или сбой — не
изменилось ничего.
"""

from __future__ import annotations

import database
from transfers import repo, requests as req_mod
from transfers.engine import WindowSettings
from transfers.service import InputError

COIN_TX_TYPE = "transfer_slot"
COIN_REFUND_TX_TYPE = "transfer_slot_refund"
COIN_REF_TYPE = "transfer_slot"
SLOT_TYPES = ("buy", "sell")
SLOT_LABELS = {"buy": "покупок", "sell": "продаж"}


def _season_blocked(window: dict, club: str, user_id: int) -> bool:
    season = window.get("season_id")
    return bool(season) and (repo.is_sanctioned(int(season), club_name=club)
                             or repo.is_sanctioned(int(season), user_id=int(user_id)))


def _reason(window: dict | None, club: str | None, settings: WindowSettings | None,
            extras: int, balance: int, user_id: int) -> str | None:
    """Почему сейчас купить нельзя (текст для тренера) или None."""
    if window is None or window.get("status") != "open":
        return "Трансферное окно закрыто."
    if not club:
        return "За вами не закреплён клуб."
    if settings.slot_price_coins <= 0 or settings.max_extra_slots <= 0:
        return "Докупка слотов в этом окне выключена."
    if _season_blocked(window, club, user_id):
        return "На клуб или тренера наложена санкция — слоты недоступны."
    if extras >= settings.max_extra_slots:
        return f"Лимит докупок исчерпан ({extras} из {settings.max_extra_slots})."
    if balance < settings.slot_price_coins:
        return f"Не хватает монет: нужно {settings.slot_price_coins} 🪙, у вас {balance} 🪙."
    return None


def _extras(window_id: int, club: str) -> int:
    return sum(1 for p in repo.list_slot_purchases(window_id, club) if p["status"] == "active")


def info(user_id: int) -> dict:
    """Карточка слотов для магазина: цена, потолок, что куплено, можно ли купить."""
    window = repo.get_active_window()
    club = req_mod._coach_club_or_none(user_id)
    balance = database.get_wallet_balance(int(user_id))
    if window is None or window.get("status") != "open":
        return {"available": False, "reason": "Трансферное окно закрыто.", "club": club, "balance": balance}
    settings = WindowSettings.from_row(window)
    extras = _extras(window["id"], club) if club else 0
    reason = _reason(window, club, settings, extras, balance, user_id)
    data = {
        "available": reason is None, "reason": reason, "club": club, "balance": balance,
        "price": settings.slot_price_coins, "max_extra": settings.max_extra_slots,
        "bought": extras, "left": max(0, settings.max_extra_slots - extras),
        "window_title": window.get("title"),
    }
    if club:
        lg = repo.get_club_ledger(window["id"], club)
        data.update(buys_used=lg.buys_used, buys_limit=lg.buys_limit,
                    sells_used=lg.sells_used, sells_limit=lg.sells_limit,
                    extra_buys=lg.extra_buys, extra_sells=lg.extra_sells)
    return data


def buy(user_id: int, slot_type: str) -> dict:
    """Купить один доп. слот. Возвращает покупку и новое состояние карточки."""
    if slot_type not in SLOT_TYPES:
        raise InputError("Выберите, что докупаете: слот покупки или продажи.")
    with database.transaction():
        window = repo.get_active_window()
        club = req_mod._coach_club_or_none(user_id)
        settings = WindowSettings.from_row(window) if window else None
        balance = database.get_wallet_balance(int(user_id))
        extras = _extras(window["id"], club) if window and club else 0
        reason = _reason(window, club, settings, extras, balance, user_id)
        if reason:
            raise InputError(reason)
        price = settings.slot_price_coins
        tx_id = database.spend_coins(int(user_id), price, COIN_TX_TYPE, COIN_REF_TYPE)
        if tx_id is None:
            raise InputError("Не хватает монет.")
        purchase_id = repo.add_slot_purchase(window["id"], club, slot_type, price, int(user_id), tx_id)
    return {"purchase_id": purchase_id, "club": club, "slot_type": slot_type, "price": price,
            "window_id": window["id"], "state": info(user_id)}


def active_purchases(window_id: int) -> list[dict]:
    """Действующие покупки слотов окна, новые сверху (для экрана возврата у ответственного)."""
    rows = [p for p in repo.list_slot_purchases(window_id) if p["status"] == "active"]
    return list(reversed(rows))


def refund(purchase_id: int) -> dict:
    """Вернуть монеты за слот и снять его с клуба. Монеты и статус меняются одной транзакцией.

    Слот, под который уже подана заявка, не отдаём: после возврата клуб оказался бы
    сверх лимита. Тогда сначала нужно отозвать или отклонить заявку.
    """
    with database.transaction():
        purchase = repo.get_slot_purchase(int(purchase_id))
        if purchase is None:
            raise InputError("Такой покупки нет.")
        if purchase["status"] != "active":
            raise InputError("Эта покупка уже возвращена.")
        ledger = repo.get_club_ledger(purchase["window_id"], purchase["club_name"])
        used, limit = ((ledger.buys_used, ledger.buys_limit) if purchase["slot_type"] == "buy"
                       else (ledger.sells_used, ledger.sells_limit))
        if used > limit - 1:
            raise InputError(f"Слот уже занят заявкой ({used} из {limit}): сначала отзовите или отклоните её.")
        if not repo.refund_slot_purchase(purchase["id"]):
            raise InputError("Эта покупка уже возвращена.")
        price = int(purchase["price_coins"])
        tx_id = None
        if purchase["user_id"] and price > 0:
            tx_id = database.refund_coins(int(purchase["user_id"]), price, COIN_REFUND_TX_TYPE,
                                          COIN_REF_TYPE, purchase["coin_tx_id"])
    return {**purchase, "status": "refunded", "refund_tx_id": tx_id}
