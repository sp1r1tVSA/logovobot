"""Сверка составов: одобренные заявки окна против живых `squad_players`."""

import pytest

import config
import database
from transfers import approval, handlers, reconcile, repo, requests as req_mod, squad

MANAGER = 777


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in (
            "transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
            "transfer_core_snapshot", "transfer_players", "transfer_topics",
            "transfer_sanctions", "squad_players", "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfer_swap_links")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [990001])


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _squad(team, *players):
    with database.transaction() as conn:
        for name in players:
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, 'CM')",
                         (team, name))


def _drop(team, name):
    with database.transaction() as conn:
        conn.execute("DELETE FROM squad_players WHERE team_name = ? AND player_name = ?", (team, name))


def _setup():
    """Окно, Арсенал с Сакой, одобренная сделка Сака → Челси (состав не тронут)."""
    wid = repo.create_window(1, 10, title="ТО Зима")
    repo.open_window(wid, 1)
    _user(101, "chelsea", "Челси")
    _user(102, "arsenal", "Арсенал")
    repo.set_club_budget(wid, "Челси", 50000, 1)
    repo.set_club_budget(wid, "Арсенал", 50000, 1)
    _squad("Арсенал", "B. Saka", "P1", "P2", "P3", "P4", "P5", "P6")
    _squad("Челси", "C1", "C2")
    deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
    deal = req_mod.confirm(102, deal["id"])
    return wid, approval.approve(MANAGER, deal["id"]).transfer


def _kinds(issues):
    return [i.kind for i in issues]


class TestReconcile:
    def test_no_requests_no_issues(self):
        wid = repo.create_window(1, 10, title="ТО")
        assert reconcile.reconcile(wid) == []

    def test_unapplied_request_reported(self):
        wid, t = _setup()
        issues = reconcile.reconcile(wid)
        assert _kinds(issues) == ["not_applied"] and issues[0].transfer_id == t["id"]

    def test_applied_request_is_clean(self):
        wid, t = _setup()
        squad.apply(MANAGER, t["id"])
        assert reconcile.reconcile(wid) == []

    def test_player_removed_by_hand_after_apply_is_missing(self):
        wid, t = _setup()
        squad.apply(MANAGER, t["id"])
        _drop("Челси", "B. Saka")
        issues = reconcile.reconcile(wid)
        assert _kinds(issues) == ["missing"] and "Челси" in issues[0].text

    def test_player_still_in_source_club_is_extra(self):
        wid, t = _setup()
        squad.apply(MANAGER, t["id"])
        _squad("Арсенал", "B. Saka")
        issues = reconcile.reconcile(wid)
        assert _kinds(issues) == ["extra"] and "Арсенал" in issues[0].text

    def test_unapplied_player_in_two_clubs_is_duplicate(self):
        wid, _ = _setup()
        _squad("Челси", "B. Saka")
        assert _kinds(reconcile.reconcile(wid)) == ["not_applied", "duplicate"]

    def test_only_window_players_are_checked(self):
        wid, t = _setup()
        squad.apply(MANAGER, t["id"])
        _squad("Челси", "Same Name")
        _squad("Арсенал", "Same Name")          # двойник вне заявок окна
        assert reconcile.reconcile(wid) == []

    def test_cancelled_request_is_ignored(self):
        wid, t = _setup()
        squad.apply(MANAGER, t["id"])
        squad.cancel(MANAGER, t["id"])
        assert reconcile.reconcile(wid) == []

    def test_urn_sale_expects_player_nowhere(self):
        wid, _ = _setup()
        req = req_mod.create_urn_sale(102, player="P1", tm_price="10", special_price="2", sellable=True)
        t = approval.approve(MANAGER, req["id"]).transfer
        squad.apply(MANAGER, t["id"])
        # сделку Саки применяем тоже, чтобы в отчёте остался только P1
        squad.apply(MANAGER, next(x["id"] for x in repo.list_transfers(wid) if x["player_name"] == "B. Saka"))
        assert reconcile.reconcile(wid) == []
        _squad("Арсенал", "P1")
        issues = reconcile.reconcile(wid)
        assert _kinds(issues) == ["extra"] and issues[0].player == "P1"

    def test_summary_counts(self):
        wid, _ = _setup()
        _squad("Челси", "B. Saka")
        counts = reconcile.summary(reconcile.reconcile(wid))
        assert counts == {"not_applied": 1, "missing": 0, "extra": 0, "duplicate": 1}


class TestPanel:
    def test_clean_view(self):
        wid, t = _setup()
        squad.apply(MANAGER, t["id"])
        text, kb = handlers._reconcile_view(True)
        assert "совпадают" in text
        assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["tw:rec", "tw:hub"]

    def test_manager_gets_apply_button(self):
        _setup()
        text, kb = handlers._reconcile_view(True)
        assert "не применена" in text
        assert "tw:sqall" in [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_admin_does_not_get_apply_button(self):
        _setup()
        _, kb = handlers._reconcile_view(False)
        assert "tw:sqall" not in [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_no_approved_requests(self):
        repo.create_window(1, 10, title="ТО")
        text, _ = handlers._reconcile_view(True)
        assert "сверять нечего" in text

    def test_hub_button_hidden_without_approved(self):
        wid = repo.create_window(1, 10, title="ТО")
        repo.open_window(wid, 1)
        _, kb = handlers._hub_view()
        assert "tw:rec" not in [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_hub_button_shown_with_approved(self):
        _setup()
        _, kb = handlers._hub_view()
        assert "tw:rec" in [b.callback_data for row in kb.inline_keyboard for b in row]
