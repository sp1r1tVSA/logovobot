"""Карточка трансфера в ленте: рендерер, сборка данных и публикация с откатом на текст."""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

import config
import database
from services.graphics import transfer_card_generator as gen
from transfers import card, notify, repo
from transfers import requests as req_mod

MANAGER = 777
GROUP, FEED_THREAD = -100500, 8


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in ("transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
                      "transfer_core_snapshot", "transfer_players", "transfer_topics",
                      "transfer_topics_ext", "transfer_sanctions", "squad_players", "users"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    yield


def _deal(**over):
    t = {"id": 5, "kind": "deal", "player_name": "Rodri", "ovr": 105, "price_k": 20000,
         "from_club": "Челси", "to_club": "Манчестер Сити", "from_user": 102, "to_user": 101}
    t.update(over)
    return t


def _bot(photo_error=None):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_photo = AsyncMock(return_value=MagicMock(), side_effect=photo_error)
    return bot


def _png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


class TestRenderer:
    def test_renders_png_without_any_assets(self):
        img = _png(gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="20 млн",
                                            ovr=105, from_club="Челси", to_club="Манчестер Сити"))
        assert img.format == "PNG" and img.size == (gen.WIDTH, gen.HEIGHT)

    @pytest.mark.parametrize("kind", ["deal", "free_agent", "urn_buy", "urn_sale", "surcharge", None, "unknown"])
    def test_every_kind_renders(self, kind):
        data = gen.render_transfer_card(kind=kind, player_name="Rodri", price_text="1 млн",
                                        from_club="Челси", to_club="Манчестер Сити")
        assert _png(data).size == (gen.WIDTH, gen.HEIGHT)

    def test_missing_clubs_ovr_and_id_are_fine(self):
        assert gen.render_transfer_card(kind="deal", player_name="", price_text="—")

    def test_very_long_name_still_renders(self):
        assert gen.render_transfer_card(kind="deal", player_name="Я" * 200, price_text="999 млн",
                                        from_club="Х" * 120, to_club="Ы" * 120)

    def test_real_portrait_is_used(self, tmp_path):
        photo = tmp_path / "p.png"
        Image.new("RGBA", (300, 400), (200, 30, 30, 255)).save(photo)       # без прозрачности: кадр в круг
        with_photo = _png(gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="1 млн",
                                                   portrait_path=str(photo)))
        without = _png(gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="1 млн"))
        spot = (gen.CIRCLE_CX, gen.CIRCLE_CY + 200)
        assert with_photo.getpixel(spot)[1] < 80 < without.getpixel(spot)[1]     # зелёный: фото красное, круг янтарный

    def test_cutout_portrait_stands_on_the_circle(self, tmp_path):
        photo = tmp_path / "cut.png"
        img = Image.new("RGBA", (300, 400), (0, 0, 0, 0))
        img.paste(Image.new("RGBA", (200, 400), (30, 30, 200, 255)), (50, 0))
        img.save(photo)
        out = _png(gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="1 млн",
                                            portrait_path=str(photo)))
        assert out.getpixel((gen.CIRCLE_CX, gen.CIRCLE_CY))[2] > 150
        assert out.getpixel((gen.WIDTH - 20, gen.HEIGHT - 20)) == gen.PAPER      # вне круга и силуэта — бумага

    def test_no_ovr_means_no_badge(self):
        with_ovr = _png(gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="1 млн", ovr=99))
        without = _png(gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="1 млн"))
        assert with_ovr.getpixel((1280, 100)) == gen.INK and without.getpixel((1280, 100)) == gen.PAPER

    def test_broken_portrait_falls_back_to_monogram(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image")
        assert gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="1 млн",
                                        portrait_path=str(bad))

    def test_missing_logo_files_do_not_raise(self, monkeypatch):
        monkeypatch.setattr(gen, "LOGOS_DIR", "/nonexistent-dir")
        assert gen.render_transfer_card(kind="deal", player_name="Rodri", price_text="1 млн",
                                        from_club="Челси", to_club="Манчестер Сити")


class TestAccent:
    def test_default_without_logos(self, monkeypatch):
        monkeypatch.setattr(gen, "LOGOS_DIR", "/nonexistent-dir")
        assert gen.accent_color("deal", "Челси", "Манчестер Сити") == gen.DEFAULT_ACCENT

    def test_dominant_color_ignores_white_and_black(self):
        logo = Image.new("RGBA", (60, 60), (255, 255, 255, 255))
        logo.paste(Image.new("RGBA", (30, 60), (10, 10, 10, 255)), (0, 0))
        logo.paste(Image.new("RGBA", (20, 20), (0, 160, 60, 255)), (35, 20))
        r, g, b = gen._dominant_color(logo)
        assert g > r and g > b

    def test_pale_color_is_darkened(self):
        assert sum(gen._readable((250, 250, 120))) < sum((250, 250, 120))
        assert gen._readable((20, 100, 60)) == (20, 100, 60)

    def test_uses_logo_of_destination_club(self, tmp_path, monkeypatch):
        name = gen.get_team_logo_filename("Манчестер Сити")
        if not name:
            pytest.skip("no logo mapping")
        Image.new("RGBA", (64, 64), (0, 120, 220, 255)).save(tmp_path / name)
        monkeypatch.setattr(gen, "LOGOS_DIR", str(tmp_path))
        r, g, b = gen.accent_color("deal", "Челси", "Манчестер Сити")
        assert b > r


class TestRoute:
    def test_deal_goes_from_to(self):
        assert gen.route_sides("deal", "А", "Б") == (gen.Side("club", "А"), gen.Side("club", "Б"))

    def test_urn_sale_ends_in_urn(self):
        assert gen.route_sides("urn_sale", "А", None) == (gen.Side("club", "А"), gen.Side("urn"))

    def test_urn_buy_starts_in_urn(self):
        assert gen.route_sides("urn_buy", None, "Б") == (gen.Side("urn"), gen.Side("club", "Б"))

    def test_surcharge_has_one_side(self):
        assert gen.route_sides("surcharge", None, "Б") == (gen.Side("club", "Б"), None)

    def test_missing_club_is_none_side(self):
        assert gen.route_sides("deal", None, "Б")[0] == gen.Side("none")


class TestBuildCard:
    def test_builds_png(self):
        assert _png(card.build_card(_deal())).format == "PNG"

    def test_price_and_clubs_reach_the_renderer(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(gen, "render_transfer_card", lambda **kw: seen.update(kw) or b"x")
        assert card.build_card(_deal()) == b"x"
        assert seen["price_text"] == "20 млн" and seen["ovr"] == 105
        assert seen["from_club"] == "Челси" and seen["transfer_id"] == 5

    def test_portrait_comes_from_cache_path(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(req_mod, "portrait_path", lambda name, *clubs: "/cache/rodri.png")
        monkeypatch.setattr(gen, "render_transfer_card", lambda **kw: seen.update(kw) or b"x")
        card.build_card(_deal())
        assert seen["portrait_path"] == "/cache/rodri.png"

    def test_never_raises(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("pillow exploded")

        monkeypatch.setattr(gen, "render_transfer_card", boom)
        assert card.build_card(_deal()) is None


class TestPortraitPath:
    def test_no_name_no_path(self):
        assert req_mod.portrait_path(None) is None
        assert req_mod.portrait_url(None) is None

    def test_url_is_built_from_the_path(self, monkeypatch, tmp_path):
        photo = tmp_path / "rodri.png"
        photo.write_bytes(b"png")
        monkeypatch.setattr(req_mod.player_photos, "get_cached_photo_path", lambda name, club=None: str(photo))
        assert req_mod.portrait_path("Rodri", "Челси") == str(photo)
        assert req_mod.portrait_url("Rodri", "Челси") == "/assets/players/rodri.png"

    def test_urn_is_not_a_club(self, monkeypatch):
        asked = []
        monkeypatch.setattr(req_mod.player_photos, "get_cached_photo_path",
                            lambda name, club=None: asked.append(club) or "/nope.png")
        assert req_mod.portrait_path("Rodri", req_mod.URN_CLUB) is None
        assert asked == [None]


class TestPosting:
    def _topic(self):
        repo.bind_topic("feed", GROUP, FEED_THREAD, 1)

    def test_photo_goes_to_the_feed_topic(self):
        self._topic()
        bot = _bot()
        assert asyncio.run(notify.post_card_to_topic(bot, "feed", _deal(), "подпись")) is True
        kw = bot.send_photo.await_args.kwargs
        assert kw["chat_id"] == GROUP and kw["message_thread_id"] == FEED_THREAD
        assert kw["caption"] == "подпись" and kw["parse_mode"] == "HTML"
        assert _png(kw["photo"]).format == "PNG"
        bot.send_message.assert_not_awaited()

    def test_send_failure_falls_back_to_text(self):
        self._topic()
        bot = _bot(photo_error=RuntimeError("forbidden"))
        assert asyncio.run(notify.post_card_to_topic(bot, "feed", _deal(), "подпись")) is True
        assert bot.send_message.await_args.kwargs["text"] == "подпись"
        assert bot.send_message.await_args.kwargs["message_thread_id"] == FEED_THREAD

    def test_render_failure_falls_back_to_text(self, monkeypatch):
        self._topic()
        monkeypatch.setattr(card, "build_card", lambda t: None)
        bot = _bot()
        assert asyncio.run(notify.post_card_to_topic(bot, "feed", _deal(), "подпись")) is True
        bot.send_photo.assert_not_awaited()
        assert bot.send_message.await_args.kwargs["text"] == "подпись"

    def test_long_caption_goes_as_text(self):
        self._topic()
        bot = _bot()
        text = "я" * (notify.CAPTION_LIMIT + 1)
        assert asyncio.run(notify.post_card_to_topic(bot, "feed", _deal(), text)) is True
        bot.send_photo.assert_not_awaited()

    def test_no_topic_tells_the_manager(self):
        bot = _bot()
        assert asyncio.run(notify.post_card_to_topic(bot, "feed", _deal(), "подпись")) is False
        bot.send_photo.assert_not_awaited()
        assert bot.send_message.await_args.kwargs["chat_id"] == MANAGER


class TestApprovalFlow:
    def _wire(self):
        repo.bind_topic("feed", GROUP, FEED_THREAD, 1)

    def test_approved_deal_posts_card_with_old_text_as_caption(self):
        self._wire()
        bot = _bot()
        assert asyncio.run(notify.notify_approved(bot, _deal())) is True
        caption = bot.send_photo.await_args.kwargs["caption"]
        assert "Одобрен трансфер #5" in caption and "Rodri" in caption
        # ЛС сторонам остаются текстом
        assert all("photo" not in c.kwargs for c in bot.send_message.await_args_list)

    def test_free_agent_announcement_is_a_card(self):
        self._wire()
        bot = _bot()
        t = _deal(kind="free_agent", from_club="Borussia Dortmund", price_k=45000, to_user=101, from_user=None)
        asyncio.run(notify.announce_free_agent(bot, t))
        assert "Свободный агент записан" in bot.send_photo.await_args.kwargs["caption"]

    def test_bot_without_photo_support_still_gets_text(self):
        self._wire()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock())
        assert asyncio.run(notify.notify_approved(bot, _deal())) is True
        texts = [c.kwargs["text"] for c in bot.send_message.await_args_list
                 if c.kwargs.get("chat_id") == GROUP]
        assert texts and "Одобрен трансфер" in texts[0]

    def test_rejection_stays_text_only(self):
        self._wire()
        bot = _bot()
        asyncio.run(notify.notify_rejected(bot, _deal(status="rejected", decided_reason="нет")))
        bot.send_photo.assert_not_awaited()
