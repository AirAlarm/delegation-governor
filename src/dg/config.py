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
    "schemaVersion": 2,
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
            # A separate CPU VM: slow, but contends with nothing.
            "oracle": {"worker": "cc-delegate", "profile": "oracle-coder",
                       "maxWriteJobs": 2, "tier": "oracle"},
        },
        # Preferred lanes per task class, best first. A lane that is busy,
        # unavailable or excluded is skipped, so this is a preference, not a
        # pin. Reorder freely -- e.g. put codex first everywhere to keep local
        # models idle while the subscription lasts.
        "classRouting": {
            "hard": ["codex", "station"],
            "standard": ["codex", "station", "oracle"],
            "simple": ["station", "oracle", "codex"],
            "tiny": ["oracle", "station"],
        },
        "defaultClass": "standard",
        "totalWriteJobsPerRepo": 3,
        "maxReadOnlyJobs": 3,
        # How long a lane's availability probe is trusted, so dispatch does not
        # re-probe a slow remote every time.
        "laneProbeTtlSeconds": 60,
        # A worker past this is SLOW, not failed. No hard kill by default.
        "slowAfterSeconds": 900,
        "hardTimeoutSeconds": 0,
        "minCheckSpacingSeconds": 60,
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
            # gpt-oss-20b, not the larger qwen: at 12GB it leaves room to load
            # 128k of context, where the 35B's weights alone (22GB) trip LM
            # Studio's memory guardrails on this box.
            "model": "openai/gpt-oss-20b",
            "smallModel": "openai/gpt-oss-20b",
            # Env var holding the token, when the server requires one. The
            # value is never stored here.
            "tokenEnvVar": "LMSTUDIO_API_KEY",
            # Claude Code's system prompt plus tool definitions measured ~34k
            # tokens, so a model at LM Studio's default context refuses the
            # very first turn with exceed_context_size_error.
            "contextLength": 131072,
            "minContextLength": 40960,
            "loadTimeoutSeconds": 600,
            "ttlSeconds": 3600,
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
        _retire(stored)
        return json.loads(json.dumps(DEFAULTS))
    return _merge(DEFAULTS, stored)


def _retire(stored: dict) -> None:
    import time
    bak = CONFIG_PATH.with_suffix(f".v{stored.get('schemaVersion', 1)}."
                                  f"{time.strftime('%Y%m%d-%H%M%S')}.json")
    try:
        bak.write_text(json.dumps(stored, indent=2), "utf-8")
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), "utf-8")
    except OSError:
        pass


def ensure_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), "utf-8")
    return HOME
