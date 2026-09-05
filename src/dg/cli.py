"""dg -- Delegation Governor control CLI."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config, gitutil, quota_codex, routing, scheduler, store, supervisor, sync, workorder
from .workers import cc_delegate
from .workers import codex as codex_worker


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
        priority=args.priority)
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
    """Claim a task, start Codex on it, return immediately."""
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

    worker = args.worker or routing.select(con, cfg)["worker"]
    if worker == routing.CC_DELEGATE:
        # cc-delegate is MCP-only: hand Claude the work order and the profile,
        # then `dg attach` once run_dev_task returns its task id.
        # cc-delegate creates its own worktree from this repo, so the order
        # must not claim the repo path itself is one.
        order = _order(t, args, "the isolated git worktree cc-delegate places you in")
        print(json.dumps({"worker": "cc-delegate", "action": "call mcp run_dev_task",
                          "taskId": t["id"], "repo": t["repo"],
                          "then": f"dg attach {t['id']} <cc-task-id>",
                          "workOrder": order}, indent=2))
        return 0

    if not scheduler.worker_capacity_free(con, worker, t["repo"], cfg):
        print(f"codex write capacity full for {t['repo']}", file=sys.stderr)
        return 3
    if not store.claim(con, args.id, _session_id(), worker) and not args.force:
        print(f"{args.id} was claimed by another session", file=sys.stderr)
        return 4

    order = _order(t, args, "(the working directory below)")
    res = codex_worker.dispatch(con, t, order, _session_id(), sandbox=args.sandbox)
    if not res.get("ok"):
        store.release(con, args.id)
        print(res.get("error", "dispatch failed"), file=sys.stderr)
        return 5
    res["task"] = args.id
    res["nextReady"] = [r["id"] for r in scheduler.ready(scheduler.evaluate(con, cfg)["tasks"])]
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
    if t["status"] == "PLANNED":
        store.claim(con, args.id, _session_id(), routing.CC_DELEGATE)
    res = cc_delegate.attach(con, t, args.cc_task_id, args.work_order or "(recorded by Claude)")
    return _emit(res, True)


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
    new = store.create_task(
        con, title=t["title"], mode=t["mode"], goal=t["goal"], repo=t["repo"],
        paths=t["paths"], depends_on=t["dependsOn"], priority=t["priority"] + 1)
    store.set_status(con, args.id, "SUPERSEDED",
                     failure_reason=f"{t['status']}; superseded by {new}")
    # Partial worker output is preserved on disk but never carried into the
    # retry: the fallback starts from the original clean base (spec 34).
    return _emit({"original": args.id, "fallback": new,
                  "worker": routing.select(con, cfg)["worker"],
                  "note": "partial work from the failed attempt is preserved, not reused"}, True)


def cmd_collect(args) -> int:
    """Pull a finished attempt's result for review."""
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    sync.reconcile(con, cfg)
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
    view = scheduler.evaluate(con, cfg)
    out["unblockedNow"] = [r["id"] for r in scheduler.ready(view["tasks"])]
    return _emit(out, True)


def cmd_integrate(args) -> int:
    con = store.connect()
    t = store.get_task(con, args.id)
    if not t:
        print(f"no such task {args.id}", file=sys.stderr)
        return 1
    if t["status"] != "SUCCEEDED" and not args.force:
        print(f"{args.id} is {t['status']}, expected SUCCEEDED", file=sys.stderr)
        return 2
    store.set_status(con, args.id, "INTEGRATED")
    cleaned = []
    if args.cleanup:
        for a in store.attempts_for(con, args.id):
            if a["worktree"] and a["worker"] == "codex":
                cleaned.append(gitutil.remove_worktree(
                    t["repo"], a["worktree"], a["branch"], force=args.discard))
    out = {"task": args.id, "status": "INTEGRATED", "cleanup": cleaned}
    kept = [c["keptBranch"] for c in cleaned if c.get("keptBranch")]
    if kept:
        out["note"] = (f"kept unmerged branch(es) {', '.join(kept)} -- the work is only "
                       f"there. Merge, or re-run with --cleanup --discard to drop it.")
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
    from . import install as install_mod
    env_cur, env_ours = install_mod.proxy_env_state(port)
    if env_ours:
        # Claude is routed through the proxy, so the proxy MUST be up.
        chk(f"router proxy :{port}", h is not None,
            f"serving {h.get('stats')}" if h else
            "ANTHROPIC_BASE_URL points here but nothing is listening -- "
            "run `dg proxy` (SessionStart normally does)")
        chk("claude routed through router", True, f"{env_cur} (Desktop failover active)")
    else:
        chk("router proxy", True,
            (f"running on :{port}, not wired in" if h else f"not running (:{port})")
            + "; `dg install --proxy` routes Claude through it for Desktop failover")

    lp = store.kv_get(con, "launcher") or {}
    if lp:
        chk("last dg launch", True,
            f"route={lp.get('route')} model={lp.get('model')} "
            f"session={lp.get('sessionId')} restarts={lp.get('restarts', 0)}")

    plugin = _cc_delegate_root()
    chk("cc-delegate plugin", plugin is not None, str(plugin or "not found (fallback worker)"))

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


def cmd_proxy(args) -> int:
    from . import proxy
    con = store.connect()
    cfg = _cfg_with_overrides(con)
    port = args.port or cfg["proxy"]["port"]
    if args.status:
        h = proxy.health(port)
        return _emit(h or {"running": False, "port": port}, True)
    if args.stop:
        print("stop the `dg proxy` process directly (Ctrl-C, or kill its PID)",
              file=sys.stderr)
        return 1
    if (h := proxy.health(port)) and not args.force:
        return _emit({**h, "note": f"a dg proxy is already listening on {port}"}, True)
    return proxy.serve(cfg, port)


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
    return install.run(dry_run=args.dry_run, proxy=args.proxy)


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

    s = add("fallback", cmd_fallback, help="supersede a failed task with a clean retry")
    s.add_argument("id")

    s = add("collect", cmd_collect, help="fetch a finished attempt's result")
    s.add_argument("id")

    s = add("integrate", cmd_integrate, help="mark reviewed work integrated")
    s.add_argument("id")
    s.add_argument("--cleanup", action="store_true", help="remove the codex worktree")
    s.add_argument("--discard", action="store_true",
                   help="with --cleanup, also delete a branch that is not merged (destructive)")
    s.add_argument("--force", action="store_true")

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
            help="lifecycle-supervised claude: relaunches across backend switches")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="start on Anthropic even when the supervisor says LOCAL "
                        "and LM Studio is unusable")
    s.add_argument("claude_args", nargs=argparse.REMAINDER)

    s = add("proxy", cmd_proxy,
            help="router proxy: per-request failover, works inside Claude Desktop")
    s.add_argument("--port", type=int)
    s.add_argument("--status", action="store_true")
    s.add_argument("--stop", action="store_true")
    s.add_argument("--force", action="store_true", help="start even if one seems to be running")

    s = add("hook", cmd_hook, help="Claude Code integration points (used by settings.json)")
    s.add_argument("name", choices=["statusline", "prompt", "stopfailure", "session"])

    s = add("test", cmd_test, help="run the test suite")
    s.add_argument("-v", "--verbose", action="store_true")

    s = add("install", cmd_install, help="install hooks, skill and statusline")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--proxy", action="store_true",
                   help="also route Claude through the local router by setting a "
                        "persistent user ANTHROPIC_BASE_URL (needed for Claude Desktop "
                        "failover)")
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
