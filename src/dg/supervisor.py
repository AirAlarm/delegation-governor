"""Claude (Anthropic) quota + supervisor state machine.

Quota is not polled: Claude Code hands it to us for free on every statusline
refresh and every StopFailure. Verified against Claude Code 2.1.116, the
statusline payload carries

    rate_limits: { five_hour:  {utilization, resets_at},
                   seven_day:  {utilization, resets_at} }

built from the `anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}`
response headers. `utilization` is 0-100; `resets_at` is unix seconds. Both
windows are absent until the first successful API response, so absence is
normal and must never read as 0%.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import store

NORMAL = "CLAUDE_NORMAL"
SAVE = "CLAUDE_SAVE"
LOCAL = "CLAUDE_LOCAL"
PROBE = "CLAUDE_PROBE"

K_STATE = "supervisorState"
K_QUOTA = "claudeQuota"
K_HARD = "claudeHardLimit"


def _window(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise one window to a 0-100 percentage.

    `utilization` is a **fraction 0..1**, not a percentage: Claude Code renders
    it as `Math.floor(utilization * 100)` and warns below `utilization < 0.7`.
    Comparing it directly against percentage thresholds silently disables the
    whole supervisor, so the scale is converted here, at the one place the
    field enters the system.

    The older `used_percentage` spelling is already a percentage and is
    accepted unscaled, so a Claude Code change in either direction still
    parses rather than reading as 'unknown'.
    """
    if not isinstance(raw, dict):
        return None
    used = raw.get("utilization")
    if isinstance(used, (int, float)):
        used = float(used) * 100.0
    else:
        used = raw.get("used_percentage")
        if not isinstance(used, (int, float)):
            return None
        used = float(used)
    resets = raw.get("resets_at")
    return {"usedPercent": used,
            "resetsAt": int(resets) if isinstance(resets, (int, float)) else None}


def ingest_statusline(con: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Record the quota Claude Code just handed the statusline. Zero cost."""
    rl = payload.get("rate_limits") or {}
    quota = {"fiveHour": _window(rl.get("five_hour")),
             "sevenDay": _window(rl.get("seven_day")),
             "at": time.time()}
    if quota["fiveHour"] is None and quota["sevenDay"] is None:
        # Nothing usable -- keep whatever we last knew rather than blanking it.
        return store.kv_get(con, K_QUOTA) or quota
    store.kv_set(con, K_QUOTA, quota)
    return quota


def record_hard_limit(con: sqlite3.Connection, error: str, detail: str = "") -> None:
    """StopFailure(rate_limit): Anthropic refused the turn outright."""
    store.kv_set(con, K_HARD, {"at": time.time(), "error": error, "detail": detail[:300]})
    store.kv_set(con, K_STATE, LOCAL)


def evaluate(con: sqlite3.Connection, cfg: dict) -> dict[str, Any]:
    """Derive the supervisor state. Pure -- reads state, writes only the result.

    A hard rate_limit pins LOCAL until its reset passes; then we go to PROBE
    and only a real Anthropic response clears it (spec 3).
    """
    scfg = cfg["supervisor"]
    override = cfg["overrides"]["supervisor"]
    quota = store.kv_get(con, K_QUOTA) or {}
    hard = store.kv_get(con, K_HARD)
    now = time.time()

    five, seven = quota.get("fiveHour"), quota.get("sevenDay")
    age = (now - quota["at"]) if quota.get("at") else None
    stale = age is not None and age > scfg["quotaStaleSeconds"]

    reason = ""
    if hard:
        resets = _soonest_reset(five, seven)
        if resets and resets <= now:
            state, reason = PROBE, "hard limit reset time passed, probing"
        else:
            state = LOCAL
            reason = "hard rate_limit from Anthropic"
    elif five is None and seven is None:
        state, reason = NORMAL, "no quota data yet"
    elif stale:
        state = store.kv_get(con, K_STATE, NORMAL)
        reason = f"quota {int(age)}s stale, holding {state}"
    else:
        state, reason = _from_thresholds(five, seven, scfg)

    effective = state
    if override == "claude":
        effective = SAVE if state == SAVE else NORMAL
        reason = f"override supervisor=claude ({reason})"
    elif override == "local":
        effective, reason = LOCAL, "override supervisor=local"

    store.kv_set(con, K_STATE, state)
    return {
        "state": effective, "autoState": state, "override": override, "reason": reason,
        "fiveHour": five, "sevenDay": seven, "hardLimit": hard,
        "quotaAgeSeconds": None if age is None else int(age),
    }


def _from_thresholds(five, seven, scfg) -> tuple[str, str]:
    hits: list[tuple[str, str]] = []
    for win, key, label in ((five, "fiveHour", "5h"), (seven, "sevenDay", "7d")):
        if win is None:
            continue  # missing != 0%
        used, th = win["usedPercent"], scfg[key]
        if used >= th["local"]:
            hits.append((LOCAL, f"{label} {used:.0f}% >= {th['local']}%"))
        elif used >= th["save"]:
            hits.append((SAVE, f"{label} {used:.0f}% >= {th['save']}%"))
    if any(s == LOCAL for s, _ in hits):
        return LOCAL, "; ".join(r for s, r in hits if s == LOCAL)
    if hits:
        return SAVE, "; ".join(r for _, r in hits)
    return NORMAL, "within thresholds"


def _soonest_reset(five, seven) -> int | None:
    vals = [w["resetsAt"] for w in (five, seven) if w and w.get("resetsAt")]
    return min(vals) if vals else None


def clear_hard_limit(con: sqlite3.Connection) -> None:
    """A successful Anthropic turn happened -- the probe succeeded."""
    store.kv_set(con, K_HARD, None)


def probe_anthropic(cfg: dict, timeout: float = 20.0) -> dict[str, Any]:
    """Confirm Anthropic actually serves us again (spec 3: confirm, don't assume).

    Uses the cheapest possible real call -- one token against the Claude Code
    credentials already on this machine -- via `claude -p`. Costs a rounding
    error of quota and is the only way to know for sure.
    """
    import shutil
    import subprocess

    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "reason": "claude not on PATH"}
    env = {k: v for k, v in _clean_env().items()}
    try:
        p = subprocess.run(
            [exe, "-p", "ping", "--max-turns", "1", "--model", "haiku"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "probe timed out"}
    except OSError as e:
        return {"ok": False, "reason": str(e)}
    blob = (p.stdout + p.stderr).lower()
    if p.returncode == 0 and "rate limit" not in blob and "usage limit" not in blob:
        return {"ok": True, "reason": "anthropic responded"}
    return {"ok": False, "reason": (p.stderr or p.stdout or "non-zero exit").strip()[:200]}


def _clean_env() -> dict[str, str]:
    """Env with any LM Studio redirect stripped, so the probe really hits Anthropic."""
    import os
    env = dict(os.environ)
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
              "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        env.pop(k, None)
    return env
