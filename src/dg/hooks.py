"""Claude Code integration points, all reachable as `dg hook <name>`.

One executable on PATH instead of three scripts that would each need PYTHONPATH
wiring in settings.json.

None of these may ever raise: a broken hook must not break a session, and a
broken statusline must not blank the status bar.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

from . import config, quota_codex, routing, scheduler, store, supervisor, sync


def _load() -> tuple[Any, dict]:
    con = store.connect()
    cfg = config.load()
    saved = store.kv_get(con, "overrides") or {}
    cfg["overrides"].update({k: v for k, v in saved.items() if v})
    return con, cfg


def _stdin_json() -> dict:
    try:
        return json.load(sys.stdin)
    except (ValueError, OSError):
        return {}


def _safe_stdout() -> None:
    """Never let an unencodable character take out the status bar.

    Windows consoles still default to cp1252, where a stray non-ASCII glyph
    raises UnicodeEncodeError mid-print. Hook output stays ASCII by design;
    this is the belt to that braces.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


# ---------------------------------------------------------------- statusline

def statusline() -> int:
    """Zero-token status bar, and the Governor's free Claude-quota feed.

    Claude Code hands this the session JSON including `rate_limits`; recording
    it here is why the Governor never polls Anthropic for quota. Must stay
    fast, so it reads the Codex cache and never starts the app-server.
    """
    _safe_stdout()
    payload = _stdin_json()
    try:
        con, cfg = _load()
        supervisor.ingest_statusline(con, payload)
        sup = supervisor.evaluate(con, cfg)
        wrk = routing.select(con, cfg, refresh=False)
        counts = scheduler.evaluate(con, cfg)["counts"]
    except Exception as e:
        print(f"DG error: {type(e).__name__}")
        return 0

    sup_txt = sup["state"].replace("CLAUDE_", "")
    if cfg["overrides"]["supervisor"] != "auto":
        sup_txt += f"!{cfg['overrides']['supervisor']}"
    bits = [f"DG {sup_txt}"]

    q = []
    if sup["fiveHour"]:
        q.append(f"5h {sup['fiveHour']['usedPercent']:.0f}%")
    if sup["sevenDay"]:
        q.append(f"7d {sup['sevenDay']['usedPercent']:.0f}%")
    if q:
        bits.append(" ".join(q))

    w = wrk["worker"]
    if cfg["overrides"]["worker"] != "auto":
        w += "!"
    if wrk.get("codexResetsAt"):
        w += " Codex>" + time.strftime("%H:%M", time.localtime(wrk["codexResetsAt"]))
    elif wrk["codexState"] not in (quota_codex.READY, "OVERRIDDEN"):
        w += f" ({wrk['codexState'].replace('CODEX_', '').lower()})"
    bits.append(w)

    jobs = " ".join(f"{n}{k[0]}" for k, n in sorted(counts.items()) if n)
    bits.append("jobs " + (jobs or "0"))
    print(" | ".join(bits))
    return 0


# ---------------------------------------------------------------- prompt

def prompt() -> int:
    import os as _os
    if _os.environ.get("DG_HOOK_TRACE"):
        import json as _j, pathlib as _pl, sys as _sys
        raw = _sys.stdin.read()
        _pl.Path(_os.environ["DG_HOOK_TRACE"]).write_text(raw[:4000], encoding="utf-8")
        _sys.stdin = __import__("io").StringIO(raw)
    """UserPromptSubmit: one compact line of Governor STATE.

    State only -- policy lives in the skill, so this costs a handful of tokens
    per prompt instead of a paragraph (spec 39/53). Silent when there is
    nothing worth saying.
    """
    _safe_stdout()
    _stdin_json()
    try:
        con, cfg = _load()
        sync.reconcile(con, cfg)
        sup = supervisor.evaluate(con, cfg)
        wrk = routing.select(con, cfg, refresh=False)
        c = scheduler.evaluate(con, cfg)["counts"]
    except Exception:
        return 0

    ready, running, blocked = c.get("READY", 0), c.get("RUNNING", 0), c.get("BLOCKED", 0)
    if not (ready or running or blocked) and sup["state"] == supervisor.NORMAL:
        return 0

    parts = [f"SUP={sup['state'].replace('CLAUDE_', '')}", f"WRK={wrk['worker']}"]
    if wrk.get("codexResetsAt"):
        parts.append("Codex exhausted until "
                     + time.strftime("%H:%M", time.localtime(wrk["codexResetsAt"])))
    parts.append(f"READY={ready}; RUNNING={running}; BLOCKED={blocked}")
    line = "Governor: " + "; ".join(parts) + "."
    if running and ready:
        line += " Workers are busy -- advance a READY task, do not wait on them."
    elif ready:
        line += " Delegate suitable work and keep advancing READY tasks."
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": line}}))
    return 0


# ---------------------------------------------------------------- stop failure

def stopfailure() -> int:
    """StopFailure(rate_limit): Anthropic refused the turn.

    Records the hard limit so the supervisor machine can move to LOCAL and
    later PROBE. It does not switch the backend -- routing is `dg launch`
    (spec 41).
    """
    payload = _stdin_json()
    if str(payload.get("error", "")) != "rate_limit":
        return 0
    try:
        con, _ = _load()
        supervisor.record_hard_limit(con, "rate_limit",
                                     str(payload.get("error_details", "")))
    except Exception:
        return 0
    print("Governor: Anthropic hard rate limit recorded; supervisor -> CLAUDE_LOCAL. "
          "Restart with `dg launch` to continue on LM Studio, or `dg probe claude` "
          "once the window resets.", file=sys.stderr)
    return 0


HOOKS = {"statusline": statusline, "prompt": prompt, "stopfailure": stopfailure}


def run(name: str) -> int:
    return HOOKS[name]()
