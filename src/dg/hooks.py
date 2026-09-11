"""Claude Code integration points, all reachable as `dg hook <name>`.

One executable on PATH instead of three scripts that would each need PYTHONPATH
wiring in settings.json.

None of these may ever raise: a broken hook must not break a session, and a
broken statusline must not blank the status bar.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
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

_DELEGATE_MIN_LINES = 350
_DELEGATE_MIN_BYTES = 100_000
_DELEGATE_MIN_TOKENS = 25_000
_BASH_READERS = {"cat", "head", "tail", "less", "more"}


def _permission(decision: str, reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))
    return 0


def _threshold(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def _large_file_reason(path: Path) -> str | None:
    min_lines = _threshold("DG_DELEGATE_MIN_LINES", _DELEGATE_MIN_LINES)
    min_bytes = _threshold("DG_DELEGATE_MIN_BYTES", _DELEGATE_MIN_BYTES)
    min_tokens = _threshold("DG_DELEGATE_MIN_TOKENS", _DELEGATE_MIN_TOKENS)

    # Opening before deciding is important: a file that exists but cannot be
    # read must pass through so the real tool can report its own error.
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        tokens = (size + 3) // 4
        exceeded = []
        if size > min_bytes:
            exceeded.append(f"{size} bytes > {min_bytes}")
        if tokens > min_tokens:
            exceeded.append(f"~{tokens} tokens > {min_tokens}")
        if exceeded:
            return ", ".join(exceeded)

        newlines = 0
        has_data = False
        last = b""
        while chunk := stream.read(64 * 1024):
            has_data = True
            last = chunk[-1:]
            newlines += chunk.count(b"\n")
            if newlines > min_lines:
                return f"more than {min_lines} lines"
        lines = newlines + int(has_data and last != b"\n")
        if lines > min_lines:
            return f"{lines} lines > {min_lines}"
    return None


def _resolve_file(value: Any, cwd: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        base = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
        path = base / path
    path = path.resolve()
    return path if path.is_file() else None


def _bash_files(command: Any, cwd: Any) -> list[Path]:
    """Return confidently parsed operands of one plain file-reader command.

    This intentionally is not a shell parser. Anything with shell control
    syntax or options is left alone rather than risking a false denial.
    """
    if not isinstance(command, str) or not command or "\n" in command or "`" in command:
        return []
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    if not tokens or any(any(mark in token for mark in "|&;<>()") for token in tokens):
        return []
    if tokens[0] not in _BASH_READERS or len(tokens) < 2:
        return []
    if any(token.startswith("-") for token in tokens[1:]):
        return []

    paths = [_resolve_file(token, cwd) for token in tokens[1:]]
    # Missing files, globs, and non-file operands are intentionally ambiguous.
    return [path for path in paths if path is not None] if all(paths) else []


def _pretooluse_decision(payload: dict) -> tuple[str, str]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return "allow", "Delegation gate does not apply."

    tool = payload.get("tool_name")
    if tool == "Read":
        if (tool_input.get("offset") is not None
                or tool_input.get("limit") is not None):
            return "allow", "Targeted Read calls stay with the current worker."
        path = _resolve_file(tool_input.get("file_path"), payload.get("cwd"))
        paths = [path] if path is not None else []
    elif tool == "Bash":
        paths = _bash_files(tool_input.get("command"), payload.get("cwd"))
    else:
        paths = []

    for path in paths:
        exceeded = _large_file_reason(path)
        if exceeded:
            return ("deny", f"{path} exceeds the delegation threshold ({exceeded}). "
                    "Use the bulk-read alternative to delegate the full read; use "
                    "offset/limit when exact content from one section is needed.")
    return "allow", "File read is below the delegation thresholds or is not a full read."


def pretooluse(payload: dict | None = None) -> int:
    """PreToolUse(Read|Bash): hard-route confident full reads of large files."""
    _safe_stdout()
    try:
        decision, reason = _pretooluse_decision(payload if payload is not None else _stdin_json())
    except Exception:
        decision, reason = "allow", "Delegation gate failed open."
    try:
        return _permission(decision, reason)
    except Exception:
        return 0

def prompt() -> int:
    """UserPromptSubmit: one compact line of Governor STATE.

    State only -- policy lives in the skill, so this costs a handful of tokens
    per prompt instead of a paragraph (spec 39/53). Silent when there is
    nothing worth saying.
    """
    _safe_stdout()
    payload = _stdin_json()
    # The installed command is shared; the event name keeps the two paths
    # separate before UserPromptSubmit does any state work.
    if isinstance(payload, dict) and payload.get("hook_event_name") == "PreToolUse":
        return pretooluse(payload)
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

    Records the hard limit so the request router moves to LOCAL and later
    PROBE. The failed request is already over; the next one can safely use a
    fallback without restarting Claude.
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
          "The next request will try local Qwen, then Oracle; `dg proxy --status` "
          "shows the route.", file=sys.stderr)
    return 0


def session() -> int:
    """SessionStart: guarantee the router is up before the first request.

    While the redirect is armed, a session that cannot reach the proxy cannot
    reach *any* backend, so this must not merely fire-and-forget:

      * it waits until the port actually accepts connections, closing the race
        between spawning the proxy and the session's first API call;
      * if the proxy cannot be started at all, it removes the redirect so the
        next session talks to Anthropic directly instead of inheriting a broken
        setup. That matters most when nobody is at the machine to fix it.
    """
    _stdin_json()
    try:
        con, cfg = _load()
        if not cfg["proxy"].get("autoStart", True):
            return 0
        from . import proxy
        port = cfg["proxy"]["port"]
        if proxy.health(port, timeout=1.5):
            return 0
        _spawn_proxy(port)
        if _wait_for_proxy(proxy, port):
            return 0
        _disarm(port)
    except Exception:
        return 0
    return 0


def _wait_for_proxy(proxy, port: int, deadline: float = 12.0) -> bool:
    import time as _t
    end = _t.monotonic() + deadline
    while _t.monotonic() < end:
        if proxy.health(port, timeout=1.0):
            return True
        _t.sleep(0.4)
    return False


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _disarm(port: int, path: Path | None = None) -> None:
    """Unpoint Claude Code from a router that will not start.

    Only settings.json is touched: it is the file this process can safely
    rewrite, and it is what a fresh session reads.
    """
    p = path or settings_path()
    try:
        s = json.loads(p.read_text("utf-8"))
        env = s.get("env") or {}
        if f":{port}" not in str(env.get("ANTHROPIC_BASE_URL", "")):
            return
        env.pop("ANTHROPIC_BASE_URL", None)
        if not env:
            s.pop("env", None)
        tmp = p.with_suffix(".dgtmp")
        tmp.write_text(json.dumps(s, indent=2) + chr(10), "utf-8")
        tmp.replace(p)
    except OSError:
        return
    print("Governor: the router proxy would not start, so the ANTHROPIC_BASE_URL "
          "redirect was removed -- new sessions go straight to Anthropic. "
          "Run `dg doctor`, then `dg install --proxy` to re-arm it.",
          file=sys.stderr)


def _spawn_proxy(port: int) -> None:
    """Detached, so it outlives the session that started it."""
    import os
    import subprocess
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: the latter gives a console
        # app its own console, which pops an empty terminal window on screen.
        kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    else:
        kw["start_new_session"] = True
    # The proxy must not inherit a base-URL override pointing at itself.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
    subprocess.Popen([sys.executable, "-m", "dg.cli", "proxy", "--port", str(port)],
                     env=env, **kw)


HOOKS = {"statusline": statusline, "prompt": prompt, "pretooluse": pretooluse,
         "stopfailure": stopfailure, "session": session}


def run(name: str) -> int:
    return HOOKS[name]()
