"""cc-delegate adapter.

cc-delegate is an MCP server, so only Claude can *start* a job (mcp
run_dev_task) -- the Governor cannot call MCP tools. That is fine: cc-delegate
already persists each job to `<repo>/.cc-delegate/jobs/<taskId>.json`, so the
Governor syncs status straight off disk. No MCP call, no Claude inference, no
busy-polling the worker (spec 22/23).

The existing cc-delegate install is read, never written.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import store

WORK_DIR = ".cc-delegate"

# cc-delegate job["status"] -> ledger status. Anything unlisted stays RUNNING.
_MAP = {
    "running": None,
    "succeeded": "SUCCEEDED",
    "success": "SUCCEEDED",
    "passed": "SUCCEEDED",
    "completed": "SUCCEEDED",
    "failed": "FAILED",
    "error": "FAILED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "timeout": "FAILED",
    "timed_out": "FAILED",
}


def job_file(repo: str, cc_task_id: str) -> Path:
    return Path(repo) / WORK_DIR / "jobs" / f"{cc_task_id}.json"


def read_job(repo: str, cc_task_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(job_file(repo, cc_task_id).read_text("utf-8"))
    except (OSError, ValueError):
        return None


def attach(
    con: sqlite3.Connection, task: dict[str, Any], cc_task_id: str, work_order: str,
    lane: str | None = None,
) -> dict[str, Any]:
    """Register a cc-delegate job Claude just launched against a ledger task."""
    job = read_job(task["repo"], cc_task_id)
    attempt_id = store.add_attempt(
        con, task["id"], "cc-delegate", handle=f"cc:{cc_task_id}",
        worktree=(job or {}).get("worktree"), branch=(job or {}).get("branch"),
        log_path=str(job_file(task["repo"], cc_task_id)), lane=lane,
    )
    from .. import config
    (config.LOG_DIR / f"{task['id']}.spec.md").write_text(work_order, "utf-8")
    store.set_status(con, task["id"], "RUNNING")
    return {"ok": True, "attemptId": attempt_id, "ccTaskId": cc_task_id,
            "lane": lane, "jobFileFound": job is not None}


def sync(con: sqlite3.Connection, attempt: dict[str, Any]) -> str | None:
    """Reconcile one live cc-delegate attempt from its job file. Returns new status."""
    handle = attempt.get("handle") or ""
    if not handle.startswith("cc:"):
        return None
    job = read_job(attempt["repo"], handle[3:])
    store.touch_attempt(con, attempt["id"])
    if job is None:
        return None
    mapped = _MAP.get(str(job.get("status", "")).lower())
    if mapped is None:
        return None
    store.finish_attempt(con, attempt["id"], mapped,
                         None if mapped == "SUCCEEDED" else "worker execution failed",
                         str(job.get("error") or job.get("failureReason") or "")[:500])
    store.set_status(
        con, attempt["task_id"], mapped,
        failure_reason=None if mapped == "SUCCEEDED" else f"cc-delegate {job.get('status')}",
        result_location=str(job_file(attempt["repo"], handle[3:])),
    )
    return mapped
