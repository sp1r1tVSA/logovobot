"""
Карточка игрока тура в итогах тура (handlers.admin).

Проверяем три вещи: карточка строится на сезонных цифрах (а не на одном туре),
дивизион доезжает до подвала карточки, и падение карточки не отменяет
уже опубликованный дайджест.

Кодек здесь не дёргаем: анимацию мокаем, а реальный рендер проверяем
на кадрах — они чистый Pillow и не зависят от ffmpeg/OpenCV.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from handlers import admin


def _payload(**over):
    payload = {
        "kind": "digest",
        "division_id": 3,
        "division_name": "Дивизион 3",
        "round_number": 7,
        "results": [{"match_id": 1, "team1": "Порту", "team2": "Аякс", "score1": 2, "score2": 1}],
        "matches_total": 1,
        "matches_played": 1,
        "goals_total": 3,
        "rout": None,
        "player_of_the_round": {
            "player_name": "Диас", "team_name": "Порту", "goals": 2, "assists": 1,
        },
        "table": [], "movers": [], "leader": None,
    }
    payload.update(over)
    return payload


_SEASON_STATS = {
    "player_name": "Диас",
    "team_name": "Порту",
    "position": "ST",
    "total_goals": 14,
    "total_assists": 6,
    "total_points": 20,
    "items": [],
}


class TestBuildPotrCardData(unittest.TestCase):
    def test_no_player_of_the_round_means_no_card(self):
        for potr in (None, {}, {"player_name": "", "team_name": "Порту"}, {"player_name": "Диас"}):
            self.assertIsNone(admin._build_potr_card_data(_payload(player_of_the_round=potr)), potr)

    def test_card_uses_season_totals_not_the_round(self):
        with patch.object(admin.database, "get_player_card_stats", return_value=dict(_SEASON_STATS)) as stats:
            data = admin._build_potr_card_data(_payload())
        stats.assert_called_once_with("Диас", "Порту")
        self.assertEqual(data["total_goals"], 14)
        self.assertEqual(data["total_assists"], 6)

    def test_division_is_attached_for_the_card_footer(self):
        with patch.object(admin.database, "get_player_card_stats", return_value=dict(_SEASON_STATS)):
            data = admin._build_potr_card_data(_payload())
        self.assertEqual(data["division_id"], 3)
        self.assertEqual(data["division_name"], "Дивизион 3")

    def test_falls_back_to_round_numbers_when_season_stats_are_empty(self):
        with patch.object(admin.database, "get_player_card_stats", return_value={}):
            data = admin._build_potr_card_data(_payload())
        self.assertEqual((data["player_name"], data["team_name"]), ("Диас", "Порту"))
        self.assertEqual((data["total_goals"], data["total_assists"]), (2, 1))

    def test_database_failure_does_not_propagate(self):
        with patch.object(admin.database, "get_player_card_stats", side_effect=RuntimeError("db down")):
            data = admin._build_potr_card_data(_payload())
        self.assertEqual((data["total_goals"], data["total_assists"]), (2, 1))


class TestPostPlayerOfTheRoundCard(unittest.IsolatedAsyncioTestCase):
    def _context(self):
        context = MagicMock()
        context.bot = MagicMock()
        return context

    async def _run(self, payload, **patches):
        import services.animation_sender as animation_sender
        import services.graphics.fc_card_generator as fc

        sender = AsyncMock(return_value=MagicMock(message_id=555))
        renderer = MagicMock(return_value=MagicMock())
        renderer.side_effect = patches.get("render_side_effect")
        context = self._context()

        with patch.object(admin.database, "get_player_card_stats", return_value=dict(_SEASON_STATS)), \
             patch.object(fc, "generate_animated_ea_fc_card", renderer), \
             patch.object(animation_sender, "send_high_quality_animation", sender):
            sent = await admin._post_player_of_the_round_card(context, -100123, 42, payload)
        return sent, sender, renderer

    async def test_card_goes_to_the_same_topic(self):
        sent, sender, _ = await self._run(_payload())
        self.assertTrue(sent)
        args, kwargs = sender.call_args
        self.assertEqual(args[1], -100123)
        self.assertEqual(kwargs["message_thread_id"], 42)
        self.assertEqual(kwargs["parse_mode"], "HTML")

    async def test_caption_carries_both_the_round_and_the_season(self):
        _, sender, _ = await self._run(_payload())
        caption = sender.call_args.kwargs["caption"]
        self.assertIn("Диас", caption)
        self.assertIn("Порту", caption)
        self.assertIn("7", caption)      # номер тура
        self.assertIn("2+1", caption)    # результат тура
        self.assertIn("14+6", caption)   # сезон

    async def test_style_is_picked_from_the_season_rating(self):
        from services.graphics.fc_card_generator import (
            calculate_fut_attributes, get_kpl_tier_by_ovr,
        )

        _, _, renderer = await self._run(_payload())
        card_data, tier = renderer.call_args.args
        self.assertEqual(tier, get_kpl_tier_by_ovr(calculate_fut_attributes(card_data)["ovr"]))

    async def test_no_player_means_nothing_is_sent(self):
        sent, sender, _ = await self._run(_payload(player_of_the_round=None))
        self.assertFalse(sent)
        sender.assert_not_awaited()


class TestDigestSurvivesCardFailure(unittest.IsolatedAsyncioTestCase):
    async def _post_digest(self, card_side_effect, payload=None, force=False):
        from services import round_preview
        import services.graphics.round_digest_generator as digest_gen

        context = MagicMock()
        context.bot.send_photo = AsyncMock(return_value=MagicMock(message_id=777))
        recorded = MagicMock()

        with patch.object(admin.database, "has_round_content_post", return_value=False), \
             patch.object(admin.database, "record_round_content_post", recorded), \
             patch.object(admin, "_resolve_analytics_topic", AsyncMock(return_value=(-100123, 42))), \
             patch.object(round_preview, "build_digest_payload", return_value=payload or _payload()), \
             patch.object(round_preview, "generate_digest_caption", return_value="итоги"), \
             patch.object(digest_gen, "generate_round_digest_image", return_value=MagicMock()), \
             patch.object(admin, "_post_player_of_the_round_card",
                          AsyncMock(side_effect=card_side_effect)) as card:
            posted = await admin.post_round_digest(context, division_id=3, round_number=7, force=force)
        return posted, recorded, card, context

    async def test_digest_is_still_posted_when_the_card_blows_up(self):
        posted, recorded, card, context = await self._post_digest(RuntimeError("ffmpeg missing"))
        self.assertTrue(posted)
        context.bot.send_photo.assert_awaited_once()
        recorded.assert_called_once()
        card.assert_awaited_once()

    async def test_card_is_offered_the_digest_payload(self):
        _, _, card, _ = await self._post_digest(None)
        args = card.call_args.args
        self.assertEqual(args[1:3], (-100123, 42))
        self.assertEqual(args[3]["round_number"], 7)

    async def test_unfinished_round_is_not_posted(self):
        # Тур закрыт, но один матч ещё долг — ни итогов, ни игрока тура.
        payload = _payload(matches_total=2, matches_played=1)
        posted, recorded, card, context = await self._post_digest(None, payload=payload)
        self.assertFalse(posted)
        context.bot.send_photo.assert_not_awaited()
        recorded.assert_not_called()
        card.assert_not_awaited()

    async def test_manual_force_posts_an_unfinished_round(self):
        payload = _payload(matches_total=2, matches_played=1)
        posted, _, _, context = await self._post_digest(None, payload=payload, force=True)
        self.assertTrue(posted)
        context.bot.send_photo.assert_awaited_once()


class TestDivisionReachesTheRenderedCard(unittest.TestCase):
    """Реальный рендер кадров — без кодека, только Pillow."""

    def test_frames_render_for_the_player_of_the_round(self):
        from services.graphics.fc_card_generator import render_animated_card_frames

        with patch.object(admin.database, "get_player_card_stats", return_value=dict(_SEASON_STATS)):
            card_data = admin._build_potr_card_data(_payload())

        with patch("services.graphics.division_theme.database.get_division",
                   return_value={"id": 3, "code": "DIV_3"}):
            frames, fps, width, height = render_animated_card_frames(card_data, anim_style="kpl_star")

        self.assertGreater(len(frames), 1)
        self.assertGreater(fps, 0)
        self.assertEqual(frames[0].size, (width, height))
