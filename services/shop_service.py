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
        "name": "Тренировка",
        "category": "squad",
        "icon": "🏋️",
        "price": 4500,
        "badge": "+5 тренировок (1 раз в сезон)",
        "description": "Дает +5 тренировок в ваш состав. Можно покупать один раз за сезон. Максимальное количество покупок за три сезона — три штуки.",
        "requires_window": False,
        "limit_scope": "season",
        "max_per_season": 1,
        "max_recent_seasons": 3,
        "charges": 5,
    },
    {
        "id": "credit_transfer",
        "name": "Трансферный кредит",
        "category": "transfers",
        "icon": "🏦",
        "price": 5500,
        "badge": "До -20 млн в ТО",
        "description": "Позволяет уйти в минус до 20 миллионов по итогам трансферного окна. Лимит — одна покупка на ТО.",
        "requires_window": True,
        "limit_scope": "window",
        "max_per_window": 1,
        "charges": 1,
        "meta": {"loan_limit_k": 20000},
    },
    {
        "id": "slot_swap",
        "name": "Слот обмена",
        "category": "transfers",
        "icon": "🤝",
        "price": 6000,
        "badge": "Обмен без траты слота",
        "description": "При покупке обмен игроков не засчитывается за слот вашему клубу (компенсирует слот покупки и продажи). Чтобы обмен не тратил слоты у обоих участников, награду должны приобрести оба тренера (по 6 000 🪙). Лимит — одна покупка на ТО.",
        "requires_window": True,
        "limit_scope": "window",
        "max_per_window": 1,
        "charges": 1,
    },
    {
        "id": "urna_boost",
        "name": "Выгодная урна",
        "category": "transfers",
        "icon": "🗑",
        "price": 7000,
        "badge": "+1 сброс в урну",
        "description": "Позволяет выбросить в урну еще одного игрока. Для дивизионов, где выбрасывать игроков нельзя, покупка этой награды открывает возможность утилизировать одного футболиста. Лимит — одна покупка на ТО.",
        "requires_window": True,
        "limit_scope": "window",
        "max_per_window": 1,
        "charges": 1,
        "meta": {"extra_urn_slots": 1, "unlock_urn": True},
    },
    {
        "id": "surcharge_coupon",
        "name": "Купон на доплату",
        "category": "transfers",
        "icon": "📄",
        "price": 10000,
        "badge": "-50% на доплату спешл",
        "description": "При покупке игрока вы платите в два раза меньше за доплату за спешл-карты (скидка 50% на доплату за спешл). Лимит — одна покупка на ТО.",
        "requires_window": True,
        "limit_scope": "window",
        "max_per_window": 1,
        "charges": 1,
        "meta": {"discount_pct": 50},
    },
    {
        "id": "roulette_spin",
        "name": "Билет в рулетку",
        "category": "all",
        "icon": "🎰",
        "price": 25000,
        "badge": "1 билет за сезон",
        "description": "Вам может выпасть абсолютно любая награда. При этом не гарантируется, что призы будут только из текущего списка. Лимит — одна покупка за сезон.",
        "requires_window": False,
        "limit_scope": "season",
        "max_per_season": 1,
        "charges": 1,
    },
    {
        "id": "secret_player",
        "name": "Секретный игрок",
        "category": "squad",
        "icon": "🕵️",
        "price": 50000,
        "badge": "Эксклюзив / Твич-пул",
        "description": "Вы получаете одного из примерно 10 футболистов, которые заблокированы для обычной покупки. Они редкие — приобрести их на рынке нельзя, но можно выиграть здесь или в наградах на Твиче.",
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
        "reward_label": "Выгодная урна (+1 сброс в урну)",
        "charges": 1,
        "meta": {"extra_urn_slots": 1, "unlock_urn": True},
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
        "reward_label": "Купон на доплату (50% на спешл)",
        "charges": 1,
        "meta": {"discount_pct": 50},
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
    статуса ТО, текущего сезона и привязки к клубу.
    """
    club = _coach_club_or_none(user_id)
    balance = database.get_wallet_balance(int(user_id))
    active_window = transfer_repo.get_active_window()
    is_window_open = bool(active_window and active_window.get("status") == "open")
    window_id = active_window.get("id") if active_window else None

    active_season = database.get_active_season()
    current_season_id = (
        int(active_window.get("season_id"))
        if (active_window and active_window.get("season_id"))
        else (int(active_season["id"]) if active_season else 1)
    )

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

    roulette_spins_season = database.count_season_roulette_spins(int(user_id), club or "", current_season_id)

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
            elif item.get("max_per_window") and window_id is not None:
                bought_item_window = database.count_window_shop_item_purchases(
                    int(user_id), club or "", item["id"], int(window_id)
                )
                if bought_item_window >= item["max_per_window"]:
                    available = False
                    reason = f"Лимит на эту награду исчерпан ({bought_item_window} из {item['max_per_window']} на ТО)"
        elif item["id"] == "train_5":
            if not club:
                available = False
                reason = "За вами не закреплён клуб лиги"
            else:
                train_season_count = database.count_season_shop_item_purchases(
                    int(user_id), club or "", "train_5", current_season_id
                )
                train_3_seasons_count = database.count_recent_seasons_shop_item_purchases(
                    int(user_id), club or "", "train_5", current_season_id, seasons_count=3
                )
                if train_season_count >= item.get("max_per_season", 1):
                    available = False
                    reason = "Лимит на сезон исчерпан (1 покупка за сезон)"
                elif train_3_seasons_count >= item.get("max_recent_seasons", 3):
                    available = False
                    reason = "Лимит за три сезона исчерпан (максимум 3 покупки)"
        elif item["id"] == "roulette_spin":
            if roulette_spins_season >= item.get("max_per_season", 1):
                available = False
                reason = "Лимит на сезон исчерпан (1 билет за сезон)"
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
        "season_id": current_season_id,
        "window_transfer_rewards_bought": window_transfer_purchases,
        "window_transfer_rewards_limit": MAX_TRANSFER_REWARDS_PER_WINDOW,
        "roulette_spins_this_season": roulette_spins_season,
        "roulette_season_limit": 1,
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

    active_season = database.get_active_season()
    current_season_id = (
        int(active_window.get("season_id"))
        if (active_window and active_window.get("season_id"))
        else (int(active_season["id"]) if active_season else 1)
    )

    if target.get("requires_window"):
        if not club:
            raise ValueError("За вами не закреплён клуб лиги.")
        if not is_window_open:
            raise ValueError("Трансферное окно сейчас закрыто.")
        window_id = active_window["id"]
        if target["id"] in TRANSFER_SHOP_ITEM_IDS:
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
        if target.get("max_per_window"):
            bought_this_item = database.count_window_shop_item_purchases(
                user_id=user_id,
                club_name=club or "",
                item_id=target["id"],
                window_id=window_id,
            )
            if bought_this_item >= target["max_per_window"]:
                raise ValueError(
                    f"Награду «{target['name']}» можно купить максимум {target['max_per_window']} "
                    f"раз(а) за одно трансферное окно."
                )

    if target["id"] == "train_5":
        if not club:
            raise ValueError("За вами не закреплён клуб лиги.")
        train_season_count = database.count_season_shop_item_purchases(
            user_id=user_id, club_name=club or "", item_id="train_5", season_id=current_season_id
        )
        if train_season_count >= target.get("max_per_season", 1):
            raise ValueError("Тренировку можно покупать максимум один раз за сезон.")
        train_3_seasons_count = database.count_recent_seasons_shop_item_purchases(
            user_id=user_id, club_name=club or "", item_id="train_5",
            current_season_id=current_season_id, seasons_count=3
        )
        if train_3_seasons_count >= target.get("max_recent_seasons", 3):
            raise ValueError("Лимит на тренировки исчерпан: максимум 3 покупки за три сезона.")

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
            window_id = active_window.get("id") if active_window else None
            inv_id = database.add_shop_inventory_item(
                user_id=user_id,
                club_name=club or "",
                item_id=item_id,
                charges=charges,
                tx_id=tx_id,
                meta=meta,
                window_id=window_id,
                season_id=current_season_id,
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
    Прокрут Колеса Фортуны за 25 000 🪙 (лимит 1 прокрут за сезон).
    Определяет выигравший сектор по весам и начисляет награду.
    """
    user_id = int(user_id)
    club = _coach_club_or_none(user_id) or ""
    price = ROULETTE_PRICE

    active_season = database.get_active_season()
    active_window = transfer_repo.get_active_window()
    current_season_id = (
        int(active_window.get("season_id"))
        if (active_window and active_window.get("season_id"))
        else (int(active_season["id"]) if active_season else 1)
    )

    season_spins = database.count_season_roulette_spins(user_id, club, current_season_id)
    if season_spins >= 1:
        raise ValueError("В одном сезоне можно крутить рулетку максимум 1 раз.")

    # Выбираем победителя по весам
    total_weight = sum(s["weight"] for s in ROULETTE_SECTORS)
    rand_val = random.uniform(0, total_weight)
    winning_sector = ROULETTE_SECTORS[0]
    for sector in ROULETTE_SECTORS:
        if rand_val < sector["weight"]:
            winning_sector = sector
            break
        rand_val -= sector["weight"]

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
                season_id=current_season_id,
                source="roulette",
            )

        spin_id = database.record_shop_roulette_spin(
            user_id=user_id,
            club_name=club,
            cost=price,
            won_item_id=item_id,
            won_label=winning_sector["reward_label"],
            tx_id=tx_id,
            season_id=current_season_id,
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

