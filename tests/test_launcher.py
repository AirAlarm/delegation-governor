"""`dg launch` lifecycle supervisor.

Claude Code is never actually started: a fake replaces subprocess.call and
records the argv/env of every launch, so the whole transition matrix is
deterministic and costs nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from base import DGTest

from dg import launcher, quota_codex, store, supervisor


class FakeClaude:
    """Stands in for the Claude Code process.

    `script` is one entry per expected launch: (exit_code, uptime_seconds,
    side_effect) where side_effect(con) runs while that 'session' is live --
    which is how we simulate the StopFailure hook firing mid-session.
    """

    def __init__(self, con, script, clock):
        self.con = con
        self.script = list(script)
        self.clock = clock          # single-element list: the fake wall clock
        self.launches: list[dict] = []

    def __call__(self, argv, env=None, **kw):
        rc, uptime, effect = self.script.pop(0) if self.script else (0, 100.0, None)
        self.launches.append({"argv": argv, "env": env or {}})
        if effect:
            effect(self.con)
        # The launcher measures uptime off the wall clock, so the scripted
        # session length only means anything if we advance that clock.
        self.clock[0] += uptime
        return rc

    # -- assertions ------------------------------------------------------
    def arg(self, n: int, flag: str):
        argv = self.launches[n]["argv"]
        return argv[argv.index(flag) + 1] if flag in argv else None

    def base_url(self, n: int):
        return self.launches[n]["env"].get("ANTHROPIC_BASE_URL")


class LauncherTest(DGTest):
    def setUp(self):
        super().setUp()
        self.cfg["supervisorFallbacks"] = [
            {"name": "lmstudio", "kind": "lmstudio", "baseUrl": "http://127.0.0.1:1234",
             "model": "local/main", "smallModel": "local/small",
             "tokenEnvVar": "LMSTUDIO_API_KEY", "contextLength": 131072,
             "minContextLength": 40960, "loadTimeoutSeconds": 600, "ttlSeconds": 3600,
             "autoLoad": True},
            {"name": "oracle", "kind": "remote", "baseUrl": "https://oracle.example",
             "model": "oracle/main", "smallModel": "oracle/small",
             "tokenEnvVar": "ORACLE_LLM_API_KEY"},
        ]
        # LM Studio is assumed healthy unless a test says otherwise. Model
        # residency is stubbed too: the real path shells out to `lms load`,
        # which would hang the suite on an actual multi-GB model load.
        launcher.probe_lmstudio = lambda cfg: (True, "fake ok")
        launcher.probe_lmstudio_messages = lambda cfg: (True, "fake ok")
        # Keep the real implementations reachable for the classes that test them.
        self.real = {n: getattr(launcher, n) for n in
                     ("ensure_local_model", "model_context", "local_contention",
                      "probe_tier", "first_usable_tier")}
        launcher.ensure_local_model = lambda cfg: (True, "fake loaded")
        launcher.model_context = lambda cfg: ("loaded", 131072, 131072)
        launcher.local_contention = lambda cfg: ""

    def unstub(self, *names):
        """Restore the real functions this test is actually exercising."""
        for n in names:
            setattr(launcher, n, self.real[n])
        supervisor.probe_anthropic = lambda cfg, timeout=20.0: {"ok": True, "reason": "fake"}
        import shutil
        launcher.shutil = type("S", (), {"which": staticmethod(lambda x: "/fake/claude")})()
        self._real_shutil = shutil

    def fake(self, script):
        clock = [time.time()]
        f = FakeClaude(self.con, script, clock)
        launcher.subprocess = type("P", (), {"call": staticmethod(f)})()
        launcher.time = type("T", (), {"time": staticmethod(lambda: clock[0]),
                                       "sleep": staticmethod(lambda s: None)})()
        return f

    def quota(self, five=None, seven=None, resets_in=3600):
        rl = {}
        if five is not None:
            rl["five_hour"] = {"utilization": five / 100.0,
                               "resets_at": int(time.time()) + resets_in}
        if seven is not None:
            rl["seven_day"] = {"utilization": seven / 100.0,
                               "resets_at": int(time.time()) + 86400}
        supervisor.ingest_statusline(self.con, {"rate_limits": rl})


class TestRouteDecision(LauncherTest):
    def test_normal_goes_to_anthropic(self):
        self.quota(10, 10)
        self.assertEqual(launcher.decide_route(self.con, self.cfg)["route"], launcher.ANTHROPIC)

    def test_save_still_goes_to_anthropic(self):
        """SAVE changes how Claude behaves, not which backend it talks to."""
        self.quota(75, 10)
        self.assertEqual(launcher.decide_route(self.con, self.cfg)["route"], launcher.ANTHROPIC)

    def test_local_threshold_goes_local(self):
        self.quota(95, 10)
        self.assertEqual(launcher.decide_route(self.con, self.cfg)["route"], launcher.LOCAL)

    def test_hard_limit_goes_local(self):
        self.quota(10, 10)
        supervisor.record_hard_limit(self.con, "rate_limit")
        self.assertEqual(launcher.decide_route(self.con, self.cfg)["route"], launcher.LOCAL)

    def test_probe_success_returns_to_anthropic(self):
        self.quota(99, 10, resets_in=-5)
        supervisor.record_hard_limit(self.con, "rate_limit")
        supervisor.probe_anthropic = lambda cfg, timeout=20.0: {"ok": True, "reason": "back"}
        out = launcher.decide_route(self.con, self.cfg)
        self.assertEqual(out["route"], launcher.ANTHROPIC)
        self.assertIsNone(store.kv_get(self.con, supervisor.K_HARD))

    def test_probe_failure_stays_local(self):
        """A timestamp passing is not recovery."""
        self.quota(99, 10, resets_in=-5)
        supervisor.record_hard_limit(self.con, "rate_limit")
        supervisor.probe_anthropic = lambda cfg, timeout=20.0: {
            "ok": False, "reason": "still limited"}
        out = launcher.decide_route(self.con, self.cfg)
        self.assertEqual(out["route"], launcher.LOCAL)
        self.assertIsNotNone(store.kv_get(self.con, supervisor.K_HARD))

    def test_overrides(self):
        self.quota(99, 99)
        self.cfg["overrides"]["supervisor"] = "claude"
        self.assertEqual(launcher.decide_route(self.con, self.cfg)["route"], launcher.ANTHROPIC)
        self.quota(1, 1)
        self.cfg["overrides"]["supervisor"] = "local"
        self.assertEqual(launcher.decide_route(self.con, self.cfg)["route"], launcher.LOCAL)


class TestEnvironment(LauncherTest):
    def test_anthropic_env_is_clean(self):
        env, model = launcher.env_for(launcher.ANTHROPIC, self.cfg,
                                      base={"ANTHROPIC_BASE_URL": "http://leftover",
                                            "ANTHROPIC_AUTH_TOKEN": "stale", "PATH": "/x"})
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["PATH"], "/x")
        self.assertEqual(model, "sonnet")

    def test_local_env_points_at_lm_studio(self):
        env, model = launcher.env_for(launcher.LOCAL, self.cfg, base={})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:1234")
        self.assertTrue(env["ANTHROPIC_AUTH_TOKEN"])
        self.assertEqual(model, "local/main")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "local/small")

    def test_no_credentials_are_invented(self):
        env, _ = launcher.env_for(launcher.LOCAL, self.cfg, base={})
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_argv_pins_session_then_resumes_it(self):
        first = launcher.build_argv("/c", launcher.ANTHROPIC, "sonnet", "SID", True, [])
        later = launcher.build_argv("/c", launcher.LOCAL, "local/model", "SID", False, [])
        self.assertIn("--session-id", first)
        self.assertEqual(first[first.index("--session-id") + 1], "SID")
        self.assertIn("--resume", later)
        self.assertEqual(later[later.index("--resume") + 1], "SID")

    def test_model_is_always_explicit(self):
        """A resumed session restores its saved model unless --model overrides it."""
        for first in (True, False):
            argv = launcher.build_argv("/c", launcher.LOCAL, "local/model", "SID", first, [])
            self.assertIn("--model", argv)
            self.assertEqual(argv[argv.index("--model") + 1], "local/model")

    def test_user_args_are_passed_through(self):
        argv = launcher.build_argv("/c", launcher.ANTHROPIC, "sonnet", "SID", True,
                                   ["--", "--permission-mode", "plan"])
        self.assertEqual(argv[-2:], ["--permission-mode", "plan"])


class TestLifecycle(LauncherTest):
    def test_hard_quota_relaunches_local_with_same_session(self):
        """Anthropic -> simulated hard quota -> LOCAL relaunch, session retained."""
        self.quota(10, 10)

        def hit_limit(con):
            supervisor.record_hard_limit(con, "rate_limit", "429")

        f = self.fake([(0, 100.0, hit_limit), (0, 100.0, None)])
        rc = launcher.run(self.cfg, [], max_restarts=5)

        self.assertEqual(rc, 0)
        self.assertEqual(len(f.launches), 2, "expected exactly one relaunch")
        # First launch: Anthropic, session pinned.
        self.assertIsNone(f.base_url(0))
        sid = f.arg(0, "--session-id")
        self.assertTrue(sid)
        # Second launch: LM Studio, same session resumed, local model explicit.
        self.assertEqual(f.base_url(1), "http://127.0.0.1:1234")
        self.assertEqual(f.arg(1, "--resume"), sid, "session id must be preserved")
        self.assertEqual(f.arg(1, "--model"), "local/main")

    def test_ledger_and_running_jobs_survive_the_restart(self):
        self.quota(10, 10)
        repo = str(self.home)
        a = self.store.create_task(self.con, title="running job", repo=repo, paths=["a/**"])
        b = self.store.create_task(self.con, title="planned", repo=repo, paths=["b/**"])
        store.set_status(self.con, a, "RUNNING")
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:survivor")
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.EXHAUSTED)

        f = self.fake([(0, 100.0, lambda con: supervisor.record_hard_limit(con, "rate_limit")),
                       (0, 100.0, None)])
        launcher.run(self.cfg, [], max_restarts=5)

        # A fresh connection proves the state is on disk, not in this process.
        con2 = store.connect()
        try:
            self.assertEqual(store.get_task(con2, a)["status"], "RUNNING")
            self.assertEqual(store.get_task(con2, b)["status"], "PLANNED")
            live = store.live_attempt(con2, a)
            self.assertIsNotNone(live, "a running worker attempt must survive the restart")
            self.assertEqual(live["id"], att)
            self.assertEqual(live["handle"], "cc:survivor")
            # Worker routing state survives too, so no Codex request is wasted.
            self.assertEqual(store.kv_get(con2, quota_codex.K_STATE), quota_codex.EXHAUSTED)
        finally:
            con2.close()

    def test_reset_relaunches_back_on_anthropic(self):
        self.quota(99, 10, resets_in=-5)
        supervisor.record_hard_limit(self.con, "rate_limit")
        calls = {"n": 0}

        def probe(cfg, timeout=20.0):
            calls["n"] += 1
            if calls["n"] <= 1:
                return {"ok": False, "reason": "still limited"}
            # A real recovery also brings back fresh, low utilisation numbers.
            self.quota(5, 10)
            return {"ok": True, "reason": "fake"}

        supervisor.probe_anthropic = probe
        f = self.fake([(0, 100.0, None), (0, 100.0, None)])
        launcher.run(self.cfg, [], max_restarts=5)

        self.assertEqual(len(f.launches), 2)
        self.assertEqual(f.base_url(0), "http://127.0.0.1:1234")  # started LOCAL
        self.assertIsNone(f.base_url(1))                          # returned to Anthropic
        self.assertEqual(f.arg(1, "--resume"), f.arg(0, "--session-id"))
        self.assertEqual(f.arg(1, "--model"), "sonnet")

    def test_user_quitting_does_not_relaunch(self):
        self.quota(10, 10)
        f = self.fake([(0, 500.0, None)])
        rc = launcher.run(self.cfg, [], max_restarts=5)
        self.assertEqual(rc, 0)
        self.assertEqual(len(f.launches), 1, "a plain quit must not be treated as a switch")

    def test_quitting_after_switch_does_not_relaunch_again(self):
        self.quota(10, 10)
        f = self.fake([(0, 100.0, lambda con: supervisor.record_hard_limit(con, "rate_limit")),
                       (0, 100.0, None)])
        launcher.run(self.cfg, [], max_restarts=5)
        self.assertEqual(len(f.launches), 2)

    def test_dead_local_endpoint_gives_a_recovery_path_not_a_loop(self):
        self.quota(99, 10)
        launcher.probe_tier = lambda t: (False, f"{t['name']}: connection refused")
        f = self.fake([(0, 100.0, None)])
        rc = launcher.run(self.cfg, [], max_restarts=5)
        self.assertEqual(rc, 2)
        self.assertEqual(f.launches, [], "must not start Claude against a dead backend")

    def test_force_starts_on_anthropic_when_local_is_dead(self):
        self.quota(99, 10)
        launcher.probe_tier = lambda t: (False, f"{t['name']}: connection refused")
        f = self.fake([(0, 500.0, None)])
        rc = launcher.run(self.cfg, [], force=True, max_restarts=5)
        self.assertEqual(rc, 0)
        self.assertIsNone(f.base_url(0))

    def test_restart_loop_protection(self):
        """Sessions that die instantly while flapping must stop, not spin."""
        self.quota(10, 10)
        flip = {"local": False}

        def toggle(con):
            flip["local"] = not flip["local"]
            if flip["local"]:
                supervisor.record_hard_limit(con, "rate_limit")
            else:
                supervisor.clear_hard_limit(con)

        f = self.fake([(1, 0.0, toggle)] * 12)
        rc = launcher.run(self.cfg, [], max_restarts=50)
        self.assertNotEqual(rc, 0)
        self.assertLessEqual(len(f.launches), launcher.MAX_STRIKES + 1,
                             "restart-loop protection did not engage")

    def test_restart_budget_is_bounded(self):
        self.quota(10, 10)
        flip = {"local": False}

        def toggle(con):
            flip["local"] = not flip["local"]
            if flip["local"]:
                supervisor.record_hard_limit(con, "rate_limit")
            else:
                supervisor.clear_hard_limit(con)

        # Healthy uptime, so strikes never accrue: only the budget stops it.
        f = self.fake([(0, 500.0, toggle)] * 20)
        launcher.run(self.cfg, [], max_restarts=3)
        self.assertEqual(len(f.launches), 4)

    def test_launcher_state_is_recorded_for_doctor(self):
        self.quota(10, 10)
        self.fake([(0, 500.0, None)])
        launcher.run(self.cfg, [], max_restarts=1)
        lp = store.kv_get(self.con, "launcher")
        self.assertEqual(lp["route"], launcher.ANTHROPIC)
        self.assertTrue(lp["sessionId"])

    def test_dry_run_starts_nothing(self):
        self.quota(10, 10)
        f = self.fake([(0, 100.0, None)])
        self.assertEqual(launcher.run(self.cfg, [], dry_run=True), 0)
        self.assertEqual(f.launches, [])


class TestStockClaudeUnaffected(LauncherTest):
    def test_governor_never_shadows_the_claude_binary(self):
        """`dg launch` is additive: nothing installs a `claude` shim."""
        from dg import install
        self.assertNotIn("claude", [Path(p).name for p in _installed_commands(install)])

    def test_launcher_only_mutates_its_own_child_env(self):
        before = dict(os.environ)
        launcher.env_for(launcher.LOCAL, self.cfg)
        self.assertEqual(dict(os.environ), before,
                         "the parent environment must never be modified")


def _installed_commands(install) -> list[str]:
    cmds = [spec["command"] for spec in install.HOOKS_SPEC.values()]
    cmds.append(install.STATUSLINE["command"])
    return cmds




class TestHookOutput(LauncherTest):
    """A hook must never crash a session, and must render in any console."""

    def test_status_bar_is_ascii_only(self):
        """Windows consoles default to cp1252; a stray glyph raises mid-print."""
        import io
        import contextlib
        from dg import hooks, quota_codex
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.EXHAUSTED)
        store.kv_set(self.con, quota_codex.K_UNTIL, int(time.time()) + 3600)
        self.quota(12, 19)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.statusline()
        out = buf.getvalue()
        self.assertIn("Codex>", out)
        out.encode("cp1252")  # raises UnicodeEncodeError if a glyph slipped in
        out.encode("ascii")

    def test_statusline_survives_a_broken_database(self):
        from dg import hooks
        import io
        import contextlib
        hooks.store = type("S", (), {"connect": staticmethod(
            lambda: (_ for _ in ()).throw(RuntimeError("db gone")))})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hooks.statusline()
        self.assertEqual(rc, 0)
        self.assertIn("DG error", buf.getvalue())

    def test_prompt_hook_stays_silent_when_idle(self):
        from dg import hooks
        import io
        import contextlib
        self.quota(10, 10)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks.prompt()
        self.assertEqual(buf.getvalue().strip(), "")


class TestLocalModelResidency(LauncherTest):
    """Claude Code's system prompt measured ~34k tokens; a default-context
    model rejects the very first turn. Loading is part of going LOCAL."""

    def setUp(self):
        super().setUp()
        self.unstub("ensure_local_model", "first_usable_tier")

    def ctx(self, state, loaded, maximum=131072):
        launcher.model_context = lambda t: (state, loaded, maximum)

    def test_already_loaded_big_enough_does_not_reload(self):
        self.ctx("loaded", 131072)
        launcher.shutil = type("S", (), {"which": staticmethod(
            lambda x: self.fail("must not reload an adequate model"))})()
        ok, detail = launcher.ensure_local_model(self.cfg["supervisorFallbacks"][0])
        self.assertTrue(ok)
        self.assertIn("131072", detail)

    def test_loaded_too_small_is_reloaded(self):
        self.ctx("loaded", 26112)
        calls = []
        launcher.shutil = type("S", (), {"which": staticmethod(lambda x: "/fake/lms")})()
        launcher.subprocess = type("P", (), {
            "run": staticmethod(lambda cmd, **kw: calls.append(cmd) or
                                type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()),
            "TimeoutExpired": Exception})()
        # after the reload the API reports the bigger context
        seq = iter([("loaded", 26112, 131072), ("loaded", 131072, 131072)])
        launcher.model_context = lambda t: next(seq)
        ok, _ = launcher.ensure_local_model(self.cfg["supervisorFallbacks"][0])
        self.assertTrue(ok)
        self.assertIn("-c", calls[0])
        self.assertEqual(calls[0][calls[0].index("-c") + 1], "131072")

    def test_context_is_capped_at_the_model_maximum(self):
        self.cfg["supervisorFallbacks"][0]["contextLength"] = 999999
        seq = iter([("not-loaded", None, 131072), ("loaded", 131072, 131072)])
        launcher.model_context = lambda t: next(seq)
        calls = []
        launcher.shutil = type("S", (), {"which": staticmethod(lambda x: "/fake/lms")})()
        launcher.subprocess = type("P", (), {
            "run": staticmethod(lambda cmd, **kw: calls.append(cmd) or
                                type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()),
            "TimeoutExpired": Exception})()
        launcher.ensure_local_model(self.cfg["supervisorFallbacks"][0])
        self.assertEqual(calls[0][calls[0].index("-c") + 1], "131072")

    def test_load_failure_is_reported_not_swallowed(self):
        self.ctx("not-loaded", None)
        launcher.shutil = type("S", (), {"which": staticmethod(lambda x: "/fake/lms")})()
        launcher.subprocess = type("P", (), {
            "run": staticmethod(lambda cmd, **kw: type("R", (), {
                "returncode": 0, "stdout": "",
                "stderr": "Error: insufficient system resources"})()),
            "TimeoutExpired": Exception})()
        ok, detail = launcher.ensure_local_model(self.cfg["supervisorFallbacks"][0])
        self.assertFalse(ok)
        self.assertIn("insufficient system resources", detail)

    def test_autoload_off_refuses_rather_than_loading(self):
        self.cfg["supervisorFallbacks"][0]["autoLoad"] = False
        self.ctx("not-loaded", None)
        ok, detail = launcher.ensure_local_model(self.cfg["supervisorFallbacks"][0])
        self.assertFalse(ok)
        self.assertIn("autoLoad is off", detail)

    def test_launch_refuses_local_when_the_model_cannot_be_loaded(self):
        self.quota(99, 10)
        launcher.probe_tier = lambda t: (False, f"{t['name']}: out of memory")
        f = self.fake([(0, 100.0, None)])
        rc = launcher.run(self.cfg, [], max_restarts=2)
        self.assertEqual(rc, 2)
        self.assertEqual(f.launches, [], "must not start Claude with an unusable model")


class TestSharedModelSlot(LauncherTest):
    """One GPU, one resident model: the LOCAL supervisor and a station-*
    cc-delegate profile evict each other. Observed live as a cc-delegate
    model-gate timeout."""

    def setUp(self):
        super().setUp()
        self.unstub("local_contention", "first_usable_tier")

    def _cc_config(self, profiles):
        d = self.home / "cc"
        (d / ".cc-delegate").mkdir(parents=True, exist_ok=True)
        (d / ".cc-delegate" / "config.json").write_text(
            json.dumps({"profiles": profiles}), encoding="utf-8")
        launcher.Path = type("P", (), {"home": staticmethod(lambda: d)})()

    def test_shared_lm_studio_is_detected(self):
        self._cc_config({
            "station-main": {"api_base": "http://127.0.0.1:1234/v1"},
            "oracle-smart": {"api_base": "https://claude-llm.example.org"}})
        warn = launcher.local_contention(self.cfg)
        conflict, _, suggestion = warn.partition("share one LM Studio")
        # station-* contend for the slot; oracle-* is the way out, not a conflict
        self.assertIn("station-main", conflict)
        self.assertNotIn("oracle-smart", conflict)
        self.assertIn("oracle-smart", suggestion)

    def test_remote_only_profiles_are_not_a_conflict(self):
        self._cc_config({"oracle-fast": {"api_base": "https://claude-llm.example.org"}})
        self.assertEqual(launcher.local_contention(self.cfg), "")

    def test_missing_cc_delegate_config_is_silent(self):
        launcher.Path = type("P", (), {"home": staticmethod(lambda: self.home / "nope")})()
        self.assertEqual(launcher.local_contention(self.cfg), "")
