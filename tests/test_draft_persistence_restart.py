import os
import sys
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import database
import handlers.drafts as drafts_mod
from handlers.drafts import cb_draft_confirm, cb_draft_reject


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_photo = AsyncMock()
    return context


def _make_update(admin_id: int, draft_uuid: str, is_reject: bool = False):
    query = MagicMock()
    query.data = f"draft_rej_{draft_uuid}" if is_reject else f"draft_conf_{draft_uuid}"
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


class TestDraftPersistenceRestart(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.admin_id = 99881122
        self._orig_admins = list(config.ADMIN_IDS)
        config.ADMIN_IDS = [self.admin_id]
        self.addCleanup(setattr, config, "ADMIN_IDS", self._orig_admins)

        self.div_id = database.create_division(name=f"DraftDiv {self.uid}", code=f"DD_{self.uid}")
        database.create_round(1, division_id=self.div_id)

        self.user1_id = 8800101
        self.user2_id = 8800102
        database.register_user(self.user1_id, f"p1_{self.uid}", team_name=f"Benfica_{self.uid}")
        database.register_user(self.user2_id, f"p2_{self.uid}", team_name=f"River_{self.uid}")

        self.match_id = database.create_match(1, self.user1_id, self.user2_id, division_id=self.div_id)
        self.group_id = -1001234567
        database.set_config("group_id", str(self.group_id))
        database.set_config("results_topic_id", "123")
        database.bind_division_topic(self.div_id, self.group_id, 123, "results", force=True)

    def _make_game(self, match_id: int):
        return {
            "game_num": 1,
            "match_id": match_id,
            "round_number": 1,
            "home_team": f"Benfica_{self.uid}",
            "away_team": f"River_{self.uid}",
            "h_score": 4,
            "a_score": 1,
            "p1_username": f"p1_{self.uid}",
            "p2_username": f"p2_{self.uid}",
            "p1_str": "",
            "p2_str": "",
            "h_goals": {"Sudakov": 3, "Echeverri": 1},
            "a_goals": {"Otamendi": 1},
            "h_assists": {"Lukebakio": 2, "Kaminski": 1},
            "a_assists": {"Acuña": 1},
            "is_single_timeline": False,
            "events": [
                [f"Benfica_{self.uid}", "Sudakov", "goal", 3],
                [f"Benfica_{self.uid}", "Echeverri", "goal", 1],
                [f"River_{self.uid}", "Otamendi", "goal", 1],
                [f"Benfica_{self.uid}", "Lukebakio", "assist", 2],
                [f"Benfica_{self.uid}", "Kaminski", "assist", 1],
                [f"River_{self.uid}", "Acuña", "assist", 1],
            ],
            "mvp_player": "Sudakov",
            "reporter_id": self.user1_id,
            "photo_id": "test_photo_123",
            "division_id": self.div_id,
        }

    def test_database_crud_methods(self):
        draft_id = f"test_{uuid.uuid4().hex[:6]}"
        sample_data = {"is_multi": False, "match_id": 42, "games": [{"test": 1}]}

        database.save_draft(draft_id, sample_data)
        loaded = database.get_draft(draft_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["match_id"], 42)
        self.assertEqual(loaded["games"][0]["test"], 1)

        # Update draft
        sample_data["match_id"] = 99
        database.save_draft(draft_id, sample_data)
        updated = database.get_draft(draft_id)
        self.assertEqual(updated["match_id"], 99)

        # Delete draft
        database.delete_draft(draft_id)
        self.assertIsNone(database.get_draft(draft_id))

    async def test_confirm_after_bot_restart_loads_from_sqlite(self):
        """Simulates bot reboot: draft was saved in DB, but context.bot_data is completely empty."""
        draft_uuid = f"rst_{uuid.uuid4().hex[:5]}"
        game = self._make_game(self.match_id)
        draft_data = {
            "is_multi": False,
            "games": [game],
            **game,
        }

        # Draft saved to SQLite (as handle_draft_media does)
        database.save_draft(draft_uuid, draft_data)

        # SIMULATE BOT RESTART: bot_data has empty drafts dict!
        context = _make_context()
        context.bot_data = {}  # Empty RAM!

        update, query = _make_update(self.admin_id, draft_uuid)

        with patch.object(drafts_mod, "refresh_debts_summary", new=AsyncMock(), create=True), \
             patch.object(drafts_mod, "refresh_league_table", new=AsyncMock(), create=True), \
             patch("handlers.cabinet.refresh_debts_summary", new=AsyncMock()), \
             patch("handlers.cabinet.refresh_league_table", new=AsyncMock()), \
             patch("handlers.cabinet.handle_debt_played_rewards", new=AsyncMock()), \
             patch("handlers.cabinet.build_debt_footer", new=AsyncMock(return_value="")):
            await cb_draft_confirm(update, context)

        # 1. Match is confirmed in DB
        row = database.get_match(self.match_id)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["player1_score"], 4)
        self.assertEqual(row["player2_score"], 1)

        # 2. Result post sent to results topic (photo or message)
        self.assertTrue(context.bot.send_photo.called or context.bot.send_message.called)

        # 3. Message caption updated with Approval
        text = query.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Одобрено администратором", text)

        # 4. Draft deleted from SQLite
        self.assertIsNone(database.get_draft(draft_uuid))

    async def test_reject_after_bot_restart_loads_from_sqlite(self):
        """Simulates bot reboot before reject: draft is deleted from SQLite upon rejection."""
        draft_uuid = f"rej_{uuid.uuid4().hex[:5]}"
        game = self._make_game(self.match_id)
        draft_data = {
            "is_multi": False,
            "games": [game],
            **game,
        }

        database.save_draft(draft_uuid, draft_data)

        # Empty bot_data
        context = _make_context()
        context.bot_data = {}

        update, query = _make_update(self.admin_id, draft_uuid, is_reject=True)
        await cb_draft_reject(update, context)

        # Message edited to rejected
        text = query.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Черновик отклонен", text)

        # Draft deleted from SQLite
        self.assertIsNone(database.get_draft(draft_uuid))

    async def test_non_existent_draft_still_shows_expiration_message(self):
        """If draft is truly gone from both RAM and SQLite, show expiration message."""
        context = _make_context()
        context.bot_data = {}

        fake_uuid = "nonexistent999"
        update, query = _make_update(self.admin_id, fake_uuid)

        await cb_draft_confirm(update, context)
        text = query.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Данные черновика устарели или не найдены", text)


if __name__ == "__main__":
    unittest.main()
