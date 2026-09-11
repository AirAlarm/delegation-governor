"""`dg launch` -- a thin stock-Claude wrapper around the request router.

The child process keeps one stable local base URL for its entire lifetime. The
router chooses Anthropic, Qwen, or Oracle independently for every request, so a
quota transition never requires terminating or resuming Claude Code. The
legacy lifecycle helpers below remain for configuration probes and backwards
compatibility; :func:`run` is intentionally a single-launch boundary.
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


def probe_tier(t: dict, load: bool = False) -> tuple[bool, str]:
    """Is this tier usable right now? Zero inference, and by default zero side
    effects.

    LM Studio is checked with a plain GET to `/api/v0/models`, which also
    reports residency and loaded context. An earlier version POSTed an empty
    body to `/v1/messages` to draw an Anthropic-shaped error -- that worked,
    but it logged a red `invalid_request_error` in the user's LM Studio window
    on every probe, which is a poor trade for information a GET already gives.
    The POST check still exists in `dg doctor`, where it runs once and the
    question ("does this really speak the Messages API?") is the actual point.

    Remote tiers have no equivalent listing endpoint, so they keep the invalid
    POST -- it is cheap, and nobody is watching that box's console.

    `load=True` additionally makes an LM Studio tier's model resident. That is
    only wanted when actually starting a supervisor there: loading during a
    routine check pins a multi-GB model at supervisor context and makes
    cc-delegate's own model gate time out swapping it -- observed live as a
    failed delegated task.
    """
    if t.get("kind") == "lmstudio":
        state, loaded, _ = model_context(t)
        if state == "unknown":
            return False, f"{t['name']}: {t['baseUrl']} unreachable or model not served"
        if load:
            return ensure_local_model(t)
        need = t.get("minContextLength", 0)
        if state == "loaded" and loaded and loaded < need:
            return False, (f"{t['name']}: {t['model']} loaded with only {loaded} ctx "
                           f"(need {need}); dg launch will reload it")
        return True, (f"{t['name']}: {t['baseUrl']} reachable, {t['model']} {state}"
                      + ("" if state == "loaded" else " (loads on demand)"))

    if t.get("kind") == "openrouter":
        return _probe_openrouter(t)

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
    return True, f"{t['name']}: {t['baseUrl']} ready ({t['model']})"


def _probe_openrouter(t: dict) -> tuple[bool, str]:
    """Validate an OpenRouter key without spending an inference request."""
    import urllib.error
    import urllib.request
    token = tier_token(t)
    if not token:
        return False, f"{t['name']}: set {t.get('tokenEnvVar', 'OPENROUTER_API_KEY')}"
    url = t["baseUrl"].rstrip("/") + "/v1/key"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=t.get("probeTimeoutSeconds", 8)) as r:
            data = json.loads(r.read()).get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"{t['name']}: API key rejected ({e.code})"
        return False, f"{t['name']}: HTTP {e.code} checking API key"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"{t['name']}: {t['baseUrl']} unreachable ({type(e).__name__})"
    remaining = data.get("limit_remaining")
    if remaining is not None and float(remaining) <= 0:
        return False, f"{t['name']}: API key spending limit exhausted"
    suffix = f", ${float(remaining):.2f} key limit remaining" if remaining is not None else ""
    return True, f"{t['name']}: API key ready{suffix}"


def first_usable_tier(cfg: dict, load: bool = False) -> tuple[dict | None, list[str]]:
    """Walk tiers in order; the first that answers wins. Also returns why the
    earlier ones did not, so a failure explains itself.

    `load=True` is for `dg launch`, which genuinely needs the model resident.
    """
    why: list[str] = []
    for t in tiers(cfg):
        ok, detail = probe_tier(t, load=load)
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
    """Launch stock Claude through the authoritative request router.

    Routing no longer requires terminating and resuming Claude sessions: the
    proxy can switch upstreams safely at the request boundary.
    """
    claude = shutil.which("claude")
    if not claude:
        print("dg launch: claude not on PATH", file=sys.stderr)
        return 1
    session_id = os.environ.get("DG_SESSION_ID") or str(uuid.uuid4())
    from . import hooks, proxy
    port = cfg.get("proxy", {}).get("port", 8787)
    if not proxy.health(port):
        hooks._spawn_proxy(port)
        if not hooks._wait_for_proxy(proxy, port):
            print(f"dg launch: router did not start on 127.0.0.1:{port}", file=sys.stderr)
            return 2
    env = dict(os.environ)
    prefix = cfg.get("proxy", {}).get("clientPaths", {}).get("launch", "/client/launch")
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}{prefix}"
    env["DG_SESSION_ID"] = session_id
    argv = [claude, *[a for a in claude_args if a != "--"]]
    with store.session() as con:
        store.kv_set(con, "launcher", {"sessionId": session_id, "route": "proxy",
                                       "since": time.time(), "restarts": 0})
    print(f"dg launch: proxy=127.0.0.1:{port} session={session_id}", file=sys.stderr)
    if dry_run:
        print(" ".join(argv), file=sys.stderr)
        return 0
    return subprocess.call(argv, env=env)
