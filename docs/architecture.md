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
  -> claude --resume <uuid> --model openai/gpt-oss-20b     (LM Studio)
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

## ADR 2b: the LOCAL route has two hard prerequisites

Proven live (`claude -p` against LM Studio returned `LOCAL-OK`), but only
after two failures that the design now handles rather than hopes about.

**Context size.** Claude Code's system prompt plus tool definitions measured
**33,684 tokens**. A model loaded at LM Studio's default context rejects the
very first turn:

```
exceed_context_size_error: request (33684 tokens) exceeds the available
context size (26112 tokens)
```

So `dg launch` treats residency as part of switching to LOCAL: it reads
`/api/v0/models` for the model's state and `loaded_context_length`, and runs
`lms load <model> -c <contextLength>` when that is missing or too small,
capped at the model's own maximum. `minContextLength` defaults to 40960 --
comfortably above the measured 34k, low enough to allow a modest box.

**Model size.** The default supervisor model is `openai/gpt-oss-20b`, not the
larger `qwen/qwen3.6-35b-a3b`. The 35B's weights alone are 22GB, and LM Studio's
memory guardrails refuse to load it at any useful context on this machine;
the 20B is 12GB and loads at the full 131072. Both are configurable.

**One model slot.** LM Studio holds a single model resident. A LOCAL
supervisor and a cc-delegate `station-*` profile therefore evict each other --
observed live as a cc-delegate model-gate timeout while the supervisor model
was loading, which failed that delegated task. `dg doctor` and `dg launch`
detect the shared endpoint and warn; the Governor does not try to serialise
another tool's worker. While the supervisor is LOCAL, delegate to Codex or an
`oracle-*` profile.

## ADR 2c: two fallback tiers, tried in order

`supervisorFallbacks` is an ordered list; `dg launch` takes the first that
answers a zero-inference probe.

| Tier | Strength | Weakness |
|---|---|---|
| `lmstudio` (GPU box) | fast, free | one model slot, only up when the PC is |
| `oracle` (Ampere VM) | always on, independent of the GPU slot | CPU-only ARM, minutes per turn |

Oracle earns its place for two reasons beyond redundancy: it is up when the
station PC is not, and running the supervisor there leaves LM Studio's single
model slot free for `station-*` workers - the contention that really did fail a
delegated task during testing.

Verified live: the gateway answers `/v1/messages` in Anthropic format with model
`oracle-smart · gemma-4 26b` (it exposes no `/v1/models`, so model ids come from
the cc-delegate config). Its key is read from `ORACLE_LLM_API_KEY`, falling back
to the key cc-delegate already stores, rather than asking for a second copy.

**OpenRouter was considered and rejected.** It serves only the OpenAI chat
format, so using it as a supervisor would reintroduce the translation layer
ADR 1 removed - and it is metered billing, which inverts the project's goal of
spending less. As a *worker* it needs no Governor change at all: cc-delegate
already speaks that format, so it belongs there as a profile if it is wanted.

## ADR 2d: a router proxy, because Desktop cannot be relaunched

ADR 2 rejected a reverse proxy on the grounds that a process relaunch was
simpler. That holds for terminal sessions. It does not hold for Claude Desktop,
and measurement decided it:

* Desktop bundles its own `claude.exe` and spawns sessions as children, so
  nothing external can wrap or relaunch it;
* Desktop never invokes the statusLine hook (cleared `claudeQuota` stayed unset
  across many turns), the `UserPromptSubmit` payload carries no `rate_limits`,
  and transcripts hold no quota telemetry either.

So in Desktop the supervisor was both blind and unable to act. `dg proxy` fixes
both at once.

```
Claude Code --> 127.0.0.1:8787 --> api.anthropic.com   (normal)
                              \-> LM Studio / Oracle    (quota exhausted)
```

It is a **router, not a translator**: every backend already speaks the
Anthropic Messages API, so requests are forwarded, not converted. The switch is
per request, so there is no restart and no session to preserve.

Two findings made this viable:

* **OAuth survives a custom base URL.** Verified live: with only
  `ANTHROPIC_BASE_URL` set, Claude Code still sends
  `Authorization: Bearer sk-ant-oat01...`. Claude Code's own predicate for using
  OAuth turns on Bedrock/Foundry/AWS/Mantle, an explicit auth token, or an API
  key -- never on the base URL. Anthropic-bound traffic therefore passes the
  header through **verbatim and unread**; a fallback tier gets its own key
  instead, so the subscription token never leaves the machine except to
  Anthropic.
* **Anthropic returns quota on every response**
  (`anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}`). Harvesting it in
  the proxy restores SAVE mode in Desktop at zero cost.

Wiring it on is opt-in (`dg install --proxy`) because it sets a *persistent
user* `ANTHROPIC_BASE_URL` -- the only channel Desktop inherits. The trade is
explicit: Claude Code then depends on the proxy being up, so a `SessionStart`
hook starts it on demand, `dg doctor` fails loudly if the variable points at a
dead port, and `dg uninstall` removes the variable again.

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

Cleanup is likewise non-destructive, after live testing showed it was not:
`integrate --cleanup` originally removed the worktree and force-deleted the
branch while the worker's output was still *uncommitted*, destroying the only
copy - a later task branched from HEAD then failed because the change had
vanished. Cleanup now commits loose output to the branch first and keeps any
branch not already reachable from HEAD, saying so; `--discard` is the explicit
opt-in to throw it away.

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
it for free. Note the installed field is `utilization`, not the `used_percentage` the brief
guessed; both spellings are accepted defensively.

**`utilization` is a fraction 0..1, not a percentage** -- Claude Code renders it
as `Math.floor(utilization * 100)`. Comparing the raw value against percentage
thresholds silently disabled the entire supervisor: nothing could ever reach
70. The conversion lives in `supervisor._window`, the single point the field
enters the system. This one is worth remembering because the original test
fixtures passed percentages, encoding the same misunderstanding as the code, so
they validated the bug instead of catching it -- it took a real response header
to expose.
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
