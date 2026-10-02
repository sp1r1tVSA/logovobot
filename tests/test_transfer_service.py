"""Этап 2 трансферного окна: жизненный цикл, автозакрытие, снимок состава, бюджеты."""

import asyncio
import datetime as dt
import json

import pytest

import config
import database
from time_utils import now_msk
from transfers import notify, repo, service

NOW = dt.datetime(2026, 10, 2, 12, 0)


@pytest.fixture(autouse=True)
def _clean_tables():
    with database.transaction() as conn:
        for table in ("transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
                      "transfer_core_snapshot", "transfer_players", "transfer_topics",
                      "transfer_sanctions", "squad_players"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
    yield


def _squad(team, *players):
    with database.transaction() as conn:
        for name, pos in players:
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, ?)",
                         (team, name, pos))


class TestManager:
    def test_only_the_configured_id(self, monkeypatch):
        monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 555, raising=False)
        assert service.is_transfer_manager(555)
        assert not service.is_transfer_manager(556)
        assert not service.is_transfer_manager(None)

    def test_nobody_when_unset(self, monkeypatch):
        monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", None, raising=False)
        assert not service.is_transfer_manager(555)
        assert not service.is_transfer_manager(config.ADMIN_IDS[0] if config.ADMIN_IDS else 1)

    def test_panel_also_for_env_admins(self, monkeypatch):
        monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 555, raising=False)
        monkeypatch.setattr(config, "ADMIN_IDS", [990001])
        assert service.can_manage_window(555) and service.can_manage_window(990001)
        assert not service.is_transfer_manager(990001)     # уведомления — только ответственному
        assert not service.can_manage_window(556) and not service.can_manage_window(None)


class TestDatetime:
    @pytest.mark.parametrize("text, expected", [
        ("2026-10-10 20:00", dt.datetime(2026, 10, 10, 20, 0)),
        ("10.10.2026 20:00", dt.datetime(2026, 10, 10, 20, 0)),
        ("10.10.26 20:00", dt.datetime(2026, 10, 10, 20, 0)),
        ("10.10 20:00", dt.datetime(2026, 10, 10, 20, 0)),
        ("05.01  09:30", dt.datetime(2027, 1, 5, 9, 30)),      # без года — ближайшее будущее
    ])
    def test_parse(self, text, expected):
        assert service.parse_window_datetime(text, now=NOW) == expected

    @pytest.mark.parametrize("text", ["", "завтра", "32.10 20:00", "10.10"])
    def test_unreadable(self, text):
        assert service.parse_window_datetime(text, now=NOW) is None

    def test_repo_rejects_unreadable_datetime(self):
        wid = repo.create_window(1, 10)
        with pytest.raises(ValueError):
            repo.update_window_settings(wid, {"auto_close_at": "когда-нибудь"})
        w = repo.update_window_settings(wid, {"auto_close_at": "10.10.2026 20:00"})
        assert w["auto_close_at"] == "2026-10-10 20:00:00"


class TestAutoClose:
    def test_set_and_clear(self):
        wid = service.create_window(10)
        w = service.set_auto_close(wid, "10.10 20:00", now=NOW)
        assert w["auto_close_at"] == "2026-10-10 20:00:00"
        assert service.set_auto_close(wid, "off", now=NOW)["auto_close_at"] is None

    @pytest.mark.parametrize("text", ["01.10.2026 20:00", "мусор"])
    def test_rejects_past_and_garbage(self, text):
        wid = service.create_window(10)
        with pytest.raises(service.InputError):
            service.set_auto_close(wid, text, now=NOW)

    def test_due_only_for_open_window(self):
        wid = service.create_window(10)
        repo.update_window_settings(wid, {"auto_close_at": "2026-10-02 11:00:00"})
        assert service.due_auto_close(NOW) is None                 # черновик сам не закрывается
        repo.open_window(wid, 10)
        assert service.due_auto_close(NOW)["id"] == wid
        assert service.due_auto_close(dt.datetime(2026, 10, 2, 10, 59)) is None
        repo.update_window_settings(wid, {"auto_close_at": None})
        assert service.due_auto_close(NOW) is None

    def test_job_closes_and_notifies(self, monkeypatch):
        monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 777, raising=False)
        wid = service.create_window(10)
        repo.open_window(wid, 10)
        past = (now_msk() - dt.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        repo.update_window_settings(wid, {"auto_close_at": past})
        bot = FakeBot()

        from transfers.jobs import job_auto_close
        asyncio.run(job_auto_close(type("Ctx", (), {"bot": bot})()))

        w = repo.get_window(wid)
        assert w["status"] == "closed" and w["closed_by"] == service.SYSTEM_ACTOR
        assert any(m["chat_id"] == 777 and "автоматически" in m["text"] for m in bot.sent)


class TestOpenClose:
    def test_close_rejects_only_unconfirmed(self):
        wid = service.create_window(10)
        service.open_window(wid, 10)
        waiting = repo.insert_transfer(wid, "deal", "A One", "pending_counterparty",
                                       from_club="Арсенал", to_club="Лидс", from_user=1, to_user=2)
        manager = repo.insert_transfer(wid, "deal", "B Two", "pending_manager",
                                       from_club="Арсенал", to_club="Ренн")
        result = service.close_window(wid, 10)
        assert result.closed and [t["id"] for t in result.rejected] == [waiting]
        t = repo.get_transfer(waiting)
        assert t["status"] == "rejected" and t["decided_reason"] == service.AUTO_REJECT_REASON
        assert t["decided_by"] == service.SYSTEM_ACTOR
        assert repo.get_transfer(manager)["status"] == "pending_manager"
        assert not service.close_window(wid, 10).closed

    def test_second_window_conflict(self):
        service.create_window(10)
        with pytest.raises(service.InputError):
            service.create_window(10)

    def test_open_writes_snapshot(self):
        _squad("Арсенал", ("Bukayo Saka", "RW"), ("Declan Rice", "CM"))
        _squad("Лидс", ("Some Player", "ST"))
        wid = service.create_window(10)
        result = service.open_window(wid, 10)
        assert result.opened
        snap = result.snapshot
        assert snap.clubs_saved == 2 and snap.players_added == 3
        assert "Ренн" in snap.without_squad and "Арсенал" not in snap.without_squad
        assert snap.clubs_total == len(service.league_clubs())
        assert {r["norm_name"] for r in repo.get_core_snapshot(wid, "Арсенал")} == {"bukayo saka", "declan rice"}
        assert not service.open_window(wid, 10).opened

    def test_late_snapshot_undoes_window_ops(self):
        """Состав, загруженный после сделок окна, снимается таким, каким был на открытие."""
        wid = service.create_window(10)
        service.open_window(wid, 10)
        assert not repo.core_snapshot_clubs(wid)
        # Ренн продал Alpha в Лидс, купил Beta из Ниццы — а состав Ренна загрузили только сейчас.
        _squad("Ренн", ("Beta", "CM"), ("Gamma", "GK"))
        t1 = repo.insert_transfer(wid, "deal", "Alpha", "approved", from_club="Ренн", to_club="Лидс")
        repo.insert_squad_op(t1, "remove", "Ренн", "Alpha", "ST", applied_by=10)
        t2 = repo.insert_transfer(wid, "deal", "Beta", "approved", from_club="Ницца", to_club="Ренн")
        repo.insert_squad_op(t2, "add", "Ренн", "Beta", "CM", applied_by=10)
        # Откаченная операция не учитывается.
        t3 = repo.insert_transfer(wid, "deal", "Gamma", "cancelled", from_club="Ренн", to_club="Лидс")
        repo.insert_squad_op(t3, "remove", "Ренн", "Gamma", "GK", applied_by=10)
        repo.mark_squad_ops_reverted(t3)

        snap = service.snapshot_core(wid)
        assert snap.clubs_saved == 1
        assert {r["norm_name"] for r in repo.get_core_snapshot(wid, "Ренн")} == {"alpha", "gamma"}

    def test_snapshot_only_missing(self):
        _squad("Арсенал", ("Bukayo Saka", "RW"))
        wid = service.create_window(10)
        service.open_window(wid, 10)
        _squad("Арсенал", ("Declan Rice", "CM"))
        _squad("Лидс", ("Some Player", "ST"))
        snap = service.snapshot_core(wid)
        assert snap.clubs_saved == 1 and snap.players_added == 1
        assert len(repo.get_core_snapshot(wid, "Арсенал")) == 1

    def test_squad_team_names_are_resolved(self):
        _squad("арсенал", ("Bukayo Saka", "RW"))
        wid = service.create_window(10)
        service.open_window(wid, 10)
        assert len(repo.get_core_snapshot(wid, "Арсенал")) == 1


class TestBudgets:
    def test_manual_set(self):
        wid = service.create_window(10)
        club, amount, old = service.set_budget(wid, "ливерпуля", "12,5", 10)
        assert (club, amount, old) == ("Ливерпуль", 12500, None)
        assert service.set_budget(wid, "Ливерпуль", "100", 10)[2] == 12500
        assert repo.get_club_budgets(wid)[0]["source"] == "manual"

    @pytest.mark.parametrize("club, amount", [("Реал", "10"), ("Совсем не клуб", "10"), ("Ливерпуль", "много")])
    def test_bad_input(self, club, amount):
        wid = service.create_window(10)
        with pytest.raises(service.InputError):
            service.set_budget(wid, club, amount, 10)

    def test_ambiguous_club_lists_suggestions(self):
        with pytest.raises(service.InputError, match="Реал Мадрид"):
            service.resolve_club("Реал")

    def test_default_budgets_hook_is_empty(self):
        wid = service.create_window(10)
        assert service.default_budgets(wid) == {}
        assert service.apply_default_budgets(wid, 10).written == 0

    def test_rules_do_not_overwrite_manual(self):
        wid = service.create_window(10)
        service.set_budget(wid, "Арсенал", "50", 10)
        rules = {"Арсенал": 90000, "Лидс": 80000, "Нет такого": 1, "Ренн": -5}
        result = service.apply_default_budgets(wid, 10, rules=rules)
        assert result.written == 1 and result.kept_manual == ["Арсенал"]
        assert sorted(result.invalid) == ["Нет такого", "Ренн"]
        assert repo.get_club_budget(wid, "Арсенал") == 50000
        assert repo.get_club_budget(wid, "Лидс") == 80000
        forced = service.apply_default_budgets(wid, 10, rules=rules, overwrite_manual=True)
        assert forced.written == 2 and repo.get_club_budget(wid, "Арсенал") == 90000

    def test_budget_table_covers_league(self):
        wid = service.create_window(10)
        service.set_budget(wid, "Арсенал", "50", 10)
        rows = service.budget_table(wid)
        assert len(rows) == len(service.league_clubs())
        by_club = {r["club"]: r for r in rows}
        assert by_club["Арсенал"]["budget_k"] == 50000 and by_club["Лидс"]["budget_k"] is None
        assert service.overview(wid)["budgets"] == 1


class TestSettings:
    def test_parsing(self):
        wid = service.create_window(10)
        assert service.update_setting(wid, "max_buys", "5")["max_buys"] == 5
        w = service.update_setting(wid, "fa_restricted_clubs", "ливерпуля, Арсенал")
        assert json.loads(w["fa_restricted_clubs"]) == ["Ливерпуль", "Арсенал"]
        w = service.update_setting(wid, "fa_forbidden_clubs", "Real Madrid, Bayern")
        assert json.loads(w["fa_forbidden_clubs"]) == ["Real Madrid", "Bayern"]
        assert json.loads(service.update_setting(wid, "fa_forbidden_clubs", "-")["fa_forbidden_clubs"]) == []
        w = service.update_setting(wid, "surcharge_table", "100=10; 111=160, 105=12,5")
        assert json.loads(w["surcharge_table"]) == {"100": 10000, "105": 12500, "111": 160000}
        assert service.update_setting(wid, "title", "Зимнее ТО")["title"] == "Зимнее ТО"

    @pytest.mark.parametrize("key, text", [
        ("max_buys", "-1"), ("max_buys", "много"), ("urn_divisor_sellable", "0"),
        ("fa_restricted_clubs", "Реал"), ("surcharge_table", "100:10"), ("status", "open"),
        ("fa_opens_at", "когда-нибудь"),
    ])
    def test_rejects(self, key, text):
        wid = service.create_window(10)
        with pytest.raises(service.InputError):
            service.update_setting(wid, key, text)


class FakeBot:
    def __init__(self, fail_chats=()):
        self.sent = []
        self.fail_chats = set(fail_chats)

    async def send_message(self, chat_id, text, **kwargs):
        from telegram.error import Forbidden
        if chat_id in self.fail_chats:
            raise Forbidden("bot was blocked by the user")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})


class TestNotify:
    def test_unbound_topic_goes_to_manager(self, monkeypatch):
        monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 777, raising=False)
        bot = FakeBot()
        assert not asyncio.run(notify.post_to_topic(bot, "feed", "привет"))
        assert bot.sent[0]["chat_id"] == 777 and "не привязана" in bot.sent[0]["text"]

    def test_bound_topic(self):
        repo.bind_topic("feed", -100500, 12, bound_by=1)
        bot = FakeBot()
        assert asyncio.run(notify.post_to_topic(bot, "feed", "привет"))
        assert bot.sent == [{"chat_id": -100500, "text": "привет", "message_thread_id": 12,
                             "parse_mode": "HTML", "reply_markup": None, "disable_web_page_preview": True}]

    def test_close_notifies_coaches_and_reports_failed_dm(self, monkeypatch):
        monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 777, raising=False)
        repo.bind_topic("feed", -100500, 1, bound_by=1)
        repo.bind_topic("alerts", -100500, 3, bound_by=1)
        rejected = [{"id": 5, "kind": "deal", "player_name": "A One", "from_club": "Арсенал",
                     "to_club": "Лидс", "from_user": 1, "to_user": 2, "initiator_id": 2}]
        bot = FakeBot(fail_chats={2})
        asyncio.run(notify.announce_close(bot, {"id": 1, "title": None}, rejected, auto=False))
        chats = [m["chat_id"] for m in bot.sent]
        assert chats.count(1) == 1 and 2 not in chats
        alert = [m for m in bot.sent if m.get("message_thread_id") == 3][0]
        assert "A One" in alert["text"] and "<code>2</code>" in alert["text"]
