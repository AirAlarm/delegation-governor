"""OpenRouter is an independent, opt-in worker lane."""
from __future__ import annotations

import io
import json
import os
import urllib.error
from unittest import mock

from base import DGTest

from dg import config, launcher
from dg.workers import cc_delegate


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://opencode.ai/zen/go/v1/messages", code, "error", {},
        io.BytesIO(json.dumps(body).encode()))


class _Response:
    def __init__(self, body: dict):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.body).encode()


class TestOpenRouterConfig(DGTest):
    def test_default_is_a_fourth_worker_only_lane(self):
        lane = self.cfg["workers"]["lanes"]["openrouter"]
        self.assertEqual(lane["profile"], "openrouter-coder")
        self.assertEqual(lane["endpoint"], "openrouter")
        self.assertEqual(self.cfg["workers"]["totalWriteJobsPerRepo"], 4)
        self.assertNotIn("openrouter", [x["name"] for x in self.cfg["supervisorFallbacks"]])

    def test_v3_config_migrates_to_the_current_schema(self):
        """v6's classRouting change (drop OpenRouter, reorder) isn't an
        additive tweak like v4/v5 -- it replaces classRouting outright rather
        than trying to preserve a custom order against a changed shape."""
        config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_PATH.write_text(json.dumps({
            "schemaVersion": 3,
            "workers": {
                "classRouting": {"hard": ["station", "codex"]},
                "totalWriteJobsPerRepo": 3,
                "maxReadOnlyJobs": 3,
            },
        }), "utf-8")
        migrated = config.load()
        self.assertEqual(migrated["schemaVersion"], 9)
        self.assertEqual(migrated["workers"]["classRouting"],
                         config.DEFAULTS["workers"]["classRouting"])
        self.assertEqual(migrated["workers"]["totalWriteJobsPerRepo"], 4)
        self.assertTrue(list(config.CONFIG_PATH.parent.glob("config.v3.*.json")))


class TestOpenRouterProfile(DGTest):
    def test_missing_profile_keeps_lane_down(self):
        path = self.home / "cc-config.json"
        path.write_text('{"profiles": {}}', "utf-8")
        with mock.patch.object(cc_delegate, "_config_path", return_value=path):
            out = cc_delegate.profile("openrouter", "openrouter-coder")
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["reason"])

    def test_present_profile_is_ready_for_endpoint_probe(self):
        path = self.home / "cc-config.json"
        path.write_text(json.dumps({"profiles": {"openrouter-coder": {
            "model": "litellm:openrouter/qwen/qwen3-coder-next"}}}), "utf-8")
        with mock.patch.object(cc_delegate, "_config_path", return_value=path):
            self.assertTrue(cc_delegate.profile("openrouter", "openrouter-coder")["ok"])


class TestOpenRouterProbe(DGTest):
    def test_probe_requires_key_without_network(self):
        endpoint = self.cfg["workers"]["endpoints"]["openrouter"]
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(launcher.Path, "expanduser", side_effect=OSError):
            ok, detail = launcher._probe_openrouter(endpoint)
        self.assertFalse(ok)
        self.assertIn("OPENROUTER_API_KEY", detail)

    def test_probe_reads_key_limit_without_inference(self):
        endpoint = self.cfg["workers"]["endpoints"]["openrouter"]
        seen = []

        def urlopen(req, timeout):
            seen.append((req.full_url, req.get_header("Authorization"), timeout))
            return _Response({"data": {"limit_remaining": 12.5}})

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
                mock.patch("urllib.request.urlopen", side_effect=urlopen):
            ok, detail = launcher._probe_openrouter(endpoint)
        self.assertTrue(ok)
        self.assertIn("$12.50", detail)
        self.assertEqual(seen, [("https://openrouter.ai/api/v1/key", "Bearer secret", 10)])

    def test_exhausted_key_limit_marks_lane_unavailable(self):
        endpoint = self.cfg["workers"]["endpoints"]["openrouter"]
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Response({"data": {"limit_remaining": 0}})):
            ok, detail = launcher._probe_openrouter(endpoint)
        self.assertFalse(ok)
        self.assertIn("exhausted", detail)


class TestOpenCodeConfig(DGTest):
    def test_default_is_three_worker_only_opencode_tiers(self):
        for tier, profile in (("opencode-fast", "opencode-fast"),
                              ("opencode-main", "opencode-main"),
                              ("opencode-smart", "opencode-smart")):
            lane = self.cfg["workers"]["lanes"][tier]
            self.assertEqual(lane["profile"], profile)
            self.assertEqual(lane["endpoint"], "opencode")
        self.assertNotIn("opencode", [x["name"] for x in self.cfg["supervisorFallbacks"]])


class TestOpenCodeProbe(DGTest):
    """Verified live: OpenCode Go returns HTTP 401 for *both* a bad key and a
    correctly authenticated key on a workspace with no payment method, so the
    probe must read the error body, not just the status code."""

    def test_credits_error_is_not_reported_as_a_bad_key(self):
        endpoint = self.cfg["workers"]["endpoints"]["opencode"]
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "secret"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=_http_error(401, {"error": {
                               "type": "CreditsError",
                               "message": "No payment method."}})):
            ok, detail = launcher._probe_opencode(endpoint)
        self.assertFalse(ok)
        self.assertNotIn("not authorised", detail)
        self.assertIn("No payment method", detail)

    def test_bad_key_is_reported_as_not_authorised(self):
        endpoint = self.cfg["workers"]["endpoints"]["opencode"]
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "wrong"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=_http_error(401, {"error": {
                               "type": "AuthError", "message": "Missing API key."}})):
            ok, detail = launcher._probe_opencode(endpoint)
        self.assertFalse(ok)
        self.assertIn("not authorised", detail)

    def test_probe_requires_key_without_network(self):
        endpoint = self.cfg["workers"]["endpoints"]["opencode"]
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(launcher.Path, "expanduser", side_effect=OSError):
            ok, detail = launcher._probe_opencode(endpoint)
        self.assertFalse(ok)
        self.assertIn("OPENCODE_GO_API_KEY", detail)

    def test_ready_key_is_reported_usable(self):
        endpoint = self.cfg["workers"]["endpoints"]["opencode"]
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "secret"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=_Response({"type": "error", "error": {}})):
            ok, detail = launcher._probe_opencode(endpoint)
        self.assertTrue(ok)
