"""
tests/test_club_smm.py

Unit tests for Personal Club SMM center:
- OpenRouter and Gemini free models configuration and rotation
- HTML sanitizing and tag fitting
- Fallback post formatting
- Data extraction payload structure
- Text generation via OpenRouter and Gemini
- Free AI Image generation and Gemini Image fallback
- Permission guards and channel configuration
"""

import base64
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import config
import database
from services import club_smm_service
from handlers import club_smm


class TestClubSmmConfig(unittest.TestCase):
    def test_openrouter_models_parsing(self):
        with patch.dict("os.environ", {"OPENROUTER_SMM_MODELS": "meta-llama/llama-3.3-70b-instruct:free, qwen/qwen-2.5-72b-instruct:free"}):
            models = config._get_openrouter_smm_models()
            self.assertEqual(models, ["meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen-2.5-72b-instruct:free"])

    def test_gemini_smm_models_parsing(self):
        with patch.dict("os.environ", {"GEMINI_SMM_MODELS": "gemini-3.1-flash-lite, gemini-3.5-flash-lite"}):
            models = config._get_gemini_smm_models()
            self.assertEqual(models, ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"])

    def test_gemini_image_models_parsing(self):
        with patch.dict("os.environ", {"GEMINI_IMAGE_MODELS": "gemini-3.1-flash-image, imagen-3.0-generate-002"}):
            models = config._get_gemini_image_models()
            self.assertEqual(models, ["gemini-3.1-flash-image", "imagen-3.0-generate-002"])


class TestClubSmmHtmlSanitize(unittest.TestCase):
    def test_sanitize_keeps_allowed_tags(self):
        raw = "<b>Bold</b> and <i>Italic</i> and <code>code</code> and <a href=\"https://t.me\">Link</a>"
        sanitized = club_smm_service._sanitize_html(raw)
        self.assertIn("<b>Bold</b>", sanitized)
        self.assertIn("<i>Italic</i>", sanitized)
        self.assertIn("<code>code</code>", sanitized)
        self.assertIn("<a href=\"https://t.me\">Link</a>", sanitized)

    def test_sanitize_closes_unclosed_tags(self):
        raw = "<b>Unclosed bold text"
        sanitized = club_smm_service._sanitize_html(raw)
        self.assertTrue(sanitized.endswith("</b>"))

    def test_sanitize_escapes_disallowed_tags(self):
        raw = "<script>alert(1)</script> <div>text</div>"
        sanitized = club_smm_service._sanitize_html(raw)
        self.assertNotIn("<script>", sanitized)
        self.assertIn("&lt;script&gt;", sanitized)
        self.assertIn("&lt;div&gt;", sanitized)

    def test_fit_html_limits_length(self):
        long_text = "<b>" + ("Слово " * 200) + "</b>"
        fitted = club_smm_service._fit_html(long_text, 100)
        self.assertLessEqual(len(fitted), 100)
        self.assertTrue(fitted.endswith("</b>"))


class TestClubSmmFallbacks(unittest.TestCase):
    def setUp(self):
        self.dummy_payload = {
            "club": {
                "name": "Бешикташ",
                "emojis": "🦅⚪⚫",
                "hashtags": ["#Besiktas", "#ЛоговоФифарей"],
            },
            "standings": {
                "rank": 3,
                "points": 18,
                "wins": 5,
                "draws": 3,
                "losses": 1,
                "goal_diff": 8,
            },
            "top_scorers": [{"player_name": "TROSSARD", "goals": 10}],
            "last_match": {
                "opponent": "Байя",
                "my_score": 2,
                "opp_score": 1,
                "result": "win",
                "club_goals": [{"player": "TROSSARD", "count": 2}],
                "mvp_player": "TROSSARD",
            },
            "next_match": {
                "opponent": "Лейпциг",
                "round": 12,
            },
            "division": {"name": "Дивизион 4"},
        }

    def test_recap_fallback(self):
        text = club_smm_service._build_fallback_post(self.dummy_payload, "recap")
        self.assertIn("MATCH RECAP", text)
        self.assertIn("Бешикташ 2 : 1 Байя", text)
        self.assertIn("TROSSARD", text)
        self.assertIn("#Besiktas", text)

    def test_matchday_fallback(self):
        text = club_smm_service._build_fallback_post(self.dummy_payload, "matchday")
        self.assertIn("MATCHDAY", text)
        self.assertIn("<b>Бешикташ</b> — <b>Лейпциг</b>", text)
        self.assertIn("#Matchday", text)

    def test_standings_fallback(self):
        text = club_smm_service._build_fallback_post(self.dummy_payload, "standings")
        self.assertIn("ПОЛОЖЕНИЕ КЛУБА", text)
        self.assertIn("#3", text)
        self.assertIn("18", text)


class TestClubSmmPermissionsAndChannel(unittest.TestCase):
    def test_owner_is_allowed(self):
        self.assertTrue(club_smm.is_smm_allowed(1642770076))

    def test_admin_is_allowed(self):
        with patch.object(config, "ADMIN_IDS", [999111]):
            self.assertTrue(club_smm.is_smm_allowed(999111))

    def test_random_user_is_denied(self):
        with patch.object(config, "ADMIN_IDS", [999111]):
            self.assertFalse(club_smm.is_smm_allowed(12345678))

    def test_channel_save_and_retrieve(self):
        database.set_config("my_club_channel", "@test_besiktas_chan")
        retrieved = club_smm.get_target_channel()
        self.assertEqual(retrieved, "@test_besiktas_chan")


class TestClubSmmMultiProviderText(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_openrouter_text_generation(self, mock_urlopen):
        fake_response = {
            "choices": [{
                "message": {
                    "content": "🦅 <b>Матчдэй от Llama 3.3!</b>\n\nТолько победа орлов! #Besiktas"
                }
            }]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_response).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with patch.object(config, "OPENROUTER_API_KEY", "fake_openrouter_key"):
            text, model = club_smm_service._call_openrouter_text("System", "User", 1500)
            self.assertIn("Матчдэй от Llama 3.3", text)
            self.assertIsNotNone(model)

    def test_clean_smm_text_strips_think_tags(self):
        raw = "<think>Нужно написать пост про тренера\nШаг 1: анализ</think>🦅 <b>Новая эра Бешикташа!</b>\n\nВперёд к победам! #Besiktas"
        clean = club_smm_service._clean_smm_text(raw)
        self.assertNotIn("think", clean)
        self.assertNotIn("анализ", clean)
        self.assertIn("🦅 <b>Новая эра Бешикташа!</b>", clean)
        self.assertIn("#Besiktas", clean)

    def test_clean_smm_text_strips_unclosed_think_tag(self):
        raw = "<think>Нужно ответить пользователю на русском. Задача: написать короткий клубный пост"
        clean = club_smm_service._clean_smm_text(raw)
        self.assertEqual(clean, "")

    def test_is_meta_reasoning_detects_chain_of_thought(self):
        leaked_cot = (
            "Нужно ответить пользователю на русском. Задача: написать короткий клубный пост "
            "по теме тренера строго в один абзац. Требования: структура: Заголовок -> один плотный абзац."
        )
        self.assertTrue(club_smm_service._is_meta_reasoning(leaked_cot))

        valid_post = (
            "🦅 <b>ОФИЦИАЛЬНО: НОВЫЙ ТРЕНЕР «БЕШИКТАША»!</b>\n\n"
            "Клуб объявляет о назначении нового рулевого. Впереди великие победы! #Besiktas"
        )
        self.assertFalse(club_smm_service._is_meta_reasoning(valid_post))

    @patch("urllib.request.urlopen")
    def test_openrouter_skips_reasoning_only_response(self, mock_urlopen):
        # 1-я модель возвращает только reasoning без content
        resp1 = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning": "Нужно ответить пользователю на русском. Задача: написать пост..."
                }
            }]
        }
        # 2-я модель возвращает нормальный content
        resp2 = {
            "choices": [{
                "message": {
                    "content": "🦅 <b>Орлы взлетают!</b>\n\nТолько вперёд, только победа! #Besiktas"
                }
            }]
        }
        mock_r1 = MagicMock()
        mock_r1.read.return_value = json.dumps(resp1).encode("utf-8")
        mock_r1.__enter__.return_value = mock_r1

        mock_r2 = MagicMock()
        mock_r2.read.return_value = json.dumps(resp2).encode("utf-8")
        mock_r2.__enter__.return_value = mock_r2

        mock_urlopen.side_effect = [mock_r1, mock_r2]

        with patch.object(config, "OPENROUTER_API_KEY", "fake_openrouter_key"):
            text, model = club_smm_service._call_openrouter_text("System", "User", 1500)
            self.assertIsNotNone(text)
            self.assertNotIn("Нужно ответить", text)
            self.assertIn("Орлы взлетают!", text)

    @patch("services.club_smm_service.get_ordered_gemini_keys", return_value=["test_api_key"])
    @patch("services.ai.ai_recognizer._get_gemini_opener")
    def test_gemini_skips_thought_only_candidate(self, mock_opener_fn, mock_keys):
        resp_data = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"thought": True, "text": "Размышления модели: нужно составить пост..."}
                    ]
                }
            }]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_fn.return_value = mock_opener

        text, model = club_smm_service._call_gemini_text("System", "User", 1500)
        self.assertIsNone(text)


class TestClubSmmVisualFallback(unittest.TestCase):
    @patch("services.club_smm_service._call_openrouter_image", return_value=(None, None))
    def test_generate_club_ai_photo_falls_back_to_pillow(self, mock_or_img):
        buf = club_smm_service.generate_club_ai_photo("Бешикташ", post_type="matchday")
        self.assertIsNotNone(buf)
        # Pillow retina PNG
        self.assertTrue(buf.getvalue().startswith(b"\x89PNG\r\n\x1a\n"))


class TestChannelNormalization(unittest.TestCase):
    def test_normalize_channel_inputs(self):
        from handlers.club_smm import normalize_telegram_channel
        self.assertEqual(normalize_telegram_channel("https://t.me/BESIKTASLOGOVOFIFAREI"), "@BESIKTASLOGOVOFIFAREI")
        self.assertEqual(normalize_telegram_channel("http://t.me/BESIKTASLOGOVOFIFAREI"), "@BESIKTASLOGOVOFIFAREI")
        self.assertEqual(normalize_telegram_channel("t.me/BESIKTASLOGOVOFIFAREI"), "@BESIKTASLOGOVOFIFAREI")
        self.assertEqual(normalize_telegram_channel("@BESIKTASLOGOVOFIFAREI"), "@BESIKTASLOGOVOFIFAREI")
        self.assertEqual(normalize_telegram_channel("BESIKTASLOGOVOFIFAREI"), "@BESIKTASLOGOVOFIFAREI")
        self.assertEqual(normalize_telegram_channel("-1001234567890"), "-1001234567890")
        self.assertEqual(normalize_telegram_channel("1234567890"), "-1001234567890")


class TestStageAndRoundPosts(unittest.TestCase):
    def setUp(self):
        self._clean()
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO matches (round_number, tournament_type, cup_stage, player1_team, player2_team,
                                     player1_score, player2_score, status, mvp_player, match_date, match_time)
                VALUES (1, 'league', NULL, 'Бешикташ', 'Галатасарай', 2, 1, 'confirmed', 'Trossard', '2026-09-28', '19:00')
            """)
            c.execute("""
                INSERT INTO matches (round_number, tournament_type, cup_stage, player1_team, player2_team,
                                     player1_score, player2_score, status, mvp_player, match_date, match_time)
                VALUES (-1, 'cup', '1/64', 'Бешикташ', 'Фенербахче', 3, 2, 'confirmed', 'Rafa Silva', '2026-09-28', '21:00')
            """)

    def tearDown(self):
        self._clean()

    def _clean(self):
        with database.transaction() as conn:
            conn.cursor().execute("DELETE FROM matches WHERE player1_team = 'Бешикташ' OR player2_team = 'Бешикташ'")

    def test_get_club_stages_and_rounds(self):
        data = club_smm_service.get_club_stages_and_rounds("Бешикташ")
        cup_stages = data["cup_stages"]
        league_rounds = data["league_rounds"]
        self.assertTrue(any(st["stage"] == "1/64" for st in cup_stages))
        self.assertTrue(any(r["round"] == 1 for r in league_rounds))

    def test_get_stage_or_round_payload(self):
        payload_league = club_smm_service.get_stage_or_round_payload("Бешикташ", round_number=1)
        self.assertEqual(payload_league["round_number"], 1)
        self.assertEqual(payload_league["target_type"], "league")
        self.assertEqual(len(payload_league["matches"]), 1)
        self.assertEqual(payload_league["matches"][0]["opponent"], "Галатасарай")

        payload_cup = club_smm_service.get_stage_or_round_payload("Бешикташ", cup_stage="1/64")
        self.assertEqual(payload_cup["cup_stage"], "1/64")
        self.assertEqual(payload_cup["target_type"], "cup")
        self.assertEqual(len(payload_cup["matches"]), 1)
        self.assertEqual(payload_cup["matches"][0]["opponent"], "Фенербахче")

    def test_generate_stage_post_fallback(self):
        with patch("services.club_smm_service._call_openrouter_text", return_value=(None, None)), \
             patch("services.club_smm_service._call_gemini_text", return_value=(None, None)):
            post_league = club_smm_service.generate_stage_post("Бешикташ", round_number=1)
            self.assertIn("ИТОГИ ТУРА 1", post_league)
            self.assertIn("2:1", post_league)

            post_cup = club_smm_service.generate_stage_post("Бешикташ", cup_stage="1/64")
            self.assertIn("КУБОК: ИТОГИ СТАДИИ 1/64", post_cup)

    def test_generate_stage_post_with_ai(self):
        fake_ai_text = "🦅 <b>Огненный триумф в Туре 1!</b>\n\nБешикташ вырывает победу 2:1 у соперника! #Besiktas"
        with patch("services.club_smm_service._call_openrouter_text", return_value=(fake_ai_text, "openrouter/free")):
            post = club_smm_service.generate_stage_post("Бешикташ", round_number=1)
            self.assertIn("Огненный триумф в Туре 1!", post)

    def test_openrouter_models_include_space_bunny(self):
        models = config._get_openrouter_smm_models()
        self.assertIn("stealth/space-bunny-alpha", models)
        self.assertEqual(models[0], "stealth/space-bunny-alpha")
        self.assertIn("stealth/space-bunny-alpha", club_smm_service.GUARANTEED_OPENROUTER_MODELS)


class TestClubSmmOpenRouterImage(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_call_openrouter_image_b64(self, mock_urlopen):
        fake_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRopenrouter_test"
        fake_b64 = base64.b64encode(fake_image_bytes).decode("utf-8")
        fake_response = {
            "data": [
                {"b64_json": fake_b64}
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_response).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with patch.object(config, "OPENROUTER_API_KEY", "sk-or-test-key"):
            buf, model = club_smm_service._call_openrouter_image("epic soccer match")
            self.assertIsNotNone(buf)
            self.assertEqual(buf.getvalue(), fake_image_bytes)
            self.assertEqual(model, "inclusionai/ming-image-0.1-design")

    @patch("urllib.request.urlopen")
    def test_call_openrouter_image_url(self, mock_urlopen):
        fake_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRdownloaded_test"
        fake_response = {
            "data": [
                {"url": "https://openrouter.ai/sample.png"}
            ]
        }
        mock_resp_api = MagicMock()
        mock_resp_api.read.return_value = json.dumps(fake_response).encode("utf-8")
        mock_resp_api.__enter__.return_value = mock_resp_api

        mock_resp_img = MagicMock()
        mock_resp_img.read.return_value = fake_image_bytes
        mock_resp_img.__enter__.return_value = mock_resp_img

        mock_urlopen.side_effect = [mock_resp_api, mock_resp_img]

        with patch.object(config, "OPENROUTER_API_KEY", "sk-or-test-key"):
            buf, model = club_smm_service._call_openrouter_image("epic soccer match")
            self.assertIsNotNone(buf)
            self.assertEqual(buf.getvalue(), fake_image_bytes)
            self.assertEqual(model, "inclusionai/ming-image-0.1-design")

    @patch("services.club_smm_service._call_openrouter_image")
    def test_generate_club_ai_photo_prefers_openrouter(self, mock_or_img):
        fake_buf = io.BytesIO(b"fake_image_data")
        mock_or_img.return_value = (fake_buf, "recraft/recraft-v4.1-flash")

        res = club_smm_service.generate_club_ai_photo("Бешикташ", post_type="matchday")
        self.assertEqual(res, fake_buf)
        mock_or_img.assert_called_once()

    def test_openrouter_image_models_config(self):
        models = config._get_openrouter_image_models()
        self.assertIn("inclusionai/ming-image-0.1-design", models)
        self.assertEqual(models[0], "inclusionai/ming-image-0.1-design")
        self.assertIn("recraft/recraft-v4.1-flash", models)


class TestClubSmmCustomPhoto(unittest.IsolatedAsyncioTestCase):
    async def test_show_draft_preview_keyboard_without_photo(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        update.effective_chat.send_message = AsyncMock()
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {"smm_draft": {"text": "Test post", "team_name": "Бешикташ"}}

        await club_smm._show_draft_preview(update, context, "Test post")
        query.edit_message_text.assert_awaited_once()
        args, kwargs = query.edit_message_text.call_args
        markup = kwargs["reply_markup"]
        all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("smm_enter_photo", all_callbacks)
        self.assertNotIn("smm_publish:custom_photo", all_callbacks)

    async def test_show_draft_preview_keyboard_with_photo(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {
            "smm_draft": {
                "text": "Test post",
                "team_name": "Бешикташ",
                "custom_photo_id": "file_12345"
            }
        }

        await club_smm._show_draft_preview(update, context, "Test post")
        query.edit_message_text.assert_awaited_once()
        args, kwargs = query.edit_message_text.call_args
        markup = kwargs["reply_markup"]
        all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("smm_publish:custom_photo", all_callbacks)
        self.assertIn("smm_remove_photo", all_callbacks)
        self.assertIn("smm_enter_photo", all_callbacks)
        self.assertIn("🖼 <b>Своё фото:</b> прикреплено ✅", args[0])

    async def test_remove_photo_removes_custom_photo_id(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {
            "smm_draft": {
                "text": "Test post",
                "team_name": "Бешикташ",
                "custom_photo_id": "file_12345"
            }
        }

        with patch.object(club_smm, "_show_draft_preview", new_callable=AsyncMock) as mock_preview:
            await club_smm.cb_smm_remove_photo(update, context)
            self.assertNotIn("custom_photo_id", context.user_data["smm_draft"])
            mock_preview.assert_awaited_once()

    async def test_publish_custom_photo(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.data = "smm_publish:custom_photo"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        user = MagicMock()
        user.id = 999
        update.effective_user = user

        context = MagicMock()
        context.bot.send_photo = AsyncMock()
        sent_mock = MagicMock()
        sent_mock.message_id = 42
        context.bot.send_photo.return_value = sent_mock

        context.user_data = {
            "smm_draft": {
                "text": "Post with my own photo",
                "team_name": "Бешикташ",
                "custom_photo_id": "photo_file_xyz"
            }
        }

        with patch.object(club_smm, "is_smm_allowed", return_value=True), \
             patch.object(club_smm, "get_target_channel", return_value="@test_channel"):
            await club_smm.cb_smm_publish(update, context)

            context.bot.send_photo.assert_awaited_once()
            _, kwargs = context.bot.send_photo.call_args
            self.assertEqual(kwargs["chat_id"], "@test_channel")
            self.assertEqual(kwargs["photo"], "photo_file_xyz")
            self.assertIn("Post with my own photo", kwargs["caption"])

    async def test_show_draft_preview_keyboard_with_single_match_photo(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {
            "smm_draft": {
                "text": "Recap text",
                "team_name": "Бешикташ",
                "match_photos": ["match_photo_1"],
                "photo_mode": "match",
            }
        }

        await club_smm._show_draft_preview(update, context, "Recap text")
        query.edit_message_text.assert_awaited_once()
        args, kwargs = query.edit_message_text.call_args
        markup = kwargs["reply_markup"]
        all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("smm_publish:match_photos", all_callbacks)
        self.assertIn("smm_remove_photo", all_callbacks)
        self.assertIn("smm_enter_photo", all_callbacks)
        self.assertIn("🖼 <b>Скрин матча:</b> прикреплён ✅", args[0])

    async def test_show_draft_preview_keyboard_with_multiple_match_photos(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {
            "smm_draft": {
                "text": "Recap text",
                "team_name": "Бешикташ",
                "match_photos": ["match_photo_1", "match_photo_2"],
                "photo_mode": "match",
            }
        }

        await club_smm._show_draft_preview(update, context, "Recap text")
        query.edit_message_text.assert_awaited_once()
        args, kwargs = query.edit_message_text.call_args
        markup = kwargs["reply_markup"]
        all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertIn("smm_publish:match_photos", all_callbacks)
        self.assertIn("smm_remove_photo", all_callbacks)
        self.assertIn("🖼 <b>Скрины матчей:</b> прикреплено (2 шт.) ✅", args[0])

    async def test_attach_match_photos_callback(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.answer = AsyncMock()
        update.callback_query = query
        context = MagicMock()
        context.user_data = {
            "smm_draft": {
                "text": "Recap text",
                "team_name": "Бешикташ",
                "match_photos": ["match_photo_1"],
                "photo_mode": "none",
            }
        }

        with patch.object(club_smm, "_show_draft_preview", new_callable=AsyncMock) as mock_preview:
            await club_smm.cb_smm_attach_match_photos(update, context)
            self.assertEqual(context.user_data["smm_draft"]["photo_mode"], "match")
            mock_preview.assert_awaited_once()

    async def test_publish_match_single_photo(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.data = "smm_publish:match_photos"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        user = MagicMock()
        user.id = 999
        update.effective_user = user

        context = MagicMock()
        context.bot.send_photo = AsyncMock()
        sent_mock = MagicMock()
        sent_mock.message_id = 101
        context.bot.send_photo.return_value = sent_mock

        context.user_data = {
            "smm_draft": {
                "text": "Recap with match screen",
                "team_name": "Бешикташ",
                "match_photos": ["match_photo_single"],
                "photo_mode": "match",
            }
        }

        with patch.object(club_smm, "is_smm_allowed", return_value=True), \
             patch.object(club_smm, "get_target_channel", return_value="@test_channel"):
            await club_smm.cb_smm_publish(update, context)

            context.bot.send_photo.assert_awaited_once()
            _, kwargs = context.bot.send_photo.call_args
            self.assertEqual(kwargs["chat_id"], "@test_channel")
            self.assertEqual(kwargs["photo"], "match_photo_single")
            self.assertIn("Recap with match screen", kwargs["caption"])

    async def test_publish_match_multiple_photos_album(self):
        from unittest.mock import AsyncMock
        update = MagicMock()
        query = MagicMock()
        query.data = "smm_publish:match_photos"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        update.callback_query = query
        user = MagicMock()
        user.id = 999
        update.effective_user = user

        context = MagicMock()
        context.bot.send_media_group = AsyncMock()
        msg1 = MagicMock()
        msg1.message_id = 201
        context.bot.send_media_group.return_value = [msg1]

        context.user_data = {
            "smm_draft": {
                "text": "Recap of series",
                "team_name": "Бешикташ",
                "match_photos": ["screen_game1", "screen_game2"],
                "photo_mode": "match",
            }
        }

        with patch.object(club_smm, "is_smm_allowed", return_value=True), \
             patch.object(club_smm, "get_target_channel", return_value="@test_channel"):
            await club_smm.cb_smm_publish(update, context)

            context.bot.send_media_group.assert_awaited_once()
            _, kwargs = context.bot.send_media_group.call_args
            self.assertEqual(kwargs["chat_id"], "@test_channel")
            media = kwargs["media"]
            self.assertEqual(len(media), 2)
            self.assertEqual(media[0].media, "screen_game1")
            self.assertEqual(media[1].media, "screen_game2")
            self.assertIn("Recap of series", media[0].caption)
            self.assertIsNone(media[1].caption)


class TestDatabaseMatchPhotos(unittest.TestCase):
    def test_get_last_match_photos_and_stage_photos(self):
        with database.transaction() as conn:
            cursor = conn.cursor()
            # Clear or insert test matches
            cursor.execute("""
                INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, status, photo_id, tournament_type)
                VALUES (1, 'Бешикташ', 'Галатасарай', 2, 1, 'confirmed', 'photo_besiktas_1', 'league')
            """)
            cursor.execute("""
                INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, status, photo_id, tournament_type, cup_stage, cup_series_id, game_num_in_series)
                VALUES (-1, 'Бешикташ', 'Фенербахче', 3, 0, 'confirmed', 'photo_cup_g1', 'cup', '1/4', 'series_cup_99', 1)
            """)
            cursor.execute("""
                INSERT INTO matches (round_number, player1_team, player2_team, player1_score, player2_score, status, photo_id, tournament_type, cup_stage, cup_series_id, game_num_in_series)
                VALUES (-1, 'Фенербахче', 'Бешикташ', 1, 2, 'confirmed', 'photo_cup_g2', 'cup', '1/4', 'series_cup_99', 2)
            """)

        photos = database.get_last_match_photos("Бешикташ")
        self.assertEqual(photos, ["photo_cup_g1", "photo_cup_g2"])

        stage_photos = database.get_stage_match_photos("Бешикташ", round_number=1)
        self.assertEqual(stage_photos, ["photo_besiktas_1"])

        cup_photos = database.get_stage_match_photos("Бешикташ", cup_stage="1/4")
        self.assertEqual(cup_photos, ["photo_cup_g1", "photo_cup_g2"])




