"""
Tests for Gemini Vision OCR key pool rotation and fallback on rate limits (HTTP 429).
"""
import io
import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import config
from services.ai.ai_recognizer import (
    GEMINI_MODELS,
    get_ordered_ocr_keys,
    get_ordered_ocr_models,
    recognize_match_screenshots_bytes,
)
from services.ai.squad_recognizer import recognize_squad_screenshot_bytes
from services.ai.ai_chat import (
    GEMINI_CHAT_MODELS,
    get_ordered_chat_keys,
    get_ordered_chat_models,
    generate_chat_reply,
)
from services.round_preview import (
    CANDIDATE_MODELS,
    _call_gemini,
)


class TestGeminiPoolRotation(unittest.TestCase):
    def test_exact_models_list(self):
        self.assertEqual(
            GEMINI_MODELS,
            [
                "gemini-3.1-flash-lite",
                "gemini-3.5-flash-lite",
                "gemini-3.8-flash",
            ]
        )

    def test_config_parses_comma_separated_keys(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "keyA, keyB , keyC"}):
            keys = config._get_gemini_api_keys()
            self.assertEqual(keys, ["keyA", "keyB", "keyC"])

    def test_round_robin_rotation(self):
        import services.ai.ai_recognizer as ar
        with ar._ocr_key_lock:
            ar._ocr_key_index = 0
        with patch.object(config, "GEMINI_API_KEYS", ["key1", "key2", "key3"]):
            call1 = get_ordered_ocr_keys()
            call2 = get_ordered_ocr_keys()
            call3 = get_ordered_ocr_keys()
            call4 = get_ordered_ocr_keys()

            # Each call rotates the starting key
            self.assertEqual(call1[0], "key1")
            self.assertEqual(call2[0], "key2")
            self.assertEqual(call3[0], "key3")
            self.assertEqual(call4[0], "key1")

    def test_round_robin_model_rotation(self):
        import services.ai.ai_recognizer as ar
        with ar._ocr_model_lock:
            ar._ocr_model_index = 0
        call1 = get_ordered_ocr_models()
        call2 = get_ordered_ocr_models()
        call3 = get_ordered_ocr_models()
        call4 = get_ordered_ocr_models()

        self.assertEqual(call1[0], "gemini-3.1-flash-lite")
        self.assertEqual(call2[0], "gemini-3.5-flash-lite")
        self.assertEqual(call3[0], "gemini-3.8-flash")
        self.assertEqual(call4[0], "gemini-3.1-flash-lite")
        self.assertEqual(len(call1), 3)

    def test_explicit_override_keys(self):
        keys = get_ordered_ocr_keys("custom1,custom2")
        self.assertEqual(keys, ["custom1", "custom2"])


class TestGeminiFallbackOnRateLimit(unittest.TestCase):
    def setUp(self):
        self.mock_opener = MagicMock()

    @patch("services.ai.ai_recognizer._get_gemini_opener")
    @patch.object(config, "GEMINI_API_KEYS", ["key_rate_limited", "key_working"])
    def test_match_ocr_fallback_to_second_key_on_429(self, mock_get_opener):
        mock_get_opener.return_value = self.mock_opener

        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(b'{"error": "rate_limit"}')
        )

        match_data = json.dumps({
            "matches": [{
                "team1": "Real Madrid",
                "team2": "Barcelona",
                "left_score": 2,
                "right_score": 1,
                "left_goals": ["Vinicius", "Bellingham"],
                "right_goals": ["Lewandowski"],
                "left_assists": [],
                "right_assists": [],
            }]
        })

        gemini_response_bytes = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": match_data}]
                }
            }]
        }).encode("utf-8")

        def fake_open(req, timeout=30):
            if "key=key_rate_limited" in req.full_url:
                raise err_429
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = gemini_response_bytes
            return cm

        self.mock_opener.open.side_effect = fake_open

        res = recognize_match_screenshots_bytes([b"fake_image_bytes"], api_key="key_rate_limited,key_working")
        self.assertIsNotNone(res)
        self.assertEqual(res["left_score"], 2)
        self.assertEqual(res["right_score"], 1)

    @patch("services.ai.squad_recognizer._get_gemini_opener")
    @patch.object(config, "GEMINI_API_KEYS", ["key_rate_limited", "key_working"])
    def test_squad_ocr_fallback_to_second_key_on_429(self, mock_get_opener):
        mock_get_opener.return_value = self.mock_opener

        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(b'{"error": "rate_limit"}')
        )

        squad_data = json.dumps({
            "players": [
                {"name": "Courtois", "position": "GK"},
                {"name": "Modric", "position": "CM"}
            ]
        })

        gemini_response_bytes = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": squad_data}]
                }
            }]
        }).encode("utf-8")

        def fake_open(req, timeout=30):
            if "key=key_rate_limited" in req.full_url:
                raise err_429
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = gemini_response_bytes
            return cm

        self.mock_opener.open.side_effect = fake_open

        res = recognize_squad_screenshot_bytes(b"fake_image_bytes", api_key="key_rate_limited,key_working")
        self.assertIsNotNone(res)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["player_name"], "Courtois")
        self.assertEqual(res[0]["position"], "GK")

    @patch("services.ai.ai_recognizer._get_gemini_opener")
    def test_match_ocr_fallback_to_next_model_on_429(self, mock_get_opener):
        mock_get_opener.return_value = self.mock_opener
        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(b'{"error": "rate_limit"}')
        )
        match_data = json.dumps({
            "matches": [{
                "team1": "Real Madrid",
                "team2": "Barcelona",
                "left_score": 1,
                "right_score": 0,
                "left_goals": ["Vinicius"],
                "right_goals": [],
                "left_assists": [],
                "right_assists": [],
            }]
        })
        gemini_response_bytes = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": match_data}]
                }
            }]
        }).encode("utf-8")

        import services.ai.ai_recognizer as ar
        with ar._ocr_model_lock:
            ar._ocr_model_index = 0

        # Model 3.1 fails with 429, Model 3.5 succeeds
        def fake_open(req, timeout=30):
            if "gemini-3.1-flash-lite" in req.full_url:
                raise err_429
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = gemini_response_bytes
            return cm

        self.mock_opener.open.side_effect = fake_open
        res = recognize_match_screenshots_bytes([b"fake_image_bytes"], api_key="single_key")
        self.assertIsNotNone(res)
        self.assertEqual(res["left_score"], 1)
        self.assertEqual(res["team1"], "Real Madrid")


class TestGeminiChatPoolRotation(unittest.TestCase):
    def test_chat_exact_models_list(self):
        self.assertEqual(
            GEMINI_CHAT_MODELS,
            [
                "gemini-3.1-flash-lite",
                "gemini-3.5-flash-lite",
                "gemini-3.8-flash",
            ]
        )
        self.assertEqual(CANDIDATE_MODELS, GEMINI_CHAT_MODELS)

    def test_config_parses_comma_separated_chat_keys(self):
        with patch.dict("os.environ", {"GEMINI_CHAT_API_KEY": "chat1, chat2 , chat3"}):
            keys = config._get_gemini_chat_keys()
            self.assertEqual(keys, ["chat1", "chat2", "chat3"])

    def test_round_robin_chat_key_rotation(self):
        import services.ai.ai_chat as ac
        with ac._chat_key_lock:
            ac._chat_key_index = 0
        with patch.object(config, "GEMINI_CHAT_API_KEYS", ["ckey1", "ckey2", "ckey3"]):
            call1 = get_ordered_chat_keys()
            call2 = get_ordered_chat_keys()
            call3 = get_ordered_chat_keys()
            call4 = get_ordered_chat_keys()

            self.assertEqual(call1[0], "ckey1")
            self.assertEqual(call2[0], "ckey2")
            self.assertEqual(call3[0], "ckey3")
            self.assertEqual(call4[0], "ckey1")

    def test_round_robin_chat_model_rotation(self):
        import services.ai.ai_chat as ac
        with ac._chat_model_lock:
            ac._chat_model_index = 0
        call1 = get_ordered_chat_models()
        call2 = get_ordered_chat_models()
        call3 = get_ordered_chat_models()
        call4 = get_ordered_chat_models()

        self.assertEqual(call1[0], "gemini-3.1-flash-lite")
        self.assertEqual(call2[0], "gemini-3.5-flash-lite")
        self.assertEqual(call3[0], "gemini-3.8-flash")
        self.assertEqual(call4[0], "gemini-3.1-flash-lite")
        self.assertEqual(len(call1), 3)

    def test_chat_explicit_override_keys(self):
        keys = get_ordered_chat_keys("customA,customB")
        self.assertEqual(keys, ["customA", "customB"])


class TestGeminiChatFallbackOnRateLimit(unittest.TestCase):
    def setUp(self):
        self.mock_opener = MagicMock()

    @patch("services.ai.ai_recognizer._get_gemini_opener")
    @patch.object(config, "GEMINI_CHAT_API_KEYS", ["ckey_rate_limited", "ckey_working"])
    def test_ai_chat_fallback_to_second_key_on_429(self, mock_get_opener):
        mock_get_opener.return_value = self.mock_opener
        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(b'{"error": "rate_limit"}')
        )
        chat_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": "Здорово, братуха! Все чётко."}]
                }
            }]
        }).encode("utf-8")

        def fake_open(req, timeout=25):
            if "key=ckey_rate_limited" in req.full_url:
                raise err_429
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = chat_data
            return cm

        self.mock_opener.open.side_effect = fake_open
        reply = generate_chat_reply(
            user_id=123,
            user_text="Привет, как дела?",
            chat_history=[],
            context_data="",
        )
        self.assertEqual(reply, "Здорово, братуха! Все чётко.")

    @patch("services.ai.ai_recognizer._get_gemini_opener")
    @patch.object(config, "GEMINI_CHAT_API_KEYS", ["single_chat_key"])
    def test_ai_chat_fallback_to_next_model_on_429(self, mock_get_opener):
        mock_get_opener.return_value = self.mock_opener
        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(b'{"error": "rate_limit"}')
        )
        chat_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": "Ответ от запасной модели!"}]
                }
            }]
        }).encode("utf-8")

        import services.ai.ai_chat as ac
        with ac._chat_model_lock:
            ac._chat_model_index = 0

        def fake_open(req, timeout=25):
            if "gemini-3.1-flash-lite" in req.full_url:
                raise err_429
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = chat_data
            return cm

        self.mock_opener.open.side_effect = fake_open
        reply = generate_chat_reply(
            user_id=123,
            user_text="Привет!",
            chat_history=[],
            context_data="",
        )
        self.assertEqual(reply, "Ответ от запасной модели!")

    @patch("services.ai.ai_recognizer._get_gemini_opener")
    @patch.object(config, "GEMINI_CHAT_API_KEYS", ["ckey_rate_limited", "ckey_working"])
    def test_round_preview_fallback_to_second_key_on_429(self, mock_get_opener):
        mock_get_opener.return_value = self.mock_opener
        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(b'{"error": "rate_limit"}')
        )
        preview_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": "Тур обещает быть огненным!"}]
                }
            }]
        }).encode("utf-8")

        def fake_open(req, timeout=30):
            if "key=ckey_rate_limited" in req.full_url:
                raise err_429
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = preview_data
            return cm

        self.mock_opener.open.side_effect = fake_open
        res = _call_gemini("system_prompt", {"round": 1}, 500)
        self.assertEqual(res, "Тур обещает быть огненным!")

    @patch("services.ai.ai_recognizer._get_gemini_opener")
    @patch.object(config, "GEMINI_CHAT_API_KEYS", ["single_key"])
    def test_round_preview_fallback_to_next_model_on_429(self, mock_get_opener):
        mock_get_opener.return_value = self.mock_opener
        err_429 = urllib.error.HTTPError(
            url="http://fake", code=429, msg="Too Many Requests", hdrs={}, fp=io.BytesIO(b'{"error": "rate_limit"}')
        )
        preview_data = json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": "Превью от резервной модели!"}]
                }
            }]
        }).encode("utf-8")

        import services.ai.ai_chat as ac
        with ac._chat_model_lock:
            ac._chat_model_index = 0

        def fake_open(req, timeout=30):
            if "gemini-3.1-flash-lite" in req.full_url:
                raise err_429
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = preview_data
            return cm

        self.mock_opener.open.side_effect = fake_open
        res = _call_gemini("system_prompt", {"round": 1}, 500)
        self.assertEqual(res, "Превью от резервной модели!")


if __name__ == "__main__":
    unittest.main()
