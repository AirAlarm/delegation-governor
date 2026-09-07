"""The vendored cc-delegate station patch: drift detection and re-apply.

A `claude plugin update` replaces the whole install directory, silently
reverting the gate to a 32768 context and breaking the station lane. These
tests pin the detection that makes that visible instead of mysterious.
"""
from __future__ import annotations

import json

from base import DGTest

from dg import ccdelegate


class TestVendoredFiles(DGTest):
    def test_the_patch_and_gate_ship_with_the_package(self):
        self.assertTrue(ccdelegate.PATCH.exists(), "station_patch.py not vendored")
        self.assertTrue(ccdelegate.GATE.exists(), "station_lmstudio_gate.py not vendored")

    def test_the_shipped_gate_carries_the_tuned_values(self):
        g = ccdelegate.gate_settings()
        self.assertEqual(g["CONTEXT_LENGTH"], "65536")
        self.assertEqual(g["MODEL_TTL_S"], "14400")
        self.assertGreaterEqual(int(g["LOAD_TIMEOUT_S"]), 600)

    def test_the_gate_reads_its_ttl_from_the_constant(self):
        """Regression: the TTL used to be hardcoded in the load call, so
        changing MODEL_TTL_S had no effect."""
        src = ccdelegate.GATE.read_text("utf-8")
        self.assertIn('"--ttl", str(MODEL_TTL_S)', src)
        self.assertNotIn('"--ttl", "3600"', src)

    def test_the_gate_settles_before_polling(self):
        """Weights are still mapping when `lms load` returns."""
        src = ccdelegate.GATE.read_text("utf-8")
        self.assertIn("LOAD_SETTLE_S", src)


class TestDetection(DGTest):
    def _fake_plugin(self, gate_bytes: bytes):
        pdir = self.home / "plugins" / "cc-delegate" / "0.12.0"
        (pdir / "server").mkdir(parents=True, exist_ok=True)
        (pdir / "server" / "lmstudio_gate.py").write_bytes(gate_bytes)
        ccdelegate.plugin_dir = lambda: pdir
        return pdir

    def test_absent_plugin_is_not_a_failure(self):
        ccdelegate.plugin_dir = lambda: None
        out = ccdelegate.check()
        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "absent")

    def test_matching_gate_and_clean_patch_is_ok(self):
        self._fake_plugin(ccdelegate.GATE.read_bytes())
        ccdelegate._run = lambda args, timeout=120: type(
            "R", (), {"returncode": 0, "stdout": "patched", "stderr": ""})()
        self.assertTrue(ccdelegate.check()["ok"])

    def test_a_reverted_gate_is_reported_as_drift(self):
        """The exact failure a plugin update causes."""
        self._fake_plugin(b"CONTEXT_LENGTH = 32768\n")
        ccdelegate._run = lambda args, timeout=120: type(
            "R", (), {"returncode": 0, "stdout": "patched", "stderr": ""})()
        out = ccdelegate.check()
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "drifted")
        self.assertIn("dg ccdelegate --apply", out["detail"])

    def test_an_unpatched_install_is_reported(self):
        self._fake_plugin(ccdelegate.GATE.read_bytes())
        ccdelegate._run = lambda args, timeout=120: type(
            "R", (), {"returncode": 1, "stdout": "worker_launcher.py: NOT patched",
                      "stderr": ""})()
        out = ccdelegate.check()
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "unpatched")

    def test_apply_backs_up_what_it_replaces(self):
        pdir = self._fake_plugin(b"old gate\n")
        ccdelegate._run = lambda args, timeout=180: type(
            "R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
        out = ccdelegate.apply()
        self.assertTrue(out["ok"])
        self.assertTrue(out["backup"], "replaced gate was not backed up")
        self.assertIn("restart", out["note"])

    def test_apply_on_absent_plugin_is_a_no_op(self):
        ccdelegate.plugin_dir = lambda: None
        self.assertEqual(ccdelegate.apply()["state"], "absent")
