"""Reconcile running attempts with reality. Cheap, no inference, no MCP calls.

Called by `dg status`, `dg tasks`, the statusline and the prompt hook -- so the
ledger is fresh whenever anyone looks at it, and never because Claude spent a
turn asking a worker "are you done yet".
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import store
from .workers import cc_delegate
from .workers import codex as codex_worker


def reconcile(con: sqlite3.Connection, cfg: dict) -> dict[str, Any]:
    changed: list[dict[str, str]] = []
    slow: list[str] = []
    wcfg = cfg["workers"]
    now = time.time()

    for a in store.running_attempts(con):
        age = now - float(a["started_at"])
        last = a.get("last_checked_at") or 0
        if now - last < wcfg["minCheckSpacingSeconds"] and age < wcfg["slowAfterSeconds"]:
            continue  # respect minimum check spacing (spec 22)

        if a["worker"] == "cc-delegate":
            new = cc_delegate.sync(con, a)
            if new:
                changed.append({"task": a["task_id"], "status": new, "worker": a["worker"]})
                continue
        elif a["worker"] == "codex":
            store.touch_attempt(con, a["id"])
            # The runner writes the outcome itself; a dead runner that wrote
            # nothing is the only case we have to clean up here.
            if not codex_worker.alive(a.get("handle")):
                fresh = con.execute("SELECT status FROM attempts WHERE id=?",
                                    (a["id"],)).fetchone()
                if fresh and fresh["status"] == "RUNNING":
                    store.finish_attempt(con, a["id"], "FAILED", "worker execution failed",
                                         "runner exited without recording a result")
                    store.set_status(con, a["task_id"], "FAILED",
                                     failure_reason="codex runner vanished")
                    changed.append({"task": a["task_id"], "status": "FAILED",
                                    "worker": "codex"})
                    continue

        hard = wcfg["hardTimeoutSeconds"]
        if hard and age > hard:
            store.finish_attempt(con, a["id"], "FAILED", "worker timeout",
                                 f"exceeded hard timeout {hard}s")
            store.set_status(con, a["task_id"], "FAILED", failure_reason="worker timeout")
            changed.append({"task": a["task_id"], "status": "FAILED", "worker": a["worker"]})
        elif age > wcfg["slowAfterSeconds"]:
            # SLOW is a label, not a failure (spec 27/28). Nothing is killed and
            # nothing is duplicated -- a cold local model looks exactly like this.
            slow.append(a["task_id"])

    return {"changed": changed, "slow": slow}
