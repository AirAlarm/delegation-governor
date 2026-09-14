"""Persistent Governor state: SQLite so concurrent Claude sessions can share it.

SQLite gives atomic writes, cross-process locking and atomic task claiming for
free; a JSON file plus hand-rolled locking would be strictly more code and less
correct on Windows. WAL keeps readers (statusline, hooks) off the writers' back.

No credentials are ever stored here -- only env var *names*.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA_VERSION = 3

# READY and BLOCKED are never stored: they are derived from PLANNED + the
# dependency graph, so the ledger cannot go stale against its own edges.
STORED_STATUSES = {
    "PLANNED", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED",
    "QUOTA_FAILED", "AUTH_FAILED", "SUPERSEDED", "CANCELLED", "INTEGRATED",
}
TERMINAL_OK = {"INTEGRATED"}
TERMINAL_BAD = {"FAILED", "QUOTA_FAILED", "AUTH_FAILED", "CANCELLED"}
ACTIVE = {"QUEUED", "RUNNING"}
FALLBACK_FROM = {"QUOTA_FAILED", "AUTH_FAILED", "FAILED", "CANCELLED"}
ATTEMPT_ACTIVE = {"RESERVED", "QUEUED", "RUNNING"}

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    base_commit TEXT,
    paths TEXT NOT NULL DEFAULT '[]',
    depends_on TEXT NOT NULL DEFAULT '[]',
    owner TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    task_class TEXT NOT NULL DEFAULT 'standard',
    session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result_location TEXT,
    failure_reason TEXT
    ,retry_of TEXT
    ,superseded_by TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker TEXT NOT NULL,
    lane TEXT,
    status TEXT NOT NULL,
    handle TEXT,
    worktree TEXT,
    branch TEXT,
    log_path TEXT,
    error_kind TEXT,
    error_detail TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    last_checked_at REAL
    ,profile TEXT
    ,transport TEXT
    ,external_job_id TEXT
    ,reserved_at REAL
);
CREATE INDEX IF NOT EXISTS attempts_task ON attempts(task_id);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
"""


class Connection(sqlite3.Connection):
    """Compatibility guard for callers that predate ``store.session``."""

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def connect() -> sqlite3.Connection:
    config.ensure_home()
    con = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None,
                          factory=Connection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_DDL)
    try:
        _migrate(con)
    except Exception:
        con.close()  # don't leak the handle when the db is unusable
        raise
    return con


@contextmanager
def session():
    """A short-lived connection that is always closed by the caller."""
    con = connect()
    try:
        yield con
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    row = con.execute("SELECT value FROM meta WHERE key='schemaVersion'").fetchone()
    if row is None:
        con.execute("INSERT INTO meta VALUES ('schemaVersion', ?)", (str(SCHEMA_VERSION),))
        return
    have = int(row["value"])
    if have > SCHEMA_VERSION:
        raise RuntimeError(
            f"governor.db is schema v{have}, this dg understands v{SCHEMA_VERSION}. Upgrade dg."
        )
    if have < SCHEMA_VERSION:
        _upgrade(con, have)
        con.execute("UPDATE meta SET value=? WHERE key='schemaVersion'",
                    (str(SCHEMA_VERSION),))


def _upgrade(con: sqlite3.Connection, have: int) -> None:
    """Additive column adds; CREATE TABLE IF NOT EXISTS already covers new dbs.

    Existing rows keep working: an unclassified task is `standard`, and an
    attempt with no lane predates lanes entirely.
    """
    cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}
    if have < 2 and "task_class" not in cols:
        con.execute("ALTER TABLE tasks ADD COLUMN task_class TEXT NOT NULL "
                    "DEFAULT 'standard'")
    acols = {r["name"] for r in con.execute("PRAGMA table_info(attempts)")}
    if have < 2 and "lane" not in acols:
        con.execute("ALTER TABLE attempts ADD COLUMN lane TEXT")
    if have < 3:
        for name, ddl in (("retry_of", "TEXT"), ("superseded_by", "TEXT")):
            if name not in cols:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        for name, ddl in (("profile", "TEXT"), ("transport", "TEXT"),
                          ("external_job_id", "TEXT"), ("reserved_at", "REAL")):
            if name not in acols:
                con.execute(f"ALTER TABLE attempts ADD COLUMN {name} {ddl}")


# ---------------------------------------------------------------- kv state

def kv_get(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value), time.time()),
    )


def kv_age(con: sqlite3.Connection, key: str) -> float | None:
    row = con.execute("SELECT updated_at FROM kv WHERE key=?", (key,)).fetchone()
    return None if row is None else time.time() - row["updated_at"]


# ---------------------------------------------------------------- claiming

class transaction:
    """BEGIN IMMEDIATE .. COMMIT. Takes the write lock up front so two sessions
    cannot both read-then-claim the same READY task."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __enter__(self) -> sqlite3.Connection:
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# ---------------------------------------------------------------- tasks

def repo_slug(repo: str) -> str:
    """Short, id-safe project name from a repo path, e.g. .../delegation-governor -> 'delegation-governor'.

    A single global DG-N counter meant a fresh project's first task looked
    like DG-45. Slugging by repo gives each project its own counter (starting
    at 1) while ids stay globally unique -- no primary-key or dependency-
    reference change needed, just what next_id() computes.
    """
    name = Path(repo).name.lower() if repo else ""
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return slug or "dg"


def next_id(con: sqlite3.Connection, repo: str = "") -> str:
    """Stable, monotonic <repo-slug>-N ids. Never reuses an id, even after deletion.

    Counted per repo (via repo_slug), not globally -- existing DG-N ids from
    before this change are untouched and stay valid as historical references.
    """
    slug = repo_slug(repo)
    key = f"lastTaskNumber:{slug}"
    n = int(kv_get(con, key, 0)) + 1
    kv_set(con, key, n)
    return f"{slug}-{n}"


def create_task(
    con: sqlite3.Connection,
    title: str,
    mode: str = "WRITE",
    goal: str = "",
    repo: str = "",
    paths: Iterable[str] = (),
    depends_on: Iterable[str] = (),
    owner: str | None = None,
    priority: int = 0,
    base_commit: str | None = None,
    task_class: str = "standard",
    retry_of: str | None = None,
) -> str:
    mode = mode.upper()
    if mode not in ("READ_ONLY", "WRITE"):
        raise ValueError("mode must be READ_ONLY or WRITE")
    deps = list(depends_on)
    abs_repo = os.path.abspath(repo) if repo else ""
    now = time.time()
    with transaction(con):
        for d in deps:
            if con.execute("SELECT 1 FROM tasks WHERE id=?", (d,)).fetchone() is None:
                raise ValueError(f"unknown dependency {d}")
        tid = next_id(con, abs_repo)
        con.execute(
            "INSERT INTO tasks(id,title,mode,status,goal,repo,base_commit,paths,depends_on,"
            "owner,priority,task_class,created_at,updated_at,retry_of)"
            " VALUES(?,?,?,'PLANNED',?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, mode, goal, abs_repo, base_commit,
             json.dumps(list(paths)), json.dumps(deps), owner, priority, task_class,
             now, now, retry_of),
        )
    return tid


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["paths"] = json.loads(d["paths"])
    d["dependsOn"] = json.loads(d.pop("depends_on"))
    d["baseCommit"] = d.pop("base_commit")
    d["resultLocation"] = d.pop("result_location")
    d["failureReason"] = d.pop("failure_reason")
    d["createdAt"] = d.pop("created_at")
    d["updatedAt"] = d.pop("updated_at")
    d["sessionId"] = d.pop("session_id")
    d["taskClass"] = d.pop("task_class", "standard")
    d["retryOf"] = d.pop("retry_of", None)
    d["supersededBy"] = d.pop("superseded_by", None)
    return d


def get_task(con: sqlite3.Connection, tid: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return _task_row(row) if row else None


def all_tasks(con: sqlite3.Connection) -> list[dict[str, Any]]:
    # Insertion order, not the id string: ids are no longer a fixed-width
    # "DG-" prefix + number, since next_id() now scopes by repo (repo_slug).
    rows = con.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()
    return [_task_row(r) for r in rows]


def set_status(
    con: sqlite3.Connection, tid: str, status: str,
    failure_reason: str | None = None, result_location: str | None = None,
) -> None:
    if status not in STORED_STATUSES:
        raise ValueError(f"unknown status {status}")
    con.execute(
        "UPDATE tasks SET status=?, updated_at=?,"
        " failure_reason=COALESCE(?,failure_reason), result_location=COALESCE(?,result_location)"
        " WHERE id=?",
        (status, time.time(), failure_reason, result_location, tid),
    )
    if status in TERMINAL_OK | TERMINAL_BAD | {"SUPERSEDED"}:
        close_live_attempts(con, tid, status)


def close_live_attempts(con: sqlite3.Connection, tid: str, task_status: str) -> None:
    """A finished task must not keep a live attempt: it would hold its lane and
    its paths forever (a cancelled cc-delegate job blocked its own fallback)."""
    now = time.time()
    con.execute(
        "UPDATE attempts SET status=?, error_kind=COALESCE(error_kind,?), ended_at=?,"
        " last_checked_at=? WHERE task_id=? AND status IN ('RESERVED','QUEUED','RUNNING')",
        (task_status if task_status in TERMINAL_BAD else "CANCELLED",
         f"task set {task_status}", now, now, tid),
    )


def set_depends_on(con: sqlite3.Connection, tid: str, deps: Iterable[str]) -> None:
    con.execute("UPDATE tasks SET depends_on=?, updated_at=? WHERE id=?",
                (json.dumps(list(deps)), time.time(), tid))


def create_fallback(con: sqlite3.Connection, original: dict[str, Any]) -> str:
    """Create a clean retry and rewire its live dependants atomically."""
    now = time.time()
    with transaction(con):
        current = con.execute("SELECT status FROM tasks WHERE id=?",
                              (original["id"],)).fetchone()
        if current is None or current["status"] not in FALLBACK_FROM:
            raise ValueError(f"{original['id']} is not eligible for fallback")
        new = next_id(con, original["repo"])
        con.execute(
            "INSERT INTO tasks(id,title,mode,status,goal,repo,base_commit,paths,depends_on,"
            "owner,priority,task_class,created_at,updated_at,retry_of) "
            "VALUES(?,?,?,'PLANNED',?,?,?,?,?,?,?,?,?,?,?)",
            (new, original["title"], original["mode"], original["goal"], original["repo"],
             original["baseCommit"], json.dumps(original["paths"]),
             json.dumps(original["dependsOn"]), original["owner"],
             int(original["priority"]) + 1, original["taskClass"], now, now,
             original["id"]),
        )
        terminal = TERMINAL_OK | TERMINAL_BAD | {"SUPERSEDED"}
        for row in con.execute("SELECT id,status,depends_on FROM tasks").fetchall():
            if row["status"] in terminal:
                continue
            deps = json.loads(row["depends_on"])
            if original["id"] in deps:
                deps = [new if d == original["id"] else d for d in deps]
                con.execute("UPDATE tasks SET depends_on=?,updated_at=? WHERE id=?",
                            (json.dumps(deps), now, row["id"]))
        con.execute(
            "UPDATE tasks SET status='SUPERSEDED',superseded_by=?,failure_reason=?,"
            "updated_at=? WHERE id=?",
            (new, f"{current['status']}; superseded by {new}", now, original["id"]),
        )
        close_live_attempts(con, original["id"], "SUPERSEDED")
    return new


def blocks_map(tasks: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Reverse dependency edges -- who each task unblocks."""
    out: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    for t in tasks:
        for d in t["dependsOn"]:
            out.setdefault(d, []).append(t["id"])
    return out


def claim(con: sqlite3.Connection, tid: str, session_id: str, worker: str) -> bool:
    """Atomically move a PLANNED task to QUEUED for this session. False if lost."""
    with transaction(con):
        row = con.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None or row["status"] != "PLANNED":
            return False
        con.execute(
            "UPDATE tasks SET status='QUEUED', session_id=?, owner=?, updated_at=? WHERE id=?",
            (session_id, worker, time.time(), tid),
        )
    return True


def release(con: sqlite3.Connection, tid: str) -> None:
    """Undo a claim that never became a running attempt."""
    con.execute(
        "UPDATE tasks SET status='PLANNED', session_id=NULL, updated_at=? "
        "WHERE id=? AND status='QUEUED'", (time.time(), tid))


def reserve_attempt(
    con: sqlite3.Connection, tid: str, session_id: str, worker: str, lane: str,
    cfg: dict, profile: str | None = None, transport: str | None = None,
) -> dict[str, Any]:
    """Atomically claim a task and reserve one independent execution lane.

    Capacity and path ownership are checked under the same write lock as the
    reservation. This allows Codex, Station, Oracle, and OpenRouter to run in
    parallel while preventing two Claude sessions from racing into one slot.
    """
    from . import scheduler

    now = time.time()
    with transaction(con):
        row = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "task not found"}
        task = _task_row(row)
        if task["status"] != "PLANNED":
            return {"ok": False, "reason": f"task is {task['status']}"}

        active_rows = con.execute(
            "SELECT a.lane,a.status,t.id AS task_id,t.mode,t.repo,t.paths "
            "FROM attempts a JOIN tasks t ON t.id=a.task_id "
            "WHERE a.status IN ('RESERVED','QUEUED','RUNNING')"
        ).fetchall()
        if task["mode"] == "READ_ONLY":
            used = sum(1 for a in active_rows if a["mode"] == "READ_ONLY")
            limit = int(cfg["workers"]["maxReadOnlyJobs"])
            if used >= limit:
                return {"ok": False, "reason": f"read-only capacity {used}/{limit}"}
        else:
            repo = task["repo"]
            same_repo = [a for a in active_rows
                         if a["mode"] == "WRITE" and a["repo"] == repo]
            total_limit = int(cfg["workers"]["totalWriteJobsPerRepo"])
            if len(same_repo) >= total_limit:
                return {"ok": False,
                        "reason": f"write capacity {len(same_repo)}/{total_limit} in repo"}
            lane_limit = int(cfg["workers"]["lanes"][lane].get("maxWriteJobs", 1))
            # Lane slots are shared across repos (see lanes.has_capacity).
            lane_used = sum(1 for a in active_rows
                            if a["mode"] == "WRITE" and a["lane"] == lane)
            if lane_used >= lane_limit:
                return {"ok": False, "reason": f"{lane} capacity {lane_used}/{lane_limit}"}
            for other in same_repo:
                if scheduler.paths_overlap(task["paths"], json.loads(other["paths"])):
                    return {"ok": False,
                            "reason": f"path conflict with {other['task_id']}"}

        cur = con.execute(
            "INSERT INTO attempts(task_id,worker,lane,status,started_at,reserved_at,"
            "profile,transport) VALUES(?,?,?,'RESERVED',?,?,?,?)",
            (tid, worker, lane, now, now, profile, transport),
        )
        attempt_id = int(cur.lastrowid)
        con.execute(
            "UPDATE tasks SET status='QUEUED',session_id=?,owner=?,updated_at=? WHERE id=?",
            (session_id, worker, now, tid),
        )
    return {"ok": True, "attemptId": attempt_id}


def activate_attempt(
    con: sqlite3.Connection, attempt_id: int, handle: str,
    *, worktree: str | None = None, branch: str | None = None,
    log_path: str | None = None, external_job_id: str | None = None,
) -> bool:
    """Turn a durable reservation into a running external job."""
    with transaction(con):
        row = con.execute(
            "SELECT task_id,status FROM attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if row is None or row["status"] != "RESERVED":
            return False
        con.execute(
            "UPDATE attempts SET status='RUNNING',handle=?,worktree=?,branch=?,log_path=?,"
            "external_job_id=?,started_at=?,last_checked_at=? WHERE id=?",
            (handle, worktree, branch, log_path, external_job_id,
             time.time(), time.time(), attempt_id),
        )
        con.execute("UPDATE tasks SET status='RUNNING',updated_at=? WHERE id=?",
                    (time.time(), row["task_id"]))
    return True


def release_reservation(con: sqlite3.Connection, tid: str) -> bool:
    """Cancel a not-yet-attached reservation and return its task to PLANNED."""
    with transaction(con):
        row = con.execute(
            "SELECT id FROM attempts WHERE task_id=? AND status='RESERVED' "
            "ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        if row is None:
            return False
        now = time.time()
        con.execute("UPDATE attempts SET status='CANCELLED',ended_at=? WHERE id=?",
                    (now, row["id"]))
        con.execute("UPDATE tasks SET status='PLANNED',session_id=NULL,updated_at=? WHERE id=?",
                    (now, tid))
    return True


# ---------------------------------------------------------------- attempts

def add_attempt(
    con: sqlite3.Connection, task_id: str, worker: str, handle: str | None = None,
    worktree: str | None = None, branch: str | None = None, log_path: str | None = None,
    lane: str | None = None,
) -> int:
    cur = con.execute(
        "INSERT INTO attempts(task_id,worker,lane,status,handle,worktree,branch,log_path,"
        "started_at) VALUES(?,?,?,'RUNNING',?,?,?,?,?)",
        (task_id, worker, lane, handle, worktree, branch, log_path, time.time()),
    )
    return int(cur.lastrowid)


def finish_attempt(
    con: sqlite3.Connection, attempt_id: int, status: str,
    error_kind: str | None = None, error_detail: str | None = None,
) -> None:
    now = time.time()
    con.execute(
        "UPDATE attempts SET status=?, error_kind=?, error_detail=?, ended_at=?,"
        " last_checked_at=? WHERE id=?",
        (status, error_kind, error_detail, now, now, attempt_id),
    )


def attempts_for(con: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM attempts WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]


def live_attempt(con: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = con.execute(
        "SELECT * FROM attempts WHERE task_id=? AND status IN ('RESERVED','QUEUED','RUNNING') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,)).fetchone()
    return dict(row) if row else None


def running_attempts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(
        "SELECT a.*, t.repo AS repo, t.mode AS task_mode FROM attempts a"
        " JOIN tasks t ON t.id=a.task_id "
        "WHERE a.status IN ('RESERVED','QUEUED','RUNNING') ORDER BY a.id").fetchall()]


def reserved_attempts(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(
        "SELECT a.*,t.repo AS repo,t.mode AS task_mode FROM attempts a "
        "JOIN tasks t ON t.id=a.task_id WHERE a.status='RESERVED' ORDER BY a.id"
    ).fetchall()]


def touch_attempt(con: sqlite3.Connection, attempt_id: int) -> None:
    con.execute("UPDATE attempts SET last_checked_at=? WHERE id=?", (time.time(), attempt_id))
