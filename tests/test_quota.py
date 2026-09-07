"""Quota parsing and both state machines, driven entirely from fixtures.

No real quota is ever consumed here (spec 70/87).
"""
from __future__ import annotations

import time

from base import DGTest

from dg import quota_codex, routing, store, supervisor


def win(used, resets=None, mins=300):
    return {"usedPercent": used, "windowDurationMins": mins, "resetsAt": resets}


def snap(primary=None, secondary=None, limit_id="codex", **kw):
    d = {"limitId": limit_id, "limitName": None, "primary": primary, "secondary": secondary,
         "credits": None, "individualLimit": None, "spendControlReached": False,
         "planType": "plus", "rateLimitReachedType": None}
    d.update(kw)
    return d


def payload(primary=None, secondary=None, by_id=None, **kw):
    base = snap(primary, secondary, **kw)
    return {"rateLimits": base, "rateLimitsByLimitId": by_id or {"codex": base}}


class TestCodexQuotaParsing(DGTest):
    def test_ready(self):
        ev = quota_codex.evaluate(payload(win(0), win(19)), self.cfg)
        self.assertEqual(ev["state"], quota_codex.READY)
        self.assertEqual(len(ev["windows"]), 2)

    def test_primary_exhausted(self):
        ev = quota_codex.evaluate(payload(win(100, 1800), win(19)), self.cfg)
        self.assertEqual(ev["state"], quota_codex.EXHAUSTED)
        self.assertEqual(ev["unavailableUntil"], 1800)

    def test_weekly_exhausted(self):
        ev = quota_codex.evaluate(payload(win(3), win(100, 99000, 10080)), self.cfg)
        self.assertEqual(ev["state"], quota_codex.EXHAUSTED)
        self.assertEqual(ev["unavailableUntil"], 99000)

    def test_multiple_windows_wait_for_the_latest(self):
        """Unavailable until *every* exhausted window has recovered (spec 7)."""
        ev = quota_codex.evaluate(payload(win(100, 1800), win(100, 99000, 10080)), self.cfg)
        self.assertEqual(ev["unavailableUntil"], 99000)

    def test_window_absent_is_unknown_not_available(self):
        ev = quota_codex.evaluate(payload(None, None), self.cfg)
        self.assertEqual(ev["state"], quota_codex.UNKNOWN)

    def test_partial_window_absent_still_usable(self):
        ev = quota_codex.evaluate(payload(win(5), None), self.cfg)
        self.assertEqual(ev["state"], quota_codex.READY)

    def test_non_gating_bucket_never_blocks(self):
        """gpt-reserve is reported but does not gate `codex exec`."""
        data = payload(win(2), win(3))
        data["rateLimitsByLimitId"]["base_model_inference"] = snap(
            win(100, 1800), None, limit_id="base_model_inference")
        ev = quota_codex.evaluate(data, self.cfg)
        self.assertEqual(ev["state"], quota_codex.READY)
        self.assertTrue(any(not w["gating"] for w in ev["windows"]))

    def test_rate_limit_reached_type_forces_exhausted(self):
        ev = quota_codex.evaluate(
            payload(win(40), win(3), rateLimitReachedType="rate_limit_reached"), self.cfg)
        self.assertEqual(ev["state"], quota_codex.EXHAUSTED)

    def test_spend_control_forces_exhausted(self):
        ev = quota_codex.evaluate(payload(win(1), win(1), spendControlReached=True), self.cfg)
        self.assertEqual(ev["state"], quota_codex.EXHAUSTED)

    def test_malformed_payload_is_unknown(self):
        for bad in ({}, {"rateLimits": None}, {"rateLimits": {"primary": "nope"}},
                    {"rateLimits": {"primary": {"usedPercent": None}}}):
            with self.subTest(bad=bad):
                self.assertEqual(quota_codex.evaluate(bad, self.cfg)["state"],
                                 quota_codex.UNKNOWN)

    def test_unknown_optional_fields_are_tolerated(self):
        data = payload(win(1), win(1))
        data["rateLimits"]["someFutureField"] = {"nested": True}
        data["brandNewTopLevel"] = 42
        self.assertEqual(quota_codex.evaluate(data, self.cfg)["state"], quota_codex.READY)

    def test_reset_credits_are_reported_never_consumed(self):
        data = payload(win(100, 1800), win(2))
        data["rateLimitResetCredits"] = {"availableCount": 2, "credits": []}
        ev = quota_codex.evaluate(data, self.cfg)
        self.assertEqual(ev["resetCreditsAvailable"], 2)
        self.assertEqual(ev["state"], quota_codex.EXHAUSTED)

    def test_exhausted_threshold_is_configurable(self):
        self.cfg["codex"]["exhaustedPercent"] = 50
        self.assertEqual(quota_codex.evaluate(payload(win(60), win(1)), self.cfg)["state"],
                         quota_codex.EXHAUSTED)

    def test_error_classification(self):
        cases = {
            "401 Unauthorized": quota_codex.AUTH_ERROR,
            "failed to connect: dns error": quota_codex.NETWORK_ERROR,
            "429 rate limit reached": quota_codex.EXHAUSTED,
            "something else entirely": quota_codex.UNKNOWN,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(quota_codex._classify(text), want)


class TestCodexStateMachine(DGTest):
    def _set(self, state, until=0, quota=None):
        store.kv_set(self.con, quota_codex.K_STATE, state)
        store.kv_set(self.con, quota_codex.K_UNTIL, until)
        store.kv_set(self.con, quota_codex.K_QUOTA, quota or {"windows": []})

    def test_known_exhausted_short_circuits_without_probing(self):
        """The whole point of the cache: no app-server spawn, no Codex request."""
        self._set(quota_codex.EXHAUSTED, time.time() + 3600)
        quota_codex.read_rate_limits = lambda *a, **k: self.fail("must not probe")
        out = quota_codex.refresh(self.con, self.cfg)
        self.assertEqual(out["state"], quota_codex.EXHAUSTED)
        self.assertTrue(out["cached"])

    def test_reset_passed_moves_to_probe_and_confirms(self):
        self._set(quota_codex.EXHAUSTED, time.time() - 10)
        quota_codex.read_rate_limits = lambda *a, **k: {
            "ok": True, "data": payload(win(0), win(5))}
        out = quota_codex.refresh(self.con, self.cfg)
        self.assertEqual(out["state"], quota_codex.READY)

    def test_reset_passed_but_still_limited_stays_exhausted(self):
        """A timestamp passing is not recovery -- only a confirmation is."""
        self._set(quota_codex.EXHAUSTED, time.time() - 10)
        quota_codex.read_rate_limits = lambda *a, **k: {
            "ok": True, "data": payload(win(100, int(time.time()) + 7200), win(5))}
        out = quota_codex.refresh(self.con, self.cfg)
        self.assertEqual(out["state"], quota_codex.EXHAUSTED)

    def test_network_error_sets_cooldown(self):
        quota_codex.read_rate_limits = lambda *a, **k: {
            "ok": False, "errorKind": quota_codex.NETWORK_ERROR, "detail": "down"}
        out = quota_codex.refresh(self.con, self.cfg, force=True)
        self.assertEqual(out["state"], quota_codex.NETWORK_ERROR)
        self.assertGreater(store.kv_get(self.con, quota_codex.K_COOLDOWN), time.time())

    def test_cooldown_prevents_retry_before_every_task(self):
        store.kv_set(self.con, quota_codex.K_COOLDOWN, time.time() + 600)
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.NETWORK_ERROR)
        quota_codex.read_rate_limits = lambda *a, **k: self.fail("must not retry in cooldown")
        self.assertEqual(quota_codex.refresh(self.con, self.cfg)["reason"], "cooldown")

    def test_auth_error_sets_longer_cooldown(self):
        quota_codex.read_rate_limits = lambda *a, **k: {
            "ok": False, "errorKind": quota_codex.AUTH_ERROR, "detail": "expired"}
        quota_codex.refresh(self.con, self.cfg, force=True)
        self.assertGreater(store.kv_get(self.con, quota_codex.K_COOLDOWN),
                           time.time() + self.cfg["codex"]["networkCooldownSeconds"])

    def test_fresh_cache_is_reused(self):
        self._set(quota_codex.READY)
        store.kv_set(self.con, quota_codex.K_QUOTA, {"windows": []})
        quota_codex.read_rate_limits = lambda *a, **k: self.fail("cache should have served")
        self.assertTrue(quota_codex.refresh(self.con, self.cfg)["cached"])

    def test_mark_exhausted_keeps_the_latest_reset(self):
        quota_codex.mark_exhausted(self.con, 1000, "first")
        quota_codex.mark_exhausted(self.con, 500, "second")
        self.assertEqual(store.kv_get(self.con, quota_codex.K_UNTIL), 1000)
        self.assertEqual(store.kv_get(self.con, quota_codex.K_STATE), quota_codex.EXHAUSTED)


class TestWorkerRouting(DGTest):
    def _codex(self, state, until=0):
        store.kv_set(self.con, quota_codex.K_STATE, state)
        store.kv_set(self.con, quota_codex.K_UNTIL, until)

    def test_ready_routes_to_codex(self):
        self._codex(quota_codex.READY)
        self.assertEqual(routing.select(self.con, self.cfg, refresh=False)["worker"], "codex")

    def test_every_unavailable_state_falls_back(self):
        for state in (quota_codex.EXHAUSTED, quota_codex.AUTH_ERROR,
                      quota_codex.NETWORK_ERROR, quota_codex.CLI_MISSING,
                      quota_codex.PROTOCOL_ERROR, quota_codex.UNKNOWN):
            with self.subTest(state=state):
                self._codex(state)
                self.assertEqual(routing.select(self.con, self.cfg, refresh=False)["worker"],
                                 "cc-delegate")

    def test_exhausted_reports_reset_time(self):
        until = int(time.time()) + 1800
        self._codex(quota_codex.EXHAUSTED, until)
        out = routing.select(self.con, self.cfg, refresh=False)
        self.assertEqual(out["codexResetsAt"], until)
        self.assertGreater(out["codexResetsIn"], 0)

    def test_recovery_routes_back_to_codex(self):
        self._codex(quota_codex.EXHAUSTED, time.time() - 1)
        quota_codex.read_rate_limits = lambda *a, **k: {
            "ok": True, "data": payload(win(0), win(1))}
        self.assertEqual(routing.select(self.con, self.cfg)["worker"], "codex")

    def test_manual_overrides(self):
        self._codex(quota_codex.EXHAUSTED, time.time() + 9999)
        self.cfg["overrides"]["worker"] = "codex"
        self.assertEqual(routing.select(self.con, self.cfg, refresh=False)["worker"], "codex")
        self.cfg["overrides"]["worker"] = "cc-delegate"
        self._codex(quota_codex.READY)
        self.assertEqual(routing.select(self.con, self.cfg, refresh=False)["worker"],
                         "cc-delegate")


class TestSupervisor(DGTest):
    def sl(self, five=None, seven=None):
        """Percentages in, fractions on the wire -- `utilization` is 0..1."""
        rl = {}
        if five is not None:
            rl["five_hour"] = {"utilization": five / 100.0,
                               "resets_at": int(time.time()) + 3600}
        if seven is not None:
            rl["seven_day"] = {"utilization": seven / 100.0,
                               "resets_at": int(time.time()) + 86400}
        return supervisor.ingest_statusline(self.con, {"rate_limits": rl})

    def test_no_rate_limits_is_normal(self):
        supervisor.ingest_statusline(self.con, {})
        out = supervisor.evaluate(self.con, self.cfg)
        self.assertEqual(out["state"], supervisor.NORMAL)
        self.assertIsNone(out["fiveHour"])

    def test_normal_quota(self):
        self.sl(10, 20)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.NORMAL)

    def test_save_threshold(self):
        self.sl(75, 20)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.SAVE)

    def test_local_threshold(self):
        self.sl(95, 20)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.LOCAL)

    def test_seven_day_thresholds_apply_independently(self):
        self.sl(1, 85)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.SAVE)
        self.sl(1, 96)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.LOCAL)

    def test_one_window_absent_uses_the_other(self):
        self.sl(None, 85)
        out = supervisor.evaluate(self.con, self.cfg)
        self.assertEqual(out["state"], supervisor.SAVE)
        self.assertIsNone(out["fiveHour"])

    def test_absent_window_is_not_zero_percent(self):
        self.sl(96, None)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.LOCAL)

    def test_used_percentage_spelling_is_accepted(self):
        """Defensive: the older spelling is a percentage and stays unscaled."""
        supervisor.ingest_statusline(self.con, {"rate_limits": {
            "five_hour": {"used_percentage": 93, "resets_at": int(time.time()) + 60}}})
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.LOCAL)

    def test_utilization_is_a_fraction_not_a_percentage(self):
        """Regression: comparing the raw 0..1 fraction against percentage
        thresholds silently disabled the entire supervisor."""
        supervisor.ingest_statusline(self.con, {"rate_limits": {
            "five_hour": {"utilization": 0.95, "resets_at": int(time.time()) + 60}}})
        out = supervisor.evaluate(self.con, self.cfg)
        self.assertAlmostEqual(out["fiveHour"]["usedPercent"], 95.0)
        self.assertEqual(out["state"], supervisor.LOCAL)

    def test_a_nearly_empty_window_is_not_mistaken_for_exhaustion(self):
        supervisor.ingest_statusline(self.con, {"rate_limits": {
            "five_hour": {"utilization": 0.07, "resets_at": int(time.time()) + 60}}})
        out = supervisor.evaluate(self.con, self.cfg)
        self.assertAlmostEqual(out["fiveHour"]["usedPercent"], 7.0)
        self.assertEqual(out["state"], supervisor.NORMAL)

    def test_empty_payload_does_not_erase_known_quota(self):
        self.sl(75, 20)
        supervisor.ingest_statusline(self.con, {"rate_limits": {}})
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.SAVE)

    def test_hard_limit_forces_local(self):
        self.sl(10, 10)
        supervisor.record_hard_limit(self.con, "rate_limit", "429")
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.LOCAL)

    def test_hard_limit_moves_to_probe_after_reset(self):
        supervisor.ingest_statusline(self.con, {"rate_limits": {
            "five_hour": {"utilization": 99, "resets_at": int(time.time()) - 5}}})
        supervisor.record_hard_limit(self.con, "rate_limit")
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.PROBE)

    def test_clearing_hard_limit_returns_to_thresholds(self):
        self.sl(10, 10)
        supervisor.record_hard_limit(self.con, "rate_limit")
        supervisor.clear_hard_limit(self.con)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.NORMAL)

    def test_stale_quota_holds_last_state(self):
        self.sl(75, 20)
        supervisor.evaluate(self.con, self.cfg)
        q = store.kv_get(self.con, supervisor.K_QUOTA)
        q["at"] = time.time() - 99999
        store.kv_set(self.con, supervisor.K_QUOTA, q)
        out = supervisor.evaluate(self.con, self.cfg)
        self.assertEqual(out["state"], supervisor.SAVE)
        self.assertIn("stale", out["reason"])

    def test_thresholds_come_from_config(self):
        self.cfg["supervisor"]["fiveHour"] = {"save": 10, "local": 20}
        self.sl(15, 0)
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.SAVE)

    def test_overrides(self):
        self.sl(99, 99)
        self.cfg["overrides"]["supervisor"] = "claude"
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.NORMAL)
        self.sl(1, 1)
        self.cfg["overrides"]["supervisor"] = "local"
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.LOCAL)


class TestRecoveryIsNoticed(DGTest):
    """Regression: hooks read state with refresh=False to stay cheap, which
    froze EXHAUSTED past its own reset -- Codex was never used again."""

    def test_recovery_due_only_after_the_reset(self):
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.EXHAUSTED)
        store.kv_set(self.con, quota_codex.K_UNTIL, time.time() + 600)
        self.assertFalse(quota_codex.recovery_due(self.con))
        store.kv_set(self.con, quota_codex.K_UNTIL, time.time() - 1)
        self.assertTrue(quota_codex.recovery_due(self.con))

    def test_not_due_when_healthy(self):
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.READY)
        store.kv_set(self.con, quota_codex.K_UNTIL, time.time() - 600)
        self.assertFalse(quota_codex.recovery_due(self.con))

    def test_cheap_read_still_probes_once_the_reset_passed(self):
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.EXHAUSTED)
        store.kv_set(self.con, quota_codex.K_UNTIL, time.time() - 1)
        quota_codex.read_rate_limits = lambda *a, **k: {
            "ok": True, "data": payload(win(0), win(5))}
        out = routing.select(self.con, self.cfg, refresh=False)
        self.assertEqual(out["worker"], "codex")

    def test_cheap_read_stays_cheap_before_the_reset(self):
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.EXHAUSTED)
        store.kv_set(self.con, quota_codex.K_UNTIL, time.time() + 600)
        quota_codex.read_rate_limits = lambda *a, **k: self.fail("probed too early")
        self.assertEqual(routing.select(self.con, self.cfg, refresh=False)["worker"],
                         "cc-delegate")
