"""Stateless corpus preparation for one-off read-only delegation."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dg import cli, quickread


class TestQuickread(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="dg-quickread-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, *paths: Path) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(["quickread", *(str(path) for path in paths)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cli_emits_counts_estimate_and_xml_corpus(self):
        first = self.root / "one.py"
        second = self.root / "two.py"
        first.write_bytes(b"alpha\nbeta\n")
        second.write_bytes(b"gamma")

        result, stdout, stderr = self._run(first, second)

        self.assertEqual(result, 0)
        self.assertEqual(
            stdout,
            f'<file path="{first}">\nalpha\nbeta\n</file>\n\n'
            f'<file path="{second}">\ngamma</file>\n\n',
        )
        self.assertIn(f"{first}: 2 lines, 11 bytes", stderr)
        self.assertIn(f"{second}: 1 line, 5 bytes", stderr)
        self.assertIn(f"[quickread: 2 files, ~{len(stdout) // 4} input tokens]", stderr)

    def test_path_with_spaces_is_one_normal_argv_value(self):
        path = self.root / "directory with spaces" / "a file.py"
        path.parent.mkdir()
        path.write_text("answer = 42\n", encoding="utf-8")

        result, stdout, _ = self._run(path)

        self.assertEqual(result, 0)
        self.assertIn(f'<file path="{path}">', stdout)
        self.assertIn("answer = 42", stdout)

    def test_missing_path_fails_without_partial_corpus(self):
        existing = self.root / "existing.py"
        existing.write_text("must not be printed\n", encoding="utf-8")

        result, stdout, stderr = self._run(existing, self.root / "missing.py")

        self.assertNotEqual(result, 0)
        self.assertEqual(stdout, "")
        self.assertIn("file not found", stderr)
        self.assertIn("missing.py", stderr)

    def test_unreadable_file_is_explicit_and_not_skipped(self):
        path = self.root / "blocked.py"
        path.write_text("secret\n", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        args = type("Args", (), {"paths": [str(path)]})()

        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = quickread.run(args)

        self.assertNotEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unreadable file", stderr.getvalue())
        self.assertIn("blocked.py", stderr.getvalue())
        self.assertIn("denied", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
