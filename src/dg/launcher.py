"""`dg launch` -- lifecycle supervisor around stock Claude Code.

Why a supervisor and not a router
---------------------------------
Claude Code builds its Anthropic client from `process.env` (verified in 2.1.116:
`new PA({baseURL: env("ANTHROPIC_BASE_URL"), authToken: env("ANTHROPIC_AUTH_TOKEN")})`).
The backend is therefore fixed for the life of the process -- nothing can switch
it mid-session. Since LM Studio already serves the Anthropic Messages API
natively at `/v1/messages`, the only missing piece is *when* to start a process
against which base URL. That is a process lifecycle problem, so the answer is a
lifecycle supervisor, not a reverse proxy: no extra hop, no translation layer,
no router to keep compatible with two moving protocols.

The safe synchronization boundary
---------------------------------
We never signal, interrupt or kill Claude Code -- doing so could tear a session
in half mid-tool-call, mid-write, mid-git-operation. The boundary is Claude
Code's own exit: by then every tool call has finished and the transcript is
flushed. A hard rate limit does not kill the session either; the StopFailure
hook records LOCAL and tells the user, and the switch happens on the next
natural exit. So a relaunch can never land in the middle of a side effect.

Session continuity
------------------
The first launch pins `--session-id <uuid>`, so we always know the id without
scraping anything. Every relaunch uses `--resume <uuid>`, which reloads the same
transcript. `--model` is passed explicitly on every launch, because a resumed
session otherwise restores the model it was saved with -- which would be an
Anthropic model name talking to LM Studio, or the reverse.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import store, supervisor

ANTHROPIC = "anthropic"
LOCAL = "local"

# Restart-loop protection: a relaunch that dies faster than this counts as a
# strike; enough strikes in a row and we stop rather than spin.
MIN_HEALTHY_UPTIME = 20.0
MAX_STRIKES = 3
BACKOFF = (2.0, 8.0, 20.0)

_ANTHROPIC_ENV = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
)


def decide_route(con, cfg: dict, probe: bool = True) -> dict[str, Any]:
    """Which backend should the next Claude Code process talk to?"""
    override = cfg["overrides"]["supervisor"]
    if override == "local":
        return {"route": LOCAL, "reason": "override supervisor=local"}
    if override == "claude":
        return {"route": ANTHROPIC, "reason": "override supervisor=claude"}

    sup = supervisor.evaluate(con, cfg)
    state = sup["state"]
    if state == supervisor.PROBE:
        # The reset time passed. A timestamp is not recovery -- confirm it.
        if not probe:
            return {"route": LOCAL, "reason": "probe pending"}
        res = supervisor.probe_anthropic(cfg)
        if res["ok"]:
            supervisor.clear_hard_limit(con)
            return {"route": ANTHROPIC, "reason": "anthropic probe succeeded"}
        return {"route": LOCAL, "reason": f"anthropic still limited ({res['reason']})"}
    if state == supervisor.LOCAL:
        return {"route": LOCAL, "reason": sup["reason"]}
    return {"route": ANTHROPIC, "reason": sup["reason"]}


def env_for(route: str, cfg: dict, base: dict[str, str] | None = None) -> tuple[dict, str]:
    """(environment, model) for a Claude Code process on this route."""
    env = dict(base if base is not None else os.environ)
    for k in _ANTHROPIC_ENV:
        env.pop(k, None)
    if route == ANTHROPIC:
        return env, cfg.get("anthropicModel") or "sonnet"
    lm = cfg["lmstudio"]
    env["ANTHROPIC_BASE_URL"] = lm["baseUrl"]
    # LM Studio ignores the token on localhost but the SDK insists on one.
    env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get(lm["tokenEnvVar"]) or "lm-studio"
    env["ANTHROPIC_SMALL_FAST_MODEL"] = lm["smallModel"]
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = lm["smallModel"]
    # A local supervisor must not phone home for telemetry it cannot reach.
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env, lm["model"]


def probe_lmstudio(cfg: dict) -> tuple[bool, str]:
    """Is the local supervisor actually usable, and is its model loaded?"""
    import urllib.error
    import urllib.request
    lm = cfg["lmstudio"]
    req = urllib.request.Request(lm["baseUrl"].rstrip("/") + "/v1/models")
    tok = os.environ.get(lm["tokenEnvVar"])
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            ids = [m.get("id") for m in json.loads(r.read()).get("data", [])]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{lm['baseUrl']} unreachable ({type(e).__name__})"
    if lm["model"] not in ids:
        return False, f"model {lm['model']} not served; available: {', '.join(map(str, ids))}"
    return True, f"{lm['baseUrl']} serving {lm['model']}"


def model_context(cfg: dict) -> tuple[str, int | None, int | None]:
    """(state, loaded_context, max_context) for the supervisor model.

    Read from LM Studio's own REST API, which reports residency and the
    context the model was actually loaded with.
    """
    import urllib.error
    import urllib.request
    lm = cfg["lmstudio"]
    try:
        with urllib.request.urlopen(
                lm["baseUrl"].rstrip("/") + "/api/v0/models", timeout=5) as r:
            for m in json.loads(r.read()).get("data", []):
                if m.get("id") == lm["model"]:
                    return (m.get("state", "unknown"), m.get("loaded_context_length"),
                            m.get("max_context_length"))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return "unknown", None, None


def ensure_local_model(cfg: dict) -> tuple[bool, str]:
    """Guarantee the supervisor model is resident with enough context.

    Claude Code's system prompt plus tool definitions measured ~34k tokens, so
    a model at LM Studio's default context rejects the very first turn with
    `exceed_context_size_error`. Loading is therefore part of switching to
    LOCAL, not something to hope the user did.
    """
    lm = cfg["lmstudio"]
    state, loaded, maximum = model_context(cfg)
    need = lm["minContextLength"]
    if state == "loaded" and loaded and loaded >= need:
        return True, f"{lm['model']} loaded with {loaded} ctx"
    if not lm.get("autoLoad", True):
        return False, (f"{lm['model']} is {state} with ctx {loaded}; need >= {need} "
                       f"and lmstudio.autoLoad is off")

    lms = shutil.which("lms")
    if not lms:
        return False, f"`lms` not on PATH; load {lm['model']} manually with -c {need}"
    want = min(lm["contextLength"], maximum or lm["contextLength"])
    cmd = [lms, "load", lm["model"], "-c", str(want), "-y", "--ttl", str(lm["ttlSeconds"])]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=lm["loadTimeoutSeconds"])
    except subprocess.TimeoutExpired:
        return False, f"loading {lm['model']} timed out after {lm['loadTimeoutSeconds']}s"
    except OSError as e:
        return False, f"could not run lms load: {e}"
    if p.returncode != 0 or "Error" in (p.stdout + p.stderr):
        tail = (p.stdout + p.stderr).replace("\r", "\n").strip().splitlines()
        return False, f"lms load failed: {tail[-1][:200] if tail else 'unknown error'}"
    state, loaded, _ = model_context(cfg)
    if loaded and loaded < need:
        return False, f"{lm['model']} loaded but only {loaded} ctx; need >= {need}"
    return True, f"{lm['model']} loaded with {loaded or want} ctx"


def probe_lmstudio_messages(cfg: dict) -> tuple[bool, str]:
    """Confirm the Anthropic Messages API is really served, at zero inference cost.

    This is the load-bearing fact of the whole architecture, so `dg doctor`
    verifies it rather than assuming it. A deliberately invalid body draws an
    Anthropic-shaped `invalid_request_error` without starting a completion --
    note LM Studio answers unknown paths with a generic 200, so the error
    *shape* is the discriminator, not the status code.
    """
    import urllib.error
    import urllib.request
    lm = cfg["lmstudio"]
    url = lm["baseUrl"].rstrip("/") + "/v1/messages"
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"content-type": "application/json"})
    tok = os.environ.get(lm["tokenEnvVar"])
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except (ValueError, OSError):
            return False, f"{url} returned HTTP {e.code} with no JSON body"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{url} unreachable ({type(e).__name__})"
    if body.get("type") == "error" and isinstance(body.get("error"), dict):
        return True, f"{url} speaks the Anthropic Messages API"
    return False, (f"{url} did not answer in Anthropic error shape "
                   f"(got {str(body)[:120]}); this build may not support it")


def build_argv(claude: str, route: str, model: str, session_id: str, first: bool,
               extra: list[str]) -> list[str]:
    argv = [claude]
    argv += ["--session-id", session_id] if first else ["--resume", session_id]
    # Always explicit: a resumed session would otherwise restore the model it
    # was saved with, which is the wrong backend's model after a switch.
    argv += ["--model", model]
    return argv + [a for a in extra if a != "--"]


def local_contention(cfg: dict) -> str:
    """Warn when the LOCAL supervisor and cc-delegate share one model slot.

    LM Studio keeps one model resident at a time on a single-GPU box. If the
    supervisor is running there and a cc-delegate station-* profile points at
    the same instance, the two evict each other: observed live as a
    cc-delegate model-gate timeout while the supervisor model was loading.
    Detection only -- serialising someone else's worker is not ours to do.
    """
    try:
        cc = json.loads((Path.home() / ".cc-delegate" / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    host = cfg["lmstudio"]["baseUrl"].rstrip("/")
    shared = sorted(name for name, p in (cc.get("profiles") or {}).items()
                    if str(p.get("api_base", "")).rstrip("/").startswith(host))
    if not shared:
        return ""
    return ("LOCAL supervisor and cc-delegate profiles " + ", ".join(shared)
            + f" share one LM Studio ({host}), which holds a single model at a time. "
              "Run them one at a time, or delegate to Codex / an oracle-* profile "
              "while the supervisor is LOCAL.")


def _dead_local_help(cfg: dict, detail: str) -> str:
    return (f"dg launch: the supervisor wants LOCAL but the local backend is not usable.\n"
            f"  {detail}\n"
            f"  Fix one of:\n"
            f"    start LM Studio and load {cfg['lmstudio']['model']}\n"
            f"    dg override supervisor claude   # force first-party Anthropic\n"
            f"    dg probe claude                 # check whether Anthropic is back")


def run(cfg: dict, claude_args: list[str], dry_run: bool = False,
        force: bool = False, max_restarts: int = 20) -> int:
    con = store.connect()
    claude = shutil.which("claude")
    if not claude:
        print("dg launch: claude not on PATH", file=sys.stderr)
        return 1

    session_id = os.environ.get("DG_SESSION_ID") or str(uuid.uuid4())
    decision = decide_route(con, cfg)
    route = decision["route"]
    first, strikes, restarts = True, 0, 0

    forced_anthropic = False

    while True:
        if route == LOCAL:
            ok, detail = probe_lmstudio(cfg)
            if ok:
                ok, detail = ensure_local_model(cfg)
            if not ok:
                # Clear recovery path beats a restart loop into a dead endpoint.
                print(_dead_local_help(cfg, detail), file=sys.stderr)
                if not force:
                    return 2
                route = ANTHROPIC
                forced_anthropic = True
                decision = {"route": ANTHROPIC, "reason": "forced past unusable LM Studio"}

        if route == LOCAL and (warn := local_contention(cfg)):
            print(f"dg launch: warning: {warn}", file=sys.stderr)

        env, model = env_for(route, cfg)
        argv = build_argv(claude, route, model, session_id, first, claude_args)
        store.kv_set(con, "launcher", {
            "sessionId": session_id, "route": route, "model": model,
            "since": time.time(), "restarts": restarts})
        print(f"dg launch: route={route} model={model} session={session_id} "
              f"({decision['reason']})", file=sys.stderr)
        if dry_run:
            print(" ".join(argv), file=sys.stderr)
            return 0

        started = time.time()
        rc = subprocess.call(argv, env=env)
        uptime = time.time() - started
        first = False

        # Claude Code has exited: every tool call is finished and the
        # transcript is flushed. This is the only point we ever switch at.
        after = decide_route(con, cfg)
        if after["route"] == route:
            return rc  # the user quit; nothing to switch to
        if after["route"] == LOCAL:
            if forced_anthropic:
                # We were forced onto Anthropic past a dead LM Studio and the
                # user has now quit; honour the quit rather than bouncing back.
                return rc
            # Never relaunch into a backend that is not answering: that is the
            # restart loop this design exists to avoid.
            ok, detail = probe_lmstudio(cfg)
            if not ok:
                print(_dead_local_help(cfg, detail), file=sys.stderr)
                return rc or 2

        strikes = strikes + 1 if uptime < MIN_HEALTHY_UPTIME else 0
        if strikes >= MAX_STRIKES:
            print(f"dg launch: {strikes} sessions exited within {MIN_HEALTHY_UPTIME:.0f}s while "
                  f"switching {route} -> {after['route']}; stopping instead of restart-looping.\n"
                  f"  Run `dg doctor` and `dg status`, then relaunch when the cause is fixed.",
                  file=sys.stderr)
            return rc or 3
        restarts += 1
        if restarts > max_restarts:
            print(f"dg launch: restart budget ({max_restarts}) exhausted; stopping.",
                  file=sys.stderr)
            return rc or 3

        delay = BACKOFF[min(strikes, len(BACKOFF) - 1)] if strikes else 0.0
        print(f"dg launch: supervisor moved {route} -> {after['route']} "
              f"({after['reason']}); resuming session {session_id}"
              + (f" after {delay:.0f}s" if delay else ""), file=sys.stderr)
        if delay:
            time.sleep(delay)
        route, decision = after["route"], after
