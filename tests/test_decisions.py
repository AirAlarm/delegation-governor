"""Supervisor decision log and CLI coverage."""
from __future__ import annotations

import contextlib
import io
import json

from base import DGTest

from dg import cli, decisions


class TestDecisions(DGTest):
    def run_cli(self, *args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = cli.main(list(args))
        return result, stdout.getvalue()

    def test_append_and_read_preserve_all_fields(self):
        record = decisions.append_decision(
            "delegate", "implement parser", "bounded task", "DG-7")

        self.assertIsInstance(record["ts"], float)
        self.assertEqual(record["decision"], "delegate")
        self.assertEqual(record["task"], "implement parser")
        self.assertEqual(record["reason"], "bounded task")
        self.assertEqual(record["related_task_id"], "DG-7")
        self.assertEqual(decisions.read_decisions(), [record])
        line = (self.home / "decisions.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(len(line), 1)
        self.assertEqual(json.loads(line[0]), record)

    def test_decision_cli_appends_and_confirms_record(self):
        result, output = self.run_cli(
            "decision", "--type", "keep", "--task", "fix the auth bug",
            "--reason", "one-line fix")

        self.assertEqual(result, 0)
        record = json.loads(output)
        self.assertEqual(record["decision"], "keep")
        self.assertEqual(record["related_task_id"], None)
        self.assertEqual(decisions.read_decisions(), [record])

    def test_decisions_cli_summary_and_json(self):
        decisions.append_decision("delegate", "a", "reason", "DG-1")
        decisions.append_decision("keep", "b", "reason")
        decisions.append_decision("keep", "c", "reason")
        decisions.append_decision("takeover", "d", "reason", "DG-2")

        result, summary = self.run_cli("decisions")
        self.assertEqual(result, 0)
        self.assertIn("Total: 4", summary)
        self.assertIn("delegate: 1", summary)
        self.assertIn("keep: 2", summary)
        self.assertIn("takeover: 1", summary)
        self.assertIn("DG-2", summary)
        result, output = self.run_cli("decisions", "--json")
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), decisions.read_decisions())

    def test_missing_file_reports_zero_counts(self):
        result, output = self.run_cli("decisions")

        self.assertEqual(result, 0)
        self.assertIn("Total: 0", output)
        self.assertIn("delegate: 0", output)
        self.assertIn("keep: 0", output)
        self.assertIn("takeover: 0", output)

    def test_corrupt_trailing_line_is_skipped(self):
        expected = decisions.append_decision("keep", "small fix", "faster inline")
        with (self.home / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"ts":')

        result, output = self.run_cli("decisions", "--json")

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), [expected])

    def test_human_output_only_shows_ten_most_recent(self):
        for number in range(12):
            decisions.append_decision("keep", f"task-{number}", "reason")

        _, output = self.run_cli("decisions")

        self.assertNotIn("task-0 /", output)
        self.assertNotIn("task-1 /", output)
        self.assertIn("task-2 /", output)
        self.assertIn("task-11 /", output)


class TestPlanDecision(DGTest):
    def test_plan_is_a_recordable_decision_type(self):
        record = decisions.append_decision("plan", "split site into 4 pages", "shared CSS first")
        self.assertEqual(decisions.read_decisions(), [record])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["decisions"]), 0)
        self.assertIn("plan: 1", stdout.getvalue())
