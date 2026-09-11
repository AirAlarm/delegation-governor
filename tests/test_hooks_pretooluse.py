import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dg import hooks, install


def invoke(payload, env=None, entrypoint=hooks.pretooluse):
    stdin = io.StringIO(json.dumps(payload))
    stdout = io.StringIO()
    with mock.patch.dict(os.environ, env or {}, clear=False):
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(stdout):
            assert entrypoint() == 0
    return json.loads(stdout.getvalue())["hookSpecificOutput"]


def payload(tool, tool_input, cwd):
    return {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": tool_input, "cwd": str(cwd)}


def thresholds(lines=100, size=10_000, tokens=10_000):
    return {"DG_DELEGATE_MIN_LINES": str(lines),
            "DG_DELEGATE_MIN_BYTES": str(size),
            "DG_DELEGATE_MIN_TOKENS": str(tokens)}


class TestPreToolUse(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tmp = Path(self._temp.name)

    def file(self, name, text):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_read_allows_offset_and_limit_for_large_file(self):
        path = self.file("large.txt", "x\n" * 20)
        for targeted in ({"offset": 0}, {"limit": 1}):
            result = invoke(payload("Read", {"file_path": str(path), **targeted}, self.tmp),
                            thresholds(lines=1, size=1, tokens=1))
            self.assertEqual(result["permissionDecision"], "allow")

    def test_read_allows_file_under_all_thresholds(self):
        path = self.file("small.txt", "one\ntwo\n")
        result = invoke(payload("Read", {"file_path": str(path)}, self.tmp), thresholds())
        self.assertEqual(result["permissionDecision"], "allow")

    def test_read_denies_file_over_line_threshold_and_names_bulk_read(self):
        path = self.file("lines.txt", "one\ntwo\nthree\n")
        result = invoke(payload("Read", {"file_path": str(path)}, self.tmp),
                        thresholds(lines=2))
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn("bulk-read", result["permissionDecisionReason"])

    def test_read_denies_large_single_line_minified_file(self):
        path = self.file("minified.js", "x" * 101)
        result = invoke(payload("Read", {"file_path": str(path)}, self.tmp),
                        thresholds(lines=1000, size=100, tokens=1000))
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn("101 bytes", result["permissionDecisionReason"])

    def test_read_can_deny_on_estimated_tokens(self):
        path = self.file("tokens.txt", "abcdefghijkl")
        result = invoke(payload("Read", {"file_path": str(path)}, self.tmp),
                        thresholds(lines=100, size=100, tokens=2))
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn("tokens", result["permissionDecisionReason"])

    def test_bash_allows_pipes_and_redirections(self):
        path = self.file("large.txt", "x\n" * 20)
        for command in (f'cat "{path}" | grep x', f'cat "{path}" > output.txt',
                        f'cat "{path}" < input.txt'):
            result = invoke(payload("Bash", {"command": command}, self.tmp),
                            thresholds(lines=1))
            self.assertEqual(result["permissionDecision"], "allow")

    def test_bash_resolves_quoted_relative_path_containing_spaces(self):
        path = self.file("folder with spaces/large file.txt", "x\n" * 20)
        result = invoke(payload(
            "Bash", {"command": 'cat "folder with spaces/large file.txt"'}, self.tmp),
            thresholds(lines=1))
        self.assertEqual(result["permissionDecision"], "deny")
        self.assertIn(str(path.resolve()), result["permissionDecisionReason"])

    def test_bash_deliberately_allows_reader_later_in_command_chain(self):
        self.file("large.txt", "x\n" * 20)
        result = invoke(payload("Bash", {"command": "some-command && cat large.txt"},
                                self.tmp), thresholds(lines=1))
        self.assertEqual(result["permissionDecision"], "allow")

    def test_malformed_stdin_and_internal_exceptions_fail_open(self):
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("not json")):
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(hooks.pretooluse(), 0)
        decision = json.loads(stdout.getvalue())["hookSpecificOutput"]["permissionDecision"]
        self.assertEqual(decision, "allow")

        with mock.patch.object(hooks, "_pretooluse_decision",
                               side_effect=OSError("unreadable")):
            result = invoke({}, entrypoint=hooks.pretooluse)
        self.assertEqual(result["permissionDecision"], "allow")

    def test_installer_registers_current_pretooluse_contract(self):
        spec = install.HOOKS_SPEC["PreToolUse"]
        self.assertEqual(spec["matcher"], "Read|Bash")
        self.assertIn("-m dg.cli hook prompt", spec["command"])
        entry = install._hook_entry(spec)
        self.assertEqual(entry["matcher"], "Read|Bash")

    def test_registered_cli_entrypoint_dispatches_pretooluse(self):
        path = self.file("large.txt", "x\n" * 20)
        result = invoke(payload("Read", {"file_path": str(path)}, self.tmp),
                        thresholds(lines=1), entrypoint=hooks.prompt)
        self.assertEqual(result["hookEventName"], "PreToolUse")
        self.assertEqual(result["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
