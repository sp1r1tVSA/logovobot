"""Обмен «игрок на игрока»: две связанные сделки, решаются вместе."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import config
import database
from transfers import api as tapi
from transfers import approval, handlers, notify, repo, requests as req_mod, service, squad

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
    journal = []

    async def _record(*args, **kwargs):
        journal.append((args, kwargs))

    monkeypatch.setattr(handlers.admin_journal, "record", _record)
    handlers._pending.clear()
    monkeypatch.setattr(handlers, "_prefetch_portrait_later", lambda t: None)
    yield journal
    handlers._pending.clear()


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _squad(team, *players):
    with database.transaction() as conn:
        for name in players:
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, ?)",
                         (team, name, "CM"))


def _names(club):
    return sorted(r["player_name"] for r in repo.squad_rows(club))


def _window():
    wid = repo.create_window(1, 10, title="ТО Зима")
    repo.open_window(wid, 1)
    _user(101, "chelsea", "Челси")
    _user(102, "arsenal", "Арсенал")
    repo.set_club_budget(wid, "Челси", 50000, 1)
    repo.set_club_budget(wid, "Арсенал", 50000, 1)
    return wid


def _swap(**over):
    args = dict(other_club="Арсенал", give_player="C. Palmer", give_price="20", give_ovr=105,
                get_player="B. Saka", get_price="15", get_ovr=106)
    args.update(over)
    return req_mod.create_swap(101, **args)


def _both(first):
    return first, repo.get_swap_partner(first)


# ─── Заявка ──────────────────────────────────────────────────────────────────

class TestCreate:
    def test_two_linked_legs(self):
        _window()
        a = _swap()
        a, b = _both(a)
        assert b is not None
        assert a["swap_partner_id"] == b["id"] and b["swap_partner_id"] == a["id"]
        assert (a["from_club"], a["to_club"], a["player_name"]) == ("Челси", "Арсенал", "C. Palmer")
        assert (b["from_club"], b["to_club"], b["player_name"]) == ("Арсенал", "Челси", "B. Saka")
        assert a["initiator_id"] == b["initiator_id"] == 101
        assert a["status"] == b["status"] == "pending_counterparty"
        assert a["price_k"] == 20000 and b["price_k"] == 15000

    def test_serialize_exposes_partner(self):
        _window()
        a, b = _both(_swap())
        assert req_mod.serialize(a, 101, private=True)["swap_partner_id"] == b["id"]

    def test_same_player_rejected(self):
        _window()
        with pytest.raises(service.InputError, match="различаться"):
            _swap(give_player="B. Saka", get_player="b. saka")
        assert repo.list_transfers(1) == []

    def test_own_club_rejected(self):
        _window()
        with pytest.raises(service.InputError, match="своим же клубом"):
            _swap(other_club="Челси")

    def test_club_without_coach_rejected(self):
        _window()
        with pytest.raises(service.InputError, match="нет тренера"):
            _swap(other_club="Барселона")

    def test_failed_leg_leaves_nothing(self):
        _window()
        with pytest.raises(service.InputError):
            _swap(get_ovr=999)          # OVR выше потолка окна — жёсткий блок второй половины
        assert repo.list_transfers(1) == []

    def test_non_swap_has_no_partner(self):
        _window()
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="X. Y", price="5", ovr=100)
        assert deal["swap_partner_id"] is None and repo.get_swap_partner(deal) is None


# ─── Подтверждение второй стороной ───────────────────────────────────────────

class TestCounterparty:
    def test_confirm_moves_both(self):
        _window()
        a, b = _both(_swap())
        req_mod.confirm(102, a["id"])
        assert repo.get_transfer(a["id"])["status"] == repo.get_transfer(b["id"])["status"] == "pending_manager"

    def test_confirm_via_second_leg(self):
        _window()
        a, b = _both(_swap())
        req_mod.confirm(102, b["id"])
        assert repo.get_transfer(a["id"])["status"] == "pending_manager"

    def test_initiator_cannot_confirm(self):
        _window()
        a, _ = _both(_swap())
        with pytest.raises(service.InputError):
            req_mod.confirm(101, a["id"])

    def test_decline_rejects_both(self):
        _window()
        a, b = _both(_swap())
        req_mod.decline(102, a["id"])
        assert repo.get_transfer(a["id"])["status"] == repo.get_transfer(b["id"])["status"] == "rejected"

    def test_withdraw_both(self):
        _window()
        a, b = _both(_swap())
        req_mod.withdraw(101, a["id"])
        assert repo.get_transfer(a["id"])["status"] == repo.get_transfer(b["id"])["status"] == "withdrawn"


# ─── Решение ответственного ──────────────────────────────────────────────────

def _confirmed():
    wid = _window()
    a, b = _both(_swap())
    req_mod.confirm(102, a["id"])
    return wid, repo.get_transfer(a["id"]), repo.get_transfer(b["id"])


class TestApproval:
    def test_approve_both(self):
        _, a, b = _confirmed()
        decision = approval.approve(MANAGER, a["id"])
        assert decision.transfer["status"] == "approved"
        assert decision.partner is not None and decision.partner["id"] == b["id"]
        assert repo.get_transfer(b["id"])["status"] == "approved"
        assert repo.get_player("C. Palmer")["last_club"] == "Арсенал"
        assert repo.get_player("B. Saka")["last_club"] == "Челси"

    def test_approve_via_second_leg(self):
        _, a, b = _confirmed()
        approval.approve(MANAGER, b["id"])
        assert repo.get_transfer(a["id"])["status"] == "approved"

    def test_plain_deal_has_no_partner(self):
        _window()
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="X. Y", price="5", ovr=100)
        deal = req_mod.confirm(102, deal["id"])
        assert approval.approve(MANAGER, deal["id"]).partner is None

    def test_reject_both(self):
        _, a, b = _confirmed()
        approval.reject(MANAGER, a["id"], "не сейчас")
        for tid in (a["id"], b["id"]):
            t = repo.get_transfer(tid)
            assert t["status"] == "rejected" and t["decided_reason"] == "не сейчас"

    def test_failed_recheck_of_one_leg_blocks_both(self):
        wid, a, b = _confirmed()
        with database.transaction() as conn:
            conn.execute("UPDATE transfers SET ovr = 999 WHERE id = ?", (b["id"],))
        with pytest.raises(service.InputError, match="Одобрить нельзя"):
            approval.approve(MANAGER, a["id"])
        assert repo.get_transfer(a["id"])["status"] == repo.get_transfer(b["id"])["status"] == "pending_manager"

    def test_partner_not_pending_blocks(self):
        _, a, b = _confirmed()
        with database.transaction() as conn:
            conn.execute("UPDATE transfers SET status = 'withdrawn' WHERE id = ?", (b["id"],))
        with pytest.raises(service.InputError, match="Вторая половина"):
            approval.approve(MANAGER, a["id"])
        assert repo.get_transfer(a["id"])["status"] == "pending_manager"

    def test_manager_only(self):
        _, a, _ = _confirmed()
        with pytest.raises(service.InputError, match="только ответственный"):
            approval.approve(990001, a["id"])


# ─── Состав: применение и отмена ─────────────────────────────────────────────

class TestSquad:
    def _approved(self):
        wid, a, b = _confirmed()
        _squad("Челси", "C. Palmer", "C1", "C2", "C3", "C4", "C5")
        _squad("Арсенал", "B. Saka", "P1", "P2", "P3", "P4", "P5")
        service.snapshot_core(wid)
        approval.approve(MANAGER, a["id"])
        return a, b

    def test_cancel_rolls_back_both(self):
        a, b = self._approved()
        squad.apply(MANAGER, a["id"])
        squad.apply(MANAGER, b["id"])
        assert "B. Saka" in _names("Челси") and "C. Palmer" in _names("Арсенал")
        cancelled, rolled = squad.cancel(MANAGER, a["id"])
        assert cancelled["status"] == "cancelled"
        assert repo.get_transfer(b["id"])["status"] == "cancelled"
        assert rolled is not None
        assert "C. Palmer" in _names("Челси") and "B. Saka" in _names("Арсенал")
        assert "B. Saka" not in _names("Челси") and "C. Palmer" not in _names("Арсенал")

    def test_cancel_unapplied_cancels_both(self):
        a, b = self._approved()
        squad.cancel(MANAGER, b["id"])
        assert repo.get_transfer(a["id"])["status"] == repo.get_transfer(b["id"])["status"] == "cancelled"

    def test_cancel_frees_budget(self):
        a, b = self._approved()
        squad.cancel(MANAGER, a["id"])
        # после отмены обе половины не считаются активными
        active = [t for t in repo.list_transfers(1) if t["status"] == "approved" or t["status"].startswith("pending")]
        assert active == []


# ─── Очередь, уведомления, маршрут ───────────────────────────────────────────

def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _texts(bot, chat_id):
    return [c.kwargs["text"] for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == chat_id]


class TestNotify:
    def test_swap_pair_lower_id_first(self):
        _window()
        a, b = _both(_swap())
        assert notify.swap_pair(b)[0]["id"] == a["id"] and notify.swap_pair(b)[1]["id"] == b["id"]
        plain = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="X. Y", price="5", ovr=100)
        assert notify.swap_pair(plain) == (plain, None)

    def test_proposal_is_one_dm(self):
        _window()
        a, _ = _both(_swap())
        bot = _bot()
        assert asyncio.run(notify.notify_deal_proposal(bot, a)) is True
        texts = _texts(bot, 102)
        assert len(texts) == 1
        assert "C. Palmer" in texts[0] and "B. Saka" in texts[0]

    def test_approved_is_one_dm_per_party(self):
        _, a, b = _confirmed()
        decision = approval.approve(MANAGER, a["id"])
        bot = _bot()
        asyncio.run(notify.notify_approved(bot, decision.transfer))
        for chat in (101, 102):
            texts = _texts(bot, chat)
            assert len(texts) == 1 and "C. Palmer" in texts[0] and "B. Saka" in texts[0]

    def test_approved_posts_card_per_leg(self):
        _, a, b = _confirmed()
        approval.approve(MANAGER, a["id"])
        bot = _bot()
        asyncio.run(notify.notify_approved(bot, repo.get_transfer(a["id"])))
        # темы не привязаны: каждая половина уходит ответственному отдельным постом
        posted = _texts(bot, MANAGER) + [c for c in bot.send_photo.await_args_list if c.kwargs.get("chat_id") == MANAGER]
        assert len(posted) == 2


class TestQueue:
    def test_queue_keeps_lead_leg_only(self):
        _, a, b = _confirmed()
        manager, counterparty = handlers._queue_items(repo.get_window(1))
        assert [t["id"] for t in manager] == [a["id"]] and counterparty == []

    def test_unconfirmed_swap_is_one_item(self):
        _window()
        a, _ = _both(_swap())
        manager, counterparty = handlers._queue_items(repo.get_window(1))
        assert manager == [] and [t["id"] for t in counterparty] == [a["id"]]


class TestRoute:
    def test_registered(self):
        app = web.Application()
        tapi.register_routes(app)
        assert any(r.resource.canonical == "/api/transfers/swap" and r.method == "POST"
                   for r in app.router.routes())

    def test_post_creates_swap(self, monkeypatch):
        _window()
        monkeypatch.setattr(tapi, "_auth", lambda r: ({"id": 101}, None))
        monkeypatch.setattr(tapi, "_get_bot", lambda r: _bot())
        fields = dict(other_club="Арсенал", give_player="C. Palmer", give_price="20", give_ovr="105",
                      get_player="B. Saka", get_price="15", get_ovr="106")

        async def _payload(request):
            return fields, None

        monkeypatch.setattr(tapi, "_read_request_payload", _payload)
        request = make_mocked_request("POST", "/api/transfers/swap", app=web.Application())
        response = asyncio.run(tapi.handle_post_swap(request))
        body = json.loads(response.text)
        assert response.status == 200 and body["status"] == "ok"
        assert body["transfer"]["swap_partner_id"] is not None
        assert len(repo.list_transfers(1)) == 2

    def test_bad_input_is_400(self, monkeypatch):
        _window()
        monkeypatch.setattr(tapi, "_auth", lambda r: ({"id": 101}, None))
        monkeypatch.setattr(tapi, "_get_bot", lambda r: _bot())

        async def _payload(request):
            return dict(other_club="Челси"), None

        monkeypatch.setattr(tapi, "_read_request_payload", _payload)
        request = make_mocked_request("POST", "/api/transfers/swap", app=web.Application())
        assert asyncio.run(tapi.handle_post_swap(request)).status == 400
