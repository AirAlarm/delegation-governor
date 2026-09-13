# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "deepagents>=0.7.0a6",
#   "langchain-litellm",
#   "fastapi",
# ]
# ///
"""Unit tests for worker.py's pure/isolable helpers: build_model (fallback
chains), the dangerous-git command guard, the drive-scan guard, and the
proactive steering mailbox (check_steer_message / _append_steer_notice).

Not part of server/'s stdlib-only suite (worker.py needs the heavy deepagents
+ litellm stack) — run directly: `uv run worker/test_worker.py`.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import worker


class TestBuildModel(unittest.TestCase):
    def test_no_fallbacks_returns_bare_string_unchanged(self):
        self.assertEqual(
            worker.build_model("litellm:minimax/MiniMax-M3", None),
            "litellm:minimax/MiniMax-M3",
        )

    def test_empty_fallback_list_returns_bare_string_unchanged(self):
        self.assertEqual(
            worker.build_model("litellm:minimax/MiniMax-M3", []),
            "litellm:minimax/MiniMax-M3",
        )

    def test_fallbacks_build_a_chatlitellm_instance(self):
        model = worker.build_model(
            "litellm:minimax/MiniMax-M3",
            ["litellm:minimax/MiniMax-Text-01", "litellm:anthropic/claude-haiku-4-5"],
        )
        from langchain_litellm import ChatLiteLLM

        self.assertIsInstance(model, ChatLiteLLM)
        self.assertEqual(model.model, "minimax/MiniMax-M3")
        self.assertEqual(
            model.model_kwargs["fallbacks"],
            ["minimax/MiniMax-Text-01", "anthropic/claude-haiku-4-5"],
        )

    def test_bare_model_strips_provider_prefix_only_once(self):
        self.assertEqual(worker._bare_model("litellm:minimax/MiniMax-M3"), "minimax/MiniMax-M3")
        self.assertEqual(worker._bare_model("no-prefix-model"), "no-prefix-model")

    def test_extra_headers_alone_build_a_chatlitellm_instance(self):
        # Without this, headers would be silently dropped: the no-fallback path
        # returns a bare string that deepagents resolves with no way to attach
        # per-request headers.
        model = worker.build_model(
            "litellm:anthropic/minimax-m3", None, {"x-opencode-session": "t_abc123"},
        )
        from langchain_litellm import ChatLiteLLM

        self.assertIsInstance(model, ChatLiteLLM)
        self.assertEqual(
            model.model_kwargs["extra_headers"], {"x-opencode-session": "t_abc123"},
        )
        self.assertNotIn("fallbacks", model.model_kwargs)

    def test_headers_and_fallbacks_coexist(self):
        model = worker.build_model(
            "litellm:anthropic/minimax-m3",
            ["litellm:anthropic/deepseek-v4-pro"],
            {"x-opencode-session": "t_abc123"},
        )
        self.assertEqual(model.model_kwargs["fallbacks"], ["anthropic/deepseek-v4-pro"])
        self.assertEqual(
            model.model_kwargs["extra_headers"], {"x-opencode-session": "t_abc123"},
        )


class TestOpencodeHeaders(unittest.TestCase):
    def test_opencode_host_gets_the_session_header(self):
        self.assertEqual(
            worker.opencode_headers("https://opencode.ai/zen/go", "t_abc123"),
            {"x-opencode-session": "t_abc123"},
        )

    def test_subdomain_of_opencode_is_matched(self):
        self.assertEqual(
            worker.opencode_headers("https://api.opencode.ai/zen/go", "t_1"),
            {"x-opencode-session": "t_1"},
        )

    def test_lookalike_host_gets_no_header(self):
        # Substring matching would leak the session id to an attacker-controlled
        # host; the check is on the parsed hostname.
        for base in ("https://opencode.ai.example.com/v1",
                     "https://notopencode.ai/v1",
                     "https://evil.com/?x=opencode.ai"):
            self.assertIsNone(worker.opencode_headers(base, "t_1"), base)

    def test_other_endpoints_and_no_api_base_get_no_header(self):
        self.assertIsNone(worker.opencode_headers("http://127.0.0.1:1234", "t_1"))
        self.assertIsNone(worker.opencode_headers(None, "t_1"))


class TestRubricVerdict(unittest.TestCase):
    def test_satisfied_succeeds_cleanly(self):
        self.assertEqual(worker.rubric_verdict("satisfied"), ("succeeded", None, None))

    def test_grader_error_succeeds_but_is_flagged_ungraded(self):
        # The regression this guards: a grader that fails to RUN is not a
        # verdict, and must not mark finished work `failed`.
        status, error, ungraded = worker.rubric_verdict("grader_error")
        self.assertEqual(status, "succeeded")
        self.assertIsNone(error)
        self.assertIn("NOT verified", ungraded)

    def test_real_unsatisfied_verdict_still_fails(self):
        # The other half: a grader that ran and said no must still fail, or the
        # fix above would turn the rubric into a no-op.
        status, error, ungraded = worker.rubric_verdict("unsatisfied")
        self.assertEqual(status, "failed")
        self.assertIn("unsatisfied", error)
        self.assertIsNone(ungraded)

    def test_unknown_or_missing_status_fails_closed(self):
        for bad in (None, "weird_new_status"):
            status, error, ungraded = worker.rubric_verdict(bad)
            self.assertEqual(status, "failed", bad)
            self.assertIsNone(ungraded, bad)


class TestDangerousGitGuard(unittest.TestCase):
    def _blocked(self, cmd: str) -> bool:
        return bool(worker._DANGEROUS_GIT_RE.search(cmd))

    def test_blocks_push_merge_rebase(self):
        self.assertTrue(self._blocked("git push origin main"))
        self.assertTrue(self._blocked("git merge feature-branch"))
        self.assertTrue(self._blocked("git rebase -i HEAD~3"))
        self.assertTrue(self._blocked("GIT PUSH origin main"))  # case-insensitive

    def test_does_not_block_readonly_lookalikes(self):
        self.assertFalse(self._blocked("git merge-base HEAD main"))
        self.assertFalse(self._blocked("git log --merges"))
        self.assertFalse(self._blocked("git status"))
        self.assertFalse(self._blocked("echo merge"))

    def test_backend_execute_rejects_dangerous_git(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            backend = worker.SupervisedShellBackend(
                root_dir=tmp, virtual_mode=True, timeout=5, inherit_env=False, env={},
            )
            result = backend.execute("git push origin main")
            self.assertEqual(result.exit_code, 1)
            self.assertIn("blocked", result.output)

            ok = backend.execute("echo hi")
            self.assertEqual(ok.exit_code, 0)


class TestSteerMessage(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_comm_dir = os.environ.get("DELEGATE_COMM_DIR")
        os.environ["DELEGATE_COMM_DIR"] = self._tmp.name

    def tearDown(self):
        if self._orig_comm_dir is None:
            os.environ.pop("DELEGATE_COMM_DIR", None)
        else:
            os.environ["DELEGATE_COMM_DIR"] = self._orig_comm_dir
        self._tmp.cleanup()

    def _write_steer(self, message: str) -> None:
        path = os.path.join(self._tmp.name, "steer.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"message": message}, f)

    def test_no_pending_message_returns_none(self):
        self.assertIsNone(worker.check_steer_message())

    def test_reads_and_clears_pending_message(self):
        self._write_steer("use snake_case instead")
        self.assertEqual(worker.check_steer_message(), "use snake_case instead")
        # Cleared: a second read finds nothing left.
        self.assertIsNone(worker.check_steer_message())

    def test_malformed_file_does_not_raise(self):
        path = os.path.join(self._tmp.name, "steer.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json")
        self.assertIsNone(worker.check_steer_message())

    def test_no_comm_dir_env_returns_none(self):
        os.environ.pop("DELEGATE_COMM_DIR", None)
        self.assertIsNone(worker.check_steer_message())

    def test_append_steer_notice_passthrough_when_nothing_pending(self):
        self.assertEqual(worker._append_steer_notice("progress update delivered"), "progress update delivered")

    def test_append_steer_notice_appends_when_pending(self):
        self._write_steer("stop, use a different filename")
        text = worker._append_steer_notice("progress update delivered")
        self.assertIn("progress update delivered", text)
        self.assertIn("SUPERVISOR STEERING", text)
        self.assertIn("stop, use a different filename", text)

    def test_shell_backend_execute_surfaces_pending_steer(self):
        import tempfile

        self._write_steer("check the edge case for empty input")
        with tempfile.TemporaryDirectory() as tmp:
            backend = worker.SupervisedShellBackend(
                root_dir=tmp, virtual_mode=True, timeout=5, inherit_env=False, env={},
            )
            result = backend.execute("echo hi")
        self.assertIn("hi", result.output)
        self.assertIn("SUPERVISOR STEERING", result.output)
        self.assertIn("check the edge case for empty input", result.output)


if __name__ == "__main__":
    unittest.main()
