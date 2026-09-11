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
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config
from .version import VERSION

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
BACKUPS = CLAUDE_DIR / "backups"
SKILL_DST = CLAUDE_DIR / "skills" / "delegation-governor"
# Shipped inside the package so it is found identically from a source checkout
# and from an installed tool (`uv tool install`), whose wheel has no repo tree.
SKILL_SRC = Path(__file__).resolve().parent / "skill"
# Versioned runtimes allow release N to install N+1 on Windows, where a
# running python.exe cannot safely be replaced in place.
RUNTIME = config.HOME / "runtimes" / VERSION
MANIFEST = config.HOME / "install-manifest.json"
SHIM_DIR = Path.home() / ".local" / "bin"
SHIM = SHIM_DIR / ("dg.exe" if os.name == "nt" else "dg")
TASK_NAME = "DelegationGovernorProxy"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"

MARK = "_delegationGovernor"

def runtime_python() -> Path:
    return RUNTIME / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_pythonw() -> Path:
    return RUNTIME / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")


def _managed_command(*args: str) -> str:
    return " ".join([f'"{runtime_python()}"', "-m", "dg.cli", *args])


HOOKS_SPEC = {
    # The prompt entrypoint dispatches by hook_event_name, so the installed
    # command can serve both events without changing the CLI surface.
    "PreToolUse": {"matcher": "Read|Bash", "command": _managed_command("hook", "prompt"),
                   "timeout": 10},
    "UserPromptSubmit": {"matcher": None, "command": _managed_command("hook", "prompt"), "timeout": 10},
    "StopFailure": {"matcher": "rate_limit", "command": _managed_command("hook", "stopfailure"), "timeout": 10},
    # Only meaningful when the proxy is wired in, so it is added and removed
    # with it -- a hook that starts a proxy nothing routes through is noise.
    "SessionStart": {"matcher": None, "command": _managed_command("hook", "session"), "timeout": 15,
                     "proxyOnly": True},
}
STATUSLINE = {"type": "command", "command": _managed_command("hook", "statusline"), "padding": 0}


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
    return any(_is_hook_command(h.get("command", "")) for h in entry.get("hooks", []))


def _is_hook_command(command: str) -> bool:
    return "dg hook" in command or "dg.cli hook" in command


def _hook_entry(spec: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "hooks": [{"type": "command", "command": spec["command"],
                   "timeout": spec["timeout"]}]
    }
    if spec["matcher"]:
        entry["matcher"] = spec["matcher"]
    return entry


def proxy_env_state(port: int) -> tuple[str | None, bool]:
    """(current user-level ANTHROPIC_BASE_URL, whether it points at our proxy)."""
    if os.name != "nt":
        return os.environ.get("ANTHROPIC_BASE_URL"), False
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
    url = f"http://127.0.0.1:{port}/client/cli"
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
    url = f"http://127.0.0.1:{port}/client/cli"
    if os.name != "nt":
        return url
    subprocess.run(["setx", "ANTHROPIC_BASE_URL", url], capture_output=True, text=True)
    return url


def clear_proxy_env() -> None:
    if os.name != "nt":
        return
    subprocess.run(["reg", "delete", r"HKCU\Environment", "/v", "ANTHROPIC_BASE_URL", "/f"],
                   capture_output=True, text=True)


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def bootstrap_runtime() -> dict[str, Any]:
    """Build a stable, project-owned runtime and PATH launcher.

    The runtime is created at its final path because Windows entry-point
    launchers embed the interpreter location. The previous runtime is retained
    until the replacement passes a smoke test.
    """
    if os.name != "nt":
        raise RuntimeError("managed installation is currently supported on Windows only")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to build the managed Governor runtime")
    config.ensure_home()
    current = Path(sys.executable).resolve()
    if RUNTIME.resolve() in current.parents and RUNTIME.exists():
        raise RuntimeError(
            "cannot rebuild the running Governor release in place; run install from "
            "the checkout's .venv, or bump the project version")
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    previous = RUNTIME.with_name("runtime.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if RUNTIME.exists():
        RUNTIME.replace(previous)
    try:
        subprocess.run([uv, "venv", str(RUNTIME), "--python", f"{sys.version_info.major}.{sys.version_info.minor}"],
                       check=True, capture_output=True, text=True)
        subprocess.run([uv, "pip", "install", "--python", str(runtime_python()),
                        "--reinstall", str(_source_root())],
                       check=True, capture_output=True, text=True)
        smoke = subprocess.run([str(runtime_python()), "-m", "dg.cli", "--help"],
                               capture_output=True, text=True)
        if smoke.returncode != 0:
            raise RuntimeError(smoke.stderr.strip() or "managed runtime smoke test failed")
    except Exception:
        if RUNTIME.exists():
            shutil.rmtree(RUNTIME)
        if previous.exists():
            previous.replace(RUNTIME)
        raise

    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    runtime_shim = RUNTIME / "Scripts" / "dg.exe"
    backup = SHIM.with_suffix(SHIM.suffix + ".pre-governor")
    if SHIM.exists() and not backup.exists():
        shutil.copy2(SHIM, backup)
    _install_shim(runtime_shim)
    manifest = {"version": VERSION, "runtime": str(RUNTIME), "shim": str(SHIM),
                "shimBackup": str(backup) if backup.exists() else None,
                "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _atomic_write(MANIFEST, json.dumps(manifest, indent=2) + "\n")
    if previous.exists():
        shutil.rmtree(previous)
    return manifest


def _install_shim(runtime_shim: Path) -> None:
    """Replace the PATH launcher even while an old Windows image drains.

    Windows may keep an executable open briefly after its process exits. A
    rename is still allowed in the normal case, so retire the old pathname and
    publish the new launcher there. The pre-Governor backup remains the source
    of truth for uninstall.
    """
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            shutil.copy2(runtime_shim, SHIM)
            return
        except PermissionError:
            if attempt < 3:
                time.sleep(0.5)
    stale = SHIM.with_suffix(SHIM.suffix + f".stale-{os.getpid()}")
    try:
        SHIM.replace(stale)
        shutil.copy2(runtime_shim, SHIM)
    except OSError as e:
        raise RuntimeError(
            f"cannot replace the in-use launcher {SHIM}; close processes running dg and retry"
        ) from e


def register_proxy_task(port: int) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("proxy autostart is currently supported on Windows only")
    command = f'"{runtime_pythonw()}" -m dg.cli proxy --port {port}'

    def psq(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    # Restart-on-failure makes this genuinely always-on; SessionStart remains
    # a second recovery path when Task Scheduler is unavailable.
    script = (
        f"$a=New-ScheduledTaskAction -Execute {psq(str(runtime_pythonw()))} "
        f"-Argument {psq(f'-m dg.cli proxy --port {port}')};"
        "$t=New-ScheduledTaskTrigger -AtLogOn;"
        "$s=New-ScheduledTaskSettingsSet -RestartCount 999 "
        "-RestartInterval (New-TimeSpan -Minutes 1) "
        "-ExecutionTimeLimit ([TimeSpan]::Zero);"
        f"Register-ScheduledTask -TaskName {psq(TASK_NAME)} -Action $a -Trigger $t "
        "-Settings $s -Force | Out-Null"
    )
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True)
    if p.returncode != 0:
        fallback = subprocess.run(
            ["reg", "add", RUN_KEY, "/v", TASK_NAME, "/t", "REG_SZ", "/d", command, "/f"],
            capture_output=True, text=True)
        if fallback.returncode != 0:
            raise RuntimeError(
                f"could not register {TASK_NAME}: {p.stderr.strip()}; "
                f"HKCU Run fallback: {fallback.stderr.strip()}")
        return {"task": TASK_NAME, "command": command, "method": "HKCU Run",
                "warning": "Task Scheduler denied access; SessionStart supplies restart recovery"}
    # Remove an older fallback after the stronger scheduled task succeeds.
    subprocess.run(["reg", "delete", RUN_KEY, "/v", TASK_NAME, "/f"],
                   capture_output=True, text=True)
    return {"task": TASK_NAME, "command": command, "method": "Scheduled Task"}


def remove_proxy_task() -> None:
    if os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                       capture_output=True, text=True)
        subprocess.run(["reg", "delete", RUN_KEY, "/v", TASK_NAME, "/f"],
                       capture_output=True, text=True)


def start_managed_proxy(port: int) -> dict[str, Any]:
    from . import hooks, proxy
    current = proxy.health(port, timeout=1)
    if current and current.get("version") == VERSION:
        return current
    if current:
        proxy.stop(port)
        time.sleep(0.3)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        kw["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                               getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    subprocess.Popen([str(runtime_python()), "-m", "dg.cli", "proxy", "--port", str(port)],
                     **kw)
    if not hooks._wait_for_proxy(proxy, port):
        raise RuntimeError(f"managed proxy did not start on 127.0.0.1:{port}")
    return proxy.health(port) or {}


def plan(settings: dict[str, Any], proxy: bool = False) -> list[str]:
    """Human-readable diff of what install would change."""
    out: list[str] = []
    port = config.load()["proxy"]["port"]
    for event, spec in HOOKS_SPEC.items():
        existing = settings.get("hooks", {}).get(event, [])
        desired = _hook_entry(spec)
        if spec.get("proxyOnly") and not proxy:
            if any(_is_ours(e) for e in existing):
                out.append(f"hook {event}: REMOVE (only used with --proxy)")
            continue
        if desired in existing:
            out.append(f"hook {event}: already installed, no change")
        elif any(_is_ours(e) for e in existing):
            out.append(f"hook {event}: UPDATE broken/legacy dg command -> "
                       f"`{spec['command']}`")
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
        out.append(f"env: SET settings.json env.ANTHROPIC_BASE_URL="
                   f"http://127.0.0.1:{port}/client/cli")
        out.append(f"env: SET user ANTHROPIC_BASE_URL="
                   f"http://127.0.0.1:{port}/client/cli"
                   + (" (already set)" if ours else
                      f" (replacing {cur!r})" if cur else "")
                   + " -- persistent, applies to terminals started afterwards")
        out.append("Desktop: worker delegation only; personal Desktop manages "
                   "ANTHROPIC_BASE_URL and cannot use this terminal proxy")
    elif ours or settings_env_state(settings, port)[1]:
        out.append("env: REMOVE the ANTHROPIC_BASE_URL redirect from settings.json "
                   "and the user environment (Claude Code goes straight to Anthropic)")
    else:
        out.append(f"env: NOT set (pass --proxy to route terminal Claude sessions "
                   f"through the router on 127.0.0.1:{port})")
    out.append(f"backup: {SETTINGS} -> {BACKUPS}/settings.json.dg-<timestamp>")
    out.append("untouched: your cc-delegate profiles and credentials, other plugins, "
               "permissions, model, existing hooks, OpenRouter (absent), "
               "Oracle profiles, `claude` itself")
    return out


def run(dry_run: bool = False, proxy: bool | None = None) -> int:
    """proxy: True arms the router, False tears it down, None keeps it as-is.

    `None` is the default deliberately. An earlier version treated "no flag" as
    "tear it down", so running `dg install` for an unrelated reason -- say, to
    re-apply the cc-delegate patch -- silently unwired the router and the next
    session quietly stopped failing over. Installing something must not change
    a setting the user never mentioned.
    """
    settings = _load_settings()
    if proxy is None:
        port = config.load()["proxy"]["port"]
        proxy = proxy_env_state(port)[1] or settings_env_state(settings, port)[1]
    for line in plan(settings, proxy):
        print(("[dry-run] " if dry_run else "") + line)
    if dry_run:
        return 0

    # Stop an older proxy before replacing a same-release runtime and before a
    # new runtime claims the same port.
    port = config.load()["proxy"]["port"]
    from . import proxy as proxy_mod
    if proxy_mod.health(port, timeout=1):
        proxy_mod.stop(port)
        time.sleep(0.3)
    manifest = bootstrap_runtime()
    previous_user_url, _ = proxy_env_state(config.load()["proxy"]["port"])
    manifest["previousUserBaseUrl"] = previous_user_url
    _atomic_write(MANIFEST, json.dumps(manifest, indent=2) + "\n")
    print(f"managed runtime ready -> {manifest['runtime']}")
    if proxy:
        port = config.load()["proxy"]["port"]
        registration = register_proxy_task(port)
        print(f"proxy login startup -> {registration['method']}")
        if registration.get("warning"):
            print(f"warning: {registration['warning']}")
        health = start_managed_proxy(port)
        print(f"proxy ready -> instance {health.get('instanceId', 'unknown')}")
    else:
        remove_proxy_task()
        proxy_mod.stop(port)

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
        desired = _hook_entry(spec)
        if desired in entries:
            continue
        # Replace every older Governor entry (including the broken uv-tool
        # trampoline command) while leaving unrelated hooks untouched.
        entries[:] = [e for e in entries if not _is_ours(e)]
        entries.append(desired)

    cur = settings.get("statusLine")
    if cur and cur != STATUSLINE and MARK not in settings:
        settings[MARK] = {"previousStatusLine": cur}
    settings["statusLine"] = dict(STATUSLINE)
    settings.setdefault(MARK, {})["installedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if proxy and "previousBaseUrl" not in settings[MARK]:
        settings[MARK]["previousBaseUrl"] = (settings.get("env") or {}).get(
            "ANTHROPIC_BASE_URL")

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
        # Only reached when the caller asked for it, or nothing was armed.
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
        print("     restart open terminals to pick up the terminal route.")
        print("     Personal Claude Desktop will ignore this terminal route; its worker "
              "delegation remains available.")
        print("     Login startup and the SessionStart hook keep the router running; "
              "`dg proxy --status` checks it.")
    print("done. `dg doctor` to verify, `dg launch` for a managed session, "
          "plain `claude` uses the same router.")
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
    if _is_hook_command(settings.get("statusLine", {}).get("command", "")):
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
    if _is_hook_command(settings.get("statusLine", {}).get("command", "")):
        if prev:
            settings["statusLine"] = prev
        else:
            settings.pop("statusLine", None)
    for event in list(hooks):
        if not hooks[event]:
            hooks.pop(event)
    if not hooks:
        settings.pop("hooks", None)
    previous_url = (settings.get(MARK) or {}).get("previousBaseUrl")
    settings.pop(MARK, None)
    clear_settings_env(settings)
    if previous_url:
        settings.setdefault("env", {})["ANTHROPIC_BASE_URL"] = previous_url
    _atomic_write(SETTINGS, json.dumps(settings, indent=2) + "\n")
    if SKILL_DST.exists():
        shutil.rmtree(SKILL_DST)
    if ours:
        try:
            saved_manifest = json.loads(MANIFEST.read_text("utf-8"))
        except (OSError, ValueError):
            saved_manifest = {}
        previous_user = saved_manifest.get("previousUserBaseUrl")
        if previous_user and os.name == "nt":
            subprocess.run(["setx", "ANTHROPIC_BASE_URL", previous_user],
                           capture_output=True, text=True)
            print("env: restored previous user ANTHROPIC_BASE_URL")
        else:
            clear_proxy_env()
            print("env: removed user ANTHROPIC_BASE_URL; restart terminals")
    from . import proxy as proxy_mod
    proxy_mod.stop(port)
    remove_proxy_task()
    try:
        manifest = json.loads(MANIFEST.read_text("utf-8"))
    except (OSError, ValueError):
        manifest = {}
    backup = Path(manifest["shimBackup"]) if manifest.get("shimBackup") else None
    if SHIM.exists():
        SHIM.unlink()
    if backup and backup.exists():
        backup.replace(SHIM)
    if purge and config.HOME.exists():
        shutil.rmtree(config.HOME)
    print("uninstalled. cc-delegate and every other plugin are untouched.")
    return 0


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".dgtmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)
