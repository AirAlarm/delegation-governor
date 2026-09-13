"""Distribution report: is every lane getting work as routing intends, and is
load balanced?

A parallel task logs every routing decision (lane chosen, why each earlier
preference was skipped, the in-flight counts at that moment) to
``config.HOME / "routing.jsonl"`` and registers a CLI subcommand that calls
``run(args)``. This module joins those records against the task ledger so a
human (or the supervisor) can see whether the router is doing what the routing
table claims: which lanes actually get used, how often they were the first
choice versus a fallback, and where work is piling up or being dropped.

The report is intentionally a pure function over its inputs -- no file I/O,
no probes -- so it can be tested in isolation and reused from any caller
that already has the routes in hand.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from typing import Any

from . import config, lanes, store


# ---------------------------------------------------------------- filtering

def _norm_repo(repo: str) -> str:
    """Repos are stored absolute; accept either form from callers."""
    return os.path.abspath(repo) if repo else ""


def _matches_repo(value: str, repo: str) -> bool:
    """A record's ``repo`` is empty for the all-repos case; else compare absolutes."""
    if not repo:
        return True
    if not value:
        return False
    return os.path.abspath(value) == repo


# ---------------------------------------------------------------- counts

def _classify(status: str) -> str:
    """Bucket an attempt's status for the per-lane totals.

    Attempts, not tasks: a retried task has one FAILED and one SUCCEEDED
    attempt on different lanes, and each must land in its own bucket.
    """
    if status in ("RUNNING", "QUEUED"):
        return "running"
    if status in ("SUCCEEDED", "INTEGRATED"):
        return "succeeded"
    if status in store.TERMINAL_BAD:
        return "failed"
    if status == "SUPERSEDED":
        return "superseded"
    return "other"


# ---------------------------------------------------------------- report

def report(con: sqlite3.Connection, cfg: dict, routes: list[dict],
           avail: dict, repo: str = "",
           since_ts: float | None = None) -> dict[str, Any]:
    """Pure per-input distribution summary.

    ``avail`` is the same shape ``lanes.availability`` returns: lane -> [ok,
    reason]. ``routes`` is the list already read from routing.jsonl by the
    caller; the parallel task's routelog module knows how to load it.
    """
    repo = _norm_repo(repo)

    # -- routes -----------------------------------------------------------
    kept_routes: list[dict] = []
    for r in routes:
        if since_ts is not None and r.get("ts", 0.0) < since_ts:
            continue
        if not _matches_repo(r.get("repo", "") or "", repo):
            continue
        kept_routes.append(r)

    reserved_routes = [r for r in kept_routes if r.get("outcome") == "reserved"]
    no_lane_routes = [r for r in kept_routes if r.get("outcome") == "no_lane"]
    total_reserved = len(reserved_routes)

    # -- attempt data -----------------------------------------------------
    # One SQL pass: every attempt joined with its task, so the per-lane and
    # per-class buckets only need dict lookups after this. Buckets classify
    # the ATTEMPT status (a.status), not the task's: a retried task has a
    # FAILED and a SUCCEEDED attempt on different lanes.
    joined: list[dict] = []
    rows = con.execute(
            "SELECT a.id, a.task_id, a.lane, a.worker,"
            " a.status AS attempt_status, a.started_at, a.ended_at,"
            " t.task_class, t.repo AS task_repo"
            " FROM attempts a JOIN tasks t ON t.id = a.task_id"
    ).fetchall()
    for r in rows:
        d = dict(r)
        if since_ts is not None and float(d.get("started_at") or 0.0) < since_ts:
            continue
        if not _matches_repo(d.get("task_repo", "") or "", repo):
            continue
        joined.append(d)

    # -- per-lane ---------------------------------------------------------
    lane_specs = lanes.lanes(cfg)
    lane_rows: list[dict[str, Any]] = []
    for name, spec in lane_specs.items():
        ok, reason = avail.get(name, [True, "unprobed"])
        routable = [c for c in lanes.CLASSES if name in lanes.preference(c, cfg)]
        lane_attempts = [a for a in joined if a.get("lane") == name]
        buckets = {"running": 0, "succeeded": 0, "failed": 0, "superseded": 0}
        classes: dict[str, int] = {}
        for a in lane_attempts:
            kind = _classify(a["attempt_status"])
            if kind in buckets:
                buckets[kind] += 1
            cls = a.get("task_class") or "standard"
            classes[cls] = classes.get(cls, 0) + 1
        ended = [a for a in lane_attempts if a.get("ended_at") is not None]
        if ended:
            mean_dur = sum(float(a["ended_at"]) - float(a["started_at"])
                           for a in ended) / len(ended)
        else:
            mean_dur = None

        first_choice = [r for r in reserved_routes
                        if r.get("lane") == name and r.get("rank") == 0]
        fallback = [r for r in reserved_routes
                    if r.get("lane") == name and (r.get("rank") or 0) > 0]
        share = ((len(first_choice) + len(fallback)) / total_reserved
                 if total_reserved else 0.0)

        lane_rows.append({
            "lane": name,
            "worker": spec["worker"],
            "available": bool(ok),
            "reason": reason,
            "routable_classes": routable,
            "attempts": len(lane_attempts),
            "running": buckets["running"],
            "succeeded": buckets["succeeded"],
            "failed": buckets["failed"],
            "superseded": buckets["superseded"],
            "mean_duration_s": mean_dur,
            "classes": classes,
            "first_choice": len(first_choice),
            "fallback": len(fallback),
            "share": share,
        })

    # -- waits ------------------------------------------------------------
    by_class: dict[str, int] = {}
    skipped_reasons: dict[str, int] = {}
    for r in no_lane_routes:
        cls = r.get("task_class") or "standard"
        by_class[cls] = by_class.get(cls, 0) + 1
        for s in r.get("skipped") or []:
            # "lane: reason" -> keep just the reason text, the lane name is
            # the row of the table this flag is for.
            text = s.split(": ", 1)[1] if ": " in s else s
            skipped_reasons[text] = skipped_reasons.get(text, 0) + 1
    waits = {
        "total": len(no_lane_routes),
        "by_class": by_class,
        "skipped_reasons": skipped_reasons,
    }

    # -- flags ------------------------------------------------------------
    flags: list[str] = []
    lane_by_name = {row["lane"]: row for row in lane_rows}

    # (a) an available lane with routable_classes received 0 reserved routes
    # while reserved routes for those classes do exist.
    for row in lane_rows:
        if not row["available"] or not row["routable_classes"]:
            continue
        if (row["first_choice"] + row["fallback"]) > 0:
            continue
        relevant = [r for r in reserved_routes
                    if (r.get("task_class") or "standard") in row["routable_classes"]]
        if relevant:
            flags.append(
                f"lane {row['lane']} is up and routable but received 0 reserved routes"
            )

    # (b) a class that ever waited (no_lane) while some available lane outside
    # its preference had zero in_flight at the moment of the wait.
    for cls, n in by_class.items():
        pref = set(lanes.preference(cls, cfg))
        for r in no_lane_routes:
            if (r.get("task_class") or "standard") != cls:
                continue
            in_flight = r.get("in_flight") or {}
            outside = [lane for lane, info in lane_by_name.items()
                       if lane not in pref and info["available"]
                       and in_flight.get(lane, 0) == 0]
            if outside:
                outside_names = ",".join(sorted(outside))
                flags.append(
                    f"class {cls} waited on a lane while {outside_names} "
                    f"outside its preference was idle"
                )
                break

    # (c) a lane whose failure rate is at least 50% with at least 2 attempts.
    for row in lane_rows:
        if row["attempts"] < 2:
            continue
        if row["failed"] / row["attempts"] >= 0.5:
            flags.append(
                f"lane {row['lane']} failing: {row['failed']}/{row['attempts']} attempts"
            )

    return {"lanes": lane_rows, "waits": waits, "flags": flags}


# ---------------------------------------------------------------- CLI

def _print_table(report: dict[str, Any]) -> None:
    lanes_out = report["lanes"]
    header = (f"{'LANE':<22} {'UP':<5} {'CLASSES':<28} {'ATT':>4} {'RUN':>4} "
              f"{'OK':>4} {'FAIL':>4} {'1ST':>4} {'FB':>4} {'SHARE':>6} {'AVG':>7}")
    print(header)
    for r in lanes_out:
        up = "ok" if r["available"] else "down"
        classes = ",".join(r["routable_classes"]) or "-"
        avg = "-" if r["mean_duration_s"] is None else f"{r['mean_duration_s']:.0f}s"
        print(f"{r['lane']:<22} {up:<5} {classes:<28} {r['attempts']:>4} {r['running']:>4}"
              f" {r['succeeded']:>4} {r['failed']:>4} {r['first_choice']:>4}"
              f" {r['fallback']:>4} {r['share']:>6.2f} {avg:>7}")

    waits = report["waits"]
    print()
    print(f"waits: {waits['total']}")
    if waits["by_class"]:
        print("  by class: " + ", ".join(
            f"{c}={n}" for c, n in sorted(waits["by_class"].items())))
    if waits["skipped_reasons"]:
        print("  reasons: " + ", ".join(
            f"{r}={n}" for r, n in sorted(waits["skipped_reasons"].items(),
                                           key=lambda kv: -kv[1])))

    if report["flags"]:
        print()
        print("flags:")
        for f in report["flags"]:
            print(f"  {f}")


def run(args: argparse.Namespace) -> int:
    con = store.connect()
    cfg = config.load()
    # The parallel task writes routelog; if it isn't wired up yet (e.g. the
    # test harness is in place before the recorder), treat the log as empty
    # rather than crashing the report.
    try:
        from . import routelog
    except ImportError:
        routelog = None
    routes = routelog.read_routes() if routelog is not None else []
    avail = lanes.availability(con, cfg)
    since = time.time() - args.since * 3600 if getattr(args, "since", 0) else None
    out = report(con, cfg, routes, avail,
                 repo=getattr(args, "repo", "") or "",
                 since_ts=since)
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    else:
        _print_table(out)
    return 0
