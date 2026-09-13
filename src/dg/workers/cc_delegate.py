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


def _config_path() -> Path:
    return Path.home() / ".delegation-governor" / "config.json"


def profile(endpoint_name: str, profile_name: str | None) -> dict:
    """Check that a configured worker endpoint has a matching delegate profile.

    Endpoint reachability alone is insufficient: a lane cannot launch if the
    MCP worker does not know the profile name printed in its work order. Only
    profile metadata is read here; credentials are never returned or logged.
    """
    if not profile_name:
        return {"ok": False, "reason": f"{endpoint_name}: no cc-delegate profile configured"}
    path = _config_path()
    try:
        profiles = (json.loads(path.read_text("utf-8")).get("profiles") or {})
    except (OSError, ValueError):
        return {"ok": False, "reason": f"{endpoint_name}: cc-delegate config not found"}
    if profile_name not in profiles:
        return {"ok": False,
                "reason": f"{endpoint_name}: cc-delegate profile {profile_name!r} not found"}
    return {"ok": True, "name": profile_name}

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
    lane: str | None = None, attempt_id: int | None = None,
) -> dict[str, Any]:
    """Attach a cc-delegate job to its durable lane reservation."""
    job = read_job(task["repo"], cc_task_id)
    reserved = store.live_attempt(con, task["id"])
    if attempt_id is not None and (not reserved or reserved["id"] != attempt_id):
        return {"ok": False, "error": f"attempt {attempt_id} is not the active reservation"}
    if not reserved or reserved["status"] != "RESERVED" or reserved["worker"] != "cc-delegate":
        return {"ok": False, "error": "task has no active cc-delegate reservation"}
    attempt_id = reserved["id"]
    if lane and reserved.get("lane") != lane:
        return {"ok": False, "error": f"reservation is for lane {reserved.get('lane')}"}
    ok = store.activate_attempt(
        con, attempt_id, f"cc:{cc_task_id}",
        worktree=(job or {}).get("worktree"), branch=(job or {}).get("branch"),
        log_path=str(job_file(task["repo"], cc_task_id)), external_job_id=cc_task_id)
    if not ok:
        return {"ok": False, "error": "reservation was already attached or released"}
    from .. import config
    (config.LOG_DIR / f"{task['id']}.a{attempt_id}.spec.md").write_text(work_order, "utf-8")
    return {"ok": True, "attemptId": attempt_id, "ccTaskId": cc_task_id,
            "lane": reserved.get("lane"), "jobFileFound": job is not None}


def sync(con: sqlite3.Connection, attempt: dict[str, Any]) -> str | None:
    """Reconcile one live cc-delegate attempt from its job file. Returns new status."""
    handle = attempt.get("handle") or ""
    if not handle.startswith("cc:"):
        return None
    job = read_job(attempt["repo"], handle[3:])
    store.touch_attempt(con, attempt["id"])
    if job is None:
        return None
    # `run_dev_task` may return before its job file is visible. Attach keeps
    # the reservation in that case; populate the integration coordinates as
    # soon as reconciliation can see them.
    worktree, branch = job.get("worktree"), job.get("branch")
    if (worktree and worktree != attempt.get("worktree")) or (
            branch and branch != attempt.get("branch")):
        con.execute("UPDATE attempts SET worktree=COALESCE(?,worktree),"
                    "branch=COALESCE(?,branch) WHERE id=?",
                    (worktree, branch, attempt["id"]))
    if branch:
        task = store.get_task(con, attempt["task_id"])
        if task and not task.get("baseCommit"):
            from .. import gitutil
            base = gitutil.git(attempt["repo"], "merge-base", branch, "HEAD",
                               check=False).stdout.strip()
            if base:
                con.execute("UPDATE tasks SET base_commit=? WHERE id=?",
                            (base, attempt["task_id"]))
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
