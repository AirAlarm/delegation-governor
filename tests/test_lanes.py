"""Lanes: class routing, per-endpoint capacity, and real four-way concurrency.

The point of lanes is that `station` and `oracle` are different computers, so
they can work at the same time as Codex and OpenRouter. These tests pin that.
"""
from __future__ import annotations

from base import DGTest

from dg import lanes, quota_codex, routing, scheduler, store


class LaneTest(DGTest):
    def setUp(self):
        super().setUp()
        # Every lane healthy unless a test says otherwise.
        self.avail = {"codex": [True, "ok"], "station": [True, "ok"],
                      "oracle": [True, "ok"], "openrouter": [True, "ok"],
                      "opencode": [True, "ok"]}
        lanes.availability = lambda con, cfg, refresh=True: self.avail

    def task(self, title="t", mode="WRITE", deps=(), paths=(), repo="/repo",
             priority=0, task_class="standard"):
        return store.create_task(self.con, title=title, mode=mode, repo=repo, paths=paths,
                                 depends_on=deps, priority=priority, task_class=task_class)

    def choose(self, tid):
        return lanes.choose(self.con, self.cfg, store.get_task(self.con, tid), self.avail)

    def occupy(self, lane, repo="/repo", paths=("x/**",)):
        tid = self.task("busy", paths=paths, repo=repo)
        store.set_status(self.con, tid, "RUNNING")
        worker = lanes.lanes(self.cfg)[lane]["worker"]
        store.add_attempt(self.con, tid, worker, lane=lane)
        return tid


class TestClassRouting(LaneTest):
    def test_hard_prefers_codex(self):
        self.assertEqual(self.choose(self.task(task_class="hard"))["lane"], "codex")

    def test_simple_prefers_the_gpu_box_over_codex(self):
        """The whole point: trivial work must not consume the Codex slot."""
        self.assertEqual(self.choose(self.task(task_class="simple"))["lane"], "station")

    def test_tiny_prefers_the_vm(self):
        self.assertEqual(self.choose(self.task(task_class="tiny"))["lane"], "oracle")

    def test_standard_prefers_opencode(self):
        self.assertEqual(self.choose(self.task(task_class="standard"))["lane"], "opencode")

    def test_unknown_class_falls_back_to_the_default(self):
        tid = self.task()
        self.con.execute("UPDATE tasks SET task_class='nonsense' WHERE id=?", (tid,))
        out = self.choose(tid)
        self.assertEqual(out["taskClass"], "standard")
        self.assertEqual(out["lane"], "opencode")

    def test_routing_table_is_configurable(self):
        self.cfg["workers"]["classRouting"]["hard"] = ["oracle", "codex"]
        self.assertEqual(self.choose(self.task(task_class="hard"))["lane"], "oracle")


class TestFallThrough(LaneTest):
    def test_busy_preferred_lane_falls_through(self):
        self.occupy("codex", paths=("a/**",))
        out = self.choose(self.task(task_class="hard", paths=("b/**",)))
        self.assertEqual(out["lane"], "opencode")
        self.assertIn("codex: at capacity", out["skipped"])

    def test_unavailable_lane_falls_through(self):
        self.avail["codex"] = [False, "CODEX_EXHAUSTED"]
        out = self.choose(self.task(task_class="hard"))
        self.assertEqual(out["lane"], "opencode")
        self.assertIn("codex: CODEX_EXHAUSTED", out["skipped"])

    def test_no_lane_left_is_reported_not_guessed(self):
        for lane in ("codex", "station", "oracle", "openrouter", "opencode"):
            self.avail[lane] = [False, "down"]
        out = self.choose(self.task())
        self.assertIsNone(out["lane"])
        self.assertIn("no lane available", out["reason"])
        self.assertEqual(len(out["skipped"]), 5)

    def test_hard_uses_opencode_before_the_local_gpu(self):
        self.avail["codex"] = [False, "down"]
        self.assertEqual(self.choose(self.task(task_class="hard"))["lane"], "opencode")

    def test_hard_never_lands_on_the_slow_vm(self):
        self.avail["codex"] = [False, "down"]
        self.avail["openrouter"] = [False, "down"]
        self.avail["opencode"] = [False, "down"]
        self.avail["station"] = [False, "down"]
        self.assertIsNone(self.choose(self.task(task_class="hard"))["lane"])

    def test_manual_worker_override_pins_the_tool(self):
        self.cfg["overrides"]["worker"] = "cc-delegate"
        out = self.choose(self.task(task_class="hard"))
        self.assertEqual(out["worker"], "cc-delegate")
        self.assertIn(out["lane"], ("opencode", "openrouter", "station", "oracle"))


class TestCapacity(LaneTest):
    def test_each_lane_has_its_own_budget(self):
        """Regression: station and oracle used to share one cc-delegate slot."""
        self.occupy("station", paths=("a/**",))
        free = lanes.free_lanes(self.con, self.cfg, "/repo", self.avail)
        self.assertNotIn("station", free)
        self.assertIn("oracle", free, "the VM must stay free when the GPU is busy")
        self.assertIn("codex", free)
        self.assertIn("openrouter", free)

    def test_oracle_takes_two(self):
        self.occupy("oracle", paths=("a/**",))
        self.assertTrue(lanes.has_capacity(self.con, "oracle", "/repo", self.cfg))
        self.occupy("oracle", paths=("b/**",))
        self.assertFalse(lanes.has_capacity(self.con, "oracle", "/repo", self.cfg))

    def test_total_cap_still_applies(self):
        self.cfg["workers"]["totalWriteJobsPerRepo"] = 2
        self.occupy("codex", paths=("a/**",))
        self.occupy("station", paths=("b/**",))
        self.assertEqual(lanes.free_lanes(self.con, self.cfg, "/repo", self.avail), [])

    def test_other_repos_do_not_consume_the_budget(self):
        self.occupy("codex", repo="/other", paths=("a/**",))
        self.assertTrue(lanes.has_capacity(self.con, "codex", "/repo", self.cfg))

    def test_legacy_attempts_without_a_lane_still_count(self):
        tid = self.task("old", paths=("a/**",))
        store.set_status(self.con, tid, "RUNNING")
        store.add_attempt(self.con, tid, "codex")  # pre-lane row
        self.assertFalse(lanes.has_capacity(self.con, "codex", "/repo", self.cfg))


class TestFourLaneConcurrency(LaneTest):
    def test_four_tasks_run_on_four_endpoints_at_once(self):
        """The acceptance criterion for this feature."""
        hard = self.task("refactor", task_class="hard", paths=("src/core/**",))
        standard = self.task("new endpoint", task_class="standard", paths=("src/api/**",))
        simple = self.task("update call sites", task_class="simple", paths=("src/web/**",))
        tiny = self.task("regen fixtures", task_class="tiny", paths=("fixtures/**",))

        placed = {}
        for tid in (hard, standard, simple, tiny):
            out = self.choose(tid)
            self.assertIsNotNone(out["lane"], f"{tid} found no lane")
            placed[tid] = out["lane"]
            store.set_status(self.con, tid, "RUNNING")
            store.add_attempt(self.con, tid, out["worker"], lane=out["lane"])

        self.assertEqual(placed[hard], "codex")
        self.assertEqual(placed[standard], "opencode")
        self.assertEqual(placed[simple], "station")
        self.assertEqual(placed[tiny], "oracle")
        self.assertEqual(len(set(placed.values())), 4, "all four must be distinct endpoints")

        counts = lanes.in_flight(self.con, "/repo")
        self.assertEqual(counts, {"codex": 1, "opencode": 1, "station": 1,
                                  "oracle": 1})

    def test_a_fifth_task_waits(self):
        for cls, paths in (("hard", "a/**"), ("standard", "b/**"),
                           ("simple", "c/**"), ("tiny", "d/**")):
            tid = self.task(cls, task_class=cls, paths=(paths,))
            out = self.choose(tid)
            store.set_status(self.con, tid, "RUNNING")
            store.add_attempt(self.con, tid, out["worker"], lane=out["lane"])
        self.assertIsNone(self.choose(self.task("fifth", paths=("e/**",)))["lane"])

    def test_read_only_tasks_ignore_write_capacity(self):
        self.occupy("codex", paths=("a/**",))
        out = self.choose(self.task("look", mode="READ_ONLY", task_class="hard"))
        self.assertEqual(out["lane"], "codex")


class TestSupervisorContention(LaneTest):
    """The GPU cannot host the supervisor and a worker at once -- observed live."""

    def test_station_is_excluded_while_the_supervisor_runs_there(self):
        from dg import launcher, supervisor
        del lanes.availability  # use the real one
        import importlib
        importlib.reload(lanes)
        supervisor.record_hard_limit(self.con, "rate_limit")
        launcher.first_usable_tier = lambda cfg, load=False: ({"name": "lmstudio"}, [])
        launcher.probe_tier = lambda t, load=False: (True, "ok")
        quota_codex.refresh = lambda con, cfg, force=False: {"state": quota_codex.READY}
        avail = lanes.availability(self.con, self.cfg)
        self.assertFalse(avail["station"][0])
        self.assertIn("hosting the local supervisor", avail["station"][1])
        self.assertTrue(avail["oracle"][0], "the VM is a different machine")


class TestSchedulerIntegration(LaneTest):
    def test_worker_capacity_free_uses_lanes(self):
        self.occupy("station", paths=("a/**",))
        # cc-delegate still has the oracle lane
        self.assertTrue(scheduler.worker_capacity_free(self.con, "cc-delegate", "/repo",
                                                       self.cfg))
        self.occupy("oracle", paths=("b/**",))
        self.occupy("oracle", paths=("c/**",))
        self.occupy("openrouter", paths=("d/**",))
        self.occupy("opencode", paths=("e/**",))
        self.assertFalse(scheduler.worker_capacity_free(self.con, "cc-delegate", "/repo",
                                                        self.cfg))

    def test_select_for_returns_a_lane(self):
        out = routing.select_for(self.con, self.cfg, store.get_task(
            self.con, self.task(task_class="simple")))
        self.assertEqual(out["lane"], "station")
        self.assertEqual(out["worker"], "cc-delegate")


class TestTaskClassPersistence(DGTest):
    def test_class_round_trips(self):
        tid = store.create_task(self.con, title="x", repo="/r", task_class="tiny")
        self.assertEqual(store.get_task(self.con, tid)["taskClass"], "tiny")

    def test_default_is_standard(self):
        tid = store.create_task(self.con, title="x", repo="/r")
        self.assertEqual(store.get_task(self.con, tid)["taskClass"], "standard")

    def test_v1_database_migrates(self):
        """A ledger written before lanes must keep working."""
        self.con.execute("UPDATE meta SET value='1' WHERE key='schemaVersion'")
        self.con.close()
        con = store.connect()
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}
            self.assertIn("task_class", cols)
            acols = {r["name"] for r in con.execute("PRAGMA table_info(attempts)")}
            self.assertIn("lane", acols)
            self.assertEqual(
                con.execute("SELECT value FROM meta WHERE key='schemaVersion'")
                .fetchone()["value"], "3")
        finally:
            con.close()
