"""Миграция 033 и репозиторий трансферного окна на живой SQLite."""

import json
import sqlite3

import pytest

import database
from transfers import repo
from transfers.schema import MIGRATION_033_TRANSFER_WINDOW


@pytest.fixture(autouse=True)
def _clean_tables():
    with database.transaction() as conn:
        for table in ("transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
                      "transfer_core_snapshot", "transfer_players", "transfer_topics",
                      "transfer_sanctions"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
    yield


def test_migration_recorded_and_idempotent():
    database.init_db()      # повторный прогон ничего не ломает
    with database.transaction() as conn:
        row = conn.execute("SELECT version FROM schema_migrations WHERE version = ?",
                           (MIGRATION_033_TRANSFER_WINDOW,)).fetchone()
    assert row is not None


class TestWindows:
    def test_create_copies_defaults(self):
        wid = repo.create_window(season_id=1, created_by=10, title="Зимнее ТО")
        w = repo.get_window(wid)
        assert w["status"] == "draft" and w["max_buys"] == 3 and w["ovr_cap"] == 114
        assert json.loads(w["surcharge_table"]) == {"100": 10000, "111": 160000}
        assert w["created_at"] and w["updated_at"]
        assert repo.get_active_window()["id"] == wid

    def test_only_one_unclosed_window(self):
        wid = repo.create_window(1, 10)
        with pytest.raises(repo.WindowConflict):
            repo.create_window(1, 10)
        assert repo.open_window(wid, 10)
        with pytest.raises(repo.WindowConflict):
            repo.create_window(1, 10)
        assert repo.close_window(wid, 10)
        assert repo.get_active_window() is None
        second = repo.create_window(1, 10)
        assert repo.get_active_window()["id"] == second

    def test_status_transitions(self):
        wid = repo.create_window(1, 10)
        assert repo.open_window(wid, 7)
        assert not repo.open_window(wid, 7)
        w = repo.get_window(wid)
        assert w["opened_by"] == 7 and w["opened_at"]
        assert repo.close_window(wid, 8)
        assert not repo.close_window(wid, 8)
        assert not repo.open_window(wid, 7)

    def test_update_settings(self):
        wid = repo.create_window(1, 10)
        w = repo.update_window_settings(wid, {
            "max_buys": 5, "fa_restricted_clubs": "Ливерпуль, Челси",
            "surcharge_table": {"105": 50000, 100: 12000}, "fa_opens_at": "2026-10-05 12:00:00",
        })
        assert w["max_buys"] == 5 and w["max_sells"] == 3
        assert json.loads(w["fa_restricted_clubs"]) == ["Ливерпуль", "Челси"]
        s = repo.get_window_settings(wid)
        assert dict(s.surcharge_table) == {100: 12000, 105: 50000}
        assert s.fa_opens_at == "2026-10-05 12:00:00"

    @pytest.mark.parametrize("changes", [
        {"status": "open"}, {"max_buys": -1}, {"urn_divisor_sellable": 0},
        {"max_buys": "x"}, {"surcharge_table": {"100": -5}},
    ])
    def test_update_rejects(self, changes):
        wid = repo.create_window(1, 10)
        with pytest.raises(ValueError):
            repo.update_window_settings(wid, changes)
        assert repo.get_window(wid)["status"] == "draft"


class TestLedgerFromDb:
    def test_budget_and_transfers(self):
        wid = repo.create_window(1, 10)
        repo.open_window(wid, 10)
        assert repo.get_club_budget(wid, "Ливерпуль") is None
        repo.set_club_budget(wid, "Ливерпуль", 50000, updated_by=10)
        repo.set_club_budget(wid, "ливерпуль", 60000, updated_by=10, source="rule")
        assert repo.get_club_budget(wid, "Ливерпуль") == 60000
        assert len(repo.get_club_budgets(wid)) == 1

        t1 = repo.insert_transfer(wid, "deal", "Bukayo Saka", "pending_manager",
                                  from_club="Арсенал", to_club="Ливерпуль", price_k=20000, ovr=108)
        repo.insert_transfer(wid, "deal", "Cole Palmer", "rejected",
                             from_club="Челси", to_club="Ливерпуль", price_k=90000, ovr=108)
        repo.add_slot_purchase(wid, "Ливерпуль", "buy", 500, user_id=1)

        led = repo.get_club_ledger(wid, "Ливерпуль")
        assert led.spent_k == 20000 and led.remaining_k == 40000
        assert led.buys_used == 1 and led.buys_limit == 4
        assert repo.get_club_ledger(wid, "Ливерпуль", exclude_transfer_id=t1).spent_k == 0

        seller = repo.get_club_ledger(wid, "Арсенал")
        assert seller.sells_used == 1 and seller.earned_k == 0
        assert repo.set_transfer_status(t1, "approved", expected=("pending_manager",), actor_id=10)
        assert repo.get_club_ledger(wid, "Арсенал").earned_k == 20000

    def test_status_compare_and_set(self):
        wid = repo.create_window(1, 10)
        tid = repo.insert_transfer(wid, "deal", "Bukayo Saka", "pending_counterparty",
                                   from_club="Арсенал", to_club="Ливерпуль", price_k=1000, ovr=100)
        assert not repo.set_transfer_status(tid, "approved", expected=("pending_manager",))
        assert repo.set_transfer_status(tid, "pending_manager", expected=("pending_counterparty",))
        assert repo.set_transfer_status(tid, "rejected", expected=("pending_manager",),
                                        actor_id=5, reason="нет денег")
        t = repo.get_transfer(tid)
        assert t["status"] == "rejected" and t["decided_by"] == 5 and t["decided_reason"] == "нет денег"
        assert t["norm_name"] == "bukayo saka"

    def test_insert_validation(self):
        wid = repo.create_window(1, 10)
        with pytest.raises(ValueError):
            repo.insert_transfer(wid, "loan", "A B", "pending_manager")
        with pytest.raises(ValueError):
            repo.insert_transfer(wid, "deal", "A B", "pending_manager", color="red")
        with pytest.raises(ValueError):
            repo.insert_transfer(wid, "deal", "   ", "pending_manager")

    def test_list_filters(self):
        wid = repo.create_window(1, 10)
        repo.insert_transfer(wid, "urn_sale", "Some Player", "approved", from_club="Ливерпуль",
                             price_k=15100, tm_price_k=25000, special_price_k=5350, sellable=True)
        repo.insert_transfer(wid, "free_agent", "Lamine Yamal", "pending_manager", to_club="Челси",
                             price_k=3000, commented_at="2026-10-05 12:00:30")
        assert len(repo.list_transfers(wid, club="ливерпуль")) == 1
        assert len(repo.list_transfers(wid, kinds=("free_agent",))) == 1
        assert len(repo.list_transfers(wid, statuses=("approved",))) == 1
        assert repo.list_transfers(wid, player_name="lamine  YAMAL")[0]["to_club"] == "Челси"
        assert repo.list_transfers(wid, kinds=("urn_sale",))[0]["sellable"] == 1


class TestDirectoryAndMisc:
    def test_player_directory(self):
        repo.upsert_player("Bukayo Saka", last_club="Арсенал", ovr=108, price_k=20000)
        repo.upsert_player("bukayo saka", last_club="Ливерпуль")
        p = repo.get_player("BUKAYO SAKA")
        assert p["ovr"] == 108 and p["last_club"] == "Ливерпуль" and p["banned"] == 0
        repo.set_player_ban("Bukayo Saka", True, "ходовой")
        assert repo.get_player("Bukayo Saka")["ban_reason"] == "ходовой"
        repo.set_player_ban("New Name", True)
        assert repo.get_player("new name")["banned"] == 1

    def test_sanctions(self):
        sid = repo.add_sanction(club_name="Ливерпуль", user_id=None, from_season_id=2,
                                until_season_id=3, reason="неявка", created_by=10)
        repo.add_sanction(club_name=None, user_id=42, from_season_id=2, until_season_id=2,
                          reason=None, created_by=10)
        assert repo.is_sanctioned(3, club_name="ливерпуль")
        assert not repo.is_sanctioned(4, club_name="Ливерпуль")
        assert repo.is_sanctioned(2, user_id=42) and not repo.is_sanctioned(3, user_id=42)
        assert repo.lift_sanction(sid, 10) and not repo.lift_sanction(sid, 10)
        assert not repo.is_sanctioned(3, club_name="Ливерпуль")
        with pytest.raises(ValueError):
            repo.add_sanction(club_name=None, user_id=None, from_season_id=1, until_season_id=1,
                              reason=None, created_by=1)

    def test_sanction_check_constraint(self):
        with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
            conn.execute("INSERT INTO transfer_sanctions (from_season_id, until_season_id, created_at) "
                         "VALUES (1, 1, '2026-10-01 00:00:00')")

    def test_topics(self):
        repo.bind_topic("requests", -100123, 5, bound_by=10)
        repo.bind_topic("requests", -100123, 7, bound_by=11)
        assert repo.get_topic("requests")["message_thread_id"] == 7
        assert set(repo.get_topics()) == {"requests"}
        with pytest.raises(ValueError):
            repo.bind_topic("chat", -1, None, None)

    def test_core_snapshot(self):
        wid = repo.create_window(1, 10)
        assert not repo.has_core_snapshot(wid)
        added = repo.save_core_snapshot(wid, "Ливерпуль", [("Mohamed Salah", "RW"), ("Virgil van Dijk", "CB")])
        again = repo.save_core_snapshot(wid, "Ливерпуль", [("mohamed salah", "RW")])
        assert (added, again) == (2, 0)
        assert [r["norm_name"] for r in repo.get_core_snapshot(wid, "ливерпуль")] == \
            ["mohamed salah", "virgil van dijk"]
        assert repo.has_core_snapshot(wid)

    def test_squad_ops(self):
        wid = repo.create_window(1, 10)
        tid = repo.insert_transfer(wid, "deal", "Bukayo Saka", "approved", from_club="Арсенал",
                                   to_club="Ливерпуль", price_k=1, ovr=100)
        repo.insert_squad_op(tid, "remove", "Арсенал", "Bukayo Saka", "RW", applied_by=10)
        repo.insert_squad_op(tid, "add", "Ливерпуль", "Bukayo Saka", "RW", applied_by=10)
        repo.mark_squad_applied(tid)
        assert repo.get_transfer(tid)["squad_applied_at"]
        assert len(repo.list_squad_ops(tid)) == 2
        assert repo.mark_squad_ops_reverted(tid) == 2
        assert repo.list_squad_ops(tid) == []
        assert len(repo.list_squad_ops(tid, include_reverted=True)) == 2
