"""
tests/test_shop_rewards.py

Comprehensive tests for Shop Rewards System:
- Catalog retrieval & transfer window locking
- Purchasing items (trainings, urn boost, surcharge coupon, slots, loan)
- Wheel of Fortune roulette spins & reward payouts
- Secret Player purchase, admin approval into squad, and decline with refund
- Urn boost (+1 card, unban restricted clubs) and Surcharge coupon (50% discount) integration
- Swap slot mechanic (swap doesn't burn regular slots)
- Precise limits:
  * train_5: 1 per season, max 3 across 3 seasons
  * roulette_spin: 1 per season
  * transfer rewards: 1 per window each, max 2 total transfer rewards per window
"""

import itertools
import pytest
import database
from services import shop_service
from transfers import repo as transfer_repo
from transfers import requests as req_mod

_user_id_gen = itertools.count(8881001)


@pytest.fixture(autouse=True)
def clean_transfer_windows():
    database.init_db()
    active = transfer_repo.get_active_window()
    if active:
        transfer_repo.close_window(active["id"], 1)
    yield
    active = transfer_repo.get_active_window()
    if active:
        transfer_repo.close_window(active["id"], 1)


@pytest.fixture
def test_user():
    user_id = 8881001
    username = "shop_tester"
    team = "Арсенал"
    with database.transaction() as conn:
        conn.execute("DELETE FROM shop_inventory WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM shop_roulette_spins WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM shop_secret_player_claims WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM coin_transactions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM squad_players WHERE player_name = 'Thierry Henry'")
        conn.execute("DELETE FROM transfers WHERE initiator_id = ? OR to_user = ? OR from_user = ?", (user_id, user_id, user_id))
        conn.execute("DELETE FROM transfer_slot_purchases WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE telegram_id = ? OR LOWER(TRIM(team_name)) = LOWER(?)", (user_id, team))
    database.register_user(user_id, username, team_name=team)
    database.add_coins(user_id, 200000, tx_type="test_seed")
    return {"id": user_id, "username": username, "team": team}


def test_shop_catalog_without_window(test_user):
    """Каталог без открытого трансферного окна: трансферные товары недоступны, тренировки и рулетка доступны."""
    res = shop_service.get_shop_catalog(test_user["id"])
    assert res["balance"] >= 200000
    assert res["club"] in ("Arsenal", "Арсенал")

    items_by_id = {it["id"]: it for it in res["items"]}
    assert items_by_id["train_5"]["available"] is True
    assert items_by_id["roulette_spin"]["available"] is True
    assert items_by_id["secret_player"]["available"] is True

    # Товары ТО без открытого окна недоступны
    assert items_by_id["urna_boost"]["available"] is False
    assert items_by_id["credit_transfer"]["available"] is False
    assert items_by_id["slot_swap"]["available"] is False
    assert items_by_id["surcharge_coupon"]["available"] is False


def test_buy_train_5(test_user):
    """Покупка +5 тренировок за 4 500 🪙: баланс уменьшается, в инвентарь начисляется 5 зарядов."""
    init_balance = database.get_wallet_balance(test_user["id"])
    buy_res = shop_service.buy_shop_item(test_user["id"], "train_5")
    assert buy_res["status"] == "ok"
    assert buy_res["price"] == 4500
    assert buy_res["charges"] == 5

    new_balance = database.get_wallet_balance(test_user["id"])
    assert new_balance == init_balance - 4500

    charges = database.count_active_shop_item(test_user["id"], "train_5")
    assert charges == 5


def test_train_5_season_limits(test_user):
    """
    Тренировка (4 500 🪙):
    - Можно покупать 1 раз за сезон. Вторая покупка в том же сезоне блокируется.
    - Максимальное количество покупок за три сезона — три штуки.
    """
    # 1. Первая покупка в сезоне успешна
    res1 = shop_service.buy_shop_item(test_user["id"], "train_5")
    assert res1["status"] == "ok"

    # 2. Повторная покупка в том же сезоне блокируется
    cat = shop_service.get_shop_catalog(test_user["id"])
    train_item = next(it for it in cat["items"] if it["id"] == "train_5")
    assert train_item["available"] is False
    assert "Лимит на сезон исчерпан" in train_item["reason"]

    with pytest.raises(ValueError, match="максимум один раз за сезон"):
        shop_service.buy_shop_item(test_user["id"], "train_5")

    # 3. Проверка лимита 3 сезона:
    # В шаге 1 покупка была записана в сезон 1 (дефолтный сезон при отсутствии открытого сезона)
    # Добавляем покупки для пользователя в сезоне 2 и сезоне 3
    database.add_shop_inventory_item(
        user_id=test_user["id"],
        club_name=test_user["team"],
        item_id="train_5",
        charges=5,
        tx_id=101,
        season_id=2,
        source="purchase",
    )
    database.add_shop_inventory_item(
        user_id=test_user["id"],
        club_name=test_user["team"],
        item_id="train_5",
        charges=5,
        tx_id=102,
        season_id=3,
        source="purchase",
    )

    # Всего за последние 3 сезона (при текущем сезоне 3) уже 3 покупки (сезоны 1, 2, 3)
    recent_cnt = database.count_recent_seasons_shop_item_purchases(
        test_user["id"], test_user["team"], "train_5", current_season_id=3, seasons_count=3
    )
    assert recent_cnt == 3


def test_spin_roulette(test_user):
    """Прокрут Рулетки Фортуны за 25 000 🪙: списание монет, фиксация выигрыша и начисление награды."""
    init_balance = database.get_wallet_balance(test_user["id"])
    spin_res = shop_service.spin_roulette(test_user["id"])
    assert spin_res["status"] == "ok"
    assert 0 <= spin_res["winning_index"] <= 7

    new_balance = database.get_wallet_balance(test_user["id"])
    assert new_balance == init_balance - 25000

    with database.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM shop_roulette_spins WHERE user_id = ?", (test_user["id"],))
        row = c.fetchone()
        assert row is not None
        assert row["cost"] == 25000


def test_roulette_season_limit(test_user):
    """Билет в рулетку (25 000 🪙): лимит 1 покупка (прокрут) за сезон."""
    # Первый прокрут успешен
    res1 = shop_service.spin_roulette(test_user["id"])
    assert res1["status"] == "ok"

    # Каталог отражает израсходованный лимит
    cat = shop_service.get_shop_catalog(test_user["id"])
    assert cat["roulette_spins_this_season"] == 1
    roulette_item = next(it for it in cat["items"] if it["id"] == "roulette_spin")
    assert roulette_item["available"] is False
    assert "Лимит на сезон исчерпан" in roulette_item["reason"]

    # Второй прокрут вызывает исключение
    with pytest.raises(ValueError, match="сезоне можно крутить рулетку максимум 1 раз"):
        shop_service.spin_roulette(test_user["id"])


def test_secret_player_claim_and_admin_approval(test_user):
    """Покупка секретного игрока за 50 000 🪙 -> заявка -> админ утверждает -> игрок в squad_players."""
    init_balance = database.get_wallet_balance(test_user["id"])
    buy_res = shop_service.buy_shop_item(test_user["id"], "secret_player", notes="Нужен топ форвард")
    assert buy_res["status"] == "ok"
    claim_id = buy_res["claim_id"]

    new_balance = database.get_wallet_balance(test_user["id"])
    assert new_balance == init_balance - 50000

    claims = database.list_secret_player_claims(status="pending")
    assert any(c["id"] == claim_id for c in claims)

    admin_id = 999999
    star_name = "Thierry Henry"
    ok, msg = database.resolve_secret_player_claim(
        claim_id=claim_id,
        admin_id=admin_id,
        player_name=star_name,
        action="approved",
        notes="Выдан топ-форвард",
    )
    assert ok is True

    player_record = database.find_player_in_squad(star_name, "Арсенал")
    assert player_record is not None
    assert player_record["player_name"] == star_name


def test_secret_player_claim_and_admin_decline_refund(test_user):
    """Покупка секретного игрока -> отклонение админом -> полный возврат 50 000 🪙."""
    init_balance = database.get_wallet_balance(test_user["id"])
    buy_res = shop_service.buy_shop_item(test_user["id"], "secret_player")
    claim_id = buy_res["claim_id"]
    assert database.get_wallet_balance(test_user["id"]) == init_balance - 50000

    admin_id = 999999
    ok, msg = database.resolve_secret_player_claim(
        claim_id=claim_id,
        admin_id=admin_id,
        action="declined",
        notes="Квота звезд исчерпана",
    )
    assert ok is True
    assert database.get_wallet_balance(test_user["id"]) == init_balance


def test_transfer_item_per_window_limit(test_user):
    """Каждый трансферный товар (5 500 - 10 000 🪙) имеет лимит: 1 покупка на ТО."""
    wid = transfer_repo.create_window(1, 10, title="ТО Зима")
    transfer_repo.open_window(wid, 1)

    # 1. Покупка кредита
    r1 = shop_service.buy_shop_item(test_user["id"], "credit_transfer")
    assert r1["status"] == "ok"

    # Попытка купить кредит второй раз в том же окне блокируется индивидуальным лимитом
    with pytest.raises(ValueError, match="можно купить максимум 1 раз"):
        shop_service.buy_shop_item(test_user["id"], "credit_transfer")

    transfer_repo.close_window(wid, 1)


def test_transfer_rewards_window_limit(test_user):
    """
    Лимит наград за трансферное окно:
    В одно ТО можно купить МАКСИМУМ 2 награды, относящиеся к трансферам (5500 - 10000 🪙).
    Третья покупка блокируется. Не-трансферные награды (тренировки, рулетка) остаются доступными.
    """
    wid = transfer_repo.create_window(1, 10, title="ТО Зима")
    transfer_repo.open_window(wid, 1)

    cat = shop_service.get_shop_catalog(test_user["id"])
    assert cat["is_window_open"] is True
    assert cat["window_transfer_rewards_bought"] == 0
    assert cat["window_transfer_rewards_limit"] == 2

    items = {it["id"]: it for it in cat["items"]}
    assert items["credit_transfer"]["available"] is True
    assert items["slot_swap"]["available"] is True
    assert items["urna_boost"]["available"] is True
    assert items["surcharge_coupon"]["available"] is True

    # 1-я покупка: Трансферный кредит (5 500 🪙)
    r1 = shop_service.buy_shop_item(test_user["id"], "credit_transfer")
    assert r1["status"] == "ok"

    cat1 = shop_service.get_shop_catalog(test_user["id"])
    assert cat1["window_transfer_rewards_bought"] == 1
    items1 = {it["id"]: it for it in cat1["items"]}
    assert items1["urna_boost"]["available"] is True

    # 2-я покупка: Выгодная урна (7 000 🪙)
    r2 = shop_service.buy_shop_item(test_user["id"], "urna_boost")
    assert r2["status"] == "ok"

    cat2 = shop_service.get_shop_catalog(test_user["id"])
    assert cat2["window_transfer_rewards_bought"] == 2
    items2 = {it["id"]: it for it in cat2["items"]}
    # Все 4 трансферные награды теперь заблокированы по лимиту окна
    assert items2["credit_transfer"]["available"] is False
    assert items2["slot_swap"]["available"] is False
    assert items2["urna_boost"]["available"] is False
    assert items2["surcharge_coupon"]["available"] is False
    assert "Лимит наград за окно исчерпан" in items2["slot_swap"]["reason"]

    # 3-я покупка трансферной награды вызывает ошибку
    with pytest.raises(ValueError, match="максимум 2"):
        shop_service.buy_shop_item(test_user["id"], "slot_swap")

    # Но тренировки (4 500 🪙) по-прежнему доступны!
    assert items2["train_5"]["available"] is True
    r_train = shop_service.buy_shop_item(test_user["id"], "train_5")
    assert r_train["status"] == "ok"

    transfer_repo.close_window(wid, 1)


def test_transfer_rewards_new_window_resets_limit(test_user):
    """
    В новом трансферном окне лимит покупок трансферных наград считается заново.
    """
    wid1 = transfer_repo.create_window(1, 10, title="ТО Зима")
    transfer_repo.open_window(wid1, 1)

    shop_service.buy_shop_item(test_user["id"], "credit_transfer")
    shop_service.buy_shop_item(test_user["id"], "slot_swap")

    transfer_repo.close_window(wid1, 1)
    wid2 = transfer_repo.create_window(1, 11, title="ТО Лето")
    transfer_repo.open_window(wid2, 1)

    cat = shop_service.get_shop_catalog(test_user["id"])
    assert cat["window_transfer_rewards_bought"] == 0
    items = {it["id"]: it for it in cat["items"]}
    assert items["credit_transfer"]["available"] is True
    assert items["urna_boost"]["available"] is True

    r = shop_service.buy_shop_item(test_user["id"], "urna_boost")
    assert r["status"] == "ok"
    transfer_repo.close_window(wid2, 1)


def test_surcharge_coupon_transfer_integration(test_user):
    """
    Купон на доплату (10 000 🪙):
    Дает скидку 50% на доплату за спешл-карты и списывается при создании заявки.
    """
    wid = transfer_repo.create_window(1, 10, title="ТО Доплата")
    transfer_repo.open_window(wid, 1)
    transfer_repo.update_window_settings(wid, {"surcharge_table": {105: 30000}})
    transfer_repo.set_club_budget(wid, "Арсенал", 50000, 1)

    # Покупаем купон
    shop_service.buy_shop_item(test_user["id"], "surcharge_coupon")
    assert database.count_active_shop_item(test_user["id"], "surcharge_coupon") == 1

    # Создаем доплату: базовая цена 30 000k, со скидкой 50% -> 15 000k
    sc = req_mod.create_surcharge(test_user["id"], player="Special Player", ovr=105)
    assert sc["status"] == "pending_manager"
    assert sc["price_k"] == 15000

    # Купон израсходован
    assert database.count_active_shop_item(test_user["id"], "surcharge_coupon") == 0
    transfer_repo.close_window(wid, 1)


def test_urna_boost_transfer_integration(test_user):
    """
    Выгодная урна (7 000 🪙):
    Позволяет выбросить в урну еще одного игрока.
    Для клубов/дивизионов с запретом на урну снимает запрет и дает возможность утилизировать 1 игрока.
    """
    wid = transfer_repo.create_window(1, 10, title="ТО Урна")
    transfer_repo.open_window(wid, 1)
    # Запрещаем урну для Арсенала и ставим лимит 0
    transfer_repo.update_window_settings(wid, {
        "urn_restricted_clubs": ["Arsenal", "Арсенал"],
        "urn_max_per_club": 0,
    })

    # Без урны заявка блокируется
    with pytest.raises(Exception):
        req_mod.create_urn_sale(test_user["id"], player="Old Card", tm_price="10", special_price="2", sellable=True)

    # Покупаем Выгодную урну
    shop_service.buy_shop_item(test_user["id"], "urna_boost")
    assert database.count_active_shop_item(test_user["id"], "urna_boost") == 1

    # Теперь продажа в урну разрешена!
    sale = req_mod.create_urn_sale(test_user["id"], player="Old Card", tm_price="10", special_price="2", sellable=True)
    assert sale["status"] == "pending_manager"
    assert sale["price_k"] == 6000  # (10 + 2) / 2

    # Заряд урны списан
    assert database.count_active_shop_item(test_user["id"], "urna_boost") == 0
    transfer_repo.close_window(wid, 1)


def test_admin_shop_overview_and_stats(test_user):
    """Проверка сбора статистики и обзора магазина для админ-панели."""
    # Покупка тренировки
    shop_service.buy_shop_item(test_user["id"], "train_5")

    stats = database.get_shop_admin_stats()
    assert stats["total_purchases"] >= 1
    assert stats["active_inventory_items"] >= 1
    assert stats["total_coins_spent"] >= 4500

    inv = database.list_all_shop_inventory()
    assert len(inv) >= 1
    assert any(i["user_id"] == test_user["id"] and i["item_id"] == "train_5" for i in inv)


def test_admin_shop_grant_and_revoke(test_user):
    """Администратор может вручную выдать награду и списать/аннулировать ее."""
    admin_id = 999999
    # 1. Выдача награды
    inv_id = database.admin_grant_shop_item(
        user_id=test_user["id"],
        item_id="slot_swap",
        charges=2,
        notes="Приз за турнир",
        admin_id=admin_id,
    )
    assert inv_id > 0
    assert database.count_active_shop_item(test_user["id"], "slot_swap") == 2

    # 2. Аннулирование награды
    ok = database.admin_revoke_shop_item(inv_id, admin_id=admin_id, reason="Срок истек")
    assert ok is True
    assert database.count_active_shop_item(test_user["id"], "slot_swap") == 0


def test_admin_shop_reset_limits(test_user):
    """Сброс лимитов покупок и рулетки администратором."""
    # Крутим рулетку
    spin_res = shop_service.spin_roulette(test_user["id"])
    assert spin_res["status"] == "ok"

    # Второй раз нельзя
    with pytest.raises(ValueError):
        shop_service.spin_roulette(test_user["id"])

    # Админ сбрасывает лимит рулетки
    res = database.admin_reset_shop_user_limits(test_user["id"], limit_type="roulette", admin_id=999999)
    assert "roulette" in res["cleared"]

    # Теперь можно крутить снова
    spin_res2 = shop_service.spin_roulette(test_user["id"])
    assert spin_res2["status"] == "ok"


def test_slot_swap_compensates_both_clubs(test_user):
    """
    «Слот обмена» (6 000 🪙):
    Компенсирует слоты покупки и продажи ОБОИМ участникам сделки,
    даже если у второго клуба лимит слотов был 0.
    """
    other_user_id = 8882002
    with database.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
            (other_user_id, "chelsea_coach", "Челси"),
        )
        conn.execute("INSERT OR REPLACE INTO squad_players (team_name, player_name, position) VALUES ('Арсенал', 'B. Saka', 'RW')")
        conn.execute("INSERT OR REPLACE INTO squad_players (team_name, player_name, position) VALUES ('Челси', 'C. Palmer', 'CAM')")

    wid = transfer_repo.create_window(1, 10, title="ТО Обмены")
    transfer_repo.open_window(wid, 1)
    # Ставим 0 базовых слотов для проверки, что Слот обмена открывает сделку обоим
    transfer_repo.update_window_settings(wid, {
        "max_buys": 0,
        "max_sells": 0,
    })
    transfer_repo.set_club_budget(wid, "Арсенал", 50000, 1)
    transfer_repo.set_club_budget(wid, "Челси", 50000, 1)

    # Покупаем «Слот обмена» для test_user (Арсенал)
    shop_service.buy_shop_item(test_user["id"], "slot_swap")
    assert database.count_active_shop_item(test_user["id"], "slot_swap") == 1

    # Совершаем обмен
    t1 = req_mod.create_swap(
        test_user["id"],
        other_club="Челси",
        give_player="B. Saka",
        give_price="20",
        give_ovr="105",
        get_player="C. Palmer",
        get_price="20",
        get_ovr="105",
    )
    assert t1["status"] == "pending_counterparty"

    # Предмет израсходован
    assert database.count_active_shop_item(test_user["id"], "slot_swap") == 0

    # Слоты компенсированы ОБОИМ клубам
    slot_purchases = transfer_repo.list_slot_purchases(wid)
    arsenal_slots = [p for p in slot_purchases if p["club_name"] == "Арсенал"]
    chelsea_slots = [p for p in slot_purchases if p["club_name"] == "Челси"]

    assert len(arsenal_slots) == 2  # buy + sell
    assert len(chelsea_slots) == 2  # buy + sell
    assert any(p["slot_type"] == "buy" for p in chelsea_slots)
    assert any(p["slot_type"] == "sell" for p in chelsea_slots)

    transfer_repo.close_window(wid, 1)


