"""
Подтверждение черновика не должно расходиться с базой.

Баг: результаты уходили в РЕЗУЛЬТАТЫ, но в базе не сохранялись.
Три причины, каждая проверяется отдельно:
  A. confirm_and_finalize_match молча коммитил UPDATE по несуществующему id;
  B. все игры серии резолвились в один и тот же match_id;
  C. пост в топик отправлялся даже после провалившегося сохранения.
"""

import itertools
import os
import sys
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import database
import handlers.drafts as drafts_mod
from handlers.drafts import cb_draft_confirm

_ID_SEQ = itertools.count(772300)


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_photo = AsyncMock()
    return context


def _make_update(admin_id: int, draft_uuid: str):
    query = MagicMock()
    query.data = f"draft_conf_{draft_uuid}"
    query.from_user = MagicMock(id=admin_id, username="chief", first_name="Chief")
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message = MagicMock()
    query.message.photo = None
    query.message.text = "📝 ЧЕРНОВИК\n⏳ <i>Ожидает подтверждения администратором...</i>"
    query.message.reply_markup = MagicMock()
    query.message.reply_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_user = query.from_user
    return update, query


class DraftTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.admin_id = next(_ID_SEQ)
        self._orig_admins = list(config.ADMIN_IDS)
        config.ADMIN_IDS = [self.admin_id]
        self.addCleanup(setattr, config, "ADMIN_IDS", self._orig_admins)

        self.team1 = f"Клуб A{self.uid}"
        self.team2 = f"Клуб B{self.uid}"

    def _make_match(self, round_number: int, status: str = "pending") -> int:
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO matches (player1_team, player2_team, round_number, status, tournament_type) "
                "VALUES (?, ?, ?, ?, 'league')",
                (self.team1, self.team2, round_number, status)
            )
            return cur.lastrowid

    def _game(self, match_id, h_score=2, a_score=1, game_num=1) -> dict:
        return {
            "match_id": match_id,
            "round_number": 1,
            "division_id": None,
            "game_num": game_num,
            "home_team": self.team1,
            "away_team": self.team2,
            "h_score": h_score,
            "a_score": a_score,
            "p1_username": None,
            "p2_username": None,
            "h_goals": {},
            "a_goals": {},
            "h_assists": {},
            "a_assists": {},
            "is_single_timeline": False,
            "events": [],
            "reporter_id": self.admin_id,
            "photo_id": None,
        }


class TestConfirmAndFinalizeGuards(unittest.TestCase):
    """Defect A: UPDATE по несуществующему id не должен считаться успехом."""

    def setUp(self):
        database.init_db()

    def test_missing_match_raises(self):
        with self.assertRaises(ValueError):
            database.confirm_and_finalize_match(99_123_456, 3, 1, [])

    def test_missing_match_leaves_no_events(self):
        ghost_id = 99_123_457
        with self.assertRaises(ValueError):
            database.confirm_and_finalize_match(ghost_id, 1, 0, [("A", "Игрок", "goal", 1)])
        self.assertEqual(database.get_match_events(ghost_id), [])

    def test_existing_match_still_saves(self):
        with database.transaction() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO matches (player1_team, player2_team, round_number, status, tournament_type) "
                "VALUES ('Гуард 1', 'Гуард 2', 1, 'pending', 'league')"
            )
            match_id = cur.lastrowid

        database.confirm_and_finalize_match(match_id, 3, 2, [("Гуард 1", "Игрок", "goal", 3)])
        row = database.get_match(match_id)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual((row["player1_score"], row["player2_score"]), (3, 2))

    def test_technical_result_on_missing_match_raises(self):
        with self.assertRaises(ValueError):
            database.set_technical_result(99_123_458, 3, 0)


class TestMatchResolutionExcludesUsedIds(unittest.TestCase):
    """Defect B: игры серии должны ложиться на разные матчи."""

    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.team1 = f"Резолв A{self.uid}"
        self.team2 = f"Резолв B{self.uid}"
        season_id = int(database.get_active_season() or 1)
        self.ids = []
        with database.transaction() as conn:
            cur = conn.cursor()
            for rnd in (1, 2):
                cur.execute(
                    "INSERT INTO matches (player1_team, player2_team, round_number, status, tournament_type, season_id) "
                    "VALUES (?, ?, ?, 'pending', 'league', ?)",
                    (self.team1, self.team2, rnd, season_id)
                )
                self.ids.append(cur.lastrowid)

    def test_repeated_calls_without_exclusion_return_the_same_match(self):
        a = database.get_active_match_by_teams(self.team1, self.team2)
        b = database.get_active_match_by_teams(self.team1, self.team2)
        self.assertEqual(a["id"], b["id"], "Скоринг детерминированный — это и есть причина бага")

    def test_exclusion_moves_to_the_next_match(self):
        first = database.get_active_match_by_teams(self.team1, self.team2)
        second = database.get_active_match_by_teams(
            self.team1, self.team2, exclude_ids={first["id"]}
        )
        self.assertIsNotNone(second)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({first["id"], second["id"]}, set(self.ids))

    def test_none_when_everything_is_taken(self):
        self.assertIsNone(
            database.get_active_match_by_teams(self.team1, self.team2, exclude_ids=set(self.ids))
        )


class TestDraftConfirmDoesNotPostUnsavedResults(DraftTestBase):
    """Defect C: пост в топик только после успешной записи."""

    async def asyncSetUp(self):
        self.group_id = -1009911001
        database.set_config("group_id", str(self.group_id))
        database.set_config("results_topic_id", "77")

    async def _run(self, games):
        draft_uuid = uuid.uuid4().hex[:8]
        update, query = _make_update(self.admin_id, draft_uuid)
        context = _make_context()
        context.bot_data["drafts"] = {
            draft_uuid: {"is_multi": len(games) > 1, "games": games}
        }
        with patch.object(drafts_mod, "refresh_debts_summary", new=AsyncMock(), create=True), \
             patch.object(drafts_mod, "refresh_league_table", new=AsyncMock(), create=True), \
             patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_league_table", new=AsyncMock()), \
             patch("handlers.cabinet.handle_debt_played_rewards", new=AsyncMock()), \
             patch("handlers.cabinet.build_debt_footer", new=AsyncMock(return_value="")):
            await cb_draft_confirm(update, context)
        return draft_uuid, query, context

    async def test_saved_match_is_posted(self):
        match_id = self._make_match(1)
        draft_uuid, query, context = await self._run([self._game(match_id)])

        row = database.get_match(match_id)
        self.assertEqual(row["status"], "confirmed")
        context.bot.send_message.assert_awaited()
        self.assertNotIn(draft_uuid, context.bot_data["drafts"])
        self.assertIn("Одобрено", query.edit_message_text.await_args.kwargs["text"])

    async def test_ghost_match_is_not_posted(self):
        draft_uuid, query, context = await self._run([self._game(99_555_001)])

        context.bot.send_message.assert_not_awaited()
        context.bot.send_photo.assert_not_awaited()
        text = query.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Сохранено игр: 0 из 1", text)
        self.assertNotIn("Одобрено", text)

    async def test_failed_game_stays_in_the_draft_for_a_retry(self):
        draft_uuid, _, context = await self._run([self._game(99_555_002)])

        self.assertIn(draft_uuid, context.bot_data["drafts"], "Черновик нельзя терять при ошибке")
        self.assertEqual(len(context.bot_data["drafts"][draft_uuid]["games"]), 1)

    async def test_partial_failure_posts_only_the_saved_game(self):
        good_id = self._make_match(1)
        draft_uuid, query, context = await self._run([
            self._game(good_id, game_num=1),
            self._game(99_555_003, game_num=2),
        ])

        self.assertEqual(database.get_match(good_id)["status"], "confirmed")
        self.assertEqual(context.bot.send_message.await_count, 1)

        text = query.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Сохранено игр: 1 из 2", text)

        # На повтор остаётся только несохранённая игра — дубля в топике не будет.
        remaining = context.bot_data["drafts"][draft_uuid]["games"]
        self.assertEqual([g["match_id"] for g in remaining], [99_555_003])


if __name__ == "__main__":
    unittest.main()
