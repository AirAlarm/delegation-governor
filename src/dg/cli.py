"""dg -- Delegation Governor control CLI."""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config, gitutil, quota_codex, routing, scheduler, store, supervisor, sync, workorder
from . import quickread
from .workers import cc_delegate
from .workers import codex_plugin


def _session_id() -> str:
    return os.environ.get("CLAUDE_SESSION_ID") or f"pid-{os.getpid()}"


def _cfg_with_overrides(con) -> dict:
    cfg = config.load()
    saved = store.kv_get(con, "overrides") or {}
    cfg["overrides"].update({k: v for k, v in saved.items() if v})
    return cfg


def _emit(obj: Any, as_json: bool, text: str = "") -> int:
    print(json.dumps(obj, indent=2, default=str) if as_json else text)
    return 0


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = int(time.time() - ts)
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    return f"{d // 3600}h{(d % 3600) // 60:02d}m"


def _until(ts: int | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%H:%M", time.localtime(ts))


# ---------------------------------------------------------------- state view

def _snapshot(con, cfg, refresh_codex: bool = True) -> dict[str, Any]:
    sync.reconcile(con, cfg)
    sup = supervisor.evaluate(con, cfg)
    wrk = routing.select(con, cfg, refresh=refresh_codex)
    view = scheduler.evaluate(con, cfg)
    return {"supervisor": sup, "worker": wrk, "counts": view["counts"], "tasks": view["tasks"]}


def cmd_status(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    snap = _snapshot(con, cfg, refresh_codex=not args.no_refresh)
    if args.json:
        return _emit({k: v for k, v in snap.items() if k != "tasks"}, True)
    s, w, c = snap["supervisor"], snap["worker"], snap["counts"]
    lines = [
        f"SUP  {s['state']:<14} {s['reason']}",
        f"     5h {_pct(s['fiveHour'])}   7d {_pct(s['sevenDay'])}"
        f"   override={s['override']}",
        f"WRK  {w['worker']:<14} {w['reason']}"
        + (f" (resets {_until(w.get('codexResetsAt'))})" if w.get("codexResetsAt") else ""),
        f"     codex={w['codexState']}   override={cfg['overrides']['worker']}",
        "TASK " + ("  ".join(f"{k}:{v}" for k, v in sorted(c.items())) or "none"),
    ]
    return _emit(None, False, "\n".join(lines))


def _pct(win) -> str:
    return "n/a" if not win else f"{win['usedPercent']:.0f}%"


def cmd_quota(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    info = quota_codex.refresh(con, cfg, force=args.force)
    sup = supervisor.evaluate(con, cfg)
    out = {"claude": {"state": sup["state"], "fiveHour": sup["fiveHour"],
                      "sevenDay": sup["sevenDay"], "hardLimit": sup["hardLimit"],
                      "quotaAgeSeconds": sup["quotaAgeSeconds"]},
           "codex": info}
    if args.json:
        return _emit(out, True)
    lines = [f"Claude   {sup['state']}  5h {_pct(sup['fiveHour'])}  7d {_pct(sup['sevenDay'])}"
             f"  (data {_ago(time.time() - (sup['quotaAgeSeconds'] or 0)) if sup['quotaAgeSeconds'] is not None else 'n/a'} old)",
             f"Codex    {info['state']}" + ("  [cached]" if info.get("cached") else "")]
    q = info.get("quota") or {}
    for w in q.get("windows", []):
        mark = "*" if w["gating"] else " "
        dur = f"{w['windowDurationMins']}m" if w["windowDurationMins"] else "?"
        lines.append(f"  {mark}{w['limitId']}/{w['window']:<9} {w['usedPercent']:>3}%  "
                     f"window {dur:<7} resets {_until(w['resetsAt'])}")
    if q.get("resetCreditsAvailable"):
        lines.append(f"  reset credits available: {q['resetCreditsAvailable']} "
                     "(never consumed automatically -- use `codex` interactively)")
    lines.append("  * = gates delegation")
    return _emit(None, False, "\n".join(lines))


def cmd_route(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    w = routing.select(con, cfg)
    return _emit(w, True) if args.json else _emit(None, False, f"{w['worker']}  ({w['reason']})")


def cmd_worker_status(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    sync.reconcile(con, cfg)
    rows = store.running_attempts(con)
    out = [{"task": r["task_id"], "worker": r["worker"], "handle": r["handle"],
            "elapsed": int(time.time() - r["started_at"]),
            "slow": time.time() - r["started_at"] > cfg["workers"]["slowAfterSeconds"],
            "worktree": r["worktree"]} for r in rows]
    if args.json:
        return _emit(out, True)
    if not out:
        return _emit(None, False, "no running attempts")
    return _emit(None, False, "\n".join(
        f"{r['task']:<8} {r['worker']:<12} {r['elapsed']:>5}s"
        f"{'  SLOW' if r['slow'] else ''}  {r['handle'] or ''}" for r in out))


# ---------------------------------------------------------------- tasks

def cmd_tasks(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    sync.reconcile(con, cfg)
    view = scheduler.evaluate(con, cfg)
    rows = view["tasks"]
    if args.filter == "ready":
        rows = scheduler.ready(rows)
    elif args.filter == "running":
        rows = [r for r in rows if r["state"] == "RUNNING"]
    elif args.filter == "blocked":
        rows = [r for r in rows if r["state"] == "BLOCKED"]
    if args.json:
        return _emit(rows, True)
    if not rows:
        return _emit(None, False, "no tasks")
    out = []
    for r in rows:
        deps = ",".join(r["dependsOn"]) or "-"
        out.append(f"{r['id']:<7} {r['state']:<11} {r['mode']:<10} {r['title'][:46]:<46}"
                   f" deps={deps:<12} unlocks={r['unlocks']}"
                   + (f"  [{r['reason']}]" if r["reason"] else ""))
    return _emit(None, False, "\n".join(out))


def cmd_task_show(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    view = {r["id"]: r for r in scheduler.evaluate(con, cfg)["tasks"]}
    t.update(state=view[args.id]["state"], reason=view[args.id]["reason"],
             blocks=view[args.id]["blocks"], attempts=store.attempts_for(con, args.id))
    return _emit(t, True)


def cmd_add(args) -> int:
    con = store.connect()
    tid = store.create_task(
        con, title=args.title, mode=args.mode, goal=args.goal or args.title,
        repo=args.repo or os.getcwd(), paths=args.path or [], depends_on=args.depends_on or [],
        priority=args.priority, task_class=args.task_class)
    print(tid)
    return 0


def cmd_set(args) -> int:
    con = store.connect()
    if store.get_task(con, args.id) is None:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    store.set_status(con, args.id, args.status.upper(), failure_reason=args.reason)
    print(f"{args.id} -> {args.status.upper()}")
    return 0


def cmd_graph(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    rows = scheduler.evaluate(con, cfg)["tasks"]
    if args.id:
        keep = _closure(rows, args.id)
        rows = [r for r in rows if r["id"] in keep]
    if args.json:
        return _emit([{"id": r["id"], "state": r["state"], "dependsOn": r["dependsOn"],
                       "blocks": r["blocks"]} for r in rows], True)
    out = []
    for r in rows:
        out.append(f"{r['id']:<7} {r['state']:<11} {r['title'][:44]}")
        for d in r["dependsOn"]:
            out.append(f"          depends on -> {d}")
        for b in r["blocks"]:
            out.append(f"          unlocks    -> {b}")
    return _emit(None, False, "\n".join(out) or "no tasks")


def _closure(rows, tid) -> set[str]:
    by = {r["id"]: r for r in rows}
    seen, stack = set(), [tid]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in by:
            continue
        seen.add(cur)
        stack.extend(by[cur]["dependsOn"] + by[cur]["blocks"])
    return seen


# ---------------------------------------------------------------- dispatch

def cmd_workorder(args) -> int:
    con = store.connect()
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    print(workorder.build(t, cwd=t["repo"] or os.getcwd(), state=args.state or "",
                          constraints=args.constraint or [], non_goals=args.non_goal or [],
                          acceptance=args.acceptance or [], tests=args.tests or ""))
    return 0


def cmd_dispatch(args) -> int:
    """Reserve a lane, start its worker, and return immediately."""
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1

    view = {r["id"]: r for r in scheduler.evaluate(con, cfg)["tasks"]}
    if view[args.id]["state"] != scheduler.READY and not args.force:
        print(f"{args.id} is {view[args.id]['state']}: {view[args.id]['reason']}", file=sys.stderr)
        return 2

    from . import lanes as lanes_mod
    if args.worker:
        forced = copy.deepcopy(cfg)
        forced["overrides"]["worker"] = args.worker
        lane_choice = lanes_mod.choose(con, forced, t)
    else:
        lane_choice = routing.select_for(con, cfg, t)
    if lane_choice["worker"] is None:
        print(f"{args.id}: {lane_choice['reason']}", file=sys.stderr)
        for sk in lane_choice.get("skipped", []):
            print(f"  {sk}", file=sys.stderr)
        return 6
    worker = lane_choice["worker"]
    reservation = store.reserve_attempt(
        con, t["id"], _session_id(), worker, lane_choice["lane"], cfg,
        profile=lane_choice.get("profile"),
        transport="codex-plugin" if worker == routing.CODEX else "cc-delegate-mcp")
    if not reservation["ok"]:
        print(f"{args.id}: {reservation['reason']}", file=sys.stderr)
        return 4
    attempt_id = reservation["attemptId"]
    if worker == routing.CC_DELEGATE:
        # cc-delegate is MCP-only: hand Claude the work order and the profile,
        # then `dg attach` once run_dev_task returns its task id.
        # cc-delegate creates its own worktree from this repo, so the order
        # must not claim the repo path itself is one.
        order = _order(t, args, "the isolated git worktree cc-delegate places you in")
        print(json.dumps({"worker": "cc-delegate", "action": "call mcp run_dev_task",
                          "taskId": t["id"], "repo": t["repo"],
                          "lane": lane_choice.get("lane"),
                          "profile": lane_choice.get("profile"),
                          "attemptId": attempt_id,
                          "taskClass": lane_choice.get("taskClass"),
                          "then": f"dg attach {t['id']} <cc-task-id> "
                                  f"--attempt {attempt_id}",
                          "workOrder": order}, indent=2))
        return 0

    order = _order(t, args, "(the working directory below)")
    res = codex_plugin.dispatch(con, t, order, attempt_id)
    if not res.get("ok"):
        store.release_reservation(con, args.id)
        print(res.get("error", "dispatch failed"), file=sys.stderr)
        return 5
    res["task"] = args.id
    res["lane"] = lane_choice.get("lane")
    res["taskClass"] = lane_choice.get("taskClass")
    lanes_mod.invalidate(con)
    res["nextReady"] = [r["id"] for r in scheduler.ready(scheduler.evaluate(con, cfg)["tasks"])]
    res["freeLanes"] = lanes_mod.free_lanes(con, cfg, t["repo"])
    return _emit(res, True)


def _order(t, args, cwd) -> str:
    return workorder.build(t, cwd=cwd, state=getattr(args, "state", "") or "",
                           constraints=getattr(args, "constraint", None) or [],
                           non_goals=getattr(args, "non_goal", None) or [],
                           acceptance=getattr(args, "acceptance", None) or [],
                           tests=getattr(args, "tests", "") or "")


def cmd_attach(args) -> int:
    con = store.connect()
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    res = cc_delegate.attach(con, t, args.cc_task_id,
                             args.work_order or "(recorded by Claude)", lane=args.lane,
                             attempt_id=args.attempt)
    from . import lanes as lanes_mod
    lanes_mod.invalidate(con)
    return _emit(res, True)


def cmd_release(args) -> int:
    with store.session() as con:
        ok = store.release_reservation(con, args.id)
    if not ok:
        print(f"{args.id} has no releasable reservation", file=sys.stderr)
        return 1
    return _emit({"task": args.id, "status": "PLANNED", "released": True}, True)


def cmd_cancel(args) -> int:
    with store.session() as con:
        t = store.get_task(con, args.id)
        a = store.live_attempt(con, args.id)
        if not t or not a:
            print(f"{args.id} has no active attempt", file=sys.stderr)
            return 1
        a["repo"] = t["repo"]
        if a["status"] == "RESERVED":
            store.release_reservation(con, args.id)
            return _emit({"task": args.id, "status": "PLANNED", "released": True}, True)
        if a["worker"] != routing.CODEX:
            return _emit({"ok": False, "task": args.id,
                          "action": "cancel this cc-delegate job through its MCP tool",
                          "externalJobId": a.get("external_job_id")}, True)
        result = codex_plugin.cancel(a)
        if result.get("ok"):
            store.finish_attempt(con, a["id"], "CANCELLED", "cancelled by user")
            store.set_status(con, args.id, "CANCELLED", failure_reason="cancelled by user")
        return _emit(result, True)


def cmd_recover_handoffs(args) -> int:
    recovered, ambiguous = [], []
    with store.session() as con:
        for a in store.reserved_attempts(con):
            candidates: list[dict[str, Any]] = []
            if a["worker"] == routing.CODEX:
                candidates = codex_plugin.recovery_candidates(a["task_id"], a["id"])
                if len(candidates) == 1:
                    c = candidates[0]
                    job = codex_plugin.read_job(c["jobId"]) or {}
                    wt = job.get("workspaceRoot")
                    store.activate_attempt(
                        con, a["id"], f"codex-plugin:{c['jobId']}", worktree=wt,
                        branch=gitutil.current_branch(wt) if wt and gitutil.is_repo(wt) else None,
                        log_path=job.get("logFile"), external_job_id=c["jobId"])
                    recovered.append({"task": a["task_id"], **c})
                    continue
            else:
                jobs = Path(a["repo"]) / ".cc-delegate" / "jobs"
                if jobs.is_dir():
                    for p in jobs.glob("*.json"):
                        if p.stat().st_mtime + 1 < float(a.get("reserved_at") or 0):
                            continue
                        try:
                            job = json.loads(p.read_text("utf-8"))
                        except (OSError, ValueError):
                            continue
                        candidates.append({"jobId": str(job.get("taskId") or p.stem),
                                           "status": job.get("status"), "jobFile": str(p)})
                    if len(candidates) == 1:
                        t = store.get_task(con, a["task_id"])
                        res = cc_delegate.attach(con, t, candidates[0]["jobId"],
                                                 "(recovered)", attempt_id=a["id"])
                        if res.get("ok"):
                            recovered.append({"task": a["task_id"], **candidates[0]})
                            continue
            if candidates:
                ambiguous.append({"task": a["task_id"], "attemptId": a["id"],
                                  "candidates": candidates})
    return _emit({"recovered": recovered, "ambiguous": ambiguous}, True)


def cmd_fallback(args) -> int:
    """Turn a QUOTA_FAILED/FAILED Codex task into a clean cc-delegate retry (spec 32/34)."""
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    if t["status"] not in ("QUOTA_FAILED", "AUTH_FAILED", "FAILED"):
        print(f"{args.id} is {t['status']}, nothing to fall back from", file=sys.stderr)
        return 2
    new = store.create_fallback(con, t)
    # Partial worker output is preserved on disk but never carried into the
    # retry: the fallback starts from the original clean base (spec 34).
    return _emit({"original": args.id, "fallback": new,
                  "worker": routing.select(con, cfg)["worker"],
                  "note": "partial work from the failed attempt is preserved, not reused"}, True)


def cmd_collect(args) -> int:
    """Pull a finished attempt's result for review."""
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    sync.reconcile(con, cfg, force=True)
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    atts = store.attempts_for(con, args.id)
    out: dict[str, Any] = {"task": args.id, "status": t["status"],
                           "failureReason": t["failureReason"],
                           "resultLocation": t["resultLocation"],
                           "attempts": atts}
    if t["resultLocation"] and Path(t["resultLocation"]).exists():
        try:
            out["result"] = json.loads(Path(t["resultLocation"]).read_text("utf-8"))
        except ValueError:
            out["result"] = {"raw": Path(t["resultLocation"]).read_text("utf-8")[:4000]}
    if t["status"] == "SUCCEEDED" and t["mode"] == "WRITE":
        completed = [a for a in atts if a["status"] == "SUCCEEDED" and a.get("worktree")]
        if completed:
            a = completed[-1]
            wt = a["worktree"]
            if Path(wt).exists():
                snap = gitutil.snapshot(wt, f"feat(dg): {t['id']} {t['title']}")
                head = gitutil.head_commit(wt)
                changed = gitutil.git(t["repo"], "diff", "--name-only",
                                      f"{t['baseCommit']}..{head}", check=False).stdout.splitlines()
                violations = []
                if t["paths"]:
                    import fnmatch
                    for path in changed:
                        norm = path.replace("\\", "/")
                        if not any(fnmatch.fnmatch(norm, p.replace("\\", "/")) or
                                   norm.startswith(p.replace("\\", "/").rstrip("*/") + "/")
                                   for p in t["paths"]):
                            violations.append(path)
                out["review"] = {
                    "branch": a.get("branch"), "baseCommit": t["baseCommit"],
                    "headCommit": head, "snapshotted": snap, "changedFiles": changed,
                    "contractViolations": violations,
                    "merge": f"git merge --no-ff {a.get('branch')}",
                    "cherryPick": f"git cherry-pick {head}",
                }
    view = scheduler.evaluate(con, cfg)
    out["unblockedNow"] = [r["id"] for r in scheduler.ready(view["tasks"])]
    return _emit(out, True)


def cmd_integrate(args) -> int:
    con = store.connect()
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    if t["status"] != "SUCCEEDED":
        print(f"{args.id} is {t['status']}, expected SUCCEEDED", file=sys.stderr)
        return 2
    verification: dict[str, Any] = {"integrated": True, "method": "read-only"}
    work_attempts = [a for a in store.attempts_for(con, args.id)
                     if a["status"] == "SUCCEEDED" and a.get("branch")]
    if t["mode"] == "WRITE":
        if not work_attempts:
            print(f"{args.id} has no successful worker branch", file=sys.stderr)
            return 3
        a = work_attempts[-1]
        if a.get("worktree") and Path(a["worktree"]).exists():
            gitutil.snapshot(a["worktree"], f"feat(dg): {t['id']} {t['title']}")
        verification = gitutil.integration_state(t["repo"], a["branch"], t["baseCommit"])
        accepted = args.accept_equivalent or args.force
        if not verification["integrated"] and not accepted:
            _emit({"task": args.id, "status": "SUCCEEDED", "verified": False,
                   "verification": verification,
                   "next": f"merge or cherry-pick {a['branch']}, then retry"}, True)
            return 4
        if not verification["integrated"] and accepted and not args.note:
            print("--accept-equivalent requires --note", file=sys.stderr)
            return 5
        if accepted and not verification["integrated"]:
            verification = {**verification, "integrated": True,
                            "method": "accepted-equivalent", "note": args.note}
    store.set_status(con, args.id, "INTEGRATED",
                     failure_reason=(f"integration override: {args.note}"
                                     if verification["method"] == "accepted-equivalent" else None))
    cleaned = []
    if args.cleanup:
        for a in store.attempts_for(con, args.id):
            if a["worktree"]:
                cleaned.append(gitutil.remove_worktree(
                    t["repo"], a["worktree"], a["branch"], force=True))
    out = {"task": args.id, "status": "INTEGRATED", "verification": verification,
           "cleanup": cleaned}
    kept = [c["keptBranch"] for c in cleaned if c.get("keptBranch")]
    if kept:
        out["note"] = f"kept branch(es) {', '.join(kept)}"
    return _emit(out, True)


# ---------------------------------------------------------------- overrides

def cmd_override(args) -> int:
    con = store.connect()
    valid = {"supervisor": {"auto", "claude", "local"},
             "worker": {"auto", "codex", "cc-delegate"}}
    if args.value not in valid[args.what]:
        print(f"{args.what} must be one of {sorted(valid[args.what])}", file=sys.stderr)
        return 1
    ov = store.kv_get(con, "overrides") or {}
    ov[args.what] = args.value
    store.kv_set(con, "overrides", ov)
    print(f"{args.what}={args.value}")
    return 0


def cmd_clear_override(args) -> int:
    con = store.connect()
    store.kv_set(con, "overrides", {"supervisor": "auto", "worker": "auto"})
    print("overrides cleared")
    return 0


# ---------------------------------------------------------------- ops

def cmd_doctor(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    # ok / warn / fail. A warning is an advisory about this environment, not
    # a broken install, so it must not make `dg doctor` exit non-zero.
    checks: list[tuple[str, str, str]] = []

    def chk(name, ok, detail=""):
        checks.append((name, "ok" if ok else "fail", detail))

    def warn(name, detail=""):
        checks.append((name, "warn", detail))

    chk("governor state dir", config.HOME.exists(), str(config.HOME))
    chk("governor db", config.DB_PATH.exists(), str(config.DB_PATH))
    from . import install as install_mod
    chk("managed runtime", install_mod.runtime_python().exists(),
        str(install_mod.runtime_python()))
    plugin_info = codex_plugin.discover()
    chk("Codex Claude plugin", plugin_info["ok"],
        (f"{plugin_info.get('version')} at {plugin_info.get('root')}"
         if plugin_info["ok"] else plugin_info["reason"] + "; run /codex:setup"))
    cx = quota_codex.codex_bin()
    chk("codex CLI", bool(cx), cx or "not on PATH")
    if cx:
        v = subprocess.run([cx, "--version"], capture_output=True, text=True)
        chk("codex version", v.returncode == 0, v.stdout.strip())
        info = quota_codex.refresh(con, cfg, force=args.force)
        chk("codex rate limits", info["state"] not in (quota_codex.PROTOCOL_ERROR,
                                                       quota_codex.UNKNOWN),
            f"{info['state']}{'' if not info.get('error') else ': ' + info['error']}")
    chk("claude CLI", bool(shutil.which("claude")), shutil.which("claude") or "not on PATH")
    chk("git", bool(shutil.which("git")), "")

    from . import launcher
    # Every supervisor fallback tier, in the order dg launch would try them.
    for t in launcher.tiers(cfg):
        tok, detail = launcher.probe_tier(t)
        chk(f"supervisor tier {t['name']}", tok, detail)
    if launcher.tier(cfg, "lmstudio"):
        amok, amdetail = _probe_lmstudio_messages(cfg)
        chk("lm studio /v1/messages (anthropic api)", amok, amdetail)
        contention = launcher.local_contention(cfg)
        if contention:
            warn("lm studio single model slot", contention)

    from . import proxy as proxy_mod
    port = cfg["proxy"]["port"]
    h = proxy_mod.health(port, timeout=1.5)
    env_cur, env_ours = install_mod.proxy_env_state(port)
    if env_ours:
        # Claude is routed through the proxy, so the proxy MUST be up.
        chk(f"router proxy :{port}", h is not None,
            f"serving {h.get('stats')}" if h else
            "ANTHROPIC_BASE_URL points here but nothing is listening -- "
            "run `dg proxy` (SessionStart normally does)")
        chk("claude routed through router", True,
            f"{env_cur} -- terminal Claude Code only; personal Desktop manages its URL")
    else:
        chk("router proxy", True,
            (f"running on :{port}, not wired in" if h else f"not running (:{port})")
            + "; `dg install --proxy` routes terminal sessions through it")

    lp = store.kv_get(con, "launcher") or {}
    if lp:
        chk("last dg launch", True,
            f"route={lp.get('route')} model={lp.get('model')} "
            f"session={lp.get('sessionId')} restarts={lp.get('restarts', 0)}")
    chk("Claude Desktop routing", True,
        "not attempted: personal Desktop manages ANTHROPIC_BASE_URL; worker delegation remains active")

    plugin = _cc_delegate_root()
    chk("cc-delegate plugin", plugin is not None, str(plugin or "not found (fallback worker)"))
    if plugin is not None:
        from . import ccdelegate
        st = ccdelegate.check()
        g = ccdelegate.gate_settings()
        detail = st["detail"]
        if st["ok"] and g:
            detail += f" (ctx {g.get('CONTEXT_LENGTH')}, ttl {g.get('MODEL_TTL_S')}s)"
        if st["ok"]:
            chk("cc-delegate station patch", True, detail)
        else:
            # A plugin update reverts this silently and delegation then fails
            # without saying why, so it is a warning, not a footnote.
            warn("cc-delegate station patch", detail)

    sup = supervisor.evaluate(con, cfg)
    chk("claude quota data", sup["fiveHour"] is not None or sup["sevenDay"] is not None,
        "none yet -- install the statusline hook and start a session"
        if sup["fiveHour"] is None and sup["sevenDay"] is None else
        f"5h {_pct(sup['fiveHour'])} 7d {_pct(sup['sevenDay'])}")

    if args.json:
        return _emit([{"check": c, "status": st, "ok": st != "fail", "detail": d}
                      for c, st, d in checks], True)
    label = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    print("\n".join(f"[{label[st]}] {c:<32} {d}" for c, st, d in checks))
    return 1 if any(st == "fail" for _, st, _ in checks) else 0


def _probe_lmstudio(cfg: dict) -> tuple[bool, str]:
    from . import launcher
    return launcher.probe_lmstudio(cfg)


def _probe_lmstudio_messages(cfg: dict) -> tuple[bool, str]:
    from . import launcher
    return launcher.probe_lmstudio_messages(cfg)


def _cc_delegate_root() -> Path | None:
    base = Path.home() / ".claude" / "plugins" / "cache"
    hits = sorted(base.glob("*/cc-delegate/*/.claude-plugin/plugin.json"))
    return hits[-1].parents[1] if hits else None


def cmd_logs(args) -> int:
    files = sorted(config.LOG_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if args.id:
        files = [f for f in files if f.name.startswith(args.id + ".")]
    if not files:
        return _emit(None, False, "no logs")
    if args.tail:
        text = files[0].read_text("utf-8", errors="replace").splitlines()[-args.tail:]
        return _emit(None, False, f"== {files[0]}\n" + "\n".join(text))
    return _emit(None, False, "\n".join(
        f"{_ago(f.stat().st_mtime):>6} ago  {f.stat().st_size:>8}  {f.name}" for f in files[:40]))


def cmd_launch(args) -> int:
    """Lifecycle-supervised Claude Code. Stock `claude` is never shadowed."""
    from . import launcher
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    return launcher.run(cfg, args.claude_args, dry_run=args.dry_run, force=args.force)


def cmd_fill(args) -> int:
    """Start one READY task in every free lane, best task first.

    The scheduler allows Codex, the GPU box, the VM, and OpenRouter to run at
    once; this puts work in every configured lane instead of one at a time.
    """
    from . import lanes as lanes_mod
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    repo = os.path.abspath(args.repo or os.getcwd())
    sync.reconcile(con, cfg)

    started: list[dict[str, Any]] = []
    handoff: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    avail = lanes_mod.availability(con, cfg)

    while True:
        if args.max and len(started) + len(handoff) >= args.max:
            break
        rows = scheduler.evaluate(con, cfg)["tasks"]
        candidates = [r for r in scheduler.ready(rows)
                      if not r["repo"] or r["repo"] == repo]
        candidates = [r for r in candidates
                      if r["id"] not in {x["task"] for x in started + handoff + skipped}]
        if not candidates:
            break
        task = candidates[0]
        choice = lanes_mod.choose(con, cfg, task, avail)
        if choice["lane"] is None:
            skipped.append({"task": task["id"], "reason": choice["reason"],
                            "detail": choice["skipped"]})
            continue
        entry = {"task": task["id"], "title": task["title"], "lane": choice["lane"],
                 "worker": choice["worker"], "class": choice["taskClass"]}
        if args.dry_run:
            started.append(entry)
            continue
        reservation = store.reserve_attempt(
            con, task["id"], _session_id(), choice["worker"], choice["lane"], cfg,
            profile=choice.get("profile"),
            transport="codex-plugin" if choice["worker"] == routing.CODEX
            else "cc-delegate-mcp")
        if not reservation["ok"]:
            skipped.append({"task": task["id"], "reason": reservation["reason"]})
            continue
        attempt_id = reservation["attemptId"]
        entry["attemptId"] = attempt_id
        if choice["worker"] == routing.CC_DELEGATE:
            # MCP-only: hand Claude the order, it submits and then `dg attach`.
            entry["profile"] = choice["profile"]
            entry["workOrder"] = workorder.build(
                task, cwd="the isolated git worktree cc-delegate places you in")
            entry["then"] = (f"dg attach {task['id']} <cc-task-id> "
                             f"--attempt {attempt_id}")
            handoff.append(entry)
            continue
        order = workorder.build(task, cwd="(the working directory below)")
        res = codex_plugin.dispatch(con, task, order, attempt_id)
        if not res.get("ok"):
            store.release_reservation(con, task["id"])
            skipped.append({"task": task["id"], "reason": res.get("error", "dispatch failed")})
            continue
        entry.update(jobId=res.get("jobId"), worktree=res.get("worktree"))
        started.append(entry)
        lanes_mod.invalidate(con)
        avail = lanes_mod.availability(con, cfg)

    out = {"started": started, "handoff": handoff, "skipped": skipped,
           "freeLanes": lanes_mod.free_lanes(con, cfg, repo)}
    if handoff:
        out["next"] = ("call mcp run_dev_task for each handoff entry with its profile, "
                       "then run its `then` command")
    return _emit(out, True)


def cmd_ccdelegate(args) -> int:
    from . import ccdelegate
    if args.apply:
        return _emit(ccdelegate.apply(), True)
    out = dict(ccdelegate.check())
    out["gate"] = ccdelegate.gate_settings()
    return _emit(out, True)


def cmd_lanes(args) -> int:
    from . import lanes as lanes_mod
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    rows = lanes_mod.summary(con, cfg, os.path.abspath(args.repo or os.getcwd()))
    if args.json:
        return _emit(rows, True)
    out = []
    for r in rows:
        mark = "ok  " if r["available"] else "DOWN"
        out.append(f"[{mark}] {r['lane']:<9} {r['running']}/{r['max']} running  "
                   f"{','.join(r['classes']):<24} {r['profile'] or r['worker']:<14} "
                   f"{'' if r['available'] else r['reason']}")
    return _emit(None, False, chr(10).join(out))


def cmd_proxy(args) -> int:
    from . import proxy
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    port = args.port or cfg["proxy"]["port"]
    if args.status:
        h = proxy.health(port)
        return _emit(h or {"running": False, "port": port}, True)
    if args.stop:
        return _emit(proxy.stop(port), True)
    if (h := proxy.health(port)) and not args.force:
        return _emit({**h, "note": f"a dg proxy is already listening on {port}"}, True)
    if args.start:
        # Detached: `dg proxy` alone blocks in the foreground, which is not
        # what someone wants when they just need the router up.
        from . import hooks
        hooks._spawn_proxy(port)
        if hooks._wait_for_proxy(proxy, port):
            return _emit({**(proxy.health(port) or {}), "started": True}, True)
        return _emit({"ok": False, "error": f"proxy did not come up on {port}"}, True)
    return proxy.serve(cfg, port)


def cmd_verify_desktop(args) -> int:
    """Compatibility command that explains the measured Desktop limitation."""
    return _emit({
        "ok": False,
        "supported": False,
        "desktopSeen": False,
        "reason": "personal Claude Desktop manages ANTHROPIC_BASE_URL and rejects an override",
        "available": "Governor hooks, ledger, Codex, Local, Oracle, and OpenRouter workers",
        "proxyScope": "terminal Claude Code only",
    }, True)


def cmd_hook(args) -> int:
    from . import hooks
    return hooks.run(args.name)


def cmd_probe(args) -> int:
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    if args.target == "codex":
        return _emit(quota_codex.refresh(con, cfg, force=True), True)
    res = supervisor.probe_anthropic(cfg)
    if res["ok"]:
        supervisor.clear_hard_limit(con)
    return _emit({**res, "supervisor": supervisor.evaluate(con, cfg)["state"]}, True)


def cmd_test(args) -> int:
    # Tests are not shipped in the wheel, so find the source checkout: the
    # repo containing this file when running from one, else the CWD.
    for root in (Path(__file__).resolve().parents[2], Path.cwd()):
        if (root / "tests").is_dir() and (root / "src" / "dg").is_dir():
            break
    else:
        print("dg test: no source checkout found. Run it from a clone of the "
              "delegation-governor repository.", file=sys.stderr)
        return 1
    tests = root / "tests"
    return subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-t", str(tests),
         *(["-v"] if args.verbose else [])],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(tests)])})


def cmd_install(args) -> int:
    from . import install
    proxy = True if args.proxy else (False if args.no_proxy else None)
    return install.run(dry_run=args.dry_run, proxy=proxy)


def cmd_uninstall(args) -> int:
    from . import install
    return install.uninstall(dry_run=args.dry_run, purge=args.purge)


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dg", description="Delegation Governor")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        s = sub.add_parser(name, **kw)
        s.set_defaults(func=fn)
        return s

    s = add("status", cmd_status, help="supervisor + worker + task counts")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-refresh", action="store_true", help="use cached codex state only")

    s = add("quota", cmd_quota, help="Claude and Codex quota detail")
    s.add_argument("--json", action="store_true")
    s.add_argument("--force", action="store_true", help="bypass the codex quota cache")

    s = add("route", cmd_route, help="which worker would be chosen now")
    s.add_argument("--json", action="store_true")

    s = add("worker-status", cmd_worker_status, help="live worker attempts")
    s.add_argument("--json", action="store_true")

    s = add("tasks", cmd_tasks, help="the ledger")
    s.add_argument("filter", nargs="?", choices=["all", "ready", "running", "blocked"],
                   default="all")
    s.add_argument("--json", action="store_true")

    s = add("show", cmd_task_show, help="one task in full")
    s.add_argument("id")

    s = add("add", cmd_add, help="create a task")
    s.add_argument("title")
    s.add_argument("--mode", default="WRITE", choices=["WRITE", "READ_ONLY"])
    s.add_argument("--goal", default="")
    s.add_argument("--repo", default="")
    s.add_argument("--path", action="append", help="path glob this task owns (repeatable)")
    s.add_argument("--depends-on", action="append")
    s.add_argument("--priority", type=int, default=0)
    s.add_argument("--class", dest="task_class", default="standard",
                   choices=["tiny", "simple", "standard", "hard"],
                   help="how demanding the work is; picks the lane "
                        "(tiny/simple -> local boxes, hard -> codex)")

    s = add("set", cmd_set, help="force a task status")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("--reason", default=None)

    s = add("graph", cmd_graph, help="dependency listing")
    s.add_argument("id", nargs="?")
    s.add_argument("--json", action="store_true")

    for name, fn, helptext in (("workorder", cmd_workorder, "render the bounded work order"),
                               ("dispatch", cmd_dispatch, "claim + start a worker, non-blocking")):
        s = add(name, fn, help=helptext)
        s.add_argument("id")
        s.add_argument("--state", default="")
        s.add_argument("--constraint", action="append")
        s.add_argument("--non-goal", action="append")
        s.add_argument("--acceptance", action="append")
        s.add_argument("--tests", default="")
        if name == "dispatch":
            s.add_argument("--worker", choices=["codex", "cc-delegate"])
            s.add_argument("--sandbox", choices=["read-only", "workspace-write",
                                                 "danger-full-access"])
            s.add_argument("--force", action="store_true")

    s = add("attach", cmd_attach, help="register a cc-delegate job against a task")
    s.add_argument("id")
    s.add_argument("cc_task_id")
    s.add_argument("--work-order", default="")
    s.add_argument("--lane", default=None, help="which lane it occupies (station/oracle)")
    s.add_argument("--attempt", type=int, help="reservation id returned by dispatch/fill")

    s = add("release", cmd_release, help="release an unsubmitted worker reservation")
    s.add_argument("id")

    s = add("cancel", cmd_cancel, help="cancel or release an active worker attempt")
    s.add_argument("id")

    add("recover-handoffs", cmd_recover_handoffs,
        help="recover external jobs whose reservation was not attached")

    s = add("fallback", cmd_fallback, help="supersede a failed task with a clean retry")
    s.add_argument("id")

    s = add("collect", cmd_collect, help="fetch a finished attempt's result")
    s.add_argument("id")

    s = add("integrate", cmd_integrate, help="mark reviewed work integrated")
    s.add_argument("id")
    s.add_argument("--cleanup", action="store_true", help="remove verified worker worktrees")
    s.add_argument("--accept-equivalent", action="store_true",
                   help="accept a reviewed squash/manual copy that patch-id cannot prove")
    s.add_argument("--note", default="", help="required reason for --accept-equivalent")
    s.add_argument("--force", action="store_true", help=argparse.SUPPRESS)

    s = add("override", cmd_override, help="force supervisor or worker")
    s.add_argument("what", choices=["supervisor", "worker"])
    s.add_argument("value")
    add("clear-override", cmd_clear_override, help="back to automatic")

    s = add("doctor", cmd_doctor, help="environment health")
    s.add_argument("--json", action="store_true")
    s.add_argument("--force", action="store_true")

    s = add("logs", cmd_logs, help="worker logs")
    s.add_argument("id", nargs="?")
    s.add_argument("--tail", type=int, default=0)

    s = add("probe", cmd_probe, help="confirm a provider really recovered")
    s.add_argument("target", choices=["codex", "claude"])

    s = add("launch", cmd_launch,
            help="launch stock Claude through the request-boundary router")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="deprecated compatibility flag; routing overrides use `dg override`")
    s.add_argument("claude_args", nargs=argparse.REMAINDER)

    s = add("ccdelegate", cmd_ccdelegate,
            help="cc-delegate station patch: check drift, or re-apply it")
    s.add_argument("--apply", action="store_true",
                   help="re-apply the patch (idempotent; needed after a plugin update)")

    s = add("lanes", cmd_lanes, help="worker lanes: capacity, availability, classes")
    s.add_argument("--repo", default="")
    s.add_argument("--json", action="store_true")

    s = add("quickread", quickread.run,
            help="prepare files for one-shot read-only delegation")
    s.add_argument("paths", nargs="+", metavar="PATH")

    s = add("fill", cmd_fill,
            help="start one READY task in every free lane, non-blocking")
    s.add_argument("--repo", default="")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max", type=int, default=0, help="cap how many to start")

    s = add("proxy", cmd_proxy,
            help="terminal Claude Code router: per-request supervisor failover")
    s.add_argument("--port", type=int)
    s.add_argument("--status", action="store_true")
    s.add_argument("--start", action="store_true",
                   help="start it in the background and wait until it answers")
    s.add_argument("--stop", action="store_true")
    s.add_argument("--force", action="store_true", help="start even if one seems to be running")

    add("verify-desktop", cmd_verify_desktop,
        help="explain why personal Claude Desktop cannot use the router")

    s = add("hook", cmd_hook, help="Claude Code integration points (used by settings.json)")
    s.add_argument("name", choices=["statusline", "prompt", "stopfailure", "session"])

    s = add("test", cmd_test, help="run the test suite")
    s.add_argument("-v", "--verbose", action="store_true")

    s = add("install", cmd_install, help="install hooks, skill and statusline")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--proxy", action="store_true",
                   help="arm the local router: point Claude Code at it via settings.json "
                        "and a persistent user ANTHROPIC_BASE_URL")
    s.add_argument("--no-proxy", action="store_true",
                   help="tear the router wiring down. Without either flag the current "
                        "wiring is left exactly as it is")
    s = add("uninstall", cmd_uninstall, help="remove them again")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--purge", action="store_true", help="also delete governor state")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError) as e:
        print(f"dg: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
