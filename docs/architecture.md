# Delegation Governor - architecture decisions

Status: implemented. Verified against the versions in [Discovered environment](#discovered-environment).

## Problem

Claude Code should spend its tokens on judgement, not typing, and should never
sit idle while a delegated worker runs. Two quotas can run out (Anthropic,
Codex) and each needs a different fallback.

Three concerns, deliberately kept separate:

| Concern | Question | Answer lives in |
|---|---|---|
| Supervisor state | which model drives Claude Code? | `supervisor.py`, `launcher.py` |
| Worker state | which worker should Claude prefer? | `quota_codex.py`, `routing.py` |
| Task scheduler | what can proceed right now? | `store.py`, `scheduler.py` |

They never read each other's state. `SUP=SAVE` with `WRK=codex` and three
RUNNING jobs is a perfectly ordinary combination.

## Discovered environment

Everything below was queried, not assumed.

| Component | Finding |
|---|---|
| OS | Windows 11 26200, MINGW64 + PowerShell |
| Claude Code | 2.1.116, native `claude.exe` (not a Node install) |
| Codex CLI | `codex-cli 0.153.2`, logged in via ChatGPT |
| Node / Python / uv / git | v24.15.0 / 3.9.13 system, 3.14.7 via uv / 0.12.9 / 2.53.0 |
| cc-delegate | plugin 0.12.0, MCP server, station-*/oracle-* profiles |
| LM Studio | on **this machine**, `127.0.0.1:1234`, loopback-bound |
| CCR | not installed |

## ADR 1: no router. LM Studio already speaks Anthropic

The brief assumed a Claude Code Router was needed to reach LM Studio.

Verified instead: **LM Studio serves the Anthropic Messages API natively.**

```
POST http://127.0.0.1:1234/v1/messages  ->  200, {"type":"message", "content":[...],
                                                  "usage":{"input_tokens":68,...}}
```

So no translation layer is required, and CCR is not installed. That removes a
dependency, a process, a config file and a protocol-compatibility surface.

`dg doctor` verifies the endpoint at zero inference cost by posting an invalid
body and checking for an Anthropic-shaped `invalid_request_error` - LM Studio
answers unknown paths with a generic 200, so the error *shape* is the
discriminator, not the status code.

**Rejected:** CCR (unnecessary hop), LiteLLM in Anthropic mode (heavy
dependency for a translation that is not needed), a hand-written proxy
(a protocol to maintain forever).

## ADR 2: supervisor failover is a process lifecycle, not a runtime switch

Claude Code constructs its client from `process.env` at startup:

```js
new PA({ baseURL: env("ANTHROPIC_BASE_URL"), authToken: env("ANTHROPIC_AUTH_TOKEN") })
```

The backend is therefore **fixed for the life of the process**. Nothing can
switch it mid-session. Since the endpoint problem is already solved (ADR 1),
what remains is deciding *when to start a process against which base URL* -
a lifecycle problem. `dg launch` is that lifecycle supervisor.

```
dg launch
  -> claude --session-id <uuid> --model sonnet            (Anthropic)
  -> user works; StopFailure(rate_limit) records LOCAL
  -> claude exits (natural boundary)
  -> claude --resume <uuid> --model qwen/qwen3.6-35b-a3b  (LM Studio)
  -> reset passes, probe confirms Anthropic
  -> claude --resume <uuid> --model sonnet                (Anthropic)
```

**The safe synchronization boundary is Claude Code's own exit.** The Governor
never signals, interrupts or kills it: doing so could tear a session apart
mid-tool-call, mid-write or mid-git-operation. A hard rate limit does not kill
the session either - the hook records the state and tells the user; the switch
happens on the next natural exit. A relaunch therefore cannot land in the
middle of a side effect.

Session continuity: the first launch pins `--session-id`, so the id is known
without scraping anything, and every relaunch uses `--resume`. `--model` is
passed explicitly on **every** launch, because a resumed session otherwise
restores the model it was saved with - which after a switch would be an
Anthropic model name pointed at LM Studio, or the reverse.

Restart-loop protection: a relaunch that exits within 20s is a strike; three
strikes stop the loop, with backoff between attempts and a bounded restart
budget. `dg launch` also refuses to start against an unreachable LM Studio and
prints the recovery path instead.

**Rejected:** killing Claude Code on the hook (unsafe boundary); a local
reverse proxy that swaps upstreams mid-stream (a custom proxy to avoid a
process restart - more moving parts, and mid-request switching is exactly
where correctness gets hard).

## ADR 3: SQLite for state

`~/.claude/delegation-governor/governor.db`, WAL, schema-versioned.

Multiple Claude sessions share this. SQLite gives atomic writes, cross-process
locking and atomic task claiming (`BEGIN IMMEDIATE`) for free. JSON files plus
hand-rolled locking would be more code and less correct on Windows.

`config.json` stays separate and human-editable. No credentials are ever
stored - only environment variable *names*.

## ADR 4: READY and BLOCKED are computed, never stored

Stored: `PLANNED QUEUED RUNNING SUCCEEDED FAILED QUOTA_FAILED AUTH_FAILED
SUPERSEDED CANCELLED INTEGRATED`.

READY/BLOCKED are derived from `PLANNED` + the dependency graph + path
conflicts + capacity, on every read. A stored READY flag would go stale
against its own edges; a computed one cannot.

A dependency in a terminal-bad state blocks its dependants permanently. Dead
dependencies never silently unblock.

## ADR 5: task identity is separate from attempt identity

`tasks` is the logical unit; `attempts` records each execution. One task can
have a Codex attempt that hit quota and a cc-delegate attempt that succeeded,
and both stay visible. Failed attempts are never hidden.

Partial output from a failed attempt is preserved on disk for diagnosis, and
never reused: `dg fallback` starts a fresh task from the original clean base.

## ADR 6: quota inspection costs zero inference

**Codex** - app-server JSON-RPC, verified live:

```
codex app-server --stdio
  -> initialize -> initialized -> account/rateLimits/read
  -> { rateLimits: { primary: {usedPercent, windowDurationMins, resetsAt},
                     secondary: {...}, planType, rateLimitReachedType, ... },
       rateLimitsByLimitId: { codex: {...}, base_model_inference: {...} },
       rateLimitResetCredits: { availableCount, credits } }
```

Only `limitId: "codex"` gates delegation; `base_model_inference` (gpt-reserve)
is reported but never blocks. Missing windows are `UNKNOWN`, never "available"
and never "exhausted". Codex stays unavailable until the **latest** exhausted
window has recovered, and even then a confirmation probe is required.

Text matching exists only as a last-resort classifier when the structured
channel itself fails.

**Claude** - never polled. Claude Code hands the statusline
`rate_limits.{five_hour,seven_day}.{utilization,resets_at}` on every redraw
(built from the `anthropic-ratelimit-unified-*` headers), so the hook records
it for free. Note the installed field is `utilization`, not the
`used_percentage` the brief guessed; both spellings are accepted defensively.
Absence is normal before the first API response and never reads as 0%.

## ADR 7: cc-delegate is read, never driven

cc-delegate is an MCP server, so only Claude can call `run_dev_task`. Rather
than shell out or reimplement it, the Governor reads the job files it already
writes to `<repo>/.cc-delegate/jobs/<taskId>.json`. Status sync costs no MCP
call, no inference and no polling of the worker.

Its configuration, profiles and worktrees are untouched.

## ADR 8: worktrees live outside the repo

`~/.claude/delegation-governor/worktrees/<repo>-<hash>/<task-id>`, branched
from the current commit (not the branch name, so a checkout in the user's tree
cannot move the ground under a running worker).

Inside the repo, a `.dg/` directory would show up as untracked in the user's
`git status` and would need a `.gitignore` entry written into their project.
Outside it, the working tree stays byte-for-byte clean - verified by a test.

## ADR 9: hooks are subcommands

`dg hook statusline|prompt|stopfailure`, one executable on PATH, rather than
three scripts each needing `PYTHONPATH` wiring in `settings.json`.

The `UserPromptSubmit` hook emits **state only** (`SUP=... WRK=... READY=n`)
and stays silent when there is nothing to say. Policy lives in the skill, which
loads on demand. That is the token-efficiency split: the hook is a handful of
tokens per prompt, the policy is loaded once when it is relevant.

Every hook is failure-tolerant by construction: a broken hook must not break a
session, and a broken statusline must not blank the status bar.

## Out of scope, as instructed

OpenRouter is not installed or configured. Oracle profiles are untouched. LM
Link is not configured. Stock `claude` is never shadowed or replaced.
