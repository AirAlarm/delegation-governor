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
    "schemaVersion": 1,
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
        "codex": {"maxWriteJobsPerRepo": 1},
        "cc-delegate": {"maxWriteJobsPerRepo": 1},
        "totalWriteJobsPerRepo": 2,
        "maxReadOnlyJobs": 3,
        # A worker past this is SLOW, not failed (§28). No hard kill by default.
        "slowAfterSeconds": 900,
        "hardTimeoutSeconds": 0,
        "minCheckSpacingSeconds": 60,
    },
    "lmstudio": {
        "baseUrl": "http://127.0.0.1:1234",
        # gpt-oss-20b, not the larger qwen: at 12GB it leaves room to load
        # 128k of context, where the 35B's weights alone (22GB) trip LM
        # Studio's memory guardrails on this box. Change both freely.
        "model": "openai/gpt-oss-20b",
        "smallModel": "openai/gpt-oss-20b",
        # Env var holding the LM Studio token, when the server requires one.
        # The value is never stored here.
        "tokenEnvVar": "LMSTUDIO_API_KEY",
        # Claude Code's system prompt plus tool definitions measured ~34k
        # tokens against this build, so a model loaded at LM Studio's default
        # context refuses the very first turn with exceed_context_size_error.
        # Load it big, or the LOCAL route is useless.
        "contextLength": 131072,
        "minContextLength": 40960,
        "loadTimeoutSeconds": 600,
        "ttlSeconds": 3600,
        # `lms load` is how a model gets a usable context. Set false if you
        # manage LM Studio residency yourself.
        "autoLoad": True,
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
    if CONFIG_PATH.exists():
        try:
            return _merge(DEFAULTS, json.loads(CONFIG_PATH.read_text("utf-8")))
        except (OSError, ValueError):
            pass
    return json.loads(json.dumps(DEFAULTS))


def ensure_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2), "utf-8")
    return HOME
