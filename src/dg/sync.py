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
from .workers import codex_plugin


def reconcile(con: sqlite3.Connection, cfg: dict, force: bool = False) -> dict[str, Any]:
    changed: list[dict[str, str]] = []
    slow: list[str] = []
    wcfg = cfg["workers"]
    now = time.time()

    for a in store.running_attempts(con):
        if a["status"] == "RESERVED":
            # A reservation occupies capacity but has no external job yet.
            # It is released only explicitly: the MCP/plugin launch may have
            # succeeded even if its acknowledgement was lost.
            continue
        age = now - float(a["started_at"])
        last = a.get("last_checked_at") or 0
        if (not force and now - last < wcfg["minCheckSpacingSeconds"]
                and age < wcfg["slowAfterSeconds"]):
            continue  # respect minimum check spacing (spec 22)

        if a["worker"] == "cc-delegate":
            new = cc_delegate.sync(con, a)
            if new:
                changed.append({"task": a["task_id"], "status": new, "worker": a["worker"]})
                continue
        elif a["worker"] == "codex":
            new = codex_plugin.sync(con, a)
            if new:
                changed.append({"task": a["task_id"], "status": new,
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
