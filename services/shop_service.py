"""
services/shop_service.py

Service layer for shop rewards catalog, inventory management,
item purchases, Wheel of Fortune roulette spins, and secret player claims.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import database
from transfers import repo as transfer_repo
from transfers.requests import _coach_club_or_none
import time_utils

logger = logging.getLogger(__name__)

# Награды, относящиеся к трансферам (стоимостью от 5 500 до 10 000 монет)
TRANSFER_SHOP_ITEM_IDS = ("credit_transfer", "slot_swap", "urna_boost", "surcharge_coupon")
MAX_TRANSFER_REWARDS_PER_WINDOW = 2

# Цены и каталог товаров магазина Логова
SHOP_CATALOG: list[dict[str, Any]] = [
    {
        "id": "train_5",
        "name": "Тренировки (+5)",
        "category": "squad",
        "icon": "🏋️",
        "price": 4500,
        "badge": "+5 слотов прокачки",
        "description": "Добавляет +5 зарядов тренировок составу вашего клуба. Прокачивайте игроков сверх базового лимита.",
        "requires_window": False,
        "charges": 5,
    },
    {
        "id": "credit_transfer",
        "name": "Трансферный кредит",
        "category": "transfers",
        "icon": "🏦",
        "price": 5500,
        "badge": "До 15 млн в ТО",
        "description": "Трансферный заём до 15.000.000 € на текущее трансферное окно для экстренного выкупа игроков.",
        "requires_window": True,
        "charges": 1,
        "meta": {"loan_limit_k": 15000},
    },
    {
        "id": "slot_swap",
        "name": "Слот обмена",
        "category": "transfers",
        "icon": "🤝",
        "price": 6000,
        "badge": "+1 обмен игроками",
        "description": "Дополнительный слот для проведения прямого обмена игроками между клубами в текущем ТО.",
        "requires_window": True,
        "charges": 1,
    },
    {
        "id": "urna_boost",
        "name": "Выгодная урна (+25%)",
        "category": "transfers",
        "icon": "🗑",
        "price": 7000,
        "badge": "+25% к выплате за карту",
        "description": "Бонусная надбавка +25% к сумме сдачи карты в урну на следующую продажу игрока.",
        "requires_window": True,
        "charges": 1,
        "meta": {"bonus_pct": 25},
    },
    {
        "id": "surcharge_coupon",
        "name": "Купон на доплату",
        "category": "transfers",
        "icon": "📄",
        "price": 10000,
        "badge": "Покрытие спец-карты",
        "description": "Купон на проведение доплаты за повышение OVR или спешл-карту в текущем окне.",
        "requires_window": True,
        "charges": 1,
    },
    {
        "id": "roulette_spin",
        "name": "Рулетка фортуны",
        "category": "all",
        "icon": "🎰",
        "price": 25000,
        "badge": "Колесо призов",
        "description": "Вращай интерактивное Колесо Фортуны с шансом выиграть супер-призы, бюджет, тренировки или Секретного игрока!",
        "requires_window": False,
        "charges": 1,
    },
    {
        "id": "secret_player",
        "name": "Секретный игрок",
        "category": "squad",
        "icon": "🕵️",
        "price": 50000,
        "badge": "VIP Звездный игрок",
        "description": "Прямая заявка на подписание секретного топ-игрока лиги. После покупки администратор подбирает и выдает звезду в состав.",
        "requires_window": False,
        "charges": 1,
    },
]

ROULETTE_PRICE = 25000

# 8 секторов Колеса Фортуны (соответствуют Canvas в Mini App)
ROULETTE_SECTORS: list[dict[str, Any]] = [
    {
        "index": 0,
        "id": "money_1",
        "name": "+10 млн",
        "icon": "💰",
        "color": "#1f242d",
        "textColor": "#ffffff",
        "weight": 20.0,
        "item_id": "budget_10m",
        "reward_type": "inventory",
        "reward_label": "+10.000.000 € в бюджет ТО",
        "charges": 1,
        "meta": {"budget_add_k": 10000},
    },
    {
        "index": 1,
        "id": "urna",
        "name": "Выгодная урна",
        "icon": "🗑",
        "color": "#2a2f3a",
        "textColor": "#ffffff",
        "weight": 25.0,
        "item_id": "urna_boost",
        "reward_type": "inventory",
        "reward_label": "Выгодная урна (+25% к выплате)",
        "charges": 1,
        "meta": {"bonus_pct": 25},
    },
    {
        "index": 2,
        "id": "train",
        "name": "+5 тренировок",
        "icon": "🏋️",
        "color": "#1d3557",
        "textColor": "#64b5f6",
        "weight": 18.0,
        "item_id": "train_5",
        "reward_type": "inventory",
        "reward_label": "+5 тренировок состава",
        "charges": 5,
    },
    {
        "index": 3,
        "id": "coupon",
        "name": "Купон на доплату",
        "icon": "📄",
        "color": "#163b4d",
        "textColor": "#4dd0e1",
        "weight": 15.0,
        "item_id": "surcharge_coupon",
        "reward_type": "inventory",
        "reward_label": "Купон на доплату (спешл-карта)",
        "charges": 1,
    },
    {
        "index": 4,
        "id": "money_2",
        "name": "+10 млн",
        "icon": "💰",
        "color": "#1f242d",
        "textColor": "#ffffff",
        "weight": 15.0,
        "item_id": "budget_10m",
        "reward_type": "inventory",
        "reward_label": "+10.000.000 € в бюджет ТО",
        "charges": 1,
        "meta": {"budget_add_k": 10000},
    },
    {
        "index": 5,
        "id": "exchange",
        "name": "Слот обмена",
        "icon": "🤝",
        "color": "#381a4d",
        "textColor": "#ce93d8",
        "weight": 5.0,
        "item_id": "slot_swap",
        "reward_type": "inventory",
        "reward_label": "Дополнительный слот обмена",
        "charges": 1,
    },
    {
        "index": 6,
        "id": "credit",
        "name": "Кредит 20M",
        "icon": "🏦",
        "color": "#4a1525",
        "textColor": "#ff80ab",
        "weight": 3.0,
        "item_id": "credit_transfer",
        "reward_type": "inventory",
        "reward_label": "Трансферный кредит 20.000.000 €",
        "charges": 1,
        "meta": {"loan_limit_k": 20000},
    },
    {
        "index": 7,
        "id": "secret",
        "name": "Секретный игрок",
        "icon": "🕵️",
        "color": "#805b00",
        "textColor": "#ffe082",
        "is_legendary": True,
        "weight": 0.5,
        "item_id": "secret_player",
        "reward_type": "claim",
        "reward_label": "Секретный игрок (VIP джекпот)",
    },
]


def get_shop_catalog(user_id: int) -> dict[str, Any]:
    """
    Возвращает каталог наград для пользователя с учётом баланса,
    статуса ТО и привязки к клубу.
    """
    club = _coach_club_or_none(user_id)
    balance = database.get_wallet_balance(int(user_id))
    active_window = transfer_repo.get_active_window()
    is_window_open = bool(active_window and active_window.get("status") == "open")
    window_id = active_window.get("id") if active_window else None

    # Подсчёт купленных трансферных наград за текущее окно (максимум 2 на окно)
    window_transfer_purchases = 0
    if window_id is not None:
        window_transfer_purchases = database.count_window_shop_transfer_purchases(
            user_id=int(user_id),
            club_name=club or "",
            window_id=int(window_id),
            transfer_item_ids=TRANSFER_SHOP_ITEM_IDS,
        )
    transfer_limit_reached = (window_transfer_purchases >= MAX_TRANSFER_REWARDS_PER_WINDOW)

    items = []
    for item in SHOP_CATALOG:
        item_copy = dict(item)
        available = True
        reason = None

        if balance < item["price"]:
            available = False
            reason = f"Не хватает монет (нужно {item['price']} 🪙, у вас {balance} 🪙)"
        elif item.get("requires_window"):
            if not club:
                available = False
                reason = "За вами не закреплён клуб лиги"
            elif not is_window_open:
                available = False
                reason = "Трансферное окно сейчас закрыто"
            elif item["id"] in TRANSFER_SHOP_ITEM_IDS and transfer_limit_reached:
                available = False
                reason = f"Лимит наград за окно исчерпан ({window_transfer_purchases} из {MAX_TRANSFER_REWARDS_PER_WINDOW})"
        elif item["id"] == "secret_player" and not club:
            available = False
            reason = "За вами не закреплён клуб лиги"

        item_copy["available"] = available
        item_copy["reason"] = reason
        items.append(item_copy)

    inventory = database.get_user_shop_inventory(int(user_id))

    return {
        "items": items,
        "roulette_sectors": ROULETTE_SECTORS,
        "roulette_price": ROULETTE_PRICE,
        "balance": balance,
        "club": club,
        "is_window_open": is_window_open,
        "window_title": active_window.get("title") if active_window else None,
        "window_id": window_id,
        "window_transfer_rewards_bought": window_transfer_purchases,
        "window_transfer_rewards_limit": MAX_TRANSFER_REWARDS_PER_WINDOW,
        "inventory": inventory,
    }


def buy_shop_item(user_id: int, item_id: str, notes: str | None = None) -> dict[str, Any]:
    """
    Покупка товара из каталога за монеты.
    """
    target = next((it for it in SHOP_CATALOG if it["id"] == item_id), None)
    if not target:
        raise ValueError("Товар не найден в каталоге.")

    if target["id"] == "roulette_spin":
        raise ValueError("Для рулетки используйте функцию прокрута колеса.")

    user_id = int(user_id)
    club = _coach_club_or_none(user_id)
    active_window = transfer_repo.get_active_window()
    is_window_open = bool(active_window and active_window.get("status") == "open")

    if target.get("requires_window"):
        if not club:
            raise ValueError("За вами не закреплён клуб лиги.")
        if not is_window_open:
            raise ValueError("Трансферное окно сейчас закрыто.")
        if target["id"] in TRANSFER_SHOP_ITEM_IDS:
            window_id = active_window["id"]
            current_purchases = database.count_window_shop_transfer_purchases(
                user_id=user_id,
                club_name=club or "",
                window_id=window_id,
                transfer_item_ids=TRANSFER_SHOP_ITEM_IDS,
            )
            if current_purchases >= MAX_TRANSFER_REWARDS_PER_WINDOW:
                raise ValueError(
                    f"В одно трансферное окно можно купить максимум {MAX_TRANSFER_REWARDS_PER_WINDOW} "
                    f"трансферные награды (у вас уже куплено {current_purchases})."
                )

    if target["id"] == "secret_player" and not club:
        raise ValueError("За вами не закреплён клуб лиги.")

    price = target["price"]

    with database.transaction():
        balance = database.get_wallet_balance(user_id)
        if balance < price:
            raise ValueError(f"Недостаточно монет: нужно {price} 🪙, у вас {balance} 🪙.")

        tx_id = database.spend_coins(
            user_id,
            price,
            tx_type="shop_purchase",
            ref_type="shop_item",
            ref_id=item_id,
        )
        if not tx_id:
            raise ValueError("Ошибка списания монет.")

        if target["id"] == "secret_player":
            claim_id = database.create_secret_player_claim(
                user_id=user_id,
                club_name=club or "",
                tx_id=tx_id,
                cost=price,
                notes=notes,
            )
            return {
                "status": "ok",
                "item_id": item_id,
                "name": target["name"],
                "price": price,
                "claim_id": claim_id,
                "balance": database.get_wallet_balance(user_id),
                "message": "Заявка на Секретного игрока принята! Ожидайте утверждения администратора.",
            }
        else:
            charges = target.get("charges", 1)
            meta = target.get("meta")
            inv_id = database.add_shop_inventory_item(
                user_id=user_id,
                club_name=club or "",
                item_id=item_id,
                charges=charges,
                tx_id=tx_id,
                meta=meta,
                window_id=active_window.get("id") if active_window else None,
                source="purchase",
            )
            return {
                "status": "ok",
                "item_id": item_id,
                "name": target["name"],
                "price": price,
                "charges": charges,
                "inventory_id": inv_id,
                "balance": database.get_wallet_balance(user_id),
                "message": f"Вы успешно приобрели {target['name']}!",
            }


def spin_roulette(user_id: int) -> dict[str, Any]:
    """
    Прокрут Колеса Фортуны за 25 000 🪙.
    Определяет выигравший сектор по весам и начисляет награду.
    """
    user_id = int(user_id)
    club = _coach_club_or_none(user_id) or ""
    price = ROULETTE_PRICE

    # Выбираем победителя по весам
    total_weight = sum(s["weight"] for s in ROULETTE_SECTORS)
    rand_val = random.uniform(0, total_weight)
    winning_sector = ROULETTE_SECTORS[0]
    for sector in ROULETTE_SECTORS:
        if rand_val < sector["weight"]:
            winning_sector = sector
            break
        rand_val -= sector["weight"]

    active_window = transfer_repo.get_active_window()
    window_id = active_window.get("id") if active_window else None

    with database.transaction():
        balance = database.get_wallet_balance(user_id)
        if balance < price:
            raise ValueError(f"Недостаточно монет: нужно {price} 🪙, у вас {balance} 🪙.")

        tx_id = database.spend_coins(
            user_id,
            price,
            tx_type="shop_roulette",
            ref_type="roulette_spin",
        )
        if not tx_id:
            raise ValueError("Ошибка списания монет для прокрута рулетки.")

        # Начисляем награду в зависимости от сектора
        reward_type = winning_sector.get("reward_type")
        item_id = winning_sector.get("item_id", "")
        meta = winning_sector.get("meta")
        charges = winning_sector.get("charges", 1)

        claim_id = None
        inv_id = None

        if reward_type == "claim":
            # Выигран Секретный Игрок!
            claim_id = database.create_secret_player_claim(
                user_id=user_id,
                club_name=club,
                tx_id=tx_id,
                cost=0,  # Выигран бесплатно из рулетки
                notes="Выигран в Рулетке Фортуны (Джекпот!)",
            )
        elif reward_type in ("inventory", "budget"):
            inv_id = database.add_shop_inventory_item(
                user_id=user_id,
                club_name=club,
                item_id=item_id,
                charges=charges,
                tx_id=tx_id,
                meta=meta,
                window_id=window_id,
                source="roulette",
            )

        spin_id = database.record_shop_roulette_spin(
            user_id=user_id,
            club_name=club,
            cost=price,
            won_item_id=item_id,
            won_label=winning_sector["reward_label"],
            tx_id=tx_id,
            won_payload={
                "sector_index": winning_sector["index"],
                "sector_id": winning_sector["id"],
                "claim_id": claim_id,
                "inventory_id": inv_id,
            },
        )

        new_balance = database.get_wallet_balance(user_id)

    return {
        "status": "ok",
        "spin_id": spin_id,
        "winning_index": winning_sector["index"],
        "sector": winning_sector,
        "reward_label": winning_sector["reward_label"],
        "balance": new_balance,
        "claim_id": claim_id,
        "inventory_id": inv_id,
    }
