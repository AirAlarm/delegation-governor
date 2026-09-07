"""Codex worker adapter.

Dispatch is non-blocking: `dg` spawns a detached runner process that owns the
`codex exec` lifetime and writes the outcome back into the ledger itself. Claude
never waits, never polls, and never spends inference to learn a job finished
(spec 19, 22, 23).

`codex exec --json` emits JSONL events; the runner keeps the raw stream on disk
for diagnosis and classifies the terminal state from it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .. import config, gitutil, quota_codex, store

# Quota failures surface in the JSONL error events, not the exit code.
_QUOTA_MARKERS = ("rate_limit", "rate limit", "usage limit", "quota", "429",
                  "usage_limit_reached")
_AUTH_MARKERS = ("unauthorized", "401", "not logged in", "authentication",
                 "credentials", "re-authenticate")


def spec_path(task_id: str) -> Path:
    return config.LOG_DIR / f"{task_id}.spec.md"


def log_path(task_id: str, attempt: int) -> Path:
    return config.LOG_DIR / f"{task_id}.a{attempt}.codex.jsonl"


def dispatch(
    con: sqlite3.Connection, task: dict[str, Any], work_order: str, session_id: str,
    sandbox: str | None = None, lane: str = "codex",
) -> dict[str, Any]:
    """Start Codex on a task and return immediately."""
    config.ensure_home()
    exe = quota_codex.codex_bin()
    if not exe:
        return {"ok": False, "error": "codex CLI not on PATH"}

    repo = task["repo"] or os.getcwd()
    wt: dict[str, str] = {}
    if task["mode"] == "WRITE":
        if not gitutil.is_repo(repo):
            return {"ok": False, "error": f"{repo} is not a git repository"}
        wt = gitutil.create_worktree(repo, task["id"], task.get("baseCommit"))
        cwd, mode = wt["worktree"], sandbox or "workspace-write"
    else:
        cwd, mode = repo, sandbox or "read-only"

    attempt_id = store.add_attempt(
        con, task["id"], "codex", worktree=wt.get("worktree"), branch=wt.get("branch"),
        lane=lane)
    lp = log_path(task["id"], attempt_id)
    sp = spec_path(task["id"])
    sp.write_text(work_order, "utf-8")
    con.execute("UPDATE attempts SET log_path=? WHERE id=?", (str(lp), attempt_id))
    store.set_status(con, task["id"], "RUNNING")
    if wt:
        con.execute("UPDATE tasks SET base_commit=? WHERE id=?", (wt["baseCommit"], task["id"]))

    payload = {
        "taskId": task["id"], "attemptId": attempt_id, "repo": repo, "cwd": cwd,
        "sandbox": mode, "specPath": str(sp), "logPath": str(lp), "codex": exe,
        "worktree": wt.get("worktree"), "sessionId": session_id,
    }
    job = config.LOG_DIR / f"{task['id']}.a{attempt_id}.job.json"
    job.write_text(json.dumps(payload), "utf-8")

    pid = _spawn_runner(job)
    con.execute("UPDATE attempts SET handle=? WHERE id=?", (f"pid:{pid}", attempt_id))
    return {"ok": True, "attemptId": attempt_id, "pid": pid, "worktree": wt.get("worktree"),
            "branch": wt.get("branch"), "log": str(lp)}


def _spawn_runner(job_file: Path) -> int:
    """Detached child that outlives this `dg` invocation."""
    cmd = [sys.executable, "-m", "dg.workers.codex_runner", str(job_file)]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(Path(__file__).resolve().parents[2]),
        "env": {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: the latter gives a console
        # app its own console, which pops an empty terminal window on screen.
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs).pid


# ---------------------------------------------------------------- outcome

def classify(events: list[dict[str, Any]], returncode: int) -> dict[str, Any]:
    """Terminal state from the JSONL stream.

    Quota and auth are read out of the structured events; the free-text scan is
    the last-resort compatibility layer (spec 8), not the primary path.
    """
    reset_at = None
    blob_parts: list[str] = []
    for ev in events:
        s = json.dumps(ev)
        blob_parts.append(s)
        for key in ("resetsAt", "resets_at", "reset_at"):
            v = _deep_get(ev, key)
            if isinstance(v, (int, float)):
                reset_at = int(v)
    blob = " ".join(blob_parts).lower()

    if any(m in blob for m in _AUTH_MARKERS):
        return {"status": "AUTH_FAILED", "errorKind": "authentication expired",
                "resetsAt": None}
    if any(m in blob for m in _QUOTA_MARKERS):
        return {"status": "QUOTA_FAILED", "errorKind": "quota exhausted", "resetsAt": reset_at}
    if returncode == 0:
        return {"status": "SUCCEEDED", "errorKind": None, "resetsAt": None}
    return {"status": "FAILED", "errorKind": "worker execution failed", "resetsAt": None}


def _deep_get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _deep_get(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_get(v, key)
            if found is not None:
                return found
    return None


def read_events(path: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    out.append({"raw": line})
    except OSError:
        pass
    return out


def summarize(events: list[dict[str, Any]]) -> str:
    """Last assistant message, for the review hand-off. Bounded on purpose."""
    for ev in reversed(events):
        for key in ("last_agent_message", "message", "text"):
            v = _deep_get(ev, key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:4000]
    return ""


def alive(handle: str | None) -> bool:
    if not handle or not handle.startswith("pid:"):
        return False
    pid = int(handle[4:])
    if os.name == "nt":
        p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True)
        return str(pid) in p.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def elapsed(attempt: dict[str, Any]) -> float:
    return time.time() - float(attempt["started_at"])
