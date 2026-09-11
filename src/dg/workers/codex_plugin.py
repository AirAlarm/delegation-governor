"""Official Claude Codex plugin worker adapter.

The Governor owns scheduling and isolation; the OpenAI plugin owns the Codex
conversation and process lifecycle.  Jobs are started through the same
``codex-companion.mjs`` runtime used by the plugin's slash commands, so they
remain visible to ``/codex:status`` and retain the plugin's durable results.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import config, gitutil, quota_codex, store
from . import codex as legacy_codec

PLUGIN_ID = "codex@openai-codex"
MARKER = "delegation-governor"


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def discover() -> dict[str, Any]:
    """Resolve the active plugin without depending on a versioned cache path."""
    forced = os.environ.get("DG_CODEX_PLUGIN_ROOT")
    if forced:
        root = Path(forced)
        version = _manifest_version(root)
        return _capabilities(root, version, enabled=True)

    claude = _claude_dir()
    try:
        installed = json.loads((claude / "plugins" / "installed_plugins.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "reason": "official Codex plugin is not installed"}
    entries = (installed.get("plugins") or {}).get(PLUGIN_ID) or []
    if not entries:
        return {"ok": False, "reason": "official Codex plugin is not installed"}
    entry = sorted(entries, key=lambda x: str(x.get("installedAt", "")))[-1]
    root = Path(entry.get("installPath") or "")

    enabled = True
    try:
        settings = json.loads((claude / "settings.json").read_text("utf-8"))
        enabled = (settings.get("enabledPlugins") or {}).get(PLUGIN_ID, True) is not False
    except (OSError, ValueError):
        pass
    return _capabilities(root, str(entry.get("version") or _manifest_version(root)), enabled)


def _manifest_version(root: Path) -> str:
    try:
        return str(json.loads((root / ".claude-plugin" / "plugin.json").read_text("utf-8"))
                   .get("version") or "unknown")
    except (OSError, ValueError):
        return "unknown"


def _capabilities(root: Path, version: str, enabled: bool) -> dict[str, Any]:
    script = root / "scripts" / "codex-companion.mjs"
    if not enabled:
        return {"ok": False, "reason": f"{PLUGIN_ID} is disabled", "root": str(root),
                "version": version}
    if not script.is_file():
        return {"ok": False, "reason": "Codex plugin companion runtime is missing",
                "root": str(root), "version": version}
    node = shutil.which("node")
    if not node:
        return {"ok": False, "reason": "node is required by the Codex plugin",
                "root": str(root), "version": version}
    return {"ok": True, "root": str(root), "script": str(script), "node": node,
            "version": version}


def setup_status(timeout: float = 30.0) -> dict[str, Any]:
    info = discover()
    if not info["ok"]:
        return info
    p = _run([info["node"], info["script"], "setup", "--json"], timeout,
             env=_plugin_env())
    payload = _json_output(p.stdout)
    return {**info, "ready": p.returncode == 0 and payload.get("ready") is True,
            "setup": payload,
            "detail": (p.stderr or p.stdout).strip()[:500]}


def spec_path(task_id: str, attempt_id: int) -> Path:
    return config.LOG_DIR / f"{task_id}.a{attempt_id}.spec.md"


def dispatch(con: sqlite3.Connection, task: dict[str, Any], work_order: str,
             attempt_id: int) -> dict[str, Any]:
    """Start one isolated background job through the official plugin runtime."""
    info = discover()
    if not info["ok"]:
        return {"ok": False, "error": info["reason"] + "; run /codex:setup"}

    repo = task["repo"] or os.getcwd()
    wt: dict[str, str] = {}
    if task["mode"] == "WRITE":
        if not gitutil.is_repo(repo):
            return {"ok": False, "error": f"{repo} is not a git repository"}
        try:
            wt = gitutil.create_worktree(repo, task["id"], task.get("baseCommit"))
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        cwd = wt["worktree"]
    else:
        cwd = repo

    config.ensure_home()
    sp = spec_path(task["id"], attempt_id)
    marker = (f"\n\n<{MARKER} task_id=\"{task['id']}\" "
              f"attempt_id=\"{attempt_id}\" />\n")
    sp.write_text(work_order + marker, "utf-8")
    cmd = [info["node"], info["script"], "task", "--background", "--fresh", "--json",
           "--cwd", cwd, "--prompt-file", str(sp)]
    if task["mode"] == "WRITE":
        cmd.append("--write")
    try:
        p = _run(cmd, 45, env=_plugin_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        _discard_empty_worktree(repo, wt)
        return {"ok": False, "error": f"Codex plugin launch failed: {e}"}
    payload = _json_output(p.stdout)
    job_id = str(payload.get("jobId") or "")
    if p.returncode != 0 or not job_id:
        _discard_empty_worktree(repo, wt)
        detail = (p.stderr or p.stdout or "plugin returned no job id").strip()[:800]
        return {"ok": False, "error": f"Codex plugin launch failed: {detail}"}

    jf = find_job_file(job_id)
    log = str(payload.get("logFile") or ((read_job(job_id) or {}).get("logFile") or ""))
    if not store.activate_attempt(
            con, attempt_id, f"codex-plugin:{job_id}", worktree=wt.get("worktree"),
            branch=wt.get("branch"), log_path=log or (str(jf) if jf else None),
            external_job_id=job_id):
        return {"ok": False, "error": "reservation disappeared after Codex launch",
                "orphanedJobId": job_id}
    if wt:
        con.execute("UPDATE tasks SET base_commit=? WHERE id=?",
                    (wt["baseCommit"], task["id"]))
    return {"ok": True, "attemptId": attempt_id, "jobId": job_id,
            "worktree": wt.get("worktree"), "branch": wt.get("branch"),
            "log": log or None, "pluginVersion": info.get("version")}


def _discard_empty_worktree(repo: str, wt: dict[str, str]) -> None:
    if wt and not gitutil.changed_files(wt["worktree"]):
        gitutil.remove_worktree(repo, wt["worktree"], wt.get("branch"), force=True)


def _json_output(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        for line in reversed(text.splitlines()):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
            except ValueError:
                continue
    return {}


def _plugin_data() -> Path:
    """Stable state for jobs launched outside a live Claude plugin process."""
    return Path(os.environ.get("DG_CODEX_PLUGIN_DATA") or
                (config.HOME / "codex-plugin-data"))


def _plugin_env() -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(_plugin_data())
    return env


def _run(cmd: list[str], timeout: float, env: dict[str, str] | None = None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _job_roots() -> list[Path]:
    """All state layouts used by current and older companion releases.

    Direct companion invocations have no ``CLAUDE_PLUGIN_DATA`` unless we set
    it, so plugin 1.0.4 falls back to the OS temp directory. Keep that layout
    discoverable so an interrupted upgrade can still recover already-launched
    jobs; new Governor jobs use the stable managed directory above.
    """
    roots: list[Path] = []
    state_roots = [
        _plugin_data() / "state",
        _claude_dir() / "plugins" / "data" / "codex-openai-codex" / "state",
        Path(tempfile.gettempdir()) / "codex-companion",
    ]
    for state in state_roots:
        roots.extend(state.glob("*/jobs"))
    return list(dict.fromkeys(roots))


def find_job_file(job_id: str) -> Path | None:
    for root in _job_roots():
        path = root / f"{job_id}.json"
        if path.is_file():
            return path
    return None


def read_job(job_id: str) -> dict[str, Any] | None:
    path = find_job_file(job_id)
    try:
        return json.loads(path.read_text("utf-8")) if path else None
    except (OSError, ValueError):
        return None


def sync(con: sqlite3.Connection, attempt: dict[str, Any]) -> str | None:
    job_id = attempt.get("external_job_id") or str(attempt.get("handle") or "").removeprefix(
        "codex-plugin:")
    if not job_id:
        return None
    job = read_job(job_id)
    store.touch_attempt(con, attempt["id"])
    if not job:
        return None
    status = str(job.get("status") or "").lower()
    if status in ("queued", "running", "starting", "in_progress"):
        return None
    if status in ("cancelled", "canceled"):
        mapped, error = "CANCELLED", "Codex plugin job cancelled"
    elif status == "completed" and int((job.get("result") or {}).get("status", 0) or 0) == 0:
        mapped, error = "SUCCEEDED", None
    else:
        # Never classify the stored request prompt: a task *about* quota or
        # authentication must not turn an unrelated crash into QUOTA_FAILED.
        evidence = {
            "result": job.get("result"),
            "errorMessage": job.get("errorMessage"),
            "rendered": job.get("rendered"),
            "progressPreview": job.get("progressPreview"),
        }
        verdict = legacy_codec.classify(
            [evidence], int((job.get("result") or {}).get("status", 1) or 1))
        mapped, error = verdict["status"], verdict["errorKind"]
        if mapped == "QUOTA_FAILED":
            quota_codex.mark_exhausted(con, verdict.get("resetsAt"),
                                       "quota failure in Codex plugin job")
    path = find_job_file(job_id)
    detail = str((job.get("result") or {}).get("rawOutput") or
                 job.get("errorMessage") or job.get("summary") or "")[:500]
    store.finish_attempt(con, attempt["id"], mapped, error, detail)
    store.set_status(con, attempt["task_id"], mapped,
                     failure_reason=error,
                     result_location=str(path) if path else None)
    return mapped


def cancel(attempt: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    info = discover()
    if not info["ok"]:
        return info
    job_id = attempt.get("external_job_id") or str(attempt.get("handle") or "").removeprefix(
        "codex-plugin:")
    if not job_id:
        return {"ok": False, "reason": "attempt has no Codex plugin job id"}
    cwd = attempt.get("worktree") or attempt.get("repo") or os.getcwd()
    p = _run([info["node"], info["script"], "cancel", job_id, "--json",
              "--cwd", cwd], timeout, env=_plugin_env())
    return {"ok": p.returncode == 0, "jobId": job_id,
            "result": _json_output(p.stdout), "detail": (p.stderr or "").strip()[:500]}


def recovery_candidates(task_id: str, attempt_id: int) -> list[dict[str, Any]]:
    needle1, needle2 = f'task_id="{task_id}"', f'attempt_id="{attempt_id}"'
    out = []
    for root in _job_roots():
        for path in root.glob("task-*.json"):
            try:
                job = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            prompt = str((job.get("request") or {}).get("prompt") or "")
            if MARKER in prompt and needle1 in prompt and needle2 in prompt:
                out.append({"jobId": job.get("id"), "status": job.get("status"),
                            "jobFile": str(path), "workspaceRoot": job.get("workspaceRoot")})
    return out
