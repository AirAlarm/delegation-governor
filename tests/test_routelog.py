"""Lane routing log and fill coverage."""
from __future__ import annotations

import contextlib
import io
import json
from unittest import mock

from base import DGTest

from dg import cli, lanes, routelog, store
from dg.workers import codex_plugin


class TestRouteLog(DGTest):
    def test_round_trip_and_malformed_line_skip(self):
        expected = routelog.append_route({
            "source": "fill",
            "task_id": "DG-1",
            "repo": "/repo",
            "mode": "WRITE",
            "task_class": "hard",
            "preference": ["codex", "station"],
            "lane": "codex",
            "rank": 0,
            "worker": "codex",
            "skipped": [],
            "in_flight": {},
            "forced_worker": None,
            "attempt_id": 1,
            "outcome": "reserved",
        })
        with (self.home / "routing.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"ts":')

        self.assertIsInstance(expected["ts"], float)
        self.assertEqual(routelog.read_routes(), [expected])
        line = (self.home / "routing.jsonl").read_text("utf-8").splitlines()[0]
        self.assertEqual(json.loads(line), expected)

    def test_choose_returns_preference_for_lane_and_no_lane(self):
        task = {"taskClass": "hard", "repo": "/repo", "mode": "WRITE"}
        order = self.cfg["workers"]["classRouting"]["hard"]
        available = {name: [True, "ok"] for name in lanes.lanes(self.cfg)}

        chosen = lanes.choose(self.con, self.cfg, task, available)
        self.assertEqual(chosen["preference"], order)

        down = {name: [False, "down"] for name in lanes.lanes(self.cfg)}
        unchosen = lanes.choose(self.con, self.cfg, task, down)
        self.assertIsNone(unchosen["lane"])
        self.assertEqual(unchosen["preference"], order)


class TestFillRouteLog(DGTest):
    def _task(self, title="route me"):
        return store.create_task(
            self.con, title=title, mode="WRITE", repo="/repo",
            paths=[f"{title}/**"], task_class="hard")

    def _availability(self, ok=True):
        return {name: [ok, "ok" if ok else "down"]
                for name in lanes.lanes(self.cfg)}

    def _fill(self, dry_run=False):
        args = type("Args", (), {"repo": "/repo", "dry_run": dry_run, "max": 1})()
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.cmd_fill(args)

    def test_reserved_codex_task_records_route_and_pre_reservation_counts(self):
        busy = store.create_task(
            self.con, title="busy", mode="WRITE", repo="/repo",
            paths=["busy/**"], task_class="simple")
        reserved = store.reserve_attempt(
            self.con, busy, "other", "cc-delegate", "station", self.cfg)
        self.assertTrue(reserved["ok"])
        task_id = self._task()

        with mock.patch.object(lanes, "availability",
                               return_value=self._availability()), \
                mock.patch.object(codex_plugin, "dispatch",
                                  return_value={"ok": True, "jobId": "job-1",
                                                "worktree": "/worktree"}):
            self.assertEqual(self._fill(), 0)

        records = routelog.read_routes()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["source"], "fill")
        self.assertEqual(record["task_id"], task_id)
        self.assertEqual(record["repo"], "/repo")
        self.assertEqual(record["mode"], "WRITE")
        self.assertEqual(record["task_class"], "hard")
        self.assertEqual(record["preference"], self.cfg["workers"]["classRouting"]["hard"])
        self.assertEqual(record["lane"], "codex")
        self.assertEqual(record["rank"], 0)
        self.assertEqual(record["worker"], "codex")
        self.assertEqual(record["skipped"], [])
        self.assertEqual(record["in_flight"], {"station": 1})
        self.assertIsNone(record["forced_worker"])
        self.assertIsInstance(record["attempt_id"], int)
        self.assertEqual(record["outcome"], "reserved")

    def test_dry_run_does_not_log(self):
        self._task()
        with mock.patch.object(lanes, "availability",
                               return_value=self._availability()), \
                mock.patch.object(codex_plugin, "dispatch") as dispatch:
            self.assertEqual(self._fill(dry_run=True), 0)

        dispatch.assert_not_called()
        self.assertEqual(routelog.read_routes(), [])
        self.assertFalse((self.home / "routing.jsonl").exists())

    def test_no_lane_records_one_terminal_outcome(self):
        task_id = self._task()
        with mock.patch.object(lanes, "availability",
                               return_value=self._availability(ok=False)):
            self.assertEqual(self._fill(), 0)

        records = routelog.read_routes()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["task_id"], task_id)
        self.assertEqual(record["outcome"], "no_lane")
        self.assertIsNone(record["lane"])
        self.assertIsNone(record["rank"])
        self.assertIsNone(record["worker"])
        self.assertEqual(record["in_flight"], {})
        self.assertEqual(len(record["skipped"]), len(record["preference"]))
