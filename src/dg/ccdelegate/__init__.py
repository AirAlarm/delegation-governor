"""The cc-delegate station patch, owned by the Governor.

cc-delegate is installed as a Claude Code plugin, so `claude plugin update`
replaces the whole install directory and silently drops any local edit. The
tuning that makes the station lane usable lives in those edits:

  * `lmstudio_gate.py` -- loads the right model at the right context length
    before a worker starts, and keeps it resident;
  * `main.py` / `worker_launcher.py` / `worker.py` -- thread `api_base` through
    so a worker can reach LM Studio at all, and pin `mcp<2` (2.x renamed
    FastMCP and breaks server startup outright);
  * the delegate skill -- station routing policy.

`station_patch.py` (written for this machine, vendored here) already applies all
of that idempotently and has a `--check` mode. The Governor does not reimplement
it: it ships it, re-applies it on `dg install`, and reports drift in
`dg doctor`. That is what stops a plugin update from quietly reverting the
context length to 32768 and breaking delegation again.

The gate module is read from *this* directory, so the version committed here is
the one that gets deployed -- the repo is the source of truth, not a file in
the user's home.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PATCH = HERE / "station_patch.py"
GATE = HERE / "station_lmstudio_gate.py"


def plugin_dir() -> Path | None:
    """Newest installed cc-delegate plugin directory, if any."""
    base = Path.home() / ".claude" / "plugins" / "cache"
    hits = sorted(base.glob("*/cc-delegate/*/.claude-plugin/plugin.json"))
    return hits[-1].parents[1] if hits else None


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PATCH), *args],
                          capture_output=True, text=True, timeout=timeout,
                          cwd=str(HERE))


def check() -> dict[str, Any]:
    """Is the installed plugin carrying our patch, and the gate we shipped?"""
    pdir = plugin_dir()
    if pdir is None:
        return {"ok": True, "state": "absent",
                "detail": "cc-delegate is not installed; nothing to patch"}
    if not PATCH.exists():
        return {"ok": False, "state": "missing-patch",
                "detail": f"vendored patch missing at {PATCH}"}
    try:
        p = _run(["--check", "--plugin-dir", str(pdir)])
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "state": "error", "detail": str(e)[:200]}

    installed_gate = pdir / "server" / "lmstudio_gate.py"
    drifted = (installed_gate.exists()
               and installed_gate.read_bytes() != GATE.read_bytes())
    if p.returncode == 0 and not drifted:
        return {"ok": True, "state": "patched", "pluginDir": str(pdir),
                "detail": f"station patch applied in {pdir.name}"}
    why = "gate differs from the version in this repo" if drifted else (
        (p.stdout or p.stderr).strip().splitlines()[-1:] or ["unpatched"])[0]
    return {"ok": False, "state": "drifted" if drifted else "unpatched",
            "pluginDir": str(pdir), "detail": f"{why}; run `dg ccdelegate --apply`"}


def apply() -> dict[str, Any]:
    """Re-apply the patch. Idempotent -- safe to run on every install."""
    pdir = plugin_dir()
    if pdir is None:
        return {"ok": True, "state": "absent", "detail": "cc-delegate not installed"}
    backup = None
    installed_gate = pdir / "server" / "lmstudio_gate.py"
    if installed_gate.exists():
        import time
        backup = installed_gate.with_suffix(f".py.dg-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(installed_gate, backup)
    try:
        p = _run(["--plugin-dir", str(pdir)], timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "state": "error", "detail": str(e)[:200]}
    out = (p.stdout or "") + (p.stderr or "")
    return {"ok": p.returncode == 0, "state": "applied" if p.returncode == 0 else "failed",
            "pluginDir": str(pdir), "backup": str(backup) if backup else None,
            "detail": out.strip()[-400:] or f"rc={p.returncode}",
            "note": "restart the Claude Code session -- the cc-delegate MCP server "
                    "imports the gate at startup and caches it"}


def gate_settings() -> dict[str, Any]:
    """The tuning values, read from the shipped gate rather than hardcoded."""
    out: dict[str, Any] = {}
    try:
        for line in GATE.read_text("utf-8").splitlines():
            for key in ("CONTEXT_LENGTH", "MODEL_TTL_S", "LOAD_TIMEOUT_S",
                        "UNLOAD_TIMEOUT_S", "PROBE_TIMEOUT_S"):
                if line.startswith(f"{key} ="):
                    out[key] = line.split("=", 1)[1].split("#")[0].strip()
    except OSError:
        pass
    return out
