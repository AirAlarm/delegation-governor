"""Dependency-aware scheduler.

READY / BLOCKED are computed, never stored, so the answer always matches the
current graph. The model is deliberately dumb and deterministic (spec: "do not
implement an overly clever optimizer in v1").
"""
from __future__ import annotations

import fnmatch
import sqlite3
from typing import Any

from . import store

READY = "READY"
BLOCKED = "BLOCKED"


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def paths_overlap(a: list[str], b: list[str]) -> bool:
    """Conservative glob overlap for WRITE path ownership.

    A task with no declared paths owns the whole repo, so it conflicts with
    everything -- unknown ownership must not read as "safe".
    """
    if not a or not b:
        return True
    for pa in (_norm(x) for x in a):
        for pb in (_norm(x) for x in b):
            if pa == pb:
                return True
            # Either glob matching the other's literal prefix means the two
            # subtrees intersect. Compare both directions and both as prefixes.
            if fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa):
                return True
            sa, sb = pa.rstrip("*").rstrip("/"), pb.rstrip("*").rstrip("/")
            if sa and sb and (sa == sb or sa.startswith(sb + "/") or sb.startswith(sa + "/")):
                return True
    return False


def dependency_state(task: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    """(satisfied, reason). A dead dependency blocks forever, it does not unblock."""
    for d in task["dependsOn"]:
        dep = by_id.get(d)
        if dep is None:
            return False, f"dependency {d} missing"
        if dep["status"] in store.TERMINAL_BAD:
            return False, f"dependency {d} {dep['status']}"
        if dep["status"] not in store.TERMINAL_OK:
            return False, f"waiting on {d} ({dep['status']})"
    return True, ""


def conflict_reason(task: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    """Non-empty when an active WRITE task already owns overlapping paths."""
    if task["mode"] != "WRITE":
        return ""
    for other in tasks:
        if other["id"] == task["id"] or other["mode"] != "WRITE":
            continue
        if other["status"] not in store.ACTIVE:
            continue
        if other["repo"] != task["repo"]:
            continue
        if paths_overlap(task["paths"], other["paths"]):
            return f"path conflict with {other['id']}"
    return ""


def capacity_reason(task: dict[str, Any], tasks: list[dict[str, Any]], cfg: dict) -> str:
    """Non-empty when worker/resource limits leave no room right now."""
    w = cfg["workers"]
    active = [t for t in tasks if t["status"] in store.ACTIVE]
    if task["mode"] == "READ_ONLY":
        n = len([t for t in active if t["mode"] == "READ_ONLY"])
        return "" if n < w["maxReadOnlyJobs"] else f"read-only capacity {n}/{w['maxReadOnlyJobs']}"
    same_repo = [t for t in active if t["mode"] == "WRITE" and t["repo"] == task["repo"]]
    if len(same_repo) >= w["totalWriteJobsPerRepo"]:
        return f"write capacity {len(same_repo)}/{w['totalWriteJobsPerRepo']} in repo"
    return ""


def worker_capacity_free(con: sqlite3.Connection, worker: str, repo: str, cfg: dict) -> bool:
    """Is any lane belonging to this worker free? Used at dispatch time."""
    from . import lanes as lanes_mod
    names = [n for n, spec in lanes_mod.lanes(cfg).items() if spec["worker"] == worker]
    return any(lanes_mod.has_capacity(con, n, repo, cfg) for n in names)


def evaluate(con: sqlite3.Connection, cfg: dict) -> dict[str, Any]:
    """Full scheduler view: every task tagged READY / BLOCKED / its stored status."""
    tasks = store.all_tasks(con)
    by_id = {t["id"]: t for t in tasks}
    blocks = store.blocks_map(tasks)
    handoffs = {a["task_id"]: a for a in store.reserved_attempts(con) if a["worker"] == "cc-delegate"}
    out: list[dict[str, Any]] = []
    for t in tasks:
        row = dict(t)
        row["blocks"] = blocks.get(t["id"], [])
        row["unlocks"] = len(row["blocks"])
        if t["status"] != "PLANNED":
            row["state"] = t["status"]
            row["reason"] = ""
            if (a := handoffs.get(t["id"])):
                # A missed handoff otherwise shows as a silent QUEUED that holds a lane.
                row["reason"] = (f"awaiting handoff: call run_dev_task, then dg attach {t['id']} "
                                 f"<cc-task-id> --attempt {a['id']} (or dg release {t['id']})")
        else:
            ok, why = dependency_state(t, by_id)
            if not ok:
                row["state"], row["reason"] = BLOCKED, why
            elif (why := conflict_reason(t, tasks)):
                row["state"], row["reason"] = BLOCKED, why
            elif (why := capacity_reason(t, tasks, cfg)):
                row["state"], row["reason"] = BLOCKED, why
            else:
                row["state"], row["reason"] = READY, ""
        out.append(row)
    return {"tasks": out, "counts": counts(out)}


def counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    c: dict[str, int] = {}
    for r in rows:
        c[r["state"]] = c.get(r["state"], 0) + 1
    return c


def ready(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """READY tasks, best first.

    Priority (spec 24): explicit priority, then how many tasks it unlocks
    (critical path), then age. Deterministic -- no randomness, no weights.
    """
    r = [x for x in rows if x["state"] == READY]
    r.sort(key=lambda t: (-t["priority"], -t["unlocks"], t["createdAt"], t["id"]))
    return r


def pick(con: sqlite3.Connection, cfg: dict, mode: str | None = None) -> dict[str, Any] | None:
    rows = evaluate(con, cfg)["tasks"]
    for t in ready(rows):
        if mode is None or t["mode"] == mode:
            return t
    return None
