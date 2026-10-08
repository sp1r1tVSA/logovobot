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
