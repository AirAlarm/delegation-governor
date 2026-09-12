"""Configuration-facade store tests — stdlib only, no mcp import."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config_store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["CC_DELEGATE_HOME"] = self._tmp.name
        # Shield the tests from real user env.
        self._saved = {k: os.environ.pop(k, None) for k in ("DELEGATE_API_KEY", "TEST_PROV_KEY")}

    def tearDown(self):
        os.environ.pop("CC_DELEGATE_HOME", None)
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
        self._tmp.cleanup()


class TestProfiles(StoreTestCase):
    def test_set_and_load_round_trip(self):
        config_store.set_profile("mm", "litellm:minimax/MiniMax-M3", "MINIMAX_API_KEY")
        store = config_store.load_store()
        self.assertEqual(store["profiles"]["mm"]["model"], "litellm:minimax/MiniMax-M3")
        self.assertEqual(store["default_profile"], "mm")  # first profile becomes default

    def test_model_validation(self):
        with self.assertRaises(ValueError):
            config_store.set_profile("bad", "no-prefix-model")

    def test_fallback_models_round_trip(self):
        config_store.set_profile(
            "mm", "litellm:minimax/MiniMax-M3", "MINIMAX_API_KEY",
            fallback_models=["litellm:minimax/MiniMax-Text-01", "litellm:anthropic/claude-haiku-4-5"],
        )
        store = config_store.load_store()
        self.assertEqual(
            store["profiles"]["mm"]["fallback_models"],
            ["litellm:minimax/MiniMax-Text-01", "litellm:anthropic/claude-haiku-4-5"],
        )

    def test_fallback_models_validated_like_the_primary_model(self):
        with self.assertRaises(ValueError):
            config_store.set_profile(
                "mm", "litellm:minimax/MiniMax-M3", fallback_models=["no-prefix-model"],
            )

    def test_empty_fallback_models_not_persisted(self):
        config_store.set_profile("mm", "litellm:minimax/MiniMax-M3", fallback_models=[])
        store = config_store.load_store()
        self.assertNotIn("fallback_models", store["profiles"]["mm"])

    def test_remove_reassigns_default(self):
        config_store.set_profile("a", "litellm:x/y")
        config_store.set_profile("b", "litellm:x/z")
        self.assertTrue(config_store.remove_profile("a"))
        self.assertEqual(config_store.load_store()["default_profile"], "b")
        self.assertFalse(config_store.remove_profile("a"))

    def test_set_default_unknown_raises(self):
        with self.assertRaises(KeyError):
            config_store.set_default_profile("nope")


class TestResolution(StoreTestCase):
    ENV_DEFAULTS = {"model": "litellm:env/model", "api_key_env_var": "ENV_KEY_VAR"}

    def test_no_store_falls_back_to_env(self):
        r = config_store.resolve_profile(None, self.ENV_DEFAULTS)
        self.assertEqual(r["model"], "litellm:env/model")
        self.assertIn("legacy", r["source"])
        self.assertEqual(r["fallback_models"], [])  # always present, never None

    def test_resolve_surfaces_fallback_models(self):
        config_store.set_profile(
            "mm", "litellm:minimax/MiniMax-M3",
            fallback_models=["litellm:minimax/MiniMax-Text-01"],
        )
        r = config_store.resolve_profile("mm", self.ENV_DEFAULTS)
        self.assertEqual(r["fallback_models"], ["litellm:minimax/MiniMax-Text-01"])

    def test_default_profile_wins_over_env(self):
        config_store.set_profile("mm", "litellm:minimax/MiniMax-M3", "MINIMAX_API_KEY")
        r = config_store.resolve_profile(None, self.ENV_DEFAULTS)
        self.assertEqual(r["model"], "litellm:minimax/MiniMax-M3")
        self.assertIn("default", r["source"])

    def test_unknown_profile_lists_available(self):
        config_store.set_profile("mm", "litellm:x/y")
        with self.assertRaises(KeyError) as cm:
            config_store.resolve_profile("nope", self.ENV_DEFAULTS)
        self.assertIn("mm", str(cm.exception))

    def test_key_resolution_precedence(self):
        config_store.set_profile("p", "litellm:x/y", "TEST_PROV_KEY")
        # 1. credentials file wins
        config_store.store_credential("TEST_PROV_KEY", "from-credentials")
        os.environ["TEST_PROV_KEY"] = "from-env"
        r = config_store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertEqual(r["api_key"], "from-credentials")
        # 2. env var when no credential
        Path(config_store.credentials_path()).unlink()
        r = config_store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertEqual(r["api_key"], "from-env")
        # 3. legacy DELEGATE_API_KEY as last resort
        del os.environ["TEST_PROV_KEY"]
        os.environ["DELEGATE_API_KEY"] = "legacy"
        r = config_store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertEqual(r["api_key"], "legacy")
        del os.environ["DELEGATE_API_KEY"]
        # 4. nothing -> None (OAuth profiles run keyless)
        r = config_store.resolve_profile("p", self.ENV_DEFAULTS)
        self.assertIsNone(r["api_key"])


class TestAuthState(StoreTestCase):
    def test_key_availability(self):
        config_store.set_profile("p", "litellm:x/y", "TEST_PROV_KEY")
        prof = config_store.load_store()["profiles"]["p"]
        self.assertFalse(config_store.auth_state(prof)["api_key_available"])
        config_store.store_credential("TEST_PROV_KEY", "k")
        self.assertTrue(config_store.auth_state(prof)["api_key_available"])

    def test_oauth_detection_by_model_prefix(self):
        config_store.set_profile("cop", "litellm:github_copilot/gpt-5", None)
        prof = config_store.load_store()["profiles"]["cop"]
        oauth = config_store.auth_state(prof)["oauth"]
        self.assertIsNotNone(oauth)
        self.assertEqual(oauth["provider"], "github_copilot")
        self.assertIn("token_cache_present", oauth)

    def test_corrupt_config_degrades_to_empty(self):
        config_store.config_path().parent.mkdir(parents=True, exist_ok=True)
        config_store.config_path().write_text("{not json", encoding="utf-8")
        store = config_store.load_store()
        self.assertEqual(store["profiles"], {})


if __name__ == "__main__":
    unittest.main()
