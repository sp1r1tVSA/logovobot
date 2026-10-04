"""Свободные агенты: автоматический разбор заявок из темы обычной группы."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import database
from transfers import approval, handlers, notify, repo, requests as req_mod, service

MANAGER = 777
GROUP, FA_THREAD, REQ_THREAD, FEED_THREAD = -100500, 42, 7, 8

TEMPLATE = """1. Erling Haaland OVR 108
2. Borussia Dortmund
3. {club}
4. 45
5. 15"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in (
            "transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
            "transfer_core_snapshot", "transfer_players", "transfer_topics", "transfer_topics_ext",
            "transfer_sanctions", "squad_players", "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    yield


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _setup(window="open"):
    wid = repo.create_window(1, 10, title="ТО Зима")
    if window == "open":
        repo.open_window(wid, 1)
    _user(101, "city", "Манчестер Сити")
    _user(102, "chelsea", "Челси")
    repo.set_club_budget(wid, "Манчестер Сити", 100000, 1)
    repo.set_club_budget(wid, "Челси", 100000, 1)
    repo.bind_topic("fa", GROUP, FA_THREAD, 1)
    repo.bind_topic("requests", GROUP, REQ_THREAD, 1)
    repo.bind_topic("feed", GROUP, FEED_THREAD, 1)
    return wid


def _msg(text, user_id=101, *, chat_id=GROUP, thread=FA_THREAD, chat_type="supergroup", is_bot=False,
         date=None):
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=chat_id),
        from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        text=text, caption=None, photo=[], message_thread_id=thread,
        date=date or dt.datetime(2026, 10, 2, 17, 5, tzinfo=dt.timezone.utc),
        reply_text=AsyncMock(),
    )


def _send(msg):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    update = SimpleNamespace(effective_message=msg, effective_user=msg.from_user)
    asyncio.run(handlers.on_fa_topic_message(update, SimpleNamespace(bot=bot)))
    return bot


def _reply(msg):
    return msg.reply_text.await_args.args[0]


class TestTopicFilter:
    def test_template_in_bound_topic_passes(self):
        _setup()
        assert handlers.FA_TOPIC_FILTER.filter(_msg(TEMPLATE.format(club="Сити")))

    @pytest.mark.parametrize("kwargs", [
        {"thread": 99}, {"chat_id": -100999}, {"chat_type": "private"}, {"is_bot": True},
    ])
    def test_other_place_or_sender_ignored(self, kwargs):
        _setup()
        assert not handlers.FA_TOPIC_FILTER.filter(_msg(TEMPLATE.format(club="Сити"), **kwargs))

    def test_free_talk_is_ignored(self):
        _setup()
        assert not handlers.FA_TOPIC_FILTER.filter(_msg("а когда закроется окно?"))
        assert not handlers.FA_TOPIC_FILTER.filter(_msg("1. Haaland\n2. Dortmund"))

    def test_unbound_topic_ignores_everything(self):
        assert not handlers.FA_TOPIC_FILTER.filter(_msg(TEMPLATE.format(club="Сити")))

    def test_handler_is_registered(self):
        from telegram.ext import MessageHandler

        added = []
        handlers.register_handlers(SimpleNamespace(add_handler=lambda h, *a, **k: added.append(h)))
        assert any(isinstance(h, MessageHandler) and "transfers.fa_topic" in repr(h.filters) for h in added)


class TestSubmission:
    def test_claim_becomes_pending_request_of_the_author(self):
        _setup()
        msg = _msg(TEMPLATE.format(club="Челси"))      # «Куда» — чужой клуб, берётся клуб автора
        bot = _send(msg)

        (t,) = repo.list_transfers(repo.get_active_window()["id"], kinds=("free_agent",))
        assert t["status"] == "pending_manager"
        assert t["to_club"] == "Манчестер Сити" and t["to_user"] == 101 and t["initiator_id"] == 101
        assert t["price_k"] == 45000 and t["ovr"] == 108
        assert t["commented_at"] == "2026-10-02 20:05:00"      # время сообщения, МСК
        assert "принята" in _reply(msg) and f"#{t['id']}" in _reply(msg)

        card = bot.send_message.await_args.kwargs
        assert card["chat_id"] == GROUP and card["message_thread_id"] == REQ_THREAD
        assert "Haaland" in card["text"]
        assert card["reply_markup"] is not None

    def test_second_claim_of_one_coach_refused(self):
        _setup()
        _send(_msg(TEMPLATE.format(club="Сити")))
        msg = _msg(TEMPLATE.format(club="Сити").replace("Haaland", "Mbappe"))
        _send(msg)
        assert "⚠️" in _reply(msg)
        assert len(repo.list_transfers(repo.get_active_window()["id"], kinds=("free_agent",))) == 1

    def test_user_without_club_refused(self):
        _setup()
        msg = _msg(TEMPLATE.format(club="Сити"), user_id=555)
        _send(msg)
        assert "⚠️" in _reply(msg) and "клуб" in _reply(msg)
        assert not repo.list_transfers(repo.get_active_window()["id"], kinds=("free_agent",))

    def test_closed_window_refused(self):
        wid = _setup()
        repo.close_window(wid, 1)
        msg = _msg(TEMPLATE.format(club="Сити"))
        _send(msg)
        assert "⚠️" in _reply(msg)

    def test_same_message_not_filed_twice(self):
        _setup()
        text = TEMPLATE.format(club="Сити")
        _send(_msg(text))
        msg = _msg(text)
        _send(msg)
        assert "⚠️" in _reply(msg)
        assert len(repo.list_transfers(repo.get_active_window()["id"], kinds=("free_agent",))) == 1

    def test_rejected_claim_frees_the_coach(self):
        _setup()
        _send(_msg(TEMPLATE.format(club="Сити")))
        (t,) = repo.list_transfers(repo.get_active_window()["id"], kinds=("free_agent",))
        approval.reject(MANAGER, t["id"], "не подходит")
        msg = _msg(TEMPLATE.format(club="Сити").replace("Haaland", "Mbappe"),
                   date=dt.datetime(2026, 10, 2, 18, 0, tzinfo=dt.timezone.utc))
        _send(msg)
        assert "принята" in _reply(msg)


class TestApproval:
    def _file(self):
        _setup()
        _send(_msg(TEMPLATE.format(club="Сити")))
        return repo.list_transfers(repo.get_active_window()["id"], kinds=("free_agent",))[0]

    def test_manager_approves_and_directory_is_updated(self):
        t = self._file()
        decision = approval.approve(MANAGER, t["id"])
        assert decision.transfer["status"] == "approved"
        row = repo.get_player("Erling Haaland")
        assert row is not None and row["last_club"] == "Манчестер Сити" and row["price_k"] == 45000

    def test_approved_claim_dms_coach_and_announces(self):
        t = self._file()
        approval.approve(MANAGER, t["id"])
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock())
        assert asyncio.run(notify.notify_approved(bot, repo.get_transfer(t["id"]))) is True
        sent = {c.kwargs["chat_id"]: c.kwargs for c in bot.send_message.await_args_list}
        assert "Свободный агент записан" in sent[101]["text"]
        assert sent[GROUP]["message_thread_id"] == FEED_THREAD

    def test_rejected_claim_text_names_the_route(self):
        t = self._file()
        lines = notify._decision_lines(t)
        assert any("Borussia Dortmund" in line and "Манчестер Сити" in line for line in lines)


class TestTopicPanel:
    def test_fa_topic_is_offered_by_the_panel(self):
        text, kb = handlers._topics_view()
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "tw:topic:fa" in data and "Свободные агенты" in text
        assert all(len(row) <= 2 for row in kb.inline_keyboard)

    def test_bind_accepts_fa_type(self):
        repo.bind_topic("fa", GROUP, FA_THREAD, 1)
        assert repo.get_topic("fa")["message_thread_id"] == FA_THREAD
