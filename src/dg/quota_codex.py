"""Codex quota + worker state machine.

Quota comes from the app-server JSON-RPC method `account/rateLimits/read`,
verified live against codex-cli 0.153.2. It costs zero model inference: the
app-server answers from the account usage snapshot, no turn is started.

Response shape (from `codex app-server generate-json-schema`):
    { rateLimits: RateLimitSnapshot,
      rateLimitsByLimitId: { <limitId>: RateLimitSnapshot } | null,
      rateLimitResetCredits: { availableCount, credits[] } | null }
    RateLimitSnapshot.primary/.secondary: RateLimitWindow | null
    RateLimitWindow: { usedPercent, windowDurationMins | null, resetsAt | null }
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any

from . import store

READY = "CODEX_READY"
EXHAUSTED = "CODEX_EXHAUSTED"
PROBE = "CODEX_PROBE"
AUTH_ERROR = "CODEX_AUTH_ERROR"
NETWORK_ERROR = "CODEX_NETWORK_ERROR"
CLI_MISSING = "CODEX_CLI_MISSING"
PROTOCOL_ERROR = "CODEX_PROTOCOL_ERROR"
UNKNOWN = "CODEX_UNKNOWN"

K_STATE = "codexState"
K_QUOTA = "codexQuota"
K_UNTIL = "codexUnavailableUntil"
K_COOLDOWN = "codexCooldownUntil"


def codex_bin() -> str | None:
    return shutil.which("codex") or shutil.which("codex.cmd")


def read_rate_limits(timeout: float = 45.0) -> dict[str, Any]:
    """One app-server round trip. Returns {ok, data|errorKind, detail}."""
    exe = codex_bin()
    if not exe:
        return {"ok": False, "errorKind": CLI_MISSING, "detail": "codex not on PATH"}

    try:
        proc = subprocess.Popen(
            [exe, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as e:
        return {"ok": False, "errorKind": CLI_MISSING, "detail": str(e)}

    result: dict[str, Any] = {"ok": False, "errorKind": UNKNOWN, "detail": "no response"}

    def send(obj: dict) -> None:
        assert proc.stdin
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    def reader() -> None:
        assert proc.stdout
        for raw in proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if msg.get("id") == 1:
                if "error" in msg:
                    result.update(ok=False, errorKind=PROTOCOL_ERROR,
                                  detail=str(msg["error"])[:300])
                    return
                send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                send({"jsonrpc": "2.0", "id": 2,
                      "method": "account/rateLimits/read", "params": {}})
            elif msg.get("id") == 2:
                if "error" in msg:
                    result.update(ok=False, errorKind=_classify(str(msg["error"])),
                                  detail=str(msg["error"])[:300])
                else:
                    result.update(ok=True, data=msg.get("result", {}), errorKind=None, detail="")
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "delegation-governor", "version": "0.1.0"}}})
    except OSError as e:
        result.update(ok=False, errorKind=PROTOCOL_ERROR, detail=str(e))
    t.join(timeout)
    if t.is_alive():
        result.update(ok=False, errorKind=NETWORK_ERROR, detail=f"timed out after {timeout}s")
    try:
        proc.kill()
    except OSError:
        pass
    return result


def _classify(text: str) -> str:
    """Last-resort textual classification of an app-server error.

    Only reached when the structured channel itself failed, per spec 8: it is
    never the primary source of truth.
    """
    low = text.lower()
    if any(k in low for k in ("unauthor", "401", "auth", "login", "credential", "token expired")):
        return AUTH_ERROR
    if any(k in low for k in ("network", "dns", "connect", "timeout", "unreachable", "socket")):
        return NETWORK_ERROR
    if any(k in low for k in ("rate limit", "quota", "usage limit", "429")):
        return EXHAUSTED
    return UNKNOWN


# ---------------------------------------------------------------- evaluation

def _windows(snap: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, snap[name]) for name in ("primary", "secondary")
            if isinstance(snap.get(name), dict)]


def evaluate(data: dict[str, Any], cfg: dict) -> dict[str, Any]:
    """Turn a rateLimits payload into {state, unavailableUntil, windows, credits}.

    Missing is neither exhausted nor available (spec 7): with no usable window
    at all we report UNKNOWN and let the caller decide, rather than guessing.
    """
    ccfg = cfg["codex"]
    limit = ccfg["exhaustedPercent"]
    blocking = set(ccfg["blockingLimitIds"])

    buckets: dict[str, dict[str, Any]] = {}
    by_id = data.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        buckets = {k: v for k, v in by_id.items() if isinstance(v, dict)}
    elif isinstance(data.get("rateLimits"), dict):
        snap = data["rateLimits"]
        buckets = {snap.get("limitId") or "codex": snap}

    windows: list[dict[str, Any]] = []
    exhausted_resets: list[int] = []
    saw_blocking_window = False
    reached = False

    for limit_id, snap in buckets.items():
        gating = limit_id in blocking
        if snap.get("rateLimitReachedType") and gating:
            reached = True
        for name, win in _windows(snap):
            used = win.get("usedPercent")
            if not isinstance(used, (int, float)):
                continue
            resets = win.get("resetsAt")
            row = {
                "limitId": limit_id, "window": name, "usedPercent": int(used),
                "windowDurationMins": win.get("windowDurationMins"),
                "resetsAt": resets, "gating": gating,
            }
            windows.append(row)
            if gating:
                saw_blocking_window = True
                if used >= limit:
                    # A window with no resetsAt cannot be waited out; treat it
                    # as blocking until the next successful probe says otherwise.
                    exhausted_resets.append(int(resets) if isinstance(resets, int) else 0)

    credits = data.get("rateLimitResetCredits") or {}
    out: dict[str, Any] = {
        "windows": windows,
        "planType": (data.get("rateLimits") or {}).get("planType"),
        "spendControlReached": (data.get("rateLimits") or {}).get("spendControlReached"),
        "resetCreditsAvailable": credits.get("availableCount", 0),
        "fetchedAt": time.time(),
    }
    if exhausted_resets or reached:
        # Unavailable until every exhausted blocking window has recovered.
        out["state"] = EXHAUSTED
        out["unavailableUntil"] = max(exhausted_resets) if exhausted_resets else 0
    elif not saw_blocking_window:
        out["state"] = UNKNOWN
        out["unavailableUntil"] = 0
    else:
        out["state"] = READY
        out["unavailableUntil"] = 0
    if (data.get("rateLimits") or {}).get("spendControlReached") is True:
        out["state"] = EXHAUSTED
    return out


# ---------------------------------------------------------------- state machine

def refresh(con: sqlite3.Connection, cfg: dict, force: bool = False) -> dict[str, Any]:
    """Bring cached Codex state up to date, hitting app-server only when useful.

    Cheap paths first: a fresh cache, or a known EXHAUSTED window that has not
    reached its resetsAt yet, both answer without spawning anything. That is the
    whole point of quota monitoring (spec 5).
    """
    ccfg = cfg["codex"]
    now = time.time()
    cached = store.kv_get(con, K_QUOTA)
    state = store.kv_get(con, K_STATE, UNKNOWN)
    age = store.kv_age(con, K_QUOTA)

    if not force:
        cooldown = store.kv_get(con, K_COOLDOWN, 0) or 0
        if cooldown > now:
            return {"state": state, "cached": True, "cooldownUntil": cooldown,
                    "quota": cached, "reason": "cooldown"}
        until = store.kv_get(con, K_UNTIL, 0) or 0
        if state == EXHAUSTED and until and until > now:
            return {"state": EXHAUSTED, "cached": True, "unavailableUntil": until,
                    "quota": cached, "reason": "known exhausted"}
        if cached and age is not None and age < ccfg["quotaTtlSeconds"] and state == READY:
            return {"state": state, "cached": True, "quota": cached, "reason": "fresh cache"}

    # Reset time has passed (or cache is stale): PROBE, and only trust a
    # confirmed answer -- a timestamp passing is not recovery (spec 7).
    if state == EXHAUSTED:
        store.kv_set(con, K_STATE, PROBE)
    res = read_rate_limits(ccfg["probeTimeoutSeconds"])

    if not res.get("ok"):
        kind = res.get("errorKind") or UNKNOWN
        store.kv_set(con, K_STATE, kind)
        if kind == NETWORK_ERROR:
            store.kv_set(con, K_COOLDOWN, now + ccfg["networkCooldownSeconds"])
        elif kind == AUTH_ERROR:
            store.kv_set(con, K_COOLDOWN, now + ccfg["authCooldownSeconds"])
        return {"state": kind, "cached": False, "error": res.get("detail", ""), "quota": cached}

    ev = evaluate(res["data"], cfg)
    store.kv_set(con, K_QUOTA, ev)
    store.kv_set(con, K_STATE, ev["state"])
    store.kv_set(con, K_UNTIL, ev["unavailableUntil"])
    store.kv_set(con, K_COOLDOWN, 0)
    return {"state": ev["state"], "cached": False, "quota": ev,
            "unavailableUntil": ev["unavailableUntil"]}


def mark_exhausted(con: sqlite3.Connection, resets_at: int | None, detail: str = "") -> None:
    """Called by the Codex adapter when a run dies of quota mid-task (spec 32)."""
    with store.transaction(con):
        store.kv_set(con, K_STATE, EXHAUSTED)
        prev = store.kv_get(con, K_UNTIL, 0) or 0
        store.kv_set(con, K_UNTIL, max(prev, int(resets_at or 0)))
        q = store.kv_get(con, K_QUOTA) or {}
        q["lastFailure"] = detail[:300]
        store.kv_set(con, K_QUOTA, q)


def available(con: sqlite3.Connection, cfg: dict, refresh_first: bool = True) -> bool:
    st = refresh(con, cfg)["state"] if refresh_first else store.kv_get(con, K_STATE, UNKNOWN)
    return st == READY
