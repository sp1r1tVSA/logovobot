"""
tests/test_sports_multi_key.py

Tests for API-Sports multi-key pool rotation, quota exhaustion handling,
automatic failover, and zero-credential telemetry disclosure.
"""

import asyncio
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import config
from time_utils import now_msk
from services.sports.adapters.api_sports import APISportsProvider


def run(coro):
    return asyncio.run(coro)


class TestSportsConfig(unittest.TestCase):
    def test_comma_separated_keys(self):
        with patch.dict("os.environ", {"SPORTS_API_KEY": "keyA, keyB , keyC"}, clear=True):
            keys = config._get_sports_api_keys()
            self.assertEqual(keys, ["keyA", "keyB", "keyC"])

    def test_separate_key_env_vars(self):
        with patch.dict("os.environ", {
            "SPORTS_API_KEY": "primary_key",
            "SPORTS_API_KEY_2": "backup_key_2",
            "SPORTS_API_KEY_3": "backup_key_3",
        }, clear=True):
            keys = config._get_sports_api_keys()
            self.assertEqual(keys, ["primary_key", "backup_key_2", "backup_key_3"])

    def test_apisports_fallback_and_deduplication(self):
        with patch.dict("os.environ", {
            "APISPORTS_KEY": "legacy_key",
            "SPORTS_API_KEY_2": "legacy_key",
        }, clear=True):
            keys = config._get_sports_api_keys()
            self.assertEqual(keys, ["legacy_key"])


class TestAPISportsProviderMultiKey(unittest.TestCase):
    def test_init_with_key_list(self):
        prov = APISportsProvider(api_keys=["key1", "key2"])
        self.assertEqual(prov.api_keys, ["key1", "key2"])
        self.assertTrue(prov.is_connected)
        self.assertFalse(prov.is_quota_exhausted())

    def test_round_robin_rotation(self):
        prov = APISportsProvider(api_keys=["key1", "key2", "key3"])
        k1 = prov.get_active_api_key(advance=True)
        k2 = prov.get_active_api_key(advance=True)
        k3 = prov.get_active_api_key(advance=True)
        k4 = prov.get_active_api_key(advance=True)
        self.assertEqual([k1, k2, k3, k4], ["key1", "key2", "key3", "key1"])

    def test_api_key_property_and_setter(self):
        prov = APISportsProvider(api_keys=["key1", "key2"])
        # Property does not advance rotation
        self.assertEqual(prov.api_key, "key1")
        self.assertEqual(prov.api_key, "key1")

        # Setter clears on empty
        prov.api_key = ""
        self.assertEqual(prov.api_keys, [])
        self.assertEqual(prov.api_key, "")
        self.assertFalse(prov.is_connected)

        # Setter updates on new value
        prov.api_key = "brand_new_key"
        self.assertEqual(prov.api_keys, ["brand_new_key"])
        self.assertEqual(prov.api_key, "brand_new_key")
        self.assertTrue(prov.is_connected)

    def test_failover_when_primary_key_hits_quota(self):
        import aiohttp

        p = APISportsProvider(api_keys=["key_exhausted", "key_working"])
        calls = []

        class MockResp:
            def __init__(self, key):
                self.key = key
                self.status = 200
                self.headers = {}

            async def json(self):
                if self.key == "key_exhausted":
                    return {
                        "errors": {
                            "requests": "You have reached the request limit for the day, Go to https://dashboard.api-football.com to upgrade your plan."
                        },
                        "response": []
                    }
                return {
                    "errors": [],
                    "response": [{"fixture": {"id": 1528933, "status": {"short": "FT"}}}]
                }

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class MockSession:
            def __init__(self, *a, **k):
                pass

            def get(self, url, headers=None, params=None):
                key = headers.get("x-apisports-key")
                calls.append(key)
                return MockResp(key)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with patch.object(aiohttp, "ClientSession", MockSession):
            data = run(p._fetch_json("fixtures", {"id": 1528933}))

            # Verified: First tried key_exhausted, encountered daily limit, switched to key_working
            self.assertEqual(calls, ["key_exhausted", "key_working"])
            self.assertEqual(len(data.get("response", [])), 1)

            # key_exhausted is now recorded as exhausted
            self.assertTrue(p.is_key_exhausted("key_exhausted"))
            self.assertFalse(p.is_key_exhausted("key_working"))
            self.assertFalse(p.is_quota_exhausted())

            # Next request goes directly to key_working without touching key_exhausted
            calls.clear()
            run(p._fetch_json("fixtures", {"id": 999}))
            self.assertEqual(calls, ["key_working"])

    def test_all_keys_exhausted_pauses_provider(self):
        import aiohttp

        p = APISportsProvider(api_keys=["key1", "key2"])
        calls = []

        class MockResp:
            def __init__(self, key):
                self.status = 200
                self.headers = {}

            async def json(self):
                return {
                    "errors": {"requests": "You have reached the request limit for the day."},
                    "response": []
                }

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class MockSession:
            def __init__(self, *a, **k):
                pass

            def get(self, url, headers=None, params=None):
                calls.append(headers.get("x-apisports-key"))
                return MockResp(headers.get("x-apisports-key"))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with patch.object(aiohttp, "ClientSession", MockSession):
            data = run(p._fetch_json("fixtures", {"id": 1}))

            # Both keys attempted and failed
            self.assertEqual(calls, ["key1", "key2"])
            self.assertTrue(p.is_quota_exhausted())

            # Status reflects QUOTA_EXHAUSTED
            status = p.get_provider_status()
            self.assertEqual(status["status"], "QUOTA_EXHAUSTED")
            self.assertEqual(status["pool_size"], 2)
            self.assertEqual(status["pool_available"], 0)
            self.assertEqual(status["pool_exhausted"], 2)
            self.assertTrue(status["quota_exhausted"])
            self.assertIn("quota_exhausted_until", status)

            # Subsequent call is skipped immediately without making HTTP calls
            calls.clear()
            skipped_data = run(p._fetch_json("fixtures", {"id": 2}))
            self.assertEqual(calls, [])
            self.assertIn("requests", skipped_data.get("errors", {}))

    def test_zero_credential_disclosure_in_status(self):
        secret1 = "SUPER_SECRET_KEY_ALPHA_12345"
        secret2 = "SUPER_SECRET_KEY_BETA_67890"
        prov = APISportsProvider(api_keys=[secret1, secret2])

        status = prov.get_provider_status()
        import json
        status_json = json.dumps(status)

        self.assertNotIn(secret1, status_json)
        self.assertNotIn(secret2, status_json)
        self.assertNotIn("api_key", status)
        self.assertNotIn("secret", status)
        self.assertNotIn("token", status)
