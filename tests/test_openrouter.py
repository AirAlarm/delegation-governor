"""OpenRouter is an independent, opt-in worker lane."""
from __future__ import annotations

import json
import os
from unittest import mock

from base import DGTest

from dg import config, launcher
from dg.workers import cc_delegate


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

    def test_v3_config_is_extended_without_losing_its_order(self):
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
        self.assertEqual(migrated["schemaVersion"], 4)
        self.assertEqual(migrated["workers"]["classRouting"]["hard"],
                         ["station", "openrouter", "codex"])
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
