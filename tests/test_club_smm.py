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

class TestClubSmmGeminiImageMock(unittest.TestCase):
    @patch("services.club_smm_service.get_ordered_gemini_keys", return_value=["test_api_key"])
    @patch("services.ai.ai_recognizer._get_gemini_opener")
    def test_generate_club_ai_photo_generate_images(self, mock_opener_fn, mock_keys):
        fake_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRtest_image_bytes"
        fake_b64 = base64.b64encode(fake_image_bytes).decode("utf-8")
        fake_response = {
            "generatedImages": [{
                "image": {
                    "imageBytes": fake_b64,
                }
            }]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_response).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_opener_fn.return_value = mock_opener

        buf = club_smm_service.generate_club_ai_photo("Бешикташ", post_type="matchday")
        self.assertIsNotNone(buf)
        self.assertEqual(buf.getvalue(), fake_image_bytes)

    @patch("services.club_smm_service.get_ordered_gemini_keys", return_value=["test_api_key"])
    @patch("services.ai.ai_recognizer._get_gemini_opener")
    def test_generate_club_ai_photo_generate_content(self, mock_opener_fn, mock_keys):
        fake_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRtest_image_bytes"
        fake_b64 = base64.b64encode(fake_image_bytes).decode("utf-8")
        # First call (generateImages) fails with 404, second call (generateContent) succeeds
        mock_resp_err = MagicMock()
        mock_resp_err.read.return_value = b"{}"

        fake_response = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": fake_b64,
                        }
                    }]
                }
            }]
        }
        mock_resp_ok = MagicMock()
        mock_resp_ok.read.return_value = json.dumps(fake_response).encode("utf-8")
        mock_resp_ok.__enter__.return_value = mock_resp_ok

        mock_opener = MagicMock()
        mock_opener.open.side_effect = [Exception("generateImages 404"), mock_resp_ok]
        mock_opener_fn.return_value = mock_opener

        buf = club_smm_service.generate_club_ai_photo("Бешикташ", post_type="matchday")
        self.assertIsNotNone(buf)
        self.assertEqual(buf.getvalue(), fake_image_bytes)


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


