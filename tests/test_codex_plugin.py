"""Codex plugin transport and atomic multi-machine reservations."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from base import DGTest

from dg import gitutil, store
from dg.workers import codex_plugin
from dg.workers import cc_delegate


def make_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "seed.txt").write_text("seed\n", "utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True)
    return path


class TestAtomicReservations(DGTest):
    def task_at(self, title, path):
        return store.create_task(self.con, title=title, repo="/repo", paths=[path])

    def test_codex_station_and_oracle_reserve_concurrently(self):
        ids = [self.task_at("hard", "core/**"), self.task_at("simple", "web/**"),
               self.task_at("tiny", "fixtures/**")]
        choices = [("codex", "codex", None),
                   ("station", "cc-delegate", "station-main"),
                   ("oracle", "cc-delegate", "oracle-coder")]
        for tid, (lane, worker, profile) in zip(ids, choices):
            out = store.reserve_attempt(self.con, tid, "s", worker, lane, self.cfg,
                                        profile=profile, transport="test")
            self.assertTrue(out["ok"], out)
        self.assertEqual(
            {a["lane"] for a in store.running_attempts(self.con)},
            {"codex", "station", "oracle"})

    def test_path_conflict_is_checked_inside_reservation_transaction(self):
        a, b = self.task_at("a", "src/auth/**"), self.task_at("b", "src/auth/token.py")
        self.assertTrue(store.reserve_attempt(
            self.con, a, "s", "codex", "codex", self.cfg)["ok"])
        out = store.reserve_attempt(self.con, b, "s", "cc-delegate", "station", self.cfg)
        self.assertFalse(out["ok"])
        self.assertIn("path conflict", out["reason"])

    def test_release_only_releases_unattached_reservation(self):
        tid = self.task_at("a", "a/**")
        out = store.reserve_attempt(self.con, tid, "s", "codex", "codex", self.cfg)
        self.assertTrue(store.release_reservation(self.con, tid))
        self.assertEqual(store.get_task(self.con, tid)["status"], "PLANNED")
        self.assertEqual(store.attempts_for(self.con, tid)[0]["status"], "CANCELLED")


class TestFallback(DGTest):
    def test_preserves_contract_and_rewires_dependant(self):
        base = "abc123"
        original = store.create_task(self.con, "work", repo="/repo", paths=["src/**"],
                                     base_commit=base, task_class="hard", priority=7)
        dependant = store.create_task(self.con, "next", repo="/repo",
                                      depends_on=[original], paths=["docs/**"])
        store.set_status(self.con, original, "QUOTA_FAILED")
        replacement = store.create_fallback(self.con, store.get_task(self.con, original))
        retry = store.get_task(self.con, replacement)
        self.assertEqual(retry["baseCommit"], base)
        self.assertEqual(retry["taskClass"], "hard")
        self.assertEqual(retry["retryOf"], original)
        self.assertEqual(store.get_task(self.con, original)["supersededBy"], replacement)
        self.assertEqual(store.get_task(self.con, dependant)["dependsOn"], [replacement])


class TestCodexPluginWorker(DGTest):
    def setUp(self):
        super().setUp()
        self.plugin = self.home / "plugin"
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / ".claude-plugin").mkdir()
        (self.plugin / "scripts" / "codex-companion.mjs").write_text("// fake\n", "utf-8")
        (self.plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"version": "1.0.4"}), "utf-8")
        os.environ["DG_CODEX_PLUGIN_ROOT"] = str(self.plugin)

    def tearDown(self):
        os.environ.pop("DG_CODEX_PLUGIN_ROOT", None)
        super().tearDown()

    def test_dispatch_uses_plugin_runtime_and_governor_worktree(self):
        repo = make_repo(self.home / "repo")
        tid = store.create_task(self.con, "edit", repo=str(repo), paths=["src/**"])
        reserved = store.reserve_attempt(
            self.con, tid, "session", "codex", "codex", self.cfg,
            transport="codex-plugin")
        seen = []

        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps({"jobId": "task-test", "status": "queued",
                                 "logFile": "plugin.log"})

        old = codex_plugin._run
        codex_plugin._run = lambda cmd, timeout, env=None: (seen.append((cmd, env)), Result())[1]
        try:
            out = codex_plugin.dispatch(self.con, store.get_task(self.con, tid),
                                        "Implement the bounded change.", reserved["attemptId"])
        finally:
            codex_plugin._run = old
        self.assertTrue(out["ok"], out)
        cmd, env = seen[0]
        self.assertIn("--background", cmd)
        self.assertIn("--write", cmd)
        self.assertEqual(env["CLAUDE_PLUGIN_DATA"], str(codex_plugin._plugin_data()))
        cwd = cmd[cmd.index("--cwd") + 1]
        self.assertNotEqual(Path(cwd).resolve(), repo.resolve())
        attempt = next(a for a in store.running_attempts(self.con) if a["task_id"] == tid)
        self.assertEqual(attempt["external_job_id"], "task-test")
        self.assertEqual(attempt["transport"], "codex-plugin")

    def test_completed_plugin_job_reconciles_to_succeeded(self):
        claude = self.home / "claude"
        os.environ["CLAUDE_CONFIG_DIR"] = str(claude)
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        jobs = claude / "plugins" / "data" / "codex-openai-codex" / "state" / "x" / "jobs"
        jobs.mkdir(parents=True)
        tid = store.create_task(self.con, "read", mode="READ_ONLY", repo=str(self.home))
        reserved = store.reserve_attempt(self.con, tid, "s", "codex", "codex", self.cfg)
        store.activate_attempt(self.con, reserved["attemptId"], "codex-plugin:task-done",
                               external_job_id="task-done")
        (jobs / "task-done.json").write_text(json.dumps({
            "id": "task-done", "status": "completed", "summary": "done",
            "result": {"status": 0, "rawOutput": "done", "touchedFiles": []}}), "utf-8")
        attempt = store.live_attempt(self.con, tid)
        self.assertEqual(codex_plugin.sync(self.con, attempt), "SUCCEEDED")
        self.assertEqual(store.get_task(self.con, tid)["status"], "SUCCEEDED")

    def test_direct_companion_temp_state_is_recoverable(self):
        import tempfile
        jobs = Path(tempfile.gettempdir()) / "codex-companion" / "repo-hash" / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        path = jobs / "task-temp-layout.json"
        path.write_text(json.dumps({"id": "task-temp-layout", "status": "queued"}),
                        "utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(codex_plugin.find_job_file("task-temp-layout"), path)

    def test_failure_classification_ignores_words_in_original_prompt(self):
        data = codex_plugin._plugin_data() / "state" / "repo-hash" / "jobs"
        data.mkdir(parents=True)
        tid = store.create_task(self.con, "read", mode="READ_ONLY", repo=str(self.home))
        reserved = store.reserve_attempt(self.con, tid, "s", "codex", "codex", self.cfg)
        store.activate_attempt(self.con, reserved["attemptId"], "codex-plugin:task-fail",
                               external_job_id="task-fail")
        (data / "task-fail.json").write_text(json.dumps({
            "id": "task-fail", "status": "failed",
            "request": {"prompt": "Improve the quota documentation"},
            "errorMessage": "worker process exited unexpectedly",
            "result": {"status": 1}}), "utf-8")
        attempt = store.live_attempt(self.con, tid)
        self.assertEqual(codex_plugin.sync(self.con, attempt), "FAILED")


class TestCcDelegateHandoff(DGTest):
    def test_late_job_file_populates_worktree_for_integration(self):
        repo = make_repo(self.home / "cc-repo")
        tid = store.create_task(self.con, "edit", repo=str(repo), paths=["src/**"])
        reserved = store.reserve_attempt(
            self.con, tid, "s", "cc-delegate", "station", self.cfg,
            profile="station-main", transport="cc-delegate-mcp")
        out = cc_delegate.attach(self.con, store.get_task(self.con, tid), "cc-late", "order",
                                 attempt_id=reserved["attemptId"])
        self.assertTrue(out["ok"])
        jobs = repo / ".cc-delegate" / "jobs"
        jobs.mkdir(parents=True)
        worker = repo / ".cc-delegate" / "worktrees" / "cc-late"
        (jobs / "cc-late.json").write_text(json.dumps({
            "taskId": "cc-late", "status": "succeeded", "worktree": str(worker),
            "branch": "delegate/cc-late"}), "utf-8")
        attempt = next(a for a in store.running_attempts(self.con) if a["task_id"] == tid)
        self.assertEqual(cc_delegate.sync(self.con, attempt), "SUCCEEDED")
        finished = store.attempts_for(self.con, tid)[0]
        self.assertEqual(finished["worktree"], str(worker))
        self.assertEqual(finished["branch"], "delegate/cc-late")
