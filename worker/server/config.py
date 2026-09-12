"""Environment-driven configuration. Mirror of src/config.ts."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # Legacy env key; may be None now that profiles/credentials (config_store)
    # and OAuth providers exist. Key resolution happens per task.
    worker_api_key: str | None
    api_key_env_var: str
    model: str
    default_recursion_limit: int
    default_rubric_max_iterations: int
    default_max_budget_usd: float
    default_timeout_ms: int
    work_dir: str
    command_timeout_s: int
    stall_timeout_s: int


def load_config() -> Config:
    return Config(
        worker_api_key=os.environ.get("DELEGATE_API_KEY"),
        # Provider-specific env var litellm reads for DELEGATE_MODEL's provider
        # prefix (e.g. MINIMAX_API_KEY for "minimax/..."). Change alongside
        # DELEGATE_MODEL when switching providers.
        api_key_env_var=os.environ.get("DELEGATE_API_KEY_ENV_VAR", "MINIMAX_API_KEY"),
        model=os.environ.get("DELEGATE_MODEL", "litellm:minimax/MiniMax-M3"),
        # LangGraph step count — every model call and tool call individually.
        default_recursion_limit=int(os.environ.get("DELEGATE_RECURSION_LIMIT", "400")),
        default_rubric_max_iterations=int(os.environ.get("DELEGATE_RUBRIC_MAX_ITERATIONS", "6")),
        # Accepted for forward compatibility; not yet enforced mid-run.
        default_max_budget_usd=float(os.environ.get("DELEGATE_MAX_BUDGET_USD", "5")),
        default_timeout_ms=int(os.environ.get("DELEGATE_TIMEOUT_MS", "1800000")),
        work_dir=os.environ.get("DELEGATE_WORK_DIR", ".cc-delegate"),
        # Per-shell-command budget inside the worker. One stuck command
        # (e.g. a whole-drive find) must cost at most this, not the whole
        # task timeout.
        command_timeout_s=int(os.environ.get("DELEGATE_CMD_TIMEOUT_S", "120")),
        # A run goes completely silent (no PROGRESS/QUESTION line) whenever a
        # single model call hangs — e.g. RubricMiddleware's grading call after
        # the main loop finishes — since a stuck graph step yields no update.
        # That's indistinguishable from a crash except by elapsed silence, so
        # bound it far below the full run timeout instead of waiting it out.
        stall_timeout_s=int(os.environ.get("DELEGATE_STALL_TIMEOUT_S", "300")),
    )
