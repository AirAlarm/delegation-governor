"""Lanes: the scarce resources work actually competes for.

A lane is a *machine*, not a tool. `station` and `oracle` are both reached
through cc-delegate, but they are different computers and can run at the same
time -- modelling them as one worker gave them a shared slot and left the VM
idle whenever the GPU was busy.

    codex       cloud, contends with nothing local
    station     the GPU box; one model resident at a time, so one job
    oracle      a separate CPU VM; slow, contends with nothing
    openrouter  metered cloud endpoint, independent of all three

Routing picks a lane by task class first and availability second, so a trivial
edit does not consume the Codex slot that a hard task needs.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from . import quota_codex, store

CLASSES = ("tiny", "simple", "standard", "hard")
_PROBE_KEY = "laneProbes"


def lanes(cfg: dict) -> dict[str, dict]:
    return cfg["workers"]["lanes"]


def normalise_class(value: str | None, cfg: dict) -> str:
    v = (value or "").strip().lower()
    return v if v in CLASSES else cfg["workers"].get("defaultClass", "standard")


def preference(task_class: str, cfg: dict) -> list[str]:
    """Ordered lane preference for a class, filtered to lanes that exist."""
    table = cfg["workers"]["classRouting"]
    order = table.get(task_class) or table.get("standard") or list(lanes(cfg))
    known = lanes(cfg)
    return [name for name in order if name in known]


# ---------------------------------------------------------------- capacity

def _norm_repo(repo: str) -> str:
    """Repos are stored absolute; accept either form from callers."""
    return os.path.abspath(repo) if repo else ""


def in_flight(con: sqlite3.Connection, repo: str | None = None) -> dict[str, int]:
    """Reserved or running WRITE attempts per lane, in one repo or (None) all of them."""
    repo = None if repo is None else _norm_repo(repo)
    counts: dict[str, int] = {}
    for a in store.running_attempts(con):
        if a["task_mode"] != "WRITE" or (repo is not None and a["repo"] != repo):
            continue
        lane = a["lane"] or _lane_for_worker(a["worker"])
        counts[lane] = counts.get(lane, 0) + 1
    return counts


def _lane_for_worker(worker: str) -> str:
    """Attempts predating lanes still need a bucket."""
    return "codex" if worker == "codex" else "station"


def has_capacity(con: sqlite3.Connection, lane: str, repo: str, cfg: dict) -> bool:
    spec = lanes(cfg).get(lane)
    if spec is None:
        return False
    # A lane is a machine or an account, so its slots are shared by every repo;
    # only the total budget is per repo. Counting the lane per repo let a
    # maxWriteJobs=1 lane run one job in each repo at once (seen live: codex
    # and opencode-main each double-booked across two repos).
    if in_flight(con).get(lane, 0) >= spec.get("maxWriteJobs", 1):
        return False
    return sum(in_flight(con, repo).values()) < cfg["workers"]["totalWriteJobsPerRepo"]


# ---------------------------------------------------------------- availability

def availability(con: sqlite3.Connection, cfg: dict, refresh: bool = True) -> dict[str, Any]:
    """{lane: (ok, reason)} -- cached, because probing a remote VM is slow.

    The cache is what keeps dispatch cheap: without it every `dg tasks` would
    pay a round trip to the Oracle box.

    A down verdict is trusted for a much shorter window than an up one
    (`laneProbeDownTtlSeconds`, default 10s vs. 60s). Without this, one
    transient blip on a flaky remote (verified live: OpenCode Go's endpoint)
    gets cached for the full TTL, so an entire dispatch burst (`dg fill`
    across several tasks) consistently skips a lane that already recovered.

    `refresh=False` (hooks: any answer beats a round-trip) returns cached
    whenever present, however stale. `refresh=True` (default) additionally
    requires it be within the effective TTL, else a live probe runs.
    """
    ttl = cfg["workers"].get("laneProbeTtlSeconds", 60)
    down_ttl = cfg["workers"].get("laneProbeDownTtlSeconds", 10)
    cached = store.kv_get(con, _PROBE_KEY) or {}
    age = store.kv_age(con, _PROBE_KEY)
    if cached and not refresh:
        return cached
    effective_ttl = down_ttl if any(not v[0] for v in cached.values()) else ttl
    if cached and age is not None and age < effective_ttl:
        return cached

    from . import launcher, supervisor
    out: dict[str, Any] = {}
    sup = supervisor.evaluate(con, cfg)
    local_supervisor_tier = None
    if sup["state"] == supervisor.LOCAL:
        chosen, _ = launcher.first_usable_tier(cfg)
        local_supervisor_tier = (chosen or {}).get("name")

    for name, spec in lanes(cfg).items():
        if spec["worker"] == "codex":
            from .workers import codex_plugin
            plugin = codex_plugin.discover()
            if not plugin["ok"]:
                out[name] = [False, plugin["reason"]]
                continue
            st = quota_codex.refresh(con, cfg)["state"]
            out[name] = [st == quota_codex.READY,
                         f"codex plugin {plugin.get('version')} ready"
                         if st == quota_codex.READY else st]
            continue
        tier_name = spec.get("tier")
        if tier_name and tier_name == local_supervisor_tier:
            # The supervisor is running on that machine; a worker there would
            # evict its model. Observed live as a cc-delegate gate timeout.
            out[name] = [False, f"{tier_name} is hosting the local supervisor"]
            continue
        t = launcher.tier(cfg, tier_name) if tier_name else None
        endpoint_name = spec.get("endpoint")
        if endpoint_name:
            from .workers import cc_delegate
            profile = cc_delegate.profile(endpoint_name=endpoint_name,
                                          profile_name=spec.get("profile"))
            if not profile["ok"]:
                out[name] = [False, profile["reason"]]
                continue
            t = cfg["workers"].get("endpoints", {}).get(endpoint_name)
            if t is None:
                out[name] = [False, f"worker endpoint {endpoint_name!r} is not configured"]
                continue
        if t is None:
            out[name] = [True, "no tier probe configured"]
            continue
        ok, detail = launcher.probe_tier(t)
        out[name] = [ok, detail]

    store.kv_set(con, _PROBE_KEY, out)
    return out


def invalidate(con: sqlite3.Connection) -> None:
    store.kv_set(con, _PROBE_KEY, {})


# ---------------------------------------------------------------- selection

def choose(con: sqlite3.Connection, cfg: dict, task: dict[str, Any],
           avail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Best lane for one task, or why none is usable.

    Preference by class, then capacity, then availability. A manual worker
    override still wins -- it pins the tool, and the first lane using that tool
    is taken.
    """
    task_class = normalise_class(task.get("taskClass"), cfg)
    order = preference(task_class, cfg)
    override = cfg["overrides"]["worker"]
    if override != "auto":
        order = [n for n in order if lanes(cfg)[n]["worker"] == override] or [
            n for n, s in lanes(cfg).items() if s["worker"] == override]

    if avail is None:
        avail = availability(con, cfg)
    repo = task.get("repo") or ""
    skipped: list[str] = []
    for name in order:
        ok, reason = avail.get(name, [True, "unprobed"])
        if not ok:
            skipped.append(f"{name}: {reason}")
            continue
        if task["mode"] == "WRITE" and not has_capacity(con, name, repo, cfg):
            skipped.append(f"{name}: at capacity")
            continue
        spec = lanes(cfg)[name]
        return {"lane": name, "worker": spec["worker"], "profile": spec.get("profile"),
                "taskClass": task_class, "preference": order, "skipped": skipped,
                "reason": f"{task_class} -> {name}"}
    return {"lane": None, "worker": None, "taskClass": task_class,
            "preference": order, "skipped": skipped,
            "reason": f"no lane available for a {task_class} task"}


def free_lanes(con: sqlite3.Connection, cfg: dict, repo: str,
               avail: dict[str, Any] | None = None) -> list[str]:
    if avail is None:
        avail = availability(con, cfg)
    repo = _norm_repo(repo)
    return [n for n in lanes(cfg)
            if avail.get(n, [True, ""])[0] and has_capacity(con, n, repo, cfg)]


def summary(con: sqlite3.Connection, cfg: dict, repo: str = "") -> list[dict[str, Any]]:
    avail = availability(con, cfg)
    counts = in_flight(con)
    out = []
    for name, spec in lanes(cfg).items():
        ok, reason = avail.get(name, [True, "unprobed"])
        out.append({"lane": name, "worker": spec["worker"], "profile": spec.get("profile"),
                    "available": ok, "reason": reason,
                    "running": counts.get(name, 0), "max": spec.get("maxWriteJobs", 1),
                    "classes": [c for c in CLASSES if name in preference(c, cfg)]})
    return out
