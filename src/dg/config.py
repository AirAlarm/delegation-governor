"""Governor configuration: defaults merged with ~/.claude/delegation-governor/config.json."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("DG_HOME") or (Path.home() / ".claude" / "delegation-governor"))
CONFIG_PATH = HOME / "config.json"
DB_PATH = HOME / "governor.db"
LOG_DIR = HOME / "logs"

DEFAULTS: dict[str, Any] = {
    "schemaVersion": 7,
    # Supervisor thresholds, percent utilization of each Anthropic window (§3).
    "supervisor": {
        "fiveHour": {"save": 70, "local": 92},
        "sevenDay": {"save": 80, "local": 95},
        # Anthropic quota is only trustworthy while fresh; older than this we
        # keep the last state rather than pretending we know.
        "quotaStaleSeconds": 3600,
    },
    "codex": {
        "quotaTtlSeconds": 300,
        # A window counts as exhausted at/above this; the backend reports 100
        # but leaves a little slack in practice.
        "exhaustedPercent": 99,
        "networkCooldownSeconds": 900,
        "authCooldownSeconds": 3600,
        "probeTimeoutSeconds": 45,
        # limit_ids that gate delegation. Others (e.g. gpt-reserve) are shown
        # but never block, because Codex does not spend them for `codex exec`.
        "blockingLimitIds": ["codex"],
    },
    "workers": {
        # Lanes are keyed by the *resource* that is actually scarce, not by the
        # tool: station and oracle are separate machines, so they can run at
        # the same time. Modelling them as one "cc-delegate" worker gave them a
        # shared slot and kept the VM idle whenever the GPU was busy.
        "lanes": {
            "codex": {"worker": "codex", "maxWriteJobs": 1},
            # The GPU box holds one model at a time -- one job, and none at all
            # while the supervisor itself is running there.
            "station": {"worker": "cc-delegate", "profile": "station-main",
                        "maxWriteJobs": 1, "tier": "lmstudio"},
            # A separate CPU VM: slow, but doesn't contend with station/codex.
            # It DOES contend with itself, though -- verified live 2026-09-11
            # and again 2026-09-12: two oracle-coder jobs dispatched together
            # (both "tiny", single-string-rename-scale) started and ended at
            # the exact same second, one pair stalling out and the other
            # burning the full 30-minute run timeout with zero progress on
            # either. maxWriteJobs=2 assumed Oracle could usefully serve two
            # concurrent generations; the evidence says it can't -- 1 until
            # there's a reason to believe otherwise.
            "oracle": {"worker": "cc-delegate", "profile": "oracle-coder",
                       "maxWriteJobs": 1, "tier": "oracle"},
            # A metered cloud lane reached through an explicit cc-delegate
            # profile. It has its own slot and therefore never waits for
            # Codex, the local GPU, or Oracle.
            "openrouter": {"worker": "cc-delegate", "profile": "openrouter-coder",
                           "maxWriteJobs": 1, "endpoint": "openrouter"},
            # OpenCode Go: a second metered cloud lane, reached through its own
            # cc-delegate profile and endpoint so it never contends with
            # OpenRouter's slot or budget.
            "opencode": {"worker": "cc-delegate", "profile": "opencode-go",
                         "maxWriteJobs": 1, "endpoint": "opencode"},
        },
        # Preferred lanes per task class, best first. A lane that is busy,
        # unavailable or excluded is skipped, so this is a preference, not a
        # pin. Reorder freely -- e.g. put codex first everywhere to keep local
        # models idle while the subscription lasts.
        "classRouting": {
            # OpenRouter is off for now (user request): it runs on its free
            # tier here (no credits purchased), so its model is a *shared*
            # pool subject to other users' demand -- verified live, a
            # 15-req/min shared cap tripped mid-task, twice.
            #
            # Oracle is off entirely (user request, 2026-09-12): two
            # oracle-coder jobs dispatched concurrently -- which "tiny" led
            # with and maxWriteJobs=2 explicitly allowed -- reliably starved
            # each other into total failure rather than merely running
            # slower side by side (verified live twice: DG-18/19 stalled,
            # DG-42/43 both burned the full 30-min run timeout with zero
            # progress). Not worth the ongoing cost of finding out whether a
            # concurrency-1 fix actually holds.
            #
            # Both are left out of every list below rather than removed from
            # "lanes"/"endpoints", so either is a one-line re-add if there's
            # ever a reason to trust it again.
            #
            # hard/standard: codex leads (strongest model), opencode second.
            # simple/tiny: local-first is preserved on purpose (station) so
            # trivial work doesn't eat the Codex slot -- see
            # test_simple_prefers_the_gpu_box_over_codex.
            "hard": ["codex", "opencode", "station"],
            "standard": ["codex", "opencode", "station"],
            "simple": ["station", "opencode", "codex"],
            "tiny": ["station", "opencode"],
        },
        "defaultClass": "standard",
        "totalWriteJobsPerRepo": 4,
        "maxReadOnlyJobs": 4,
        # How long a lane's availability probe is trusted, so dispatch does not
        # re-probe a slow remote every time.
        "laneProbeTtlSeconds": 60,
        # A down verdict is trusted for much less time than an up one, so one
        # transient blip doesn't make a whole dispatch burst skip a lane that
        # has already recovered.
        "laneProbeDownTtlSeconds": 10,
        # A worker past this is SLOW, not failed. No hard kill by default.
        "slowAfterSeconds": 900,
        "hardTimeoutSeconds": 0,
        "minCheckSpacingSeconds": 60,
        # Worker-only endpoints are deliberately separate from
        # supervisorFallbacks: enabling OpenRouter must not silently move the
        # Claude supervisor onto metered API billing.
        "endpoints": {
            "openrouter": {
                "name": "openrouter",
                "kind": "openrouter",
                "baseUrl": "https://openrouter.ai/api",
                "tokenEnvVar": "OPENROUTER_API_KEY",
                "tokenFile": "~/.cc-delegate/credentials.json",
                "tokenFileKey": "OPENROUTER_API_KEY",
                "probeTimeoutSeconds": 10,
            },
            # OpenCode Go speaks the Anthropic Messages API natively at
            # <baseUrl>/v1/messages (verified live). It needs its own probe
            # ("kind": "opencode") rather than the generic invalid-POST check:
            # verified live, a workspace with no payment method on file gets
            # HTTP 401 + {"error":{"type":"CreditsError"}} for a *correctly
            # authenticated* key -- the generic probe treats any 401 as a bad
            # key, which would misdiagnose "add a card" as "fix your token".
            "opencode": {
                "name": "opencode",
                "kind": "opencode",
                "baseUrl": "https://opencode.ai/zen/go",
                # qwen3.7-plus: generous $12/5h-$30/wk-$60/mo tier (~21,600
                # req/mo). qwen3.8-max looked stronger but is the *tightest*
                # limit on the whole plan (~810 req/mo) -- a bad default for a
                # lane meant to absorb routine delegated work. kimi-k2.7-code
                # is explicitly coding-branded and equally generous, but
                # verified live to 500 on this Anthropic-compatible endpoint
                # (works on the OpenAI-compatible one instead) -- not used
                # here since the probe and this profile both need /v1/messages.
                "model": "qwen3.7-plus",
                "tokenEnvVar": "OPENCODE_GO_API_KEY",
                "tokenFile": "~/.cc-delegate/credentials.json",
                "tokenFileKey": "OPENCODE_GO_API_KEY",
                "probeTimeoutSeconds": 10,
            },
        },
    },
    # Ordered local supervisor tiers, tried in turn when Anthropic is out.
    # Tier 1 is the GPU box (fast, but one model slot and only up when the PC
    # is); tier 2 is the always-on Oracle VM (slow CPU ARM, but independent of
    # both the GPU slot and the PC being awake).
    "supervisorFallbacks": [
        {
            "name": "lmstudio",
            "kind": "lmstudio",
            "baseUrl": "http://127.0.0.1:1234",
            # qwen3.5-9b: 6.5GB, so it loads with a large context and matches
            # the model cc-delegate's station-main uses, which means the
            # supervisor and a station worker want the *same* resident model
            # rather than evicting each other.
            "model": "qwen/qwen3.5-9b",
            "smallModel": "qwen/qwen3.5-9b",
            # Env var holding the token, when the server requires one. The
            # value is never stored here.
            "tokenEnvVar": "LMSTUDIO_API_KEY",
            # Claude Code's system prompt plus tool definitions measured ~34k
            # tokens, so a model at LM Studio's default context refuses the
            # very first turn with exceed_context_size_error.
            # 65536 deliberately matches cc-delegate's gate CONTEXT_LENGTH.
            # They share one LM Studio slot, and the gate reloads any model
            # resident at a different context -- so a mismatch here makes the
            # supervisor and a station worker evict each other on every hop.
            "contextLength": 65536,
            "minContextLength": 40960,
            "loadTimeoutSeconds": 600,
            "ttlSeconds": 14400,
            "autoLoad": True,
        },
        {
            "name": "oracle",
            "kind": "remote",
            "baseUrl": "https://claude-llm.vibecodelabs.org",
            "model": "oracle-smart · gemma-4 26b",
            "smallModel": "oracle-fast · gemma-4 e2b",
            "tokenEnvVar": "ORACLE_LLM_API_KEY",
            # Fall back to the key cc-delegate already stores for this gateway
            # rather than asking for a second copy. Read on use, never cached.
            "tokenFile": "~/.cc-delegate/credentials.json",
            "tokenFileKey": "ORACLE_LLM_API_KEY",
            # A CPU-only ARM box: minutes per turn is normal, not a failure.
            "probeTimeoutSeconds": 20,
        },
    ],
    # The router proxy: the only way to fail over inside Claude Desktop, which
    # spawns its own claude.exe and so cannot be relaunched by `dg launch`.
    "proxy": {
        "port": 8787,
        # Start it on demand from the SessionStart hook, so a session never
        # finds a dead ANTHROPIC_BASE_URL.
        "autoStart": True,
        "tierCacheSeconds": 30,
        "networkCooldownSeconds": 30,
        "authCooldownSeconds": 300,
        "clientPaths": {"cli": "/client/cli", "desktop": "/client/desktop",
                        "launch": "/client/launch"},
    },
    "overrides": {"supervisor": "auto", "worker": "auto"},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    """Defaults with the user's config merged over them.

    A config written against an older schema is retired rather than merged:
    the v1 shape keyed worker capacity by tool, and silently merging those keys
    over the new lane model would cap concurrency at the old numbers. The old
    file is kept beside the new one so nothing is lost.
    """
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULTS))
    try:
        stored = json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))
    if int(stored.get("schemaVersion", 1)) < DEFAULTS["schemaVersion"]:
        stored = _migrate(stored)
    return _merge(DEFAULTS, stored)


def _migrate(stored: dict) -> dict:
    """Preserve user choices while translating known legacy capacity keys."""
    import time
    old = json.loads(json.dumps(stored))
    have = int(stored.get("schemaVersion", 1))
    if have < 2:
        workers = stored.setdefault("workers", {})
        lane_cfg = workers.setdefault("lanes", {})
        if "maxCodexWriteJobs" in workers:
            lane_cfg.setdefault("codex", {})["maxWriteJobs"] = workers["maxCodexWriteJobs"]
        if "maxCcDelegateWriteJobs" in workers:
            lane_cfg.setdefault("station", {})["maxWriteJobs"] = workers[
                "maxCcDelegateWriteJobs"]
    if have < 4:
        workers = stored.setdefault("workers", {})
        routing = workers.setdefault("classRouting", {})
        desired = DEFAULTS["workers"]["classRouting"]
        for task_class, defaults in desired.items():
            current = routing.get(task_class)
            if current is None:
                continue  # deep merge will supply the new default
            if "openrouter" in defaults and "openrouter" not in current:
                # Preserve the user's relative order and place the new lane at
                # the same preference point used by a fresh v4 config.
                # (v6 later drops openrouter from DEFAULTS entirely and
                # overwrites classRouting outright, making this insertion
                # moot when migrating all the way from v3 -- the `in defaults`
                # guard just keeps this step itself from crashing.)
                position = defaults.index("openrouter")
                current.insert(min(position, len(current)), "openrouter")
        if workers.get("totalWriteJobsPerRepo") == 3:
            workers["totalWriteJobsPerRepo"] = 4
        if workers.get("maxReadOnlyJobs") == 3:
            workers["maxReadOnlyJobs"] = 4
    if have < 5:
        workers = stored.setdefault("workers", {})
        routing = workers.setdefault("classRouting", {})
        desired = DEFAULTS["workers"]["classRouting"]
        for task_class, defaults in desired.items():
            current = routing.get(task_class)
            if current is None:
                continue  # deep merge will supply the new default
            if "opencode" not in current:
                position = defaults.index("opencode")
                current.insert(min(position, len(current)), "opencode")
    if have < 6:
        # Not an additive tweak like the previous two migrations: OpenRouter
        # is being dropped from every class (unreliable free tier) and the
        # preference order actually changed (codex now leads hard/standard).
        # A user's own custom classRouting can't be meaningfully reconciled
        # against that, so this replaces it outright rather than patching it.
        workers = stored.setdefault("workers", {})
        workers["classRouting"] = json.loads(json.dumps(DEFAULTS["workers"]["classRouting"]))
    if have < 7:
        # Two concurrent oracle-coder jobs verified live to starve each
        # other into total failure (both hit the full 30-min run timeout,
        # zero progress) rather than merely running slower side by side --
        # and then Oracle was dropped from routing entirely (user request).
        # Only touch maxWriteJobs if it's still at the old default -- an
        # explicit user override stays theirs. classRouting is replaced
        # outright again, same reasoning as the v6 migration: not an
        # additive tweak, a lane is being removed from every class.
        oracle = stored.get("workers", {}).get("lanes", {}).get("oracle")
        if isinstance(oracle, dict) and oracle.get("maxWriteJobs") == 2:
            oracle["maxWriteJobs"] = 1
        workers = stored.setdefault("workers", {})
        workers["classRouting"] = json.loads(json.dumps(DEFAULTS["workers"]["classRouting"]))
    stored["schemaVersion"] = DEFAULTS["schemaVersion"]
    bak = CONFIG_PATH.with_suffix(f".v{have}."
                                  f"{time.strftime('%Y%m%d-%H%M%S')}.json")
    try:
        bak.write_text(json.dumps(old, indent=2), "utf-8")
        CONFIG_PATH.write_text(json.dumps(stored, indent=2), "utf-8")
    except OSError:
        pass
    return stored


def ensure_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), "utf-8")
    return HOME
