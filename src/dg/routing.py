"""Worker selection: Codex first, cc-delegate when Codex cannot serve.

The whole value of the quota cache is here -- when Codex is known EXHAUSTED we
route straight to cc-delegate without spending a Codex request to rediscover it.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import quota_codex, store

CODEX = "codex"
CC_DELEGATE = "cc-delegate"


def select_for(con: sqlite3.Connection, cfg: dict, task: dict[str, Any]) -> dict[str, Any]:
    """Which lane should run this specific task?

    Class first, availability second -- so a trivial edit does not consume the
    Codex slot a hard task needs, and the local boxes stop idling while Codex
    is healthy.
    """
    from . import lanes as lanes_mod
    choice = lanes_mod.choose(con, cfg, task)
    if choice["lane"] is None:
        # Nothing suits it right now; say so rather than guessing a lane.
        return {**choice, "worker": None}
    return choice


def select(con: sqlite3.Connection, cfg: dict, refresh: bool = True) -> dict[str, Any]:
    override = cfg["overrides"]["worker"]
    if override == CODEX:
        return {"worker": CODEX, "reason": "override worker=codex", "codexState": "OVERRIDDEN"}
    if override == CC_DELEGATE:
        return {"worker": CC_DELEGATE, "reason": "override worker=cc-delegate",
                "codexState": "OVERRIDDEN"}

    info = quota_codex.refresh(con, cfg) if refresh else {
        "state": store.kv_get(con, quota_codex.K_STATE, quota_codex.UNKNOWN), "cached": True}
    state = info["state"]

    if state == quota_codex.READY:
        return {"worker": CODEX, "reason": "codex ready", "codexState": state,
                "cached": info.get("cached", False)}

    reasons = {
        quota_codex.EXHAUSTED: "codex quota exhausted",
        quota_codex.AUTH_ERROR: "codex authentication failed",
        quota_codex.NETWORK_ERROR: "codex unreachable",
        quota_codex.CLI_MISSING: "codex CLI not installed",
        quota_codex.PROTOCOL_ERROR: "codex app-server protocol mismatch",
        quota_codex.PROBE: "codex probe in flight",
        quota_codex.UNKNOWN: "codex state unknown",
    }
    out = {"worker": CC_DELEGATE, "reason": reasons.get(state, "codex unavailable"),
           "codexState": state, "cached": info.get("cached", False)}
    until = store.kv_get(con, quota_codex.K_UNTIL, 0) or 0
    if state == quota_codex.EXHAUSTED and until:
        out["codexResetsAt"] = int(until)
        out["codexResetsIn"] = max(0, int(until - time.time()))
    cooldown = store.kv_get(con, quota_codex.K_COOLDOWN, 0) or 0
    if cooldown > time.time():
        out["cooldownUntil"] = int(cooldown)
    return out
