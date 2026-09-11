"""Safe single-file writes for ephemeral worker output."""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dg import cli, safewrite


class TestSafewrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="dg-safewrite-")
        self.root = Path(self.tempdir.name)
        self.source = self.root / "worker-output.txt"
        self.target = self.root / "generated.py"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, *, force: bool = False) -> tuple[int, str, str]:
        argv = ["safewrite", str(self.target), "--from", str(self.source)]
        if force:
            argv.append("--force")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cli_writes_new_target_byte_for_byte(self):
        content = b"answer = 42\r\n"
        self.source.write_bytes(content)

        result, stdout, stderr = self._run()

        self.assertEqual(result, 0)
        self.assertEqual(self.target.read_bytes(), content)
        self.assertEqual(stdout, f"{self.target}\n")
        self.assertEqual(stderr, "")

    def test_existing_target_requires_force_and_is_untouched(self):
        self.source.write_bytes(b"new content\n")
        self.target.write_bytes(b"keep this\n")

        result, _, stderr = self._run()

        self.assertNotEqual(result, 0)
        self.assertEqual(self.target.read_bytes(), b"keep this\n")
        self.assertIn("target already exists", stderr)

    def test_force_replaces_existing_target(self):
        self.source.write_bytes(b"new content\n")
        self.target.write_bytes(b"old content\n")

        result, _, _ = self._run(force=True)

        self.assertEqual(result, 0)
        self.assertEqual(self.target.read_bytes(), b"new content\n")

    def test_only_outer_fence_is_removed(self):
        self.source.write_bytes(
            b"```python\nvalue = '''\n```text\ninside\n```\n'''\n```\n"
        )

        result, _, _ = self._run()

        self.assertEqual(result, 0)
        self.assertEqual(
            self.target.read_bytes(),
            b"value = '''\n```text\ninside\n```\n'''\n",
        )

    def test_non_wrapping_fences_are_unchanged(self):
        content = b"intro\n```python\nanswer = 42\n```\noutro\n"
        self.source.write_bytes(content)

        result, _, _ = self._run()

        self.assertEqual(result, 0)
        self.assertEqual(self.target.read_bytes(), content)

    def test_failed_atomic_replace_preserves_target_and_cleans_temp(self):
        self.source.write_bytes(b"replacement\n")
        self.target.write_bytes(b"original\n")

        with mock.patch.object(safewrite.os, "replace", side_effect=OSError("disk error")):
            result, _, stderr = self._run(force=True)

        self.assertNotEqual(result, 0)
        self.assertEqual(self.target.read_bytes(), b"original\n")
        self.assertIn("disk error", stderr)
        self.assertEqual(list(self.root.glob(f".{self.target.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
