"""Shared harness: every test gets a throwaway DG_HOME and a fresh database."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path


class DGTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="dg-test-"))
        os.environ["DG_HOME"] = str(self.home)
        # config caches HOME at import time, so reload the module graph per test.
        from dg import config
        importlib.reload(config)
        for name in ("store", "scheduler", "supervisor", "quota_codex", "routing",
                     "sync", "launcher", "workorder", "install", "hooks"):
            mod = importlib.import_module(f"dg.{name}")
            importlib.reload(mod)
        for name in ("codex", "cc_delegate", "codex_runner"):
            importlib.reload(importlib.import_module(f"dg.workers.{name}"))
        from dg import store
        self.store = store
        self.con = store.connect()
        self.cfg = config.load()

    def tearDown(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass
        shutil.rmtree(self.home, ignore_errors=True)
        os.environ.pop("DG_HOME", None)

    # -- helpers ---------------------------------------------------------
    def task(self, title="t", mode="WRITE", deps=(), paths=(), repo="/repo", priority=0):
        return self.store.create_task(self.con, title=title, mode=mode, repo=repo,
                                      paths=paths, depends_on=deps, priority=priority)

    def states(self):
        from dg import scheduler
        return {r["id"]: r["state"] for r in scheduler.evaluate(self.con, self.cfg)["tasks"]}
