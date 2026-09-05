"""`dg launch` -- lifecycle supervisor around stock Claude Code.

Why a supervisor and not a router
---------------------------------
Claude Code builds its Anthropic client from `process.env` (verified in 2.1.116:
`new PA({baseURL: env("ANTHROPIC_BASE_URL"), authToken: env("ANTHROPIC_AUTH_TOKEN")})`).
The backend is therefore fixed for the life of the process -- nothing can switch
it mid-session. Since both fallback backends already serve the Anthropic
Messages API natively, the only missing piece is *when* to start a process
against which base URL. That is a process lifecycle problem, so the answer is a
lifecycle supervisor, not a reverse proxy.

Tiers
-----
Anthropic first, then each entry of `supervisorFallbacks` in order until one
answers: the GPU box (fast, one model slot, only up when the PC is) and then
the always-on Oracle VM (slow CPU ARM, but independent of both).

The safe synchronization boundary
---------------------------------
We never signal, interrupt or kill Claude Code -- doing so could tear a session
in half mid-tool-call, mid-write, mid-git-operation. The boundary is Claude
Code's own exit: by then every tool call has finished and the transcript is
flushed. A hard rate limit does not kill the session either; the StopFailure
hook records the state and tells the user, and the switch happens on the next
natural exit. So a relaunch can never land in the middle of a side effect.

Session continuity
------------------
The first launch pins `--session-id <uuid>`, so we always know the id without
scraping anything. Every relaunch uses `--resume <uuid>`, which reloads the same
transcript. `--model` is passed explicitly on every launch, because a resumed
session otherwise restores the model it was saved with -- which would be an
Anthropic model name talking to a local endpoint, or the reverse.
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
LOCAL = "local"  # "some fallback tier"; the specific one is resolved at launch

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


# ---------------------------------------------------------------- tiers

def tiers(cfg: dict) -> list[dict]:
    """Ordered local supervisor tiers. Falls back to a legacy `lmstudio` key."""
    ts = cfg.get("supervisorFallbacks")
    if ts:
        return list(ts)
    lm = cfg.get("lmstudio")
    return [{**lm, "name": "lmstudio", "kind": "lmstudio"}] if lm else []


def tier(cfg: dict, name: str) -> dict | None:
    return next((t for t in tiers(cfg) if t.get("name") == name), None)


def tier_token(t: dict) -> str | None:
    """The tier's key, from the environment or an existing credential store.

    Never written to our config and never logged -- only the variable name and
    file path live here.
    """
    tok = os.environ.get(t.get("tokenEnvVar") or "")
    if tok:
        return tok
    path, key = t.get("tokenFile"), t.get("tokenFileKey")
    if not path or not key:
        return None
    try:
        return json.loads(Path(path).expanduser().read_text("utf-8")).get(key)
    except (OSError, ValueError):
        return None


def _auth_headers(t: dict) -> dict[str, str]:
    tok = tier_token(t)
    if not tok:
        return {}
    # Anthropic-style gateways want x-api-key; bearer is sent too so a plain
    # reverse proxy in front of one also authenticates.
    return {"x-api-key": tok, "Authorization": f"Bearer {tok}",
            "anthropic-version": "2023-06-01"}


def probe_tier(t: dict) -> tuple[bool, str]:
    """Is this tier usable right now? Zero inference.

    A deliberately invalid body draws an Anthropic-shaped `invalid_request_error`
    without starting a completion. LM Studio answers unknown paths with a
    generic 200, so the error *shape* is the discriminator, not the status code.
    """
    import urllib.error
    import urllib.request
    url = t["baseUrl"].rstrip("/") + "/v1/messages"
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"content-type": "application/json",
                                          **_auth_headers(t)})
    try:
        with urllib.request.urlopen(req, timeout=t.get("probeTimeoutSeconds", 8)) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, (f"{t['name']}: not authorised ({e.code}); "
                           f"set {t.get('tokenEnvVar')}")
        try:
            body = json.loads(e.read())
        except (ValueError, OSError):
            return False, f"{t['name']}: HTTP {e.code} with no JSON body"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{t['name']}: {t['baseUrl']} unreachable ({type(e).__name__})"
    if not (body.get("type") == "error" or "error" in body):
        return False, f"{t['name']}: not an Anthropic Messages endpoint ({str(body)[:100]})"
    if t.get("kind") == "lmstudio":
        return ensure_local_model(t)
    return True, f"{t['name']}: {t['baseUrl']} ready ({t['model']})"


def first_usable_tier(cfg: dict) -> tuple[dict | None, list[str]]:
    """Walk tiers in order; the first that answers wins. Also returns why the
    earlier ones did not, so a failure explains itself."""
    why: list[str] = []
    for t in tiers(cfg):
        ok, detail = probe_tier(t)
        if ok:
            return t, why
        why.append(detail)
    return None, why


# ---------------------------------------------------------------- lm studio

def model_context(t: dict) -> tuple[str, int | None, int | None]:
    """(state, loaded_context, max_context) for an LM Studio tier's model."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
                t["baseUrl"].rstrip("/") + "/api/v0/models", timeout=5) as r:
            for m in json.loads(r.read()).get("data", []):
                if m.get("id") == t["model"]:
                    return (m.get("state", "unknown"), m.get("loaded_context_length"),
                            m.get("max_context_length"))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return "unknown", None, None


def ensure_local_model(t: dict) -> tuple[bool, str]:
    """Guarantee an LM Studio tier's model is resident with enough context.

    Claude Code's system prompt plus tool definitions measured ~34k tokens, so
    a model at LM Studio's default context rejects the very first turn with
    `exceed_context_size_error`. Loading is part of switching to this tier,
    not something to hope the user did.
    """
    state, loaded, maximum = model_context(t)
    need = t["minContextLength"]
    if state == "loaded" and loaded and loaded >= need:
        return True, f"{t['name']}: {t['model']} loaded with {loaded} ctx"
    if not t.get("autoLoad", True):
        return False, (f"{t['name']}: {t['model']} is {state} with ctx {loaded}; "
                       f"need >= {need} and autoLoad is off")
    lms = shutil.which("lms")
    if not lms:
        return False, f"{t['name']}: `lms` not on PATH; load {t['model']} with -c {need}"
    want = min(t["contextLength"], maximum or t["contextLength"])
    cmd = [lms, "load", t["model"], "-c", str(want), "-y", "--ttl", str(t["ttlSeconds"])]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=t["loadTimeoutSeconds"])
    except subprocess.TimeoutExpired:
        return False, f"{t['name']}: loading {t['model']} timed out"
    except OSError as e:
        return False, f"{t['name']}: could not run lms load: {e}"
    if p.returncode != 0 or "Error" in (p.stdout + p.stderr):
        tail = (p.stdout + p.stderr).replace("\r", "\n").strip().splitlines()
        return False, f"{t['name']}: lms load failed: {tail[-1][:200] if tail else 'unknown'}"
    _, loaded, _ = model_context(t)
    if loaded and loaded < need:
        return False, f"{t['name']}: {t['model']} loaded but only {loaded} ctx (need {need})"
    return True, f"{t['name']}: {t['model']} loaded with {loaded or want} ctx"


# ---------------------------------------------------------------- back-compat

def probe_lmstudio(cfg: dict) -> tuple[bool, str]:
    t = tier(cfg, "lmstudio")
    return (False, "no lmstudio tier configured") if not t else probe_tier(t)


def probe_lmstudio_messages(cfg: dict) -> tuple[bool, str]:
    """Confirm the Anthropic Messages API is really served, at zero cost."""
    t = tier(cfg, "lmstudio")
    if not t:
        return False, "no lmstudio tier configured"
    import urllib.error
    import urllib.request
    url = t["baseUrl"].rstrip("/") + "/v1/messages"
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"content-type": "application/json",
                                          **_auth_headers(t)})
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
    return False, f"{url} did not answer in Anthropic error shape (got {str(body)[:120]})"


# ---------------------------------------------------------------- routing

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


def env_for(route: str, cfg: dict, base: dict[str, str] | None = None,
            chosen: dict | None = None) -> tuple[dict, str]:
    """(environment, model) for a Claude Code process on this route."""
    env = dict(base if base is not None else os.environ)
    for k in _ANTHROPIC_ENV:
        env.pop(k, None)
    if route == ANTHROPIC:
        return env, cfg.get("anthropicModel") or "sonnet"
    t = chosen or tier(cfg, route) or (tiers(cfg)[0] if tiers(cfg) else None)
    if t is None:
        raise RuntimeError("no supervisor fallback tier configured")
    env["ANTHROPIC_BASE_URL"] = t["baseUrl"]
    # Local endpoints ignore the token but the SDK insists on one.
    env["ANTHROPIC_AUTH_TOKEN"] = tier_token(t) or "dg-local"
    small = t.get("smallModel") or t["model"]
    env["ANTHROPIC_SMALL_FAST_MODEL"] = small
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = small
    # A local supervisor must not phone home for telemetry it cannot reach.
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env, t["model"]


def build_argv(claude: str, route: str, model: str, session_id: str, first: bool,
               extra: list[str]) -> list[str]:
    argv = [claude]
    argv += ["--session-id", session_id] if first else ["--resume", session_id]
    # Always explicit: a resumed session would otherwise restore the model it
    # was saved with, which is the wrong backend's model after a switch.
    argv += ["--model", model]
    return argv + [a for a in extra if a != "--"]


def local_contention(cfg: dict) -> str:
    """Warn when an LM Studio supervisor tier and cc-delegate share one slot.

    LM Studio keeps one model resident at a time on a single-GPU box. If the
    supervisor runs there and a cc-delegate station-* profile points at the
    same instance, the two evict each other: observed live as a cc-delegate
    model-gate timeout while the supervisor model was loading. Detection only
    -- serialising someone else's worker is not ours to do.
    """
    lm = tier(cfg, "lmstudio")
    if not lm:
        return ""
    try:
        cc = json.loads((Path.home() / ".cc-delegate" / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    host = lm["baseUrl"].rstrip("/")
    shared = sorted(name for name, p in (cc.get("profiles") or {}).items()
                    if str(p.get("api_base", "")).rstrip("/").startswith(host))
    if not shared:
        return ""
    remote = sorted(name for name, p in (cc.get("profiles") or {}).items()
                    if not str(p.get("api_base", "")).rstrip("/").startswith(host))
    return ("supervisor tier lmstudio and cc-delegate profiles " + ", ".join(shared)
            + f" share one LM Studio ({host}), which holds a single model at a time. "
            + ("Delegate to Codex or " + ", ".join(remote) + " while the supervisor is local."
               if remote else "Run them one at a time."))


def _no_tier_help(cfg: dict, why: list[str]) -> str:
    lines = ["dg launch: Anthropic is unavailable and no fallback tier answered."]
    lines += [f"  - {w}" for w in why]
    lines += ["  Fix one of:",
              "    start LM Studio and load the configured model",
              "    check the remote gateway and its API key",
              "    dg override supervisor claude   # force first-party Anthropic",
              "    dg probe claude                 # check whether Anthropic is back"]
    return "\n".join(lines)


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
        chosen = None
        if route == LOCAL:
            chosen, why = first_usable_tier(cfg)
            if chosen is None:
                # Clear recovery path beats a restart loop into a dead endpoint.
                print(_no_tier_help(cfg, why), file=sys.stderr)
                if not force:
                    return 2
                route, forced_anthropic = ANTHROPIC, True
                decision = {"route": ANTHROPIC, "reason": "forced past unusable fallbacks"}
            else:
                for skipped in why:
                    print(f"dg launch: skipping {skipped}", file=sys.stderr)
                if chosen.get("kind") == "lmstudio" and (warn := local_contention(cfg)):
                    print(f"dg launch: warning: {warn}", file=sys.stderr)

        env, model = env_for(route, cfg, chosen=chosen)
        argv = build_argv(claude, route, model, session_id, first, claude_args)
        label = ANTHROPIC if route == ANTHROPIC else (chosen or {}).get("name", route)
        store.kv_set(con, "launcher", {
            "sessionId": session_id, "route": route, "tier": label, "model": model,
            "since": time.time(), "restarts": restarts})
        print(f"dg launch: route={label} model={model} session={session_id} "
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
                # We were forced onto Anthropic past dead fallbacks and the
                # user has now quit; honour the quit rather than bouncing back.
                return rc
            # Never relaunch into a backend that is not answering: that is the
            # restart loop this design exists to avoid.
            probe, why = first_usable_tier(cfg)
            if probe is None:
                print(_no_tier_help(cfg, why), file=sys.stderr)
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
