import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import database
import handlers.cabinet as cabinet
import handlers.drafts as drafts
from services.external_squad_lookup import find_new_goal_action_players, lookup_external_club_player


class TestNewPlayerRecognition(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.init_db()
        self.uid = uuid.uuid4().hex[:6].upper()
        self.home_team = f"Real Madrid {self.uid}"
        self.away_team = f"Barcelona {self.uid}"

    def test_find_new_goal_action_players_skips_existing_players(self):
        # Add existing players
        database.add_squad(self.home_team, [{"player_name": "Vinicius Junior", "position": "LW"}])
        database.add_squad(self.away_team, [{"player_name": "Robert Lewandowski", "position": "ST"}])

        # Goal scored by existing player
        new_players = find_new_goal_action_players(
            h_goals={"Vinicius Junior": 1},
            a_goals={"Robert Lewandowski": 1},
            h_assists={},
            a_assists={},
            home_team=self.home_team,
            away_team=self.away_team,
        )
        self.assertEqual(new_players, [])

    def test_find_new_goal_action_players_detects_new_scorers_and_assisters(self):
        database.add_squad(self.home_team, ["Existing Keeper"])

        # New scorer for home and new assister for away
        new_players = find_new_goal_action_players(
            h_goals={"New Star": 2},
            a_goals={},
            h_assists={},
            a_assists={"New Playmaker": 1},
            home_team=self.home_team,
            away_team=self.away_team,
        )
        self.assertEqual(len(new_players), 2)
        names = {p["player_name"] for p in new_players}
        self.assertIn("New Star", names)
        self.assertIn("New Playmaker", names)

    @patch("services.external_squad_lookup._lookup_fotmob")
    def test_lookup_external_club_player_uses_fotmob(self, mock_fotmob):
        mock_fotmob.return_value = {
            "raw_name": "Mbappe",
            "player_name": "Kylian Mbappé",
            "position": "ST",
            "team_name": self.home_team,
            "source": "FotMob",
            "external_team": "Real Madrid",
        }
        res = lookup_external_club_player("Mbappe", self.home_team)
        self.assertIsNotNone(res)
        self.assertEqual(res["player_name"], "Kylian Mbappé")
        self.assertEqual(res["position"], "ST")

    async def test_cabinet_cb_add_new_player(self):
        update = MagicMock()
        query = MagicMock()
        match_id = 99123
        query.data = f"cb_add_new_player_{match_id}_0"
        query.from_user = MagicMock(id=12345)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.user_data = {
            "pending_new_players": [
                {
                    "raw_name": "Endrick",
                    "player_name": "Endrick",
                    "position": "CF",
                    "team_name": self.home_team,
                    "source": "FotMob",
                    "goals": 1,
                    "assists": 0,
                }
            ],
            "home_goals_count": {"Endrick": 1},
        }

        # Player not in squad initially
        self.assertNotIn("Endrick", database.get_squad(self.home_team))

        await cabinet.cb_add_new_player(update, context)

        # Player must be added to squad in DB
        squad = database.get_squad(self.home_team)
        self.assertIn("Endrick", squad)
        query.edit_message_text.assert_called_once()
        self.assertIn("успешно внесён в состав", query.edit_message_text.call_args[1]["text"])

    async def test_cabinet_cb_skip_new_player(self):
        update = MagicMock()
        query = MagicMock()
        match_id = 99124
        query.data = f"cb_skip_new_player_{match_id}_0"
        query.from_user = MagicMock(id=12345)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.user_data = {
            "pending_new_players": [
                {
                    "raw_name": "Skipped Guy",
                    "player_name": "Skipped Guy",
                    "position": "ST",
                    "team_name": self.home_team,
                }
            ],
        }

        await cabinet.cb_skip_new_player(update, context)

        # Player must NOT be added to squad
        self.assertNotIn("Skipped Guy", database.get_squad(self.home_team))
        query.edit_message_text.assert_called_once()
        self.assertIn("пропущено", query.edit_message_text.call_args[1]["text"])

    async def test_draft_cb_draft_add_player(self):
        draft_uuid = "testd1"
        draft_data = {
            "is_multi": False,
            "reporter_id": 12345,
            "pending_new_players": [
                {
                    "raw_name": "RawGuy",
                    "player_name": "Canonical Guy",
                    "position": "CAM",
                    "team_name": self.home_team,
                }
            ],
            "games": [
                {
                    "home_team": self.home_team,
                    "away_team": self.away_team,
                    "events": [(self.home_team, "RawGuy", "goal", 1)],
                    "h_goals": {"RawGuy": 1},
                    "player1_id": 12345,
                }
            ],
        }

        update = MagicMock()
        query = MagicMock()
        query.data = f"draft_add_player_{draft_uuid}_0"
        query.from_user = MagicMock(id=12345, username="reporter")
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.bot_data = {"drafts": {draft_uuid: draft_data}}

        self.assertNotIn("Canonical Guy", database.get_squad(self.home_team))

        await drafts.cb_draft_add_player(update, context)

        # Player added to squad
        self.assertIn("Canonical Guy", database.get_squad(self.home_team))
        # Events in draft updated to canonical name
        self.assertEqual(draft_data["games"][0]["events"][0][1], "Canonical Guy")
        self.assertIn("Canonical Guy", draft_data["games"][0]["h_goals"])
        query.edit_message_text.assert_called_once()
        self.assertIn("добавлен в состав команды", query.edit_message_text.call_args[1]["text"])

    async def test_draft_cb_draft_skip_player(self):
        draft_uuid = "testd2"
        draft_data = {
            "pending_new_players": [
                {
                    "raw_name": "Ghost",
                    "player_name": "Ghost Player",
                    "position": "CB",
                    "team_name": self.home_team,
                }
            ],
        }

        update = MagicMock()
        query = MagicMock()
        query.data = f"draft_skip_player_{draft_uuid}_0"
        query.from_user = MagicMock(id=12345)
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        context = MagicMock()
        context.bot_data = {"drafts": {draft_uuid: draft_data}}

        await drafts.cb_draft_skip_player(update, context)

        self.assertNotIn("Ghost Player", database.get_squad(self.home_team))
        query.edit_message_text.assert_called_once()
        self.assertIn("пропущено", query.edit_message_text.call_args[1]["text"])


if __name__ == "__main__":
    unittest.main()
