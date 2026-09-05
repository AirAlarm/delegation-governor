"""Safe install / uninstall of the Claude Code integration.

Everything is additive and reversible:
  * ~/.claude/settings.json is backed up with a timestamp, then *merged* --
    existing hooks, permissions and plugins are preserved untouched;
  * an existing statusLine is stashed under `_dgPreviousStatusLine` and put
    back on uninstall;
  * cc-delegate is only ever read, never modified;
  * stock `claude` is never shadowed.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import config

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
BACKUPS = CLAUDE_DIR / "backups"
SKILL_DST = CLAUDE_DIR / "skills" / "delegation-governor"
# Shipped inside the package so it is found identically from a source checkout
# and from an installed tool (`uv tool install`), whose wheel has no repo tree.
SKILL_SRC = Path(__file__).resolve().parent / "skill"

MARK = "_delegationGovernor"

HOOKS_SPEC = {
    "UserPromptSubmit": {"matcher": None, "command": "dg hook prompt", "timeout": 10},
    "StopFailure": {"matcher": "rate_limit", "command": "dg hook stopfailure", "timeout": 10},
}
STATUSLINE = {"type": "command", "command": "dg hook statusline", "padding": 0}


def _load_settings() -> dict[str, Any]:
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text("utf-8"))
        except ValueError as e:
            raise RuntimeError(f"{SETTINGS} is not valid JSON ({e}); fix it before installing")
    return {}


def _backup() -> Path | None:
    if not SETTINGS.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dst = BACKUPS / f"settings.json.dg-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(SETTINGS, dst)
    return dst


def _is_ours(entry: dict) -> bool:
    return any(h.get("command", "").startswith("dg hook") for h in entry.get("hooks", []))


def plan(settings: dict[str, Any]) -> list[str]:
    """Human-readable diff of what install would change."""
    out: list[str] = []
    for event, spec in HOOKS_SPEC.items():
        existing = settings.get("hooks", {}).get(event, [])
        if any(_is_ours(e) for e in existing):
            out.append(f"hook {event}: already installed, no change")
        else:
            out.append(f"hook {event}: ADD `{spec['command']}`"
                       + (f" (matcher {spec['matcher']})" if spec["matcher"] else "")
                       + (f", preserving {len(existing)} existing entr"
                          f"{'y' if len(existing) == 1 else 'ies'}" if existing else ""))
    cur = settings.get("statusLine")
    if cur == STATUSLINE:
        out.append("statusLine: already installed, no change")
    elif cur:
        out.append(f"statusLine: REPLACE {cur.get('command', cur)!r} "
                   f"-> `{STATUSLINE['command']}` (previous stashed for uninstall)")
    else:
        out.append(f"statusLine: ADD `{STATUSLINE['command']}`")
    out.append(f"skill: COPY {SKILL_SRC} -> {SKILL_DST}")
    out.append(f"state: ENSURE {config.HOME} (config.json, governor.db, logs/)")
    out.append(f"backup: {SETTINGS} -> {BACKUPS}/settings.json.dg-<timestamp>")
    out.append("untouched: cc-delegate, plugins, permissions, model, "
               "existing hooks, OpenRouter (absent), Oracle profiles, `claude` itself")
    return out


def run(dry_run: bool = False) -> int:
    settings = _load_settings()
    for line in plan(settings):
        print(("[dry-run] " if dry_run else "") + line)
    if dry_run:
        return 0

    bk = _backup()
    if bk:
        print(f"backed up settings -> {bk}")

    hooks = settings.setdefault("hooks", {})
    for event, spec in HOOKS_SPEC.items():
        entries = hooks.setdefault(event, [])
        if any(_is_ours(e) for e in entries):
            continue
        entry: dict[str, Any] = {"hooks": [{"type": "command", "command": spec["command"],
                                            "timeout": spec["timeout"]}]}
        if spec["matcher"]:
            entry["matcher"] = spec["matcher"]
        entries.append(entry)

    cur = settings.get("statusLine")
    if cur and cur != STATUSLINE and MARK not in settings:
        settings[MARK] = {"previousStatusLine": cur}
    settings["statusLine"] = dict(STATUSLINE)
    settings.setdefault(MARK, {})["installedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(SETTINGS, json.dumps(settings, indent=2) + "\n")
    print(f"merged settings -> {SETTINGS}")

    if SKILL_SRC.exists():
        SKILL_DST.parent.mkdir(parents=True, exist_ok=True)
        if SKILL_DST.exists():
            shutil.rmtree(SKILL_DST)
        shutil.copytree(SKILL_SRC, SKILL_DST)
        print(f"installed skill -> {SKILL_DST}")
    else:
        print(f"warning: skill source missing at {SKILL_SRC}")

    config.ensure_home()
    print(f"state dir ready -> {config.HOME}")
    print("done. `dg doctor` to verify, `dg launch` for a managed session, "
          "plain `claude` still bypasses the Governor.")
    return 0


def uninstall(dry_run: bool = False, purge: bool = False) -> int:
    settings = _load_settings()
    actions: list[str] = []
    hooks = settings.get("hooks", {})
    for event in HOOKS_SPEC:
        keep = [e for e in hooks.get(event, []) if not _is_ours(e)]
        if len(keep) != len(hooks.get(event, [])):
            actions.append(f"hook {event}: remove dg entry, keep {len(keep)} other(s)")
        hooks[event] = keep
    prev = (settings.get(MARK) or {}).get("previousStatusLine")
    if settings.get("statusLine", {}).get("command", "").startswith("dg hook"):
        actions.append("statusLine: restore previous" if prev else "statusLine: remove")
    if SKILL_DST.exists():
        actions.append(f"skill: remove {SKILL_DST}")
    if purge:
        actions.append(f"state: DELETE {config.HOME} (ledger and logs are lost)")
    else:
        actions.append(f"state: keep {config.HOME} (use --purge to delete)")

    for a in actions:
        print(("[dry-run] " if dry_run else "") + a)
    if dry_run:
        return 0

    bk = _backup()
    if bk:
        print(f"backed up settings -> {bk}")
    if settings.get("statusLine", {}).get("command", "").startswith("dg hook"):
        if prev:
            settings["statusLine"] = prev
        else:
            settings.pop("statusLine", None)
    for event in list(hooks):
        if not hooks[event]:
            hooks.pop(event)
    if not hooks:
        settings.pop("hooks", None)
    settings.pop(MARK, None)
    _atomic_write(SETTINGS, json.dumps(settings, indent=2) + "\n")
    if SKILL_DST.exists():
        shutil.rmtree(SKILL_DST)
    if purge and config.HOME.exists():
        shutil.rmtree(config.HOME)
    print("uninstalled. cc-delegate and every other plugin are untouched.")
    return 0


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".dgtmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)
