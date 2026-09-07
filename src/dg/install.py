"""Safe install / uninstall of the Claude Code integration.

Everything is additive and reversible:
  * ~/.claude/settings.json is backed up with a timestamp, then *merged* --
    existing hooks, permissions and plugins are preserved untouched;
  * an existing statusLine is stashed under `_dgPreviousStatusLine` and put
    back on uninstall;
  * stock `claude` is never shadowed.

cc-delegate is the exception, deliberately: the Governor owns its station patch
(see `dg.ccdelegate`). A plugin update replaces the install directory and drops
the tuning that makes the station lane work at all, so `dg install` re-applies
it and backs up what it replaces.
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
    # Only meaningful when the proxy is wired in, so it is added and removed
    # with it -- a hook that starts a proxy nothing routes through is noise.
    "SessionStart": {"matcher": None, "command": "dg hook session", "timeout": 15,
                     "proxyOnly": True},
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


def proxy_env_state(port: int) -> tuple[str | None, bool]:
    """(current user-level ANTHROPIC_BASE_URL, whether it points at our proxy)."""
    import subprocess
    try:
        out = subprocess.run(
            ["reg", "query", r"HKCU\Environment", "/v", "ANTHROPIC_BASE_URL"],
            capture_output=True, text=True)
    except OSError:
        return None, False
    if out.returncode != 0:
        return None, False
    parts = out.stdout.split("REG_SZ")
    cur = parts[-1].strip() if len(parts) > 1 else None
    return cur, bool(cur and f":{port}" in cur)


def settings_env_state(settings: dict, port: int) -> tuple[str | None, bool]:
    cur = (settings.get("env") or {}).get("ANTHROPIC_BASE_URL")
    return cur, bool(cur and f":{port}" in cur)


def set_settings_env(settings: dict, port: int) -> str:
    """Route via `settings.json` -> `env`, the documented mechanism.

    Claude Code applies this with `Object.assign(process.env, ...)`, and it is
    file-based, so unlike a shell export it reaches GUI-launched apps that
    inherit nothing from a login shell. Verified: with no ANTHROPIC_BASE_URL in
    the environment, a request goes to the proxy.

    An explicit environment variable still wins over this, which is why the
    user-level variable is set too -- belt and braces for terminals.
    """
    url = f"http://127.0.0.1:{port}"
    settings.setdefault("env", {})["ANTHROPIC_BASE_URL"] = url
    return url


def clear_settings_env(settings: dict) -> None:
    env = settings.get("env") or {}
    env.pop("ANTHROPIC_BASE_URL", None)
    if not env:
        settings.pop("env", None)


def set_proxy_env(port: int) -> str:
    """Persist ANTHROPIC_BASE_URL so new terminal sessions reach the router.

    This was intended to cover Claude Desktop too, but measurement says it
    cannot: Desktop sets ANTHROPIC_BASE_URL=https://api.anthropic.com for its
    own sessions, which wins over the user-level variable, and a settings.json
    `env` block does not override it either (tested -- the proxy saw no
    traffic). Terminal sessions started after this do route through the proxy.
    """
    import subprocess
    url = f"http://127.0.0.1:{port}"
    subprocess.run(["setx", "ANTHROPIC_BASE_URL", url], capture_output=True, text=True)
    return url


def clear_proxy_env() -> None:
    import subprocess
    subprocess.run(["reg", "delete", r"HKCU\Environment", "/v", "ANTHROPIC_BASE_URL", "/f"],
                   capture_output=True, text=True)


def plan(settings: dict[str, Any], proxy: bool = False) -> list[str]:
    """Human-readable diff of what install would change."""
    out: list[str] = []
    port = config.load()["proxy"]["port"]
    for event, spec in HOOKS_SPEC.items():
        existing = settings.get("hooks", {}).get(event, [])
        if spec.get("proxyOnly") and not proxy:
            if any(_is_ours(e) for e in existing):
                out.append(f"hook {event}: REMOVE (only used with --proxy)")
            continue
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
    from . import ccdelegate
    st = ccdelegate.check()
    out.append("cc-delegate: " + ("re-APPLY the station patch (gate, api_base, mcp<2 pin)"
                                  if st["state"] in ("unpatched", "drifted")
                                  else st["detail"]))
    out.append(f"state: ENSURE {config.HOME} (config.json, governor.db, logs/)")
    cur, ours = proxy_env_state(port)
    if proxy:
        out.append(f"env: SET settings.json env.ANTHROPIC_BASE_URL=http://127.0.0.1:{port} "
                   f"(the file-based route; reaches GUI-launched apps)")
        out.append(f"env: SET user ANTHROPIC_BASE_URL=http://127.0.0.1:{port}"
                   + (" (already set)" if ours else
                      f" (replacing {cur!r})" if cur else "")
                   + " -- persistent, applies to apps started afterwards; "
                     "restart Claude Desktop to pick it up")
    elif ours or settings_env_state(settings, port)[1]:
        out.append("env: REMOVE the ANTHROPIC_BASE_URL redirect from settings.json "
                   "and the user environment (Claude Code goes straight to Anthropic)")
    else:
        out.append(f"env: NOT set (pass --proxy to route terminal Claude sessions "
                   f"through the router on 127.0.0.1:{port}). Claude Desktop sets "
                   f"ANTHROPIC_BASE_URL itself and ignores this.")
    out.append(f"backup: {SETTINGS} -> {BACKUPS}/settings.json.dg-<timestamp>")
    out.append("untouched: your cc-delegate profiles and credentials, other plugins, "
               "permissions, model, existing hooks, OpenRouter (absent), "
               "Oracle profiles, `claude` itself")
    return out


def run(dry_run: bool = False, proxy: bool = False) -> int:
    settings = _load_settings()
    for line in plan(settings, proxy):
        print(("[dry-run] " if dry_run else "") + line)
    if dry_run:
        return 0

    bk = _backup()
    if bk:
        print(f"backed up settings -> {bk}")

    hooks = settings.setdefault("hooks", {})
    for event, spec in HOOKS_SPEC.items():
        entries = hooks.setdefault(event, [])
        if spec.get("proxyOnly") and not proxy:
            # Tear it down when the proxy is not wired: leaving it would start
            # a router that nothing points at.
            hooks[event] = [e for e in entries if not _is_ours(e)]
            continue
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

    for event in list(hooks):
        if not hooks[event]:
            hooks.pop(event)  # an empty array is noise, not configuration
    if not hooks:
        settings.pop("hooks", None)

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

    if not proxy:
        # Never leave Claude Code pointed at a router that may not be running.
        _, ours_now = proxy_env_state(config.load()["proxy"]["port"])
        had_settings = settings_env_state(settings, config.load()["proxy"]["port"])[1]
        if ours_now:
            clear_proxy_env()
            print("env: removed user ANTHROPIC_BASE_URL")
        if had_settings:
            clear_settings_env(settings)
            _atomic_write(SETTINGS, json.dumps(settings, indent=2) + chr(10))
            print("env: removed settings.json env.ANTHROPIC_BASE_URL")

    from . import ccdelegate
    st = ccdelegate.check()
    if st["state"] in ("unpatched", "drifted"):
        res = ccdelegate.apply()
        print(f"cc-delegate: {res['state']} ({res.get('detail','')[:120]})")
        if res.get("backup"):
            print(f"cc-delegate: previous gate backed up -> {res['backup']}")
    elif st["state"] != "absent":
        print(f"cc-delegate: {st['detail']}")

    config.ensure_home()
    print(f"state dir ready -> {config.HOME}")
    if proxy:
        port = config.load()["proxy"]["port"]
        url = set_settings_env(settings, port)
        _atomic_write(SETTINGS, json.dumps(settings, indent=2) + chr(10))
        print(f"env: settings.json env.ANTHROPIC_BASE_URL={url}")
        set_proxy_env(port)
        print(f"env: user ANTHROPIC_BASE_URL={url}")
        print("     restart Claude Desktop (and any open terminals) to pick it up.")
        print("     the SessionStart hook keeps the router running; `dg proxy --status` "
              "checks it.")
    print("done. `dg doctor` to verify, `dg launch` for a managed session, "
          "plain `claude` still bypasses the Governor.")
    return 0


def uninstall(dry_run: bool = False, purge: bool = False) -> int:
    settings = _load_settings()
    actions: list[str] = []
    port = config.load()["proxy"]["port"]
    _, ours = proxy_env_state(port)
    if ours:
        actions.append("env: REMOVE user ANTHROPIC_BASE_URL (it points at the dg proxy)")
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
    if ours:
        clear_proxy_env()
        print("env: removed user ANTHROPIC_BASE_URL; restart Desktop/terminals")
    clear_settings_env(settings)
    if purge and config.HOME.exists():
        shutil.rmtree(config.HOME)
    print("uninstalled. cc-delegate and every other plugin are untouched.")
    return 0


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".dgtmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)
