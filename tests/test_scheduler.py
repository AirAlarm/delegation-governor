"""Dependency scheduler, path conflicts, capacity and priority."""
from __future__ import annotations

from base import DGTest

from dg import scheduler, store


class TestDependencies(DGTest):
    def test_independent_task_is_ready(self):
        a = self.task("a")
        self.assertEqual(self.states()[a], "READY")

    def test_dependant_blocked_while_dependency_runs(self):
        a = self.task("a")
        c = self.task("c", deps=[a], paths=["docs/**"])
        store.set_status(self.con, a, "RUNNING")
        st = self.states()
        self.assertEqual(st[a], "RUNNING")
        self.assertEqual(st[c], "BLOCKED")

    def test_dependant_waits_for_integration(self):
        a = self.task("a")
        c = self.task("c", deps=[a], paths=["docs/**"])
        store.set_status(self.con, a, "SUCCEEDED")
        self.assertEqual(self.states()[c], "BLOCKED")
        store.set_status(self.con, a, "INTEGRATED")
        self.assertEqual(self.states()[c], "READY")

    def test_integrated_also_satisfies(self):
        a = self.task("a")
        c = self.task("c", deps=[a], paths=["docs/**"])
        store.set_status(self.con, a, "INTEGRATED")
        self.assertEqual(self.states()[c], "READY")

    def test_dead_dependency_blocks_forever(self):
        """A failed dependency must never silently unblock its dependants."""
        for bad in ("FAILED", "QUOTA_FAILED", "AUTH_FAILED", "CANCELLED"):
            with self.subTest(bad=bad):
                a = self.task("a")
                c = self.task("c", deps=[a], paths=[f"docs/{bad}/**"])
                store.set_status(self.con, a, bad)
                rows = {r["id"]: r for r in scheduler.evaluate(self.con, self.cfg)["tasks"]}
                self.assertEqual(rows[c]["state"], "BLOCKED")
                self.assertIn(bad, rows[c]["reason"])

    def test_diamond_graph_unblocks_in_order(self):
        # A -> C -> E, A -> E, with B and D independent (spec 16).
        a = self.task("A", paths=["src/api/**"])
        b = self.task("B", paths=["docs/**"])
        c = self.task("C", deps=[a], paths=["tests/**"])
        d = self.task("D", paths=["web/**"])
        e = self.task("E", deps=[a, c], paths=["src/api/**"])

        st = self.states()
        self.assertEqual([st[a], st[b], st[d]], ["READY"] * 3)
        self.assertEqual([st[c], st[e]], ["BLOCKED"] * 2)

        store.set_status(self.con, a, "INTEGRATED")
        st = self.states()
        self.assertEqual(st[c], "READY")
        self.assertEqual(st[e], "BLOCKED")  # still waiting on C

        store.set_status(self.con, c, "INTEGRATED")
        self.assertEqual(self.states()[e], "READY")

    def test_missing_dependency_blocks(self):
        a = self.task("a")
        c = self.task("c", deps=[a])
        self.con.execute("DELETE FROM tasks WHERE id=?", (a,))
        rows = {r["id"]: r for r in scheduler.evaluate(self.con, self.cfg)["tasks"]}
        self.assertEqual(rows[c]["state"], "BLOCKED")


class TestPathConflicts(DGTest):
    def test_overlapping_writes_do_not_run_together(self):
        a = self.task("a", paths=["src/auth/**"])
        b = self.task("b", paths=["src/auth/**"])
        store.set_status(self.con, a, "RUNNING")
        rows = {r["id"]: r for r in scheduler.evaluate(self.con, self.cfg)["tasks"]}
        self.assertEqual(rows[b]["state"], "BLOCKED")
        self.assertIn("path conflict", rows[b]["reason"])

    def test_disjoint_writes_may_run_together(self):
        a = self.task("a", paths=["src/backend/**"])
        b = self.task("b", paths=["docs/**"])
        store.set_status(self.con, a, "RUNNING")
        self.assertEqual(self.states()[b], "READY")

    def test_nested_paths_conflict(self):
        a = self.task("a", paths=["src/**"])
        b = self.task("b", paths=["src/auth/login.py"])
        store.set_status(self.con, a, "RUNNING")
        self.assertEqual(self.states()[b], "BLOCKED")

    def test_undeclared_paths_conflict_with_everything(self):
        """Unknown ownership is treated as whole-repo ownership, never as safe."""
        a = self.task("a", paths=[])
        b = self.task("b", paths=["docs/**"])
        store.set_status(self.con, a, "RUNNING")
        self.assertEqual(self.states()[b], "BLOCKED")

    def test_different_repos_never_conflict(self):
        a = self.task("a", paths=["src/auth/**"], repo="/repo-one")
        b = self.task("b", paths=["src/auth/**"], repo="/repo-two")
        store.set_status(self.con, a, "RUNNING")
        self.assertEqual(self.states()[b], "READY")

    def test_read_only_never_conflicts(self):
        a = self.task("a", paths=["src/auth/**"])
        b = self.task("b", mode="READ_ONLY", paths=["src/auth/**"])
        store.set_status(self.con, a, "RUNNING")
        self.assertEqual(self.states()[b], "READY")

    def test_prefix_lookalike_is_not_a_conflict(self):
        self.assertFalse(scheduler.paths_overlap(["src/auth/**"], ["src/authz/**"]))

    def test_windows_separators_normalise(self):
        self.assertTrue(scheduler.paths_overlap([r"src\auth\**"], ["src/auth/x.py"]))


class TestCapacity(DGTest):
    def test_write_capacity_per_repo(self):
        self.cfg["workers"]["totalWriteJobsPerRepo"] = 2  # pin: this tests the cap, not its value
        a = self.task("a", paths=["one/**"])
        b = self.task("b", paths=["two/**"])
        c = self.task("c", paths=["three/**"])
        store.set_status(self.con, a, "RUNNING")
        store.set_status(self.con, b, "RUNNING")
        rows = {r["id"]: r for r in scheduler.evaluate(self.con, self.cfg)["tasks"]}
        self.assertEqual(rows[c]["state"], "BLOCKED")
        self.assertIn("write capacity", rows[c]["reason"])

    def test_read_only_capacity(self):
        self.cfg["workers"]["maxReadOnlyJobs"] = 3  # pin the behavior, not the default
        ids = [self.task(f"r{i}", mode="READ_ONLY") for i in range(4)]
        for i in ids[:3]:
            store.set_status(self.con, i, "RUNNING")
        rows = {r["id"]: r for r in scheduler.evaluate(self.con, self.cfg)["tasks"]}
        self.assertEqual(rows[ids[3]]["state"], "BLOCKED")
        self.assertIn("read-only capacity", rows[ids[3]]["reason"])

    def test_per_worker_capacity(self):
        a = self.task("a", paths=["one/**"], repo="/repo")
        # repo paths are stored absolute, so compare against what the ledger holds
        repo = store.get_task(self.con, a)["repo"]
        store.set_status(self.con, a, "RUNNING")
        store.add_attempt(self.con, a, "codex")
        self.assertFalse(scheduler.worker_capacity_free(self.con, "codex", repo, self.cfg))
        self.assertTrue(scheduler.worker_capacity_free(self.con, "cc-delegate", repo, self.cfg))


class TestPriority(DGTest):
    def test_critical_path_first_but_both_ready(self):
        """A unlocks three tasks, B unlocks none: A ranks first, B still READY."""
        a = self.task("A", paths=["a/**"])
        b = self.task("B", paths=["b/**"])
        for n in range(3):
            self.task(f"dep{n}", deps=[a], paths=[f"d{n}/**"])
        rows = scheduler.evaluate(self.con, self.cfg)["tasks"]
        ready = scheduler.ready(rows)
        self.assertEqual(ready[0]["id"], a)
        self.assertIn(b, [r["id"] for r in ready])

    def test_explicit_priority_wins(self):
        a = self.task("A", paths=["a/**"])
        self.task("dep", deps=[a], paths=["d/**"])
        b = self.task("B", paths=["b/**"], priority=10)
        self.assertEqual(scheduler.ready(
            scheduler.evaluate(self.con, self.cfg)["tasks"])[0]["id"], b)

    def test_ordering_is_deterministic(self):
        for i in range(5):
            self.task(f"t{i}", paths=[f"p{i}/**"])
        runs = [[r["id"] for r in scheduler.ready(
            scheduler.evaluate(self.con, self.cfg)["tasks"])] for _ in range(5)]
        self.assertEqual(len(set(map(tuple, runs))), 1)

    def test_pick_can_filter_by_mode(self):
        self.task("w", paths=["w/**"])
        r = self.task("r", mode="READ_ONLY")
        self.assertEqual(scheduler.pick(self.con, self.cfg, mode="READ_ONLY")["id"], r)
