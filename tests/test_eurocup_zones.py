"""
tests/test_eurocup_zones.py

Tests for European Cup qualification zones (Лига Чемпионов & Лига Европы)
across 5 divisions for Season 1:
- Config mappings (UCL places, UEL places, tour 15 start)
- Zone resolvers by division id, code, and position
- Graphic table generator rendering with Eurocup zones
- Tournament API endpoint returning enriched standings and eurocup_rules
"""

import io
import json
import time
import urllib.parse
import hmac
import hashlib
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

import config
import database
from aiohttp.test_utils import AioHTTPTestCase
from api.server import create_app
from services.graphics.table_generator import generate_league_table_image


class TestEurocupZonesConfig(unittest.TestCase):
    def test_eurocup_slots_mapping(self):
        """Проверка распределения мест по 5 дивизионам согласно регламенту."""
        expected = {
            "DIV_1": {"ucl_places": 10, "uel_places": 2},
            "DIV_2": {"ucl_places": 8, "uel_places": 3},
            "DIV_3": {"ucl_places": 7, "uel_places": 3},
            "DIV_4": {"ucl_places": 6, "uel_places": 4},
            "DIV_5": {"ucl_places": 5, "uel_places": 4},
        }
        for code, slots in expected.items():
            self.assertEqual(config.EUROCUP_ZONES[code], slots)

        self.assertEqual(config.EUROCUP_START_ROUND, 15)

    def test_get_eurocup_slots_resolution(self):
        """Резолвер распознает int, code, имя дивизиона и fallback."""
        self.assertEqual(config.get_eurocup_slots(1), {"ucl_places": 10, "uel_places": 2})
        self.assertEqual(config.get_eurocup_slots("1"), {"ucl_places": 10, "uel_places": 2})
        self.assertEqual(config.get_eurocup_slots("DIV_2"), {"ucl_places": 8, "uel_places": 3})
        self.assertEqual(config.get_eurocup_slots("Дивизион 3"), {"ucl_places": 7, "uel_places": 3})
        self.assertEqual(config.get_eurocup_slots("Дивизион 4"), {"ucl_places": 6, "uel_places": 4})
        self.assertEqual(config.get_eurocup_slots(5), {"ucl_places": 5, "uel_places": 4})
        self.assertEqual(config.get_eurocup_slots(None), {"ucl_places": 0, "uel_places": 0})
        self.assertEqual(config.get_eurocup_slots("UNKNOWN"), {"ucl_places": 0, "uel_places": 0})

    def test_get_eurocup_zone_positions(self):
        """Тест точного попадания мест в зоны ЛЧ, ЛЕ или вне еврокубков."""
        # Div 1: 1..10 UCL, 11..12 UEL, 13..16 None
        self.assertEqual(config.get_eurocup_zone(1, 1), "ucl")
        self.assertEqual(config.get_eurocup_zone(1, 10), "ucl")
        self.assertEqual(config.get_eurocup_zone(1, 11), "uel")
        self.assertEqual(config.get_eurocup_zone(1, 12), "uel")
        self.assertEqual(config.get_eurocup_zone(1, 13), None)

        # Div 2: 1..8 UCL, 9..11 UEL, 12..16 None
        self.assertEqual(config.get_eurocup_zone(2, 8), "ucl")
        self.assertEqual(config.get_eurocup_zone(2, 9), "uel")
        self.assertEqual(config.get_eurocup_zone(2, 11), "uel")
        self.assertEqual(config.get_eurocup_zone(2, 12), None)

        # Div 3: 1..7 UCL, 8..10 UEL, 11..16 None
        self.assertEqual(config.get_eurocup_zone(3, 7), "ucl")
        self.assertEqual(config.get_eurocup_zone(3, 8), "uel")
        self.assertEqual(config.get_eurocup_zone(3, 10), "uel")
        self.assertEqual(config.get_eurocup_zone(3, 11), None)

        # Div 4: 1..6 UCL, 7..10 UEL, 11..16 None
        self.assertEqual(config.get_eurocup_zone(4, 6), "ucl")
        self.assertEqual(config.get_eurocup_zone(4, 7), "uel")
        self.assertEqual(config.get_eurocup_zone(4, 10), "uel")
        self.assertEqual(config.get_eurocup_zone(4, 11), None)

        # Div 5: 1..5 UCL, 6..9 UEL, 10..16 None
        self.assertEqual(config.get_eurocup_zone(5, 5), "ucl")
        self.assertEqual(config.get_eurocup_zone(5, 6), "uel")
        self.assertEqual(config.get_eurocup_zone(5, 9), "uel")
        self.assertEqual(config.get_eurocup_zone(5, 10), None)


class TestEurocupGraphics(unittest.TestCase):
    def test_table_generator_renders_eurocups(self):
        """Рендерер таблицы успешно генерирует изображение со строками и легендой еврокубков."""
        sample_standings = [
            {"team_name": f"Клуб {i}", "points": 40 - i * 2, "played": 15, "wins": 10, "draws": 2, "losses": 3, "goals_scored": 30, "goals_conceded": 15}
            for i in range(1, 17)
        ]
        # Проверяем для каждого из 5 дивизионов
        for div_id in (1, 2, 3, 4, 5):
            buf = generate_league_table_image(standings=sample_standings, division_id=div_id, division_name=f"Дивизион {div_id}")
            self.assertIsInstance(buf, io.BytesIO)
            self.assertGreater(len(buf.getvalue()), 1000)


class TestEurocupApi(AioHTTPTestCase):
    async def get_application(self):
        database.init_db()
        return create_app()

    def setUp(self):
        super().setUp()
        database.init_db()

    def _generate_mock_init_data(self, user_id=12345678):
        token = config.TOKEN or "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        data = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": user_id, "username": "test_bettor"}),
        }
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        hash_val = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        data["hash"] = hash_val
        return urllib.parse.urlencode(data)

    async def test_get_standings_includes_eurocup_rules_and_zones(self):
        """GET /api/tournaments/1/standings возвращает eurocup_rules и eurocup_zone."""
        headers = {"X-Telegram-Init-Data": self._generate_mock_init_data()}
        resp = await self.client.get("/api/tournaments/1/standings?division_id=1", headers=headers)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("eurocup_rules", data)
        rules = data["eurocup_rules"]
        self.assertEqual(rules["ucl_places"], 10)
        self.assertEqual(rules["uel_places"], 2)
        self.assertEqual(rules["start_round"], 15)

        # Проверка дивизиона 2
        resp2 = await self.client.get("/api/tournaments/1/standings?division_id=2", headers=headers)
        self.assertEqual(resp2.status, 200)
        data2 = await resp2.json()
        self.assertEqual(data2["eurocup_rules"]["ucl_places"], 8)
        self.assertEqual(data2["eurocup_rules"]["uel_places"], 3)


class TestEurocupChat(unittest.IsolatedAsyncioTestCase):
    async def test_chat_context_contains_eurocups(self):
        """Контекст ИИ-чата включает регламент еврокубков и слоты дивизиона."""
        from handlers.chat import handle_ai_chat
        database.init_db()
        test_uid = 98765432
        database.register_user(test_uid, "test_coach", team_name="Лидс")
        database.assign_user_division(test_uid, 1)

        update = MagicMock()
        update.message.text = "Темшик кто идет в еврокубки?"
        update.message.voice = None
        update.message.reply_to_message = None
        update.message.message_thread_id = None
        update.message.reply_text = AsyncMock()
        update.effective_message = update.message
        update.effective_user.id = test_uid
        update.effective_user.username = "test_coach"
        update.effective_chat.id = test_uid
        update.effective_chat.type = "private"
        context = MagicMock()
        context.bot.id = 999
        context.bot.send_chat_action = AsyncMock()

        with patch("handlers.chat.ai_chat.generate_chat_reply") as gen, \
             patch("handlers.chat.database.is_ai_chat_enabled", return_value=True):
            gen.return_value = "Ответ Темшика"
            await handle_ai_chat(update, context)
            self.assertTrue(gen.called)
            ctx_data = gen.call_args[0][3]

            self.assertIn("Лига Чемпионов", ctx_data)
            self.assertIn("Лига Европы", ctx_data)
            self.assertIn("15", ctx_data)


if __name__ == "__main__":
    unittest.main()

