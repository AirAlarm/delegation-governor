"""Integration: fake workers, real worktrees, real temp repos. No quota spent.

The scheduler test in TestNonBlocking is a hard acceptance criterion (spec 71).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from base import DGTest

from dg import gitutil, quota_codex, routing, scheduler, store, sync
from dg.workers import cc_delegate
from dg.workers import codex as codex_worker


def make_repo() -> str:
    d = Path(tempfile.mkdtemp(prefix="dg-repo-"))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    for k, v in (("user.email", "dg@test"), ("user.name", "dg")):
        subprocess.run(["git", "-C", str(d), "config", k, v], check=True)
    (d / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "seed"], check=True)
    return str(d)


class TestNonBlocking(DGTest):
    """Spec 71: the Governor must never sit waiting on a slow worker."""

    def test_slow_worker_does_not_stall_the_scheduler(self):
        repo = make_repo()
        a = self.task("A slow delegated build", paths=["src/**"], repo=repo)
        b = self.task("B independent", paths=["docs/**"], repo=repo)
        c = self.task("C depends on A", paths=["tests/**"], repo=repo, deps=[a])
        d = self.task("D independent", mode="READ_ONLY", repo=repo)

        # A goes to a simulated slow worker and stays RUNNING for the whole test.
        store.set_status(self.con, a, "RUNNING")
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:slow-1")

        t0 = time.monotonic()
        rows = scheduler.evaluate(self.con, self.cfg)["tasks"]
        elapsed = time.monotonic() - t0
        states = {r["id"]: r["state"] for r in rows}

        # The scheduler answered immediately -- it did not wait on A.
        self.assertLess(elapsed, 1.0, "scheduler blocked while a worker was running")
        self.assertEqual(states[a], "RUNNING")
        self.assertEqual(states[c], "BLOCKED")
        ready_ids = [r["id"] for r in scheduler.ready(rows)]
        self.assertIn(b, ready_ids)
        self.assertIn(d, ready_ids)

        # Claude does B and D while A is still running.
        for t in (b, d):
            store.set_status(self.con, t, "SUCCEEDED")
        self.assertEqual(self.states()[a], "RUNNING")
        self.assertEqual(self.states()[c], "BLOCKED")

        # A finishes; C becomes READY with no further intervention.
        store.finish_attempt(self.con, att, "SUCCEEDED")
        store.set_status(self.con, a, "SUCCEEDED")
        self.assertEqual(self.states()[c], "READY")

    def test_slow_is_not_failed(self):
        """A worker past the soft threshold is flagged, never killed (spec 27/28)."""
        repo = make_repo()
        a = self.task("A", repo=repo)
        store.set_status(self.con, a, "RUNNING")
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:cold-start")
        self.con.execute("UPDATE attempts SET started_at=? WHERE id=?",
                         (time.time() - 5000, att))
        self.cfg["workers"]["hardTimeoutSeconds"] = 0
        out = sync.reconcile(self.con, self.cfg)
        self.assertIn(a, out["slow"])
        self.assertEqual(store.get_task(self.con, a)["status"], "RUNNING")

    def test_hard_timeout_is_opt_in(self):
        repo = make_repo()
        a = self.task("A", repo=repo)
        store.set_status(self.con, a, "RUNNING")
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:hung")
        self.con.execute("UPDATE attempts SET started_at=? WHERE id=?",
                         (time.time() - 5000, att))
        self.cfg["workers"]["hardTimeoutSeconds"] = 60
        sync.reconcile(self.con, self.cfg)
        self.assertEqual(store.get_task(self.con, a)["status"], "FAILED")
        self.assertEqual(store.attempts_for(self.con, a)[0]["error_kind"], "worker timeout")

    def test_min_check_spacing_avoids_busy_polling(self):
        repo = make_repo()
        a = self.task("A", repo=repo)
        store.set_status(self.con, a, "RUNNING")
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:x")
        store.touch_attempt(self.con, att)
        calls = []
        original = cc_delegate.sync
        cc_delegate.sync = lambda *a, **k: calls.append(1)
        try:
            sync.reconcile(self.con, self.cfg)
            self.assertEqual(calls, [], "re-checked inside the minimum spacing window")
        finally:
            cc_delegate.sync = original


class TestMultipleWorkers(DGTest):
    """Spec 72: Codex, cc-delegate and Claude in flight at once."""

    def test_three_independent_tasks_across_three_owners(self):
        repo = make_repo()
        a = self.task("codex work", paths=["src/api/**"], repo=repo)
        b = self.task("cc-delegate work", paths=["src/web/**"], repo=repo)
        c = self.task("claude work", mode="READ_ONLY", repo=repo)
        self.cfg["workers"]["totalWriteJobsPerRepo"] = 2

        for t, worker in ((a, "codex"), (b, "cc-delegate")):
            self.assertTrue(store.claim(self.con, t, "s1", worker))
            store.set_status(self.con, t, "RUNNING")
            store.add_attempt(self.con, t, worker)

        states = self.states()
        self.assertEqual(states[a], "RUNNING")
        self.assertEqual(states[b], "RUNNING")
        self.assertEqual(states[c], "READY", "Claude-owned work must stay available")

        stored_repo = store.get_task(self.con, a)["repo"]
        self.assertFalse(scheduler.worker_capacity_free(self.con, "codex", stored_repo,
                                                        self.cfg))
        self.assertFalse(scheduler.worker_capacity_free(self.con, "cc-delegate", stored_repo,
                                                        self.cfg))

    def test_capacity_stops_a_third_write_job(self):
        repo = make_repo()
        a = self.task("a", paths=["x/**"], repo=repo)
        b = self.task("b", paths=["y/**"], repo=repo)
        c = self.task("c", paths=["z/**"], repo=repo)
        for t in (a, b):
            store.set_status(self.con, t, "RUNNING")
        self.assertEqual(self.states()[c], "BLOCKED")


class TestBranches(DGTest):
    """Spec 80: independent branches progress in parallel, not serially."""

    def test_independent_branches_are_all_ready(self):
        repo = make_repo()
        self.cfg["workers"]["totalWriteJobsPerRepo"] = 3
        a = self.task("A", paths=["a/**"], repo=repo)
        b = self.task("B", paths=["b/**"], repo=repo)
        d = self.task("D", paths=["d/**"], repo=repo)
        c = self.task("C", paths=["c/**"], repo=repo, deps=[a])
        e = self.task("E", paths=["e/**"], repo=repo, deps=[d])
        ready = [r["id"] for r in scheduler.ready(
            scheduler.evaluate(self.con, self.cfg)["tasks"])]
        self.assertEqual(sorted(ready), sorted([a, b, d]))
        self.assertEqual({self.states()[c], self.states()[e]}, {"BLOCKED"})


class TestCodexOutcomes(DGTest):
    """Fake Codex JSONL streams -- the real CLI is never invoked."""

    def test_success(self):
        v = codex_worker.classify([{"type": "item.completed",
                                    "item": {"text": "done"}}], 0)
        self.assertEqual(v["status"], "SUCCEEDED")

    def test_quota_failure_captures_reset(self):
        v = codex_worker.classify(
            [{"type": "error", "error": {"message": "usage limit reached",
                                         "resetsAt": 1788614422}}], 1)
        self.assertEqual(v["status"], "QUOTA_FAILED")
        self.assertEqual(v["resetsAt"], 1788614422)

    def test_auth_failure_is_not_quota(self):
        v = codex_worker.classify(
            [{"type": "error", "error": {"message": "401 Unauthorized"}}], 1)
        self.assertEqual(v["status"], "AUTH_FAILED")

    def test_generic_failure(self):
        self.assertEqual(codex_worker.classify([{"type": "error"}], 2)["status"], "FAILED")

    def test_empty_stream_with_bad_exit_is_failed(self):
        self.assertEqual(codex_worker.classify([], 1)["status"], "FAILED")

    def test_summary_extraction(self):
        s = codex_worker.summarize([{"a": 1}, {"item": {"text": "the answer"}}])
        self.assertEqual(s, "the answer")

    def test_read_events_tolerates_garbage(self):
        p = self.home / "x.jsonl"
        p.write_text('{"ok":1}\nnot json\n\n{"ok":2}\n')
        evs = codex_worker.read_events(p)
        self.assertEqual(len(evs), 3)
        self.assertEqual(evs[1]["raw"], "not json")


class TestCodexRunner(DGTest):
    """The detached runner records outcomes into the ledger by itself."""

    def _job(self, repo, task_id, attempt_id, worktree=None):
        return {"taskId": task_id, "attemptId": attempt_id, "repo": repo,
                "worktree": worktree, "cwd": worktree or repo}

    def test_quota_failure_marks_codex_exhausted_and_preserves_partial_work(self):
        from dg.workers import codex_runner
        repo = make_repo()
        a = self.task("A", repo=repo, paths=["src/**"])
        wt = gitutil.create_worktree(repo, a)
        Path(wt["worktree"], "half-done.py").write_text("partial\n")
        att = store.add_attempt(self.con, a, "codex", worktree=wt["worktree"],
                                branch=wt["branch"])
        job = self._job(repo, a, att, wt["worktree"])

        codex_runner._record(self.con, job,
                             {"status": "QUOTA_FAILED", "errorKind": "quota exhausted",
                              "resetsAt": 1788614422}, [], None)

        self.assertEqual(store.get_task(self.con, a)["status"], "QUOTA_FAILED")
        self.assertEqual(store.kv_get(self.con, quota_codex.K_STATE), quota_codex.EXHAUSTED)
        self.assertEqual(store.kv_get(self.con, quota_codex.K_UNTIL), 1788614422)
        # Partial work is preserved for diagnosis, never integrated.
        self.assertTrue(Path(wt["worktree"], "half-done.py").exists())
        self.assertIn("partial work preserved",
                      store.attempts_for(self.con, a)[0]["error_detail"])
        # ... and the user's own working tree is untouched.
        self.assertFalse(Path(repo, "half-done.py").exists())
        self.assertEqual(subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                                        capture_output=True, text=True).stdout.strip(), "")

    def test_auth_failure_sets_auth_error_not_exhausted(self):
        from dg.workers import codex_runner
        repo = make_repo()
        a = self.task("A", repo=repo)
        att = store.add_attempt(self.con, a, "codex")
        codex_runner._record(self.con, self._job(repo, a, att),
                             {"status": "AUTH_FAILED", "errorKind": "authentication expired",
                              "resetsAt": None}, [], None)
        self.assertEqual(store.get_task(self.con, a)["status"], "AUTH_FAILED")
        self.assertEqual(store.kv_get(self.con, quota_codex.K_STATE), quota_codex.AUTH_ERROR)

    def test_success_writes_a_result_with_diff(self):
        from dg.workers import codex_runner
        repo = make_repo()
        a = self.task("A", repo=repo, paths=["src/**"])
        wt = gitutil.create_worktree(repo, a)
        Path(wt["worktree"], "new.py").write_text("print('hi')\n")
        att = store.add_attempt(self.con, a, "codex", worktree=wt["worktree"])
        codex_runner._record(self.con, self._job(repo, a, att, wt["worktree"]),
                             {"status": "SUCCEEDED", "errorKind": None, "resetsAt": None},
                             [{"item": {"text": "added new.py"}}], None)
        t = store.get_task(self.con, a)
        self.assertEqual(t["status"], "SUCCEEDED")
        result = json.loads(Path(t["resultLocation"]).read_text())
        self.assertIn("new.py", result["changedFiles"])
        self.assertIn("new.py", Path(result["diff"]).read_text())


class TestFallback(DGTest):
    """Spec 31/32/34: Codex exhausted -> cc-delegate, from a clean base."""

    def test_known_exhausted_routes_straight_to_cc_delegate(self):
        store.kv_set(self.con, quota_codex.K_STATE, quota_codex.EXHAUSTED)
        store.kv_set(self.con, quota_codex.K_UNTIL, time.time() + 3600)
        quota_codex.read_rate_limits = lambda *a, **k: self.fail(
            "no Codex request may be spent rediscovering a known exhaustion")
        self.assertEqual(routing.select(self.con, self.cfg)["worker"], "cc-delegate")

    def test_fallback_task_starts_from_the_original_clean_base(self):
        from dg import cli
        repo = make_repo()
        a = self.task("A", repo=repo, paths=["src/**"])
        wt = gitutil.create_worktree(repo, a)
        Path(wt["worktree"], "partial.py").write_text("half\n")
        store.set_status(self.con, a, "QUOTA_FAILED")

        args = type("A", (), {"id": a})()
        rc = cli.cmd_fallback(args)
        self.assertEqual(rc, 0)

        original = store.get_task(self.con, a)
        self.assertEqual(original["status"], "SUPERSEDED")
        self.assertIn("superseded by", original["failureReason"])

        new_id = [t["id"] for t in store.all_tasks(self.con) if t["id"] != a][0]
        new = store.get_task(self.con, new_id)
        self.assertEqual(new["status"], "PLANNED")
        self.assertEqual(new["paths"], original["paths"])
        # The retry carries no worktree from the failed attempt.
        self.assertEqual(store.attempts_for(self.con, new_id), [])
        # The failed attempt's partial work is still on disk for diagnosis.
        self.assertTrue(Path(wt["worktree"], "partial.py").exists())

    def test_cc_delegate_sync_maps_status(self):
        repo = make_repo()
        a = self.task("A", repo=repo)
        jobs = Path(repo) / ".cc-delegate" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "t_abc.json").write_text(json.dumps(
            {"taskId": "t_abc", "status": "succeeded", "worktree": "/wt", "branch": "b"}))
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:t_abc")
        store.set_status(self.con, a, "RUNNING")
        row = [r for r in store.running_attempts(self.con) if r["id"] == att][0]
        self.assertEqual(cc_delegate.sync(self.con, row), "SUCCEEDED")
        self.assertEqual(store.get_task(self.con, a)["status"], "SUCCEEDED")

    def test_cc_delegate_running_job_stays_running(self):
        repo = make_repo()
        a = self.task("A", repo=repo)
        jobs = Path(repo) / ".cc-delegate" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "t_run.json").write_text(json.dumps({"taskId": "t_run", "status": "running"}))
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:t_run")
        store.set_status(self.con, a, "RUNNING")
        row = [r for r in store.running_attempts(self.con) if r["id"] == att][0]
        self.assertIsNone(cc_delegate.sync(self.con, row))
        self.assertEqual(store.get_task(self.con, a)["status"], "RUNNING")

    def test_cc_delegate_missing_job_file_is_not_a_failure(self):
        """A job file that has not appeared yet means 'unknown', not 'failed'."""
        repo = make_repo()
        a = self.task("A", repo=repo)
        att = store.add_attempt(self.con, a, "cc-delegate", handle="cc:nope")
        store.set_status(self.con, a, "RUNNING")
        row = [r for r in store.running_attempts(self.con) if r["id"] == att][0]
        self.assertIsNone(cc_delegate.sync(self.con, row))
        self.assertEqual(store.get_task(self.con, a)["status"], "RUNNING")


class TestWorktreeIsolation(DGTest):
    def test_worktree_is_isolated_and_removable(self):
        repo = make_repo()
        a = self.task("A", repo=repo)
        wt = gitutil.create_worktree(repo, a)
        Path(wt["worktree"], "f.txt").write_text("x")
        self.assertIn("f.txt", gitutil.changed_files(wt["worktree"]))
        self.assertEqual(gitutil.changed_files(repo), [])
        res = gitutil.remove_worktree(repo, wt["worktree"], wt["branch"])
        self.assertTrue(res["worktreeRemoved"])
        self.assertFalse(Path(wt["worktree"]).exists())

    def test_base_commit_pins_the_worker(self):
        repo = make_repo()
        base = gitutil.head_commit(repo)
        a = self.task("A", repo=repo)
        wt = gitutil.create_worktree(repo, a, base)
        self.assertEqual(wt["baseCommit"], base)
        self.assertEqual(gitutil.head_commit(wt["worktree"]), base)
