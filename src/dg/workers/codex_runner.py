"""Detached child that owns one `codex exec` run and reports back to the ledger.

Runs outside the Claude session entirely: it survives `dg` exiting, streams the
JSONL to disk, and on completion writes the attempt + task status itself. That
is what makes delegation non-blocking with no polling and no Claude inference.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .. import config, gitutil, quota_codex, store
from . import codex


def main(job_file: str) -> int:
    job = json.loads(Path(job_file).read_text("utf-8"))
    spec = Path(job["specPath"]).read_text("utf-8")
    log = Path(job["logPath"])

    cmd = [job["codex"], "exec", "--json", "--skip-git-repo-check",
           "-C", job["cwd"], "-s", job["sandbox"], "-"]
    returncode = 1
    try:
        with open(log, "w", encoding="utf-8") as fh:
            p = subprocess.run(cmd, input=spec, stdout=fh, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")
        returncode = p.returncode
    except OSError as e:
        log.write_text(json.dumps({"error": str(e)}) + "\n", "utf-8")

    events = codex.read_events(log)
    verdict = codex.classify(events, returncode)

    con = store.connect()
    try:
        _record(con, job, verdict, events, log)
    finally:
        con.close()
    return 0


def _record(con, job, verdict, events, log) -> None:
    task_id, attempt_id = job["taskId"], job["attemptId"]
    status = verdict["status"]
    store.finish_attempt(con, attempt_id, status, verdict["errorKind"],
                         codex.summarize(events)[:500])

    if status == "QUOTA_FAILED":
        # Codex died of quota mid-task: record the reset, flip the worker state
        # so the next dispatch skips Codex entirely, and leave the partial
        # worktree in place for diagnosis -- never integrated automatically.
        quota_codex.mark_exhausted(con, verdict.get("resetsAt"), "quota failure during exec")
        store.set_status(con, task_id, "QUOTA_FAILED",
                         failure_reason="codex quota exhausted mid-task")
        _note_partial(con, job, attempt_id)
        return
    if status == "AUTH_FAILED":
        store.kv_set(con, quota_codex.K_STATE, quota_codex.AUTH_ERROR)
        store.set_status(con, task_id, "AUTH_FAILED",
                         failure_reason="codex authentication failed")
        _note_partial(con, job, attempt_id)
        return
    if status == "SUCCEEDED":
        result = _write_result(job, events)
        # A WRITE run that exits clean but changes nothing is usually the
        # worker declining the task (spec too vague, nothing to do). It is not
        # a failure, but it must not look like completed work either.
        note = None
        if job.get("worktree") and not result_changed(result):
            note = "worker exited cleanly but changed no files -- read its summary"
        store.set_status(con, task_id, "SUCCEEDED", failure_reason=note,
                         result_location=str(result))
        return
    store.set_status(con, task_id, "FAILED", failure_reason="codex run failed")
    _note_partial(con, job, attempt_id)


def result_changed(result_path) -> bool:
    try:
        return bool(json.loads(Path(result_path).read_text("utf-8")).get("changedFiles"))
    except (OSError, ValueError):
        return True  # unknown: do not cry wolf


def _note_partial(con, job, attempt_id) -> None:
    wt = job.get("worktree")
    if not wt or not Path(wt).exists():
        return
    files = gitutil.changed_files(wt)
    if files:
        con.execute("UPDATE attempts SET error_detail=COALESCE(error_detail,'')||? WHERE id=?",
                    (f" | partial work preserved in {wt} ({len(files)} files)", attempt_id))


def _write_result(job, events) -> Path:
    out = config.LOG_DIR / f"{job['taskId']}.a{job['attemptId']}.result.json"
    payload = {
        "taskId": job["taskId"], "attemptId": job["attemptId"], "worker": "codex",
        "worktree": job.get("worktree"), "summary": codex.summarize(events),
    }
    if job.get("worktree") and Path(job["worktree"]).exists():
        payload["changedFiles"] = gitutil.changed_files(job["worktree"])
        diff_path = config.LOG_DIR / f"{job['taskId']}.a{job['attemptId']}.diff"
        diff_path.write_text(gitutil.diff(job["worktree"]), "utf-8")
        payload["diff"] = str(diff_path)
    out.write_text(json.dumps(payload, indent=2), "utf-8")
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
