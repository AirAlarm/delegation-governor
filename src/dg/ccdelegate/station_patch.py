#!/usr/bin/env python3
"""Re-apply the cc-delegate 'station' local patch after a plugin update.

`claude plugin update cc-delegate` installs a fresh copy under a new version
directory, dropping these edits. Run this script afterwards:

    python ~/.cc-delegate/station_patch.py            # patch the newest install
    python ~/.cc-delegate/station_patch.py --check     # report only, exit 1 if unpatched
    python ~/.cc-delegate/station_patch.py --plugin-dir <path>

What it does (idempotent):
  1. copies server/lmstudio_gate.py into the install
  2. server/main.py      : adds  "api_base": resolved["api_base"]  to the worker args,
     AND pins the inline `dependencies = ["mcp"]` to `["mcp<2"]` — mcp 2.x renamed
     FastMCP -> MCPServer, so `uv run` (which resolves latest by default) breaks
     server startup outright (ModuleNotFoundError on every launch) without this.
  3. server/worker_launcher.py : passes --api-base through and calls
     lmstudio_gate.ensure_model_ready before spawning the worker
  4. worker/worker.py    : adds --api-base and exports OPENAI_API_BASE/OPENAI_BASE_URL
     for the worker process only
  5. skills/delegate-heavy-dev/SKILL.md : appends the station routing policy

The gate module itself lives next to this script (station_lmstudio_gate.py) so
an update never destroys it; the script copies it in.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GATE_SRC = os.path.join(HERE, "station_lmstudio_gate.py")

DEFAULT_GLOB = os.path.expanduser(
    "~/.claude/plugins/cache/cc-delegate-marketplace/cc-delegate/*/"
)

MAIN_ANCHOR = '        "api_key": resolved["api_key"],\n'
MAIN_ADD = '        "api_base": resolved["api_base"],  # station patch: thread to worker\n'

MCP_PIN_ANCHOR = '# dependencies = ["mcp"]\n'
MCP_PIN_ADD = (
    '# dependencies = ["mcp<2"]  # station patch: mcp 2.x renamed FastMCP -> MCPServer, breaks this code\n'
)

LAUNCHER_ANCHOR_ARGS = '        "--command-timeout", str(cfg.command_timeout_s),\n    ]\n'
LAUNCHER_ADD_ARGS = (
    '        "--command-timeout", str(cfg.command_timeout_s),\n    ]\n'
    '    if args.get("api_base"):  # station patch: OpenAI-compatible base URL (LM Studio)\n'
    '        cli += ["--api-base", args["api_base"]]\n'
)

LAUNCHER_ANCHOR_RUN = "    try:\n        proc = await asyncio.create_subprocess_exec(\n"
LAUNCHER_ADD_RUN = (
    '    # station patch: make the target LM Studio model the only resident LLM\n'
    '    # (and serialise vs other running local delegations) before the worker starts.\n'
    '    if args.get("api_base"):\n'
    '        try:\n'
    '            from lmstudio_gate import ensure_model_ready\n'
    '            jobs_dir = str(Path(job["repo"]) / cfg.work_dir / "jobs")\n'
    '            _touch("ensuring LM Studio model is loaded")\n'
    '            await asyncio.get_event_loop().run_in_executor(\n'
    '                None, ensure_model_ready, args.get("model"), args.get("api_base"), jobs_dir\n'
    '            )\n'
    '        except Exception as e:  # noqa: BLE001\n'
    '            job["status"] = "failed"\n'
    '            job["error"] = f"LM Studio model gate: {e}"\n'
    '            persist_job(job, cfg.work_dir)\n'
    '            _publish({"kind": "failed", "error": job["error"]})\n'
    '            return\n\n'
    "    try:\n        proc = await asyncio.create_subprocess_exec(\n"
)

WORKER_ANCHOR = (
    "    args = p.parse_args()\n"
    "    fallback_models = [m.strip() for m in args.fallback_models.split(\",\") "
    "if m.strip()] if args.fallback_models else None\n"
)
WORKER_ADD = (
    '    p.add_argument("--api-base", default=None,\n'
    '                    help="station patch: custom base URL for --model\'s litellm provider.")\n'
    "    args = p.parse_args()\n"
    "    fallback_models = [m.strip() for m in args.fallback_models.split(\",\") "
    "if m.strip()] if args.fallback_models else None\n\n"
    "    if args.api_base:  # station patch: scoped to this worker subprocess only\n"
    '        _key = os.environ.get("DELEGATE_API_KEY") or "lm-studio"\n'
    '        _provider = _bare_model(args.model).split("/", 1)[0]\n'
    '        if _provider == "anthropic":  # e.g. an Oracle-side LiteLLM Anthropic-format gateway\n'
    '            os.environ["ANTHROPIC_API_BASE"] = args.api_base\n'
    '            os.environ.setdefault("ANTHROPIC_API_KEY", _key)\n'
    '        else:  # openai-compatible (e.g. a local LM Studio endpoint)\n'
    '            os.environ["OPENAI_API_BASE"] = args.api_base\n'
    '            os.environ["OPENAI_BASE_URL"] = args.api_base\n'
    '            os.environ.setdefault("OPENAI_API_KEY", _key)\n'
)

SKILL_MARK = "## Station routing (this machine — measured station-coder-sweep policy, 2026-09-07)"
SKILL_BLOCK = f"""

{SKILL_MARK}

This box routes `run_dev_task` to local LM Studio workers. Three profiles
(`~/.cc-delegate/config.json`): **station-fast** (gemma-4-e4b),
**station-main** (**qwen3.5-9b**, the default), **station-smart** (qwen3.6-35b-a3b).
Gate loads at `--context-length 32768`.

- **DEFAULT — always `station-main`** (qwen3.5-9b). Don't pass `profile` for normal work.
- **ESCALATE once to `station-smart`** (`profile="station-smart"`, a fresh
  `run_dev_task` with `base_branch` = station-main's salvaged branch and a spec
  naming what was wrong) ONLY when station-main's attempt:
  fails the required tests / rubric; introduces a regression; reports it cannot
  finish; leaves the root cause unidentified; produces a clearly over-broad
  patch; your review finds a real correctness problem; **or the task's working
  context is likely > ~40K tokens** (qwen3.5-9b's retention degrades past ~48K).
- **gpt-oss-20b is no longer a station profile** — explicit user request or a
  deliberate second opinion only (temp profile via `set_model_profile`).
- **Do NOT escalate** for verbosity, style, or trivial cleanup — fix those
  yourself or accept them.
- **Attempt budget: one `station-main` run, then one `station-smart` run.**
  If station-smart also fails, take the task back and do it yourself. Never a
  third local attempt (weak models thrash).
- **`station-fast` is never auto-selected for code changes.** Only use it when
  the user explicitly asks, and only for summarisation / extraction / a tiny
  deterministic one-file edit.
- Local delegations are **serialised** — one worker at a time (12 GB GPU fits
  one model). The model gate enforces this and swaps models automatically.
"""

BLANKS_MARK = "## Known trap: multi-blank document filling (any profile)"
BLANKS_BLOCK = f"""

{BLANKS_MARK}

Never delegate "fill in N blanks that share one placeholder token (e.g. `_TBD_`) with
DIFFERENT intended content per blank" as-is. A worker that hits `replace_all=True` on a
repeated placeholder gets the SAME generated text written into every occurrence -
mechanical, not a reasoning failure, and not model-specific (every profile shares this
edit tool). Confirmed live 2026-09-04 (CryptAndHearth, station-main): one paragraph
pasted into all 9 `_TBD_` sections of a GDD. Either give each blank a unique
placeholder/heading, or don't delegate multi-section document authoring - this is the
same "greenfield synthesis... should stay with you" case already noted above, just
concretely observed. Always inspect a `salvaged: true` patch's real content before
reuse - status alone caught nothing here, reading the diff did.
"""

ORACLE_MARK = "## Oracle routing (remote, 24/7, background/async only)"
ORACLE_BLOCK = f"""

{ORACLE_MARK}

Three more profiles reach a 24/7 CPU-only Oracle VM: **oracle-fast** (gemma-4 e2b),
**oracle-coder** (mellum2), **oracle-smart** (gemma-4 26b). NOT part of the
station-main -> station-smart chain above and NOT benchmarked the same way.

- Use for **background/async work nobody is blocking on** - the established use
  case for a cheap always-on CPU LLM (batch/queue jobs, classification,
  non-interactive passes), not interactive delegation. A single model call here
  can take minutes.
- `oracle-fast` fits cheap, high-volume, simple substeps (classification /
  routing / short extraction), not full coding delegation.
- Use when the local PC/LM Studio isn't available and the wait is fine, on
  explicit user request, or as a deliberate second opinion.
- Don't shorten `timeout_ms` below the default expecting local-box speed.
- `oracle-main` (chat) and `oracle-vision` (images) are NOT cc-delegate
  profiles - call them directly when relevant, not through `run_dev_task`.
"""


def _patch_file(path: str, anchor: str, replacement: str, marker: str, check: bool) -> bool:
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    if marker in txt:
        return False  # already patched
    if anchor not in txt:
        raise SystemExit(f"anchor not found in {path} — upstream changed, patch by hand")
    if check:
        print(f"UNPATCHED: {path}")
        return True
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt.replace(anchor, replacement, 1))
    print(f"patched: {path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin-dir", default=None)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.plugin_dir:
        pdir = a.plugin_dir
    else:
        cands = sorted(glob.glob(DEFAULT_GLOB))
        if not cands:
            raise SystemExit(f"no cc-delegate install found under {DEFAULT_GLOB}")
        pdir = cands[-1]
    pdir = os.path.abspath(pdir)
    print(f"target install: {pdir}")

    gate_dst = os.path.join(pdir, "server", "lmstudio_gate.py")
    need = False

    if not os.path.exists(GATE_SRC):
        raise SystemExit(f"gate source missing: {GATE_SRC}")
    if not os.path.exists(gate_dst) or open(gate_dst, encoding="utf-8").read() != open(GATE_SRC, encoding="utf-8").read():
        need = True
        if a.check:
            print(f"UNPATCHED (gate module): {gate_dst}")
        else:
            shutil.copyfile(GATE_SRC, gate_dst)
            print(f"installed: {gate_dst}")

    need |= _patch_file(os.path.join(pdir, "server", "main.py"),
                        MAIN_ANCHOR, MAIN_ANCHOR + MAIN_ADD, MAIN_ADD.strip(), a.check)
    need |= _patch_file(os.path.join(pdir, "server", "main.py"),
                        MCP_PIN_ANCHOR, MCP_PIN_ADD, "mcp<2", a.check)
    need |= _patch_file(os.path.join(pdir, "server", "worker_launcher.py"),
                        LAUNCHER_ANCHOR_ARGS, LAUNCHER_ADD_ARGS, "station patch: OpenAI-compatible base URL", a.check)
    need |= _patch_file(os.path.join(pdir, "server", "worker_launcher.py"),
                        LAUNCHER_ANCHOR_RUN, LAUNCHER_ADD_RUN, "station patch: make the target LM Studio model", a.check)
    need |= _patch_file(os.path.join(pdir, "worker", "worker.py"),
                        WORKER_ANCHOR, WORKER_ADD, "station patch: scoped to this worker subprocess", a.check)

    skill = os.path.join(pdir, "skills", "delegate-heavy-dev", "SKILL.md")
    if os.path.exists(skill):
        stxt = open(skill, encoding="utf-8").read()
        if SKILL_MARK not in stxt:
            need = True
            if a.check:
                print(f"UNPATCHED (skill): {skill}")
            else:
                open(skill, "a", encoding="utf-8").write(SKILL_BLOCK)
                print(f"patched: {skill}")
                stxt += SKILL_BLOCK
        if ORACLE_MARK not in stxt:
            need = True
            if a.check:
                print(f"UNPATCHED (skill/oracle): {skill}")
            else:
                open(skill, "a", encoding="utf-8").write(ORACLE_BLOCK)
                print(f"patched (oracle): {skill}")
                stxt += ORACLE_BLOCK
        if BLANKS_MARK not in stxt:
            need = True
            if a.check:
                print(f"UNPATCHED (skill/blanks): {skill}")
            else:
                open(skill, "a", encoding="utf-8").write(BLANKS_BLOCK)
                print(f"patched (blanks): {skill}")

    if a.check:
        print("\nstatus:", "NEEDS PATCH" if need else "fully patched")
        return 1 if need else 0
    print("\ndone." if need else "\nalready fully patched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
