"""Distribution report: do the lanes that routing intends to use actually get
the work?

report() is a pure function over (ledger, routes, availability), so these tests
build a small ledger through the store helpers and synthesize routing records
rather than probing anything. The per-lane buckets are the heart of it: they
must classify the ATTEMPT's status, not the task's -- a retried task has one
FAILED and one SUCCEEDED attempt, on different lanes.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from unittest import mock

from base import DGTest

from dg import distribution, lanes, routelog, store


class DistributionTest(DGTest):
    def setUp(self):
        super().setUp()
        # Every lane healthy unless a test says otherwise; the report must
        # never probe on its own, so availability is injected.
        self.avail = {name: [True, "ok"] for name in lanes.lanes(self.cfg)}
        self._availability = lanes.availability
        lanes.availability = lambda con, cfg, refresh=True: self.avail

    def tearDown(self):
        lanes.availability = self._availability
        super().tearDown()

    # -- fixtures ---------------------------------------------------------

    def task(self, title="t", task_class="standard", repo="/repo"):
        return store.create_task(self.con, title=title, mode="WRITE", repo=repo,
                                 task_class=task_class)

    def attempt(self, tid, lane, status="RUNNING", started=None, ended=None):
        """One attempt on `lane` through the store helpers.

        add_attempt/finish_attempt stamp started_at/ended_at with "now";
        since_ts and mean-duration need exact times, so re-stamp the row
        whenever the test passes them.
        """
        aid = store.add_attempt(self.con, tid, lanes.lanes(self.cfg)[lane]["worker"],
                                lane=lane)
        if status != "RUNNING":
            store.finish_attempt(self.con, aid, status)
        if started is not None or ended is not None:
            self.con.execute(
                "UPDATE attempts SET started_at=COALESCE(?, started_at),"
                " ended_at=COALESCE(?, ended_at) WHERE id=?",
                (started, ended, aid))
        return aid

    def route(self, **kw):
        """One synthetic routing record in routelog's schema; ``preference``
        defaults to the real routing table for the record's class."""
        r = {"ts": 5000.0, "source": "dispatch", "task_id": "repo-1",
             "repo": "/repo", "mode": "WRITE", "task_class": "standard",
             "preference": [], "lane": "codex", "rank": 0, "worker": "codex",
             "skipped": [], "in_flight": {}, "forced_worker": False,
             "attempt_id": None, "outcome": "reserved"}
        r.update(kw)
        r.setdefault("preference", lanes.preference(r["task_class"], self.cfg))
        return r

    def no_lane(self, **kw):
        """A record where every preference was skipped and nothing was reserved."""
        kw.setdefault("outcome", "no_lane")
        kw.setdefault("lane", None)
        kw.setdefault("rank", None)
        kw.setdefault("worker", None)
        return self.route(**kw)

    def report(self, routes=(), repo="", since_ts=None):
        return distribution.report(self.con, self.cfg, list(routes), self.avail,
                                   repo=repo, since_ts=since_ts)

    def lane_row(self, out, lane):
        return {r["lane"]: r for r in out["lanes"]}[lane]


class TestAttemptBuckets(DistributionTest):
    def test_buckets_classify_the_attempt_not_the_task(self):
        """A retried task: the FAILED attempt on one lane and the SUCCEEDED
        retry on another. The task's own status must not decide the buckets --
        before the fix both landed wherever the *task* ended up."""
        tid = self.task(task_class="hard")
        self.attempt(tid, "codex", status="FAILED")
        self.attempt(tid, "opencode-smart", status="SUCCEEDED")
        store.set_status(self.con, tid, "SUCCEEDED")  # the retry made it succeed

        out = self.report()
        codex = self.lane_row(out, "codex")
        smart = self.lane_row(out, "opencode-smart")
        self.assertEqual(codex["failed"], 1)
        self.assertEqual(codex["succeeded"], 0)
        self.assertEqual(codex["attempts"], 1)
        self.assertEqual(smart["succeeded"], 1)
        self.assertEqual(smart["failed"], 0)
        self.assertEqual(smart["attempts"], 1)

    def test_live_attempt_counts_as_running(self):
        tid = self.task()
        self.attempt(tid, "codex", status="RUNNING")
        out = self.report()
        self.assertEqual(self.lane_row(out, "codex")["running"], 1)
        # a lane the ledger never saw stays all zeros
        station = self.lane_row(out, "station")
        self.assertEqual((station["attempts"], station["running"],
                          station["succeeded"], station["failed"]), (0, 0, 0, 0))


class TestDurations(DistributionTest):
    def test_mean_duration_averages_only_ended_attempts(self):
        ended = [(1000.0, 1030.0), (2000.0, 2060.0)]  # 30s and 60s
        for i, (start, end) in enumerate(ended):
            self.attempt(self.task(title=f"e{i}"), "codex",
                         status="SUCCEEDED", started=start, ended=end)
        # in flight: counted as an attempt, but not in the mean
        self.attempt(self.task(title="live"), "codex", status="RUNNING",
                     started=3000.0)

        row = self.lane_row(self.report(), "codex")
        self.assertAlmostEqual(row["mean_duration_s"], 45.0)  # (30 + 60) / 2
        self.assertEqual(row["attempts"], 3)

    def test_classes_count_attempt_tasks_per_lane(self):
        self.attempt(self.task(task_class="hard"), "codex", status="SUCCEEDED")
        self.attempt(self.task(task_class="standard"), "codex",
                     status="SUCCEEDED")
        self.attempt(self.task(task_class="hard"), "codex", status="FAILED")

        out = self.report()
        self.assertEqual(self.lane_row(out, "codex")["classes"],
                         {"hard": 2, "standard": 1})
        self.assertEqual(self.lane_row(out, "station")["classes"], {})

    def test_lane_without_ended_attempts_has_no_mean(self):
        self.attempt(self.task(), "opencode-smart", status="RUNNING",
                     started=1000.0)
        out = self.report()
        self.assertIsNone(self.lane_row(out, "opencode-smart")["mean_duration_s"])
        self.assertIsNone(self.lane_row(out, "station")["mean_duration_s"])


class TestRoutes(DistributionTest):
    def test_first_choice_fallback_and_share(self):
        routes = [
            self.route(task_class="standard", lane="codex", rank=0),
            self.route(task_class="hard", lane="codex", rank=0),
            # codex was busy, so a hard task fell through to its second pick
            self.route(task_class="hard", lane="opencode-smart", rank=1,
                       worker="cc-delegate", skipped=["codex: at capacity"]),
            self.route(task_class="standard", lane="opencode-main", rank=1,
                       worker="cc-delegate", skipped=["codex: at capacity"]),
            # a wait is not a reservation; it must not dilute the shares
            self.no_lane(task_class="standard", skipped=["codex: down"]),
        ]
        out = self.report(routes)
        self.assertEqual(self.lane_row(out, "codex")["first_choice"], 2)
        self.assertEqual(self.lane_row(out, "codex")["fallback"], 0)
        self.assertEqual(self.lane_row(out, "codex")["share"], 0.5)  # 2 of 4
        self.assertEqual(self.lane_row(out, "opencode-smart")["first_choice"], 0)
        self.assertEqual(self.lane_row(out, "opencode-smart")["fallback"], 1)
        self.assertEqual(self.lane_row(out, "opencode-smart")["share"], 0.25)
        self.assertEqual(self.lane_row(out, "opencode-main")["first_choice"], 0)
        self.assertEqual(self.lane_row(out, "opencode-main")["fallback"], 1)
        self.assertEqual(self.lane_row(out, "opencode-main")["share"], 0.25)
        self.assertEqual(self.lane_row(out, "station")["share"], 0.0)


class TestFilters(DistributionTest):
    def test_repo_filter_applies_to_routes_and_attempts(self):
        self.attempt(self.task(repo="/repo"), "codex", status="SUCCEEDED")
        self.attempt(self.task(repo="/other"), "opencode-smart",
                     status="SUCCEEDED")
        routes = [
            self.route(task_class="standard", lane="codex", rank=0),
            self.route(task_class="standard", lane="opencode-main", rank=1,
                       worker="cc-delegate", skipped=["codex: at capacity"],
                       repo="/other"),
        ]

        out = self.report(routes, repo="/repo")
        self.assertEqual(self.lane_row(out, "codex")["attempts"], 1)
        self.assertEqual(self.lane_row(out, "codex")["first_choice"], 1)
        self.assertEqual(self.lane_row(out, "opencode-smart")["attempts"], 0)
        self.assertEqual(self.lane_row(out, "opencode-main")["first_choice"], 0)
        self.assertEqual(self.lane_row(out, "opencode-main")["fallback"], 0,
                         "the other repo's route must be filtered out")

        out = self.report(routes, repo="/other")
        self.assertEqual(self.lane_row(out, "opencode-smart")["attempts"], 1)
        self.assertEqual(self.lane_row(out, "opencode-main")["first_choice"], 0)
        self.assertEqual(self.lane_row(out, "opencode-main")["fallback"], 1,
                         "this repo's route must survive the filter")
        self.assertEqual(self.lane_row(out, "codex")["attempts"], 0)

        # no repo: every repo's routes and attempts are in play
        out = self.report(routes)
        self.assertEqual(self.lane_row(out, "codex")["attempts"], 1)
        self.assertEqual(self.lane_row(out, "opencode-smart")["attempts"], 1)

    def test_since_ts_drops_older_routes_and_attempts(self):
        tid = self.task()
        self.attempt(tid, "codex", status="FAILED", started=1000.0, ended=1100.0)
        self.attempt(tid, "codex", status="RUNNING", started=2000.0)
        routes = [self.route(ts=1000.0, lane="codex", rank=0),
                  self.route(ts=2000.0, lane="codex", rank=0)]

        out = self.report(routes, since_ts=1500.0)
        codex = self.lane_row(out, "codex")
        self.assertEqual(codex["attempts"], 1,
                         "an attempt started before since_ts must be dropped")
        self.assertEqual(codex["failed"], 0, "the old attempt is gone entirely")
        self.assertEqual(codex["running"], 1)
        self.assertEqual(codex["first_choice"], 1,
                         "a route older than since_ts must be dropped")

        # no since_ts: nothing is dropped
        out = self.report(routes)
        self.assertEqual(self.lane_row(out, "codex")["attempts"], 2)
        self.assertEqual(self.lane_row(out, "codex")["first_choice"], 2)

    def test_since_ts_keeps_records_from_the_boundary_itself(self):
        tid = self.task()
        self.attempt(tid, "codex", status="SUCCEEDED", started=1500.0,
                     ended=1600.0)
        out = self.report([self.route(ts=1500.0, lane="codex", rank=0)],
                          since_ts=1500.0)
        self.assertEqual(self.lane_row(out, "codex")["attempts"], 1)
        self.assertEqual(self.lane_row(out, "codex")["first_choice"], 1)


class TestWaits(DistributionTest):
    def test_waits_total_by_class_and_skipped_reasons(self):
        routes = [
            self.no_lane(task_class="standard",
                         skipped=["codex: at capacity",
                                  "opencode-main: CODEX_EXHAUSTED"]),
            self.no_lane(task_class="tiny",
                         skipped=["opencode-fast: at capacity"]),
            self.no_lane(task_class="standard",
                         skipped=["opencode-main-fallback: exhausted"]),
        ]
        waits = self.report(routes)["waits"]
        self.assertEqual(waits["total"], 3)
        self.assertEqual(waits["by_class"], {"standard": 2, "tiny": 1})
        # "lane: reason" entries collapse to the reason text
        self.assertEqual(waits["skipped_reasons"],
                         {"at capacity": 2, "CODEX_EXHAUSTED": 1,
                          "exhausted": 1})

    def test_no_waits_leaves_the_waits_section_empty(self):
        waits = self.report()["waits"]
        self.assertEqual(waits, {"total": 0, "by_class": {},
                                 "skipped_reasons": {}})


class TestFlags(DistributionTest):
    def test_flag_a_fires_when_an_up_routable_lane_got_no_reserved_routes(self):
        # standard work exists but all of it bypassed codex, which is up and
        # first choice for that class
        routes = [
            self.route(task_class="standard", lane="opencode-main", rank=1,
                       worker="cc-delegate", skipped=["codex: at capacity"]),
            self.route(task_class="standard", lane="opencode-main-fallback",
                       rank=2, worker="cc-delegate",
                       skipped=["codex: at capacity",
                                "opencode-main: at capacity"]),
        ]
        out = self.report(routes)
        self.assertEqual(out["flags"],
                         ["lane codex is up and routable but received 0 "
                          "reserved routes"])

    def test_flag_a_quiet_when_the_lane_got_work_or_is_down(self):
        # no routes at all: there is nothing the lane missed out on
        self.assertEqual(self.report()["flags"], [])

        routes = [
            self.route(task_class="standard", lane="codex", rank=0),
            self.route(task_class="standard", lane="opencode-main", rank=1,
                       worker="cc-delegate", skipped=["codex: at capacity"]),
            self.route(task_class="standard", lane="opencode-main-fallback",
                       rank=2, worker="cc-delegate",
                       skipped=["codex: at capacity",
                                "opencode-main: at capacity"]),
        ]
        self.assertEqual(self.report(routes)["flags"], [])

        # an unavailable lane is excused even with zero routes
        self.avail["codex"] = [False, "CODEX_EXHAUSTED"]
        self.assertEqual(self.report(routes[1:])["flags"], [])

    def test_flag_b_fires_when_a_lane_outside_the_preference_was_idle(self):
        busy = {name: 1 for name in self.avail}  # everything in flight...
        busy.pop("station")                       # ...except station
        routes = [self.no_lane(task_class="standard",
                               skipped=["codex: at capacity"],
                               in_flight=busy)]
        out = self.report(routes)
        self.assertEqual(
            out["flags"],
            ["class standard waited on a lane while station "
             "outside its preference was idle"])

    def test_flag_b_quiet_when_outside_lanes_were_busy_or_down(self):
        routes = [self.no_lane(task_class="standard",
                               skipped=["codex: at capacity"])]

        # every lane outside the preference had work at the moment of the wait
        busy = {name: 1 for name in self.avail}
        routes[0]["in_flight"] = busy
        self.assertEqual(self.report(routes)["flags"], [])

        # an idle outside lane only counts while it is actually up
        routes[0]["in_flight"] = {}
        outside = set(self.avail) - set(lanes.preference("standard", self.cfg))
        for name in outside:
            self.avail[name] = [False, "down"]
        self.assertEqual(self.report(routes)["flags"], [])

    def test_flag_c_fires_when_a_lane_fails_half_or_more_of_its_attempts(self):
        for status in ("FAILED", "FAILED"):
            tid = self.task()
            self.attempt(tid, "station", status=status)
            store.set_status(self.con, tid, "FAILED")
        self.assertEqual(self.report()["flags"],
                         ["lane station failing: 2/2 attempts"])

    def test_flag_c_counts_exactly_half_as_failing(self):
        self.attempt(self.task(), "station", status="FAILED")
        self.attempt(self.task(), "station", status="SUCCEEDED")
        self.assertEqual(self.report()["flags"],
                         ["lane station failing: 1/2 attempts"])

    def test_flag_c_quiet_below_half_or_with_too_few_attempts(self):
        # 1 of 3 failed is under the bar
        for status in ("FAILED", "SUCCEEDED", "SUCCEEDED"):
            self.attempt(self.task(), "station", status=status)
        # a single failure is not a pattern
        self.attempt(self.task(), "oracle", status="FAILED")
        self.assertEqual(self.report()["flags"], [])


class TestRun(DistributionTest):
    def test_run_json_prints_a_valid_report(self):
        self.attempt(self.task(), "codex", status="SUCCEEDED")
        routes = [
            self.route(task_class="standard", lane="codex", rank=0),
            self.route(task_class="standard", lane="opencode-main", rank=1,
                       worker="cc-delegate", skipped=["codex: at capacity"]),
        ]
        buf = io.StringIO()
        # Patch the real module: a fake in sys.modules is ignored once another
        # test has imported dg.routelog, and leaks into later tests otherwise.
        with mock.patch.object(routelog, "read_routes", lambda: routes), \
                redirect_stdout(buf):
            rc = distribution.run(argparse.Namespace(json=True, since=0,
                                                     repo=""))
        out = json.loads(buf.getvalue())

        self.assertEqual(rc, 0)
        self.assertEqual(sorted(out), ["flags", "lanes", "waits"])
        by_lane = {r["lane"]: r for r in out["lanes"]}
        self.assertEqual(by_lane["codex"]["attempts"], 1)
        self.assertEqual(by_lane["codex"]["succeeded"], 1)
        self.assertEqual(by_lane["codex"]["first_choice"], 1)
        self.assertEqual(by_lane["opencode-main"]["fallback"], 1)
        self.assertEqual(out["waits"], {"total": 0, "by_class": {},
                                        "skipped_reasons": {}})
        self.assertEqual(out["flags"],
                         ["lane opencode-main-fallback is up and routable but "
                          "received 0 reserved routes"])
