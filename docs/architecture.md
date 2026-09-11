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

## ADR 1: route, do not translate

The brief assumed a Claude Code Router was needed to reach LM Studio.

Verified instead: **LM Studio serves the Anthropic Messages API natively.**

```
POST http://127.0.0.1:1234/v1/messages  ->  200, {"type":"message", "content":[...],
                                                  "usage":{"input_tokens":68,...}}
```

So no translation layer is required, and CCR is not installed. The Governor's
small loopback router only selects an upstream, rewrites the configured model,
and protects credentials; it does not convert protocols.

`dg doctor` verifies the endpoint at zero inference cost by posting an invalid
body and checking for an Anthropic-shaped `invalid_request_error` - LM Studio
answers unknown paths with a generic 200, so the error *shape* is the
discriminator, not the status code.

Routine lane probes do **not** do this. They GET `/api/v0/models`, which also
reports residency and loaded context. The invalid POST is correct but logs a
red `invalid_request_error` in the user's LM Studio window every time it runs,
which is a bad trade for information a GET already carries. It stays in
`dg doctor`, where it runs once on request and the question it answers is the
actual point.

**Rejected:** CCR and LiteLLM in Anthropic mode (translation layers that are
not needed).

## ADR 2: one stable client endpoint, request-boundary failover

Claude Code constructs its client from `ANTHROPIC_BASE_URL` at startup. It is
therefore pointed at one stable loopback URL for its lifetime:

```
Claude Code -> 127.0.0.1:8787 -> Anthropic
                               -> local Qwen
                               -> Oracle
```

The router never changes upstream after response bytes have been sent. It may
buffer an error response, however, and transparently try the next tier. This is
especially important for Qwen's structured
`Failed to generate a valid tool call` failure: Oracle receives the same
request before Claude sees the failed response.

`dg launch` is deliberately thin: it starts the router if needed, adds the
`/client/launch` identity prefix, and launches stock Claude once. Terminal
Claude installed with `dg install --proxy` uses `/client/cli`. These identities
make terminal routing observable without changing the Anthropic API path
forwarded upstream.

The Claude banner can continue to say “Sonnet 4.6.” It describes the model the
client selected, not which upstream actually served the request. The router's
health snapshot is authoritative.

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

The LM Studio probe reads `/api/v0/models` for residency and context. Loading
is attempted only when the tier is actually selected, never during ordinary
lane inspection.

**Model size.** The current default is `qwen/qwen3.5-9b` at 65536 context,
matching the station profile so supervisor and Station do not force a model
swap. The choice remains configurable.

**One model slot.** LM Studio holds a single model resident. A LOCAL
supervisor and a cc-delegate `station-*` profile therefore evict each other --
observed live as a cc-delegate model-gate timeout while the supervisor model
was loading, which failed that delegated task. `dg doctor` and `dg launch`
detect the shared endpoint and warn; the Governor does not try to serialise
another tool's worker. While the supervisor is LOCAL, delegate to Codex or an
`oracle-*` profile.

## ADR 2c: two fallback tiers, tried independently in order

`supervisorFallbacks` is an ordered list; the router tries each usable tier at
the request boundary. A cooldown prevents a failed endpoint being rediscovered
on every request.

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

OpenRouter is deliberately a **worker-only fourth lane**. It now supports both
OpenAI-compatible calls and an Anthropic Messages endpoint, but placing it in
`supervisorFallbacks` would silently turn supervisor traffic into metered API
usage. The worker lane instead uses the explicit `openrouter-coder`
cc-delegate profile and an independent capacity reservation. Its availability
probe calls `/api/v1/key`, not a model, so health and remaining key limit cost
no inference tokens.

## ADR 2d: personal Desktop cannot use the request router

Desktop bundles its own Claude process, so it cannot be wrapped by
`dg launch`. Measurement also showed that Desktop supplies its own base URL and
does not honor the terminal/user environment override reliably.

* Desktop bundles its own `claude.exe` and spawns sessions as children, so
  nothing external can wrap or relaunch it;
* Desktop never invokes the statusLine hook (cleared `claudeQuota` stayed unset
  across many turns), the `UserPromptSubmit` payload carries no `rate_limits`,
  and transcripts hold no quota telemetry either.

Personal Claude Desktop reports `ANTHROPIC_BASE_URL` as managed and rejects a
Local-environment override. Therefore Desktop uses the Governor's hooks,
ledger, Codex plugin, and cc-delegate workers, but not this proxy. The proxy is
supported only for terminal Claude Code; enterprise-managed gateway modes are
outside this project's tested scope.

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
* **Anthropic returns quota on terminal proxy responses**
  (`anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}`). Harvesting it
  restores SAVE mode for routed terminal sessions at zero inference cost.

Wiring terminal Claude is opt-in (`dg install --proxy`) because it sets a
persistent user and settings URL. A managed Python runtime, an ONLOGON task,
and the SessionStart hook keep the proxy available. `dg uninstall` restores the
previous terminal endpoint. Desktop continues to use worker delegation only.

## ADR 2e: lanes are machines, and tasks are classified

Capacity was keyed by *tool* (`codex: 1`, `cc-delegate: 1`), which gave the GPU
box and the Oracle VM a single shared slot even though they are different
computers -- the VM sat idle whenever the GPU was busy. Routing was also purely
availability-based, so Codex took everything and both local machines idled
until Codex died.

Now capacity is keyed by the resource that is actually scarce:

| Lane | Machine | Slots |
|---|---|---|
| `codex` | cloud | 1 |
| `station` | GPU box, one resident model | 1 |
| `oracle` | CPU VM | 2 |
| `openrouter` | metered cloud API | 1 |

and tasks carry a class (`tiny/simple/standard/hard`) that selects a lane
preference before availability is considered. Claude sets the class when
decomposing the work -- no heuristic can judge difficulty, and guessing it from
path counts would be exactly the kind of cleverness v1 avoids.

The `station` lane is excluded automatically while the supervisor is running on
that box, which is the contention that failed a real delegated task earlier.

`dg fill` reserves one READY task in every free lane in one pass. Codex starts
through the official plugin immediately; Station, Oracle, and OpenRouter return
independent MCP handoffs that should be submitted without waiting for another
lane. The per-repository write cap is four so all four can be active when their
owned paths do not overlap.

Schema v4 adds the OpenRouter lane and extends old routing tables and default
capacity without discarding user ordering. Migrations are additive and preserve
user configuration, writing a timestamped backup first.

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

Integration is now evidence-based. `dg integrate` snapshots loose worker output
and refuses to mark a WRITE task `INTEGRATED` until the branch is reachable
from HEAD or every worker patch id is present after a cherry-pick. An unusual
reviewed squash/manual copy requires `--accept-equivalent --note`. Cleanup runs
only after that proof, so it cannot delete the sole copy of unintegrated work.

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

## ADR 7: Codex is driven through the official Claude plugin

The Codex lane launches `codex@openai-codex`'s
`codex-companion.mjs task --background --fresh --json` transport. The Governor
creates the isolated worktree and bounded prompt; the plugin owns the Codex
thread, process lifecycle, log, cancellation, and durable result. The same job
is visible to `/codex:status`.

Governor-launched companion jobs receive a stable `CLAUDE_PLUGIN_DATA` under
the Governor state directory. Reconciliation also recognizes the plugin's
older Claude-data and OS-temp layouts so jobs survive an upgrade between
layouts. The prompt carries a task/attempt marker, allowing
`dg recover-handoffs` to reconnect a job when launch succeeded but its
acknowledgement was lost.

There is no automatic `codex exec` substitute. If the official plugin is
missing, disabled, incompatible, or cannot authenticate, the Codex lane is
reported down and other configured lanes remain independently usable.

## ADR 7b: cc-delegate is read, never driven

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
