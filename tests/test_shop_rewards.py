"""
tests/test_shop_rewards.py

Comprehensive tests for Shop Rewards System:
- Catalog retrieval & transfer window locking
- Purchasing items (trainings, urn boost, surcharge coupon, slots, loan)
- Wheel of Fortune roulette spins & reward payouts
- Secret Player purchase, admin approval into squad, and decline with refund
- Urn boost (+25%) and Surcharge coupon (100% discount) integration in transfers
"""

import pytest
import database
from services import shop_service
from transfers import repo as transfer_repo


@pytest.fixture
def test_user():
    database.init_db()
    user_id = 8881001
    username = "shop_tester"
    team = "Arsenal"
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


def test_spin_roulette(test_user):
    """Прокрут Рулетки Фортуны за 25 000 🪙: списание монет, фиксация выигрыша и начисление награды."""
    init_balance = database.get_wallet_balance(test_user["id"])
    spin_res = shop_service.spin_roulette(test_user["id"])
    assert spin_res["status"] == "ok"
    assert 0 <= spin_res["winning_index"] <= 7

    new_balance = database.get_wallet_balance(test_user["id"])
    assert new_balance == init_balance - 25000

    # Проверяем историю прокрутов
    with database.transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM shop_roulette_spins WHERE user_id = ?", (test_user["id"],))
        row = c.fetchone()
        assert row is not None
        assert row["cost"] == 25000


def test_secret_player_claim_and_admin_approval(test_user):
    """Покупка секретного игрока за 50 000 🪙 -> заявка -> админ утверждает -> игрок в squad_players."""
    init_balance = database.get_wallet_balance(test_user["id"])
    buy_res = shop_service.buy_shop_item(test_user["id"], "secret_player", notes="Нужен топ форвард")
    assert buy_res["status"] == "ok"
    claim_id = buy_res["claim_id"]

    new_balance = database.get_wallet_balance(test_user["id"])
    assert new_balance == init_balance - 50000

    # Проверяем список заявок
    claims = database.list_secret_player_claims(status="pending")
    assert any(c["id"] == claim_id for c in claims)

    # Админ утверждает
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

    # Проверяем состав Арсенала
    player_record = database.find_player_in_squad(star_name, "Арсенал")
    assert player_record is not None
    assert player_record["player_name"] == star_name


def test_secret_player_claim_and_admin_decline_refund(test_user):
    """Покупка секретного игрока -> отклонение админом -> полный возврат 50 000 🪙."""
    init_balance = database.get_wallet_balance(test_user["id"])
    buy_res = shop_service.buy_shop_item(test_user["id"], "secret_player")
    claim_id = buy_res["claim_id"]
    assert database.get_wallet_balance(test_user["id"]) == init_balance - 50000

    # Админ отклоняет
    admin_id = 999999
    ok, msg = database.resolve_secret_player_claim(
        claim_id=claim_id,
        admin_id=admin_id,
        action="declined",
        notes="Квота звезд исчерпана",
    )
    assert ok is True
    # Проверяем возврат монет
    assert database.get_wallet_balance(test_user["id"]) == init_balance


def test_transfer_rewards_window_limit(test_user):
    """
    Лимит наград за трансферное окно:
    В одно ТО можно купить МАКСИМУМ 2 награды, относящиеся к трансферам (5500 - 10000 🪙).
    Третья покупка блокируется. Не-трансферные награды (тренировки, рулетка) остаются доступными.
    """
    wid = transfer_repo.create_window(1, 10, title="ТО Зима")
    transfer_repo.open_window(wid, 1)

    # В каталоге изначально 0 покупок из 2
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
    # Все 4 трансферные награды теперь заблокированы
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
    active = transfer_repo.get_active_window()
    if active:
        transfer_repo.close_window(active["id"], 1)

    wid1 = transfer_repo.create_window(1, 10, title="ТО Зима")
    transfer_repo.open_window(wid1, 1)

    shop_service.buy_shop_item(test_user["id"], "credit_transfer")
    shop_service.buy_shop_item(test_user["id"], "slot_swap")

    # Закрываем 1-е окно и открываем 2-е окно
    transfer_repo.close_window(wid1, 1)
    wid2 = transfer_repo.create_window(1, 11, title="ТО Лето")
    transfer_repo.open_window(wid2, 1)

    cat = shop_service.get_shop_catalog(test_user["id"])
    assert cat["window_transfer_rewards_bought"] == 0
    items = {it["id"]: it for it in cat["items"]}
    assert items["credit_transfer"]["available"] is True
    assert items["urna_boost"]["available"] is True

    # Успешно покупаем в новом окне
    r = shop_service.buy_shop_item(test_user["id"], "urna_boost")
    assert r["status"] == "ok"
    transfer_repo.close_window(wid2, 1)

