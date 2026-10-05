"""Итоги окна: подсчёт по одобренным заявкам, картинка и публикация в ленту один раз."""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

import config
import database
from services.graphics import transfer_recap_generator as gen
from transfers import approval, handlers, notify, recap, repo, requests as req_mod, service

MANAGER = 777
GROUP, FEED_THREAD = -100500, 42


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in (
            "transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
            "transfer_core_snapshot", "transfer_players", "transfer_topics",
            "transfer_sanctions", "transfer_reminders", "squad_players", "users",
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


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_photo = AsyncMock(return_value=MagicMock())
    return bot


def _window():
    wid = repo.create_window(1, 10, title="ТО Зима")
    repo.open_window(wid, 1)
    for uid, name, team in ((101, "chelsea", "Челси"), (102, "arsenal", "Арсенал"), (103, "pool", "Ливерпуль")):
        _user(uid, name, team)
        repo.set_club_budget(wid, team, 100000, 1)
    _squad("Арсенал", "B. Saka", "P1", "P2", "P3", "P4", "P5", "P6")
    _squad("Челси", "C1", "C2", "C3", "C4", "C5", "C6")
    _squad("Ливерпуль", "L1", "L2", "L3", "L4", "L5", "L6")
    return wid


def _full_window():
    """Сделка Сака → Челси (15), обмен C1 (10) ↔ L1 (12), продажа P1 в урну."""
    wid = _window()
    deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
    approval.approve(MANAGER, req_mod.confirm(102, deal["id"])["id"])
    leg = req_mod.create_swap(101, other_club="Ливерпуль", give_player="C1", give_price="10", give_ovr=100,
                              get_player="L1", get_price="12", get_ovr=101)
    approval.approve(MANAGER, req_mod.confirm(103, leg["id"])["id"])
    sale = req_mod.create_urn_sale(102, player="P1", tm_price="10", special_price="2", sellable=True)
    approval.approve(MANAGER, sale["id"])
    return wid


class TestBuild:
    def test_empty_window(self):
        data = recap.build(_window())
        assert data.empty and data.top_deals == [] and data.top_club is None

    def test_counts(self):
        data = recap.build(_full_window())
        assert data.requests_count == 3          # обмен — одно событие
        assert data.swaps == 1 and data.urn_sales == 1
        assert data.turnover_k == 15000 + 10000 + 12000   # выплата из урны не в обороте

    def test_top_deals_by_price(self):
        data = recap.build(_full_window())
        assert [t["player_name"] for t in data.top_deals] == ["B. Saka", "L1", "C1"]

    def test_most_active_club_tie_is_alphabetical(self):
        data = recap.build(_full_window())
        assert (data.top_club, data.top_club_count) == ("Арсенал", 2)

    def test_pending_requests_are_ignored(self):
        wid = _window()
        req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
        assert recap.build(wid).empty

    def test_caption(self):
        data = recap.build(_full_window())
        text = recap.caption(data, "«ТО Зима»")
        assert "Итоги окна «ТО Зима»" in text and "37 млн" in text and "1. B. Saka" in text
        assert len(text) <= notify.CAPTION_LIMIT


class TestRenderer:
    def test_png_size(self):
        png = gen.render_window_recap(title="ТО", turnover_text="37 млн",
                                      deals=[gen.RecapDeal("Rodri", "Челси → Арсенал", "20 млн")] * 7,
                                      requests_count=3, swaps=1, urn_sales=1, top_club="Челси", top_club_count=2)
        img = Image.open(io.BytesIO(png))
        assert img.format == "PNG" and img.size == (gen.WIDTH, gen.HEIGHT)

    def test_empty_and_long_values(self):
        assert gen.render_window_recap(title="", turnover_text="0 млн", deals=[])
        assert gen.render_window_recap(title="Я" * 300, turnover_text="9" * 40 + " млн",
                                       deals=[gen.RecapDeal("Я" * 200, "А" * 200, "9" * 40)])

    def test_build_image_from_recap(self):
        assert recap.build_image(recap.build(_full_window()))


class TestAnnounce:
    def test_posts_once_to_feed(self):
        wid = _full_window()
        repo.bind_topic("feed", GROUP, FEED_THREAD, 1)
        bot = _bot()
        window = repo.get_window(wid)
        assert asyncio.run(notify.announce_recap(bot, window)) is True
        kw = bot.send_photo.await_args.kwargs
        assert kw["chat_id"] == GROUP and kw["message_thread_id"] == FEED_THREAD
        assert asyncio.run(notify.announce_recap(bot, window)) is False
        assert bot.send_photo.await_count == 1

    def test_force_reposts(self):
        wid = _full_window()
        repo.bind_topic("feed", GROUP, FEED_THREAD, 1)
        bot = _bot()
        window = repo.get_window(wid)
        asyncio.run(notify.announce_recap(bot, window))
        assert asyncio.run(notify.announce_recap(bot, window, force=True)) is True
        assert bot.send_photo.await_count == 2

    def test_empty_window_posts_nothing(self):
        wid = _window()
        repo.bind_topic("feed", GROUP, FEED_THREAD, 1)
        bot = _bot()
        assert asyncio.run(notify.announce_recap(bot, repo.get_window(wid))) is False
        bot.send_photo.assert_not_awaited()
        assert repo.claim_reminder(wid, notify.RECAP_TAG)      # пустое окно метку не тратит

    def test_no_topic_falls_back_to_manager_dm(self):
        wid = _full_window()
        bot = _bot()
        assert asyncio.run(notify.announce_recap(bot, repo.get_window(wid))) is False
        bot.send_photo.assert_not_awaited()
        kw = bot.send_message.await_args.kwargs
        assert kw["chat_id"] == MANAGER and "Итоги окна" in kw["text"]

    def test_photo_failure_falls_back_to_text(self):
        wid = _full_window()
        repo.bind_topic("feed", GROUP, FEED_THREAD, 1)
        bot = _bot()
        bot.send_photo.side_effect = RuntimeError("boom")
        assert asyncio.run(notify.announce_recap(bot, repo.get_window(wid))) is True
        assert "Итоги окна" in bot.send_message.await_args.kwargs["text"]


class TestHub:
    @staticmethod
    def _buttons(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_hidden_without_approved(self):
        _window()
        assert "tw:recap" not in self._buttons(handlers._hub_view()[1])

    def test_shown_with_approved(self):
        _full_window()
        assert "tw:recap" in self._buttons(handlers._hub_view()[1])

    def test_shown_after_close(self):
        wid = _full_window()
        service.close_window(wid, 1)
        assert "tw:recap" in self._buttons(handlers._hub_view()[1])

    def test_publish_button_manager_only(self):
        assert "tw:recapp" in self._buttons(handlers._recap_keyboard(True))
        assert "tw:recapp" not in self._buttons(handlers._recap_keyboard(False))
