# Delegation Governor

Keeps Claude Desktop or Claude Code as supervisor, pushes implementation through
the official Codex Claude plugin or `cc-delegate`, and keeps a dependency-aware
ledger so Claude never sits idle waiting on a worker. Codex, local Qwen, Oracle,
and OpenRouter are independent worker lanes and can all run at once.

The optional loopback request router is for terminal Claude Code only. Personal
Claude Desktop manages its own `ANTHROPIC_BASE_URL`, so Desktop uses the task
Governor but does not receive automatic supervisor-model fallback.

Stock `claude` is never shadowed. Everything installs additively and reverses.

```
dg status
SUP  CLAUDE_NORMAL  within thresholds
     5h 12%   7d 19%   override=auto
WRK  codex          codex ready
     codex=CODEX_READY   override=auto
TASK BLOCKED:1  READY:2  RUNNING:1
```

## Architecture

Three independent state machines. See [docs/architecture.md](docs/architecture.md)
for the decisions and what was verified rather than assumed.

```
            Delegation Governor

 SUPERVISOR          WORKER            SCHEDULER
 Claude / LM Studio  Codex / cc-deleg  ledger + graph
 NORMAL SAVE         READY EXHAUSTED   READY RUNNING
 LOCAL PROBE         AUTH NET UNKNOWN  BLOCKED ...
```

- **Supervisor** - Anthropic quota arrives free via the statusline hook. Past
  configurable thresholds Claude delegates harder (`SAVE`) or moves to LM
  Studio (`LOCAL`).
- **Worker** - Codex quota is read from its app-server at zero inference cost.
  Known-exhausted means cc-delegate is chosen without spending a Codex request
  to rediscover it. Recovery is automatic and confirmed, never assumed.
- **Scheduler** - `READY`/`BLOCKED` are computed from the dependency graph,
  path ownership and capacity on every read, so they cannot go stale.

Two facts shaped the design, both verified live:

1. **LM Studio already serves the Anthropic Messages API** at `/v1/messages`,
   so the router forwards requests without a translation layer.
2. **Claude Code fixes its base URL at process start.** It therefore points at
   one stable loopback URL; the router changes upstream only between requests.
   Fallbacks are tried independently in order: local Qwen, then Oracle.

## Requirements

The Claude Code plugin works on macOS, Linux, and Windows with git, `uv`, and
Python 3.11+. The managed `dg install` flow and optional proxy require Windows.
The official Codex Claude plugin (`codex@openai-codex`) must be authenticated to
use the Codex lane. cc-delegate, LM Studio, Oracle, and OpenRouter are optional
lanes; missing pieces are marked unavailable rather than silently replaced with
a different transport.

## Install

```bash
git clone <this repo> && cd delegation-governor
uv venv --python 3.14 && uv pip install --python .venv/Scripts/python.exe -e .

dg install --dry-run               # show every change first
.venv/Scripts/dg install --proxy   # stable runtime, PATH shim, hooks, router
dg doctor --force
```

`dg install` backs up `~/.claude/settings.json` to `~/.claude/backups/` with a
timestamp, then **merges**: existing hooks, permissions, plugins and model
settings are preserved. An existing statusline is stashed and restored on
uninstall.

It also re-applies the **cc-delegate station patch**, which the Governor owns
(see below). Your cc-delegate profiles and credentials are never touched.

### Install as a Claude Code plugin

For the cross-platform, worker-delegation-only plugin path, clone the repository
and point Claude Code at its root:

```bash
git clone <this repo>
claude --plugin-dir /absolute/path/to/delegation-governor
```

The plugin uses `uv` to run its prompt and hard-routing hooks directly from the
checkout. On the first session start it also installs the editable `dg` tool so
commands such as `dg status`, `dg quickread`, and `dg safewrite` are available
from a shell (provided the uv tool bin directory is on `PATH`).

This plugin path intentionally excludes the proxy and supervisor-fallback
subsystem. That remains a separate, Windows-only manual setup using
`dg install --proxy`; it is neither started nor configured by the plugin.

### The cc-delegate station patch

cc-delegate is a Claude Code plugin, so `claude plugin update` replaces its
whole install directory and silently drops the local edits that make the
station lane work: the LM Studio model gate (context length, TTL, load
timeouts), `api_base` threading so a worker can reach LM Studio at all, and an
`mcp<2` pin without which the MCP server fails to start.

Those edits are vendored here and re-applied on install, so an update degrades
loudly rather than mysteriously:

```bash
dg ccdelegate            # is the install still patched, and with our gate?
dg ccdelegate --apply    # re-apply (idempotent; backs up what it replaces)
```

`dg doctor` warns on drift. Gate tuning: 65536 context, 4h model TTL, 600s load
budget with a settle delay before the first poll — a cold load returns before
the weights are mapped, so polling immediately wastes the budget.

**After applying, restart the Claude Code session** — the cc-delegate MCP server
imports the gate once at startup and caches it.

### OpenRouter lane

OpenRouter is a worker lane, not a Claude supervisor fallback. Configure the
cc-delegate profile once through its MCP configuration tools:

```text
set_model_profile(
  name="openrouter-coder",
  model="litellm:openrouter/qwen/qwen3-coder-next",
  api_key_env_var="OPENROUTER_API_KEY",
  api_base="https://openrouter.ai/api/v1"
)
store_api_key(profile="openrouter-coder")  # omit key; enter it in the secure prompt
```

The model is deliberately profile-owned: replace it with any OpenRouter model
whose price and tool-calling quality suit you. `dg lanes` checks that the
profile exists, validates the API key through OpenRouter's non-inference key
endpoint, and reports an exhausted key spending limit as unavailable. The key
is never stored in Governor configuration or logs.

It adds exactly three integration points:

| Where | What | Cost |
|---|---|---|
| `statusLine` | `dg hook statusline` | zero tokens; also feeds Claude quota in |
| `UserPromptSubmit` | `dg hook prompt` | one short line, silent when idle |
| `StopFailure` (`rate_limit`) | `dg hook stopfailure` | records the hard limit |

plus the skill at `~/.claude/skills/delegation-governor/`.

## Use

```bash
claude                        # routed after `dg install --proxy`
dg launch                     # same router, explicit /client/launch identity
```

Inside a session, Claude drives the ledger itself via the skill. By hand:

```bash
A=$(dg add "backend endpoint"  --repo . --path 'src/api/**')
C=$(dg add "integration tests" --repo . --path 'tests/**' --depends-on $A)

dg tasks ready                # what can run now, best first
dg dispatch $A                # claim + start a worker, returns immediately
dg tasks                      # A RUNNING, C BLOCKED
dg collect $A                 # review branch/commit and changed files
git merge --no-ff dg/dg-1     # or cherry-pick the reported commit
dg integrate $A --cleanup
```

### Command reference

| | |
|---|---|
| `dg status [--json]` | supervisor, worker, task counts |
| `dg quota [--force]` | both quotas, every window, reset credits |
| `dg route` | which worker would be chosen, and why |
| `dg doctor` | environment health |
| `dg tasks [ready\|running\|blocked]` | the ledger |
| `dg show <id>` / `dg graph [<id>]` | one task / dependency edges |
| `dg add --class tiny\|simple\|standard\|hard` | create a task, sized to a lane |
| `dg lanes` | machines: capacity, availability, which classes go where |
| `dg fill` | start one READY task in **every** free lane |
| `dg set` | force a status |
| `dg workorder <id>` | render the bounded contract |
| `dg dispatch <id>` | claim + start, non-blocking |
| `dg attach <id> <cc-task-id>` | register a cc-delegate job |
| `dg collect <id>` / `dg integrate <id>` | result / verify an already merged or cherry-picked result |
| `dg release <id>` / `dg cancel <id>` | release a handoff or cancel active work |
| `dg recover-handoffs` | reconnect jobs launched across a CLI crash |
| `dg fallback <id>` | clean retry after a failure |
| `dg worker-status` / `dg logs [<id>]` | live attempts / worker logs |
| `dg probe codex\|claude` | confirm a provider really recovered |
| `dg ccdelegate [--apply]` | cc-delegate station patch: drift check / re-apply |
| `dg override supervisor\|worker <v>` / `dg clear-override` | manual control |
| `dg launch` / `dg test` / `dg install` / `dg uninstall` | |

## Configuration

`~/.claude/delegation-governor/config.json`, created on first run.

```jsonc
{
  "supervisor": {
    "fiveHour": { "save": 70, "local": 92 },   // % utilization
    "sevenDay": { "save": 80, "local": 95 },
    "quotaStaleSeconds": 3600
  },
  "codex": {
    "quotaTtlSeconds": 300,
    "exhaustedPercent": 99,
    "networkCooldownSeconds": 900,
    "authCooldownSeconds": 3600,
    "blockingLimitIds": ["codex"]              // gpt-reserve never blocks
  },
  "workers": {
    "lanes": {
      "codex":  { "worker": "codex", "maxWriteJobs": 1 },
      "station": { "worker": "cc-delegate", "profile": "station-main", "maxWriteJobs": 1 },
      "oracle":  { "worker": "cc-delegate", "profile": "oracle-coder", "maxWriteJobs": 2 },
      "openrouter": { "worker": "cc-delegate", "profile": "openrouter-coder",
                      "endpoint": "openrouter", "maxWriteJobs": 1 }
    },
    "classRouting": {
      "hard": ["codex", "openrouter", "station"],
      "standard": ["openrouter", "codex", "station", "oracle"],
      "simple": ["station", "openrouter", "oracle", "codex"],
      "tiny": ["oracle", "station", "openrouter"]
    },
    "totalWriteJobsPerRepo": 4,
    "maxReadOnlyJobs": 4,
    "slowAfterSeconds": 900,                   // flagged SLOW, never killed
    "hardTimeoutSeconds": 0,                   // 0 = no hard kill
    "minCheckSpacingSeconds": 60
  },
  // ordered: tried in turn when Anthropic is out
  "supervisorFallbacks": [
    { "name": "lmstudio", "kind": "lmstudio",       // GPU box: fast, one model slot
      "baseUrl": "http://127.0.0.1:1234",
      "model": "qwen/qwen3.5-9b",
      "smallModel": "qwen/qwen3.5-9b",
      "tokenEnvVar": "LMSTUDIO_API_KEY",            // name only, never the value
      "contextLength": 65536,                       // loaded via `lms load -c`
      "minContextLength": 40960,                    // Claude Code's prompt is ~34k
      "loadTimeoutSeconds": 600, "ttlSeconds": 14400,
      "autoLoad": true },                           // false = you manage residency
    { "name": "oracle", "kind": "remote",           // always-on VM: slow CPU ARM
      "baseUrl": "https://claude-llm.vibecodelabs.org",
      "model": "oracle-smart · gemma-4 26b",
      "smallModel": "oracle-fast · gemma-4 e2b",
      "tokenEnvVar": "ORACLE_LLM_API_KEY",
      "tokenFile": "~/.cc-delegate/credentials.json",
      "tokenFileKey": "ORACLE_LLM_API_KEY",
      "probeTimeoutSeconds": 20 }
  ]
}
```

## Task ledger

SQLite at `~/.claude/delegation-governor/governor.db` (WAL, schema-versioned),
shared safely by concurrent Claude sessions with atomic task claiming.

Stored statuses: `PLANNED QUEUED RUNNING SUCCEEDED FAILED QUOTA_FAILED
AUTH_FAILED SUPERSEDED CANCELLED INTEGRATED`. `READY` and `BLOCKED` are
computed, never stored.

Each task carries mode (`READ_ONLY`/`WRITE`), repo, base commit, owned path
globs, dependencies, priority and result/failure metadata. Each *attempt*
carries its worker, handle, worktree, log and error kind - so a task that
failed on Codex and succeeded on cc-delegate shows both. No credentials are
stored anywhere.

## Concurrency

Work is scheduled onto **lanes** -- machines, not tools:

| Lane | Machine | Slots | Takes |
|---|---|---|---|
| `codex` | cloud | 1 | `hard`, `standard`, `simple` |
| `station` | the GPU box (one resident model) | 1 | all classes |
| `oracle` | a separate CPU VM | 2 | `tiny`, `simple`, `standard` |
| `openrouter` | metered cloud API | 1 | `standard`, `hard`, overflow |

`station`, `oracle`, and `openrouter` all go through cc-delegate but have
independent capacity, so they run concurrently with each other and Codex. Tasks
carry a class (`--class`) and routing prefers the lane that suits them, falling
through when one is busy, unconfigured, out of credit, or down.

`dg fill` starts one READY task in every free lane at once.

- Path ownership: two WRITE tasks with overlapping globs in the same repo never
  run together. A task with no declared paths owns the whole repo.
- Capacity: per-lane slots plus a per-repo total, and a read-only cap.
- While the supervisor is running on the GPU box, the `station` lane is
  excluded automatically -- they would evict each other's model.
- Atomic claiming: `BEGIN IMMEDIATE`, so two sessions cannot claim one task.
- WRITE work runs in a git worktree **outside** your repo, branched from the
  current commit. Your working tree is never touched, not even on failure.
- `dg integrate` refuses to mark WRITE work integrated until the worker branch
  is an ancestor of HEAD or all of its patch IDs are present. After that,
  `--cleanup` can safely remove its worktree and branch. A reviewed manual copy
  needs `--accept-equivalent --note "..."`.

## Quota behaviour

| Situation | What happens |
|---|---|
| Codex healthy | Codex gets the work |
| Codex exhausted | cc-delegate immediately; **no Codex request wasted** |
| Codex quota mid-task | attempt `QUOTA_FAILED`, reset captured, partial work preserved, `dg fallback` retries clean |
| Codex auth/network error | distinguished from quota; cooldown; actionable warning |
| Codex reset passes | probed at zero inference; READY only on confirmation |
| Claude at SAVE threshold | delegate harder; same backend |
| Claude hard rate limit | current failed response is recorded; the next request tries Qwen, then Oracle |
| Qwen malformed tool call / retryable failure | response is buffered and retried on Oracle before bytes reach Claude |
| Anthropic recovers | a real probe clears the limit; a later request returns to Anthropic |
| Reset credits available | shown in `dg quota`, **never consumed automatically** |

## Troubleshooting

| Symptom | Do this |
|---|---|
| `dg` not found | `uv tool install --force .`; check `~/.local/bin` is on PATH |
| statusline shows `DG error: X` | `dg doctor`; the session is unaffected |
| No Claude quota shown | normal until the first API response; needs the statusline hook installed |
| `codex rate limits: CODEX_PROTOCOL_ERROR` | Codex CLI changed; `codex app-server generate-json-schema --out /tmp/s` and compare |
| Codex stuck exhausted | `dg quota --force`, then `dg probe codex` |
| Task stuck BLOCKED | `dg show <id>` - the `reason` field names the cause |
| Everything BLOCKED on capacity | raise `totalWriteJobsPerRepo`, or integrate finished work |
| Claude still says “Sonnet 4.6” | expected: that banner is the client-selected model, not proof of which upstream served a request; inspect `dg proxy --status` |
| Desktop rejects `ANTHROPIC_BASE_URL` | expected on personal Desktop; use worker delegation there, and reserve the router for terminal Claude Code |
| `exceed_context_size_error` on LOCAL | load the configured LM Studio model with at least the configured context |
| cc-delegate times out while LOCAL | both share one LM Studio model slot - see the `dg doctor` warning; delegate to Codex or an `oracle-*` profile instead |
| Worker seems hung | `dg worker-status` - `SLOW` is normal for a cold local model |

## Upgrade

```bash
git pull && uv pip install --python .venv/Scripts/python.exe -e .
.venv/Scripts/dg install --proxy  # rebuilds the managed runtime and re-merges hooks
dg doctor
```

The database migrates forward automatically and refuses to open a schema newer
than the installed `dg` rather than corrupting it.

## Rollback

```bash
dg uninstall              # removes hooks + skill, restores previous statusline
dg uninstall --purge      # also deletes the ledger and logs
```

`dg uninstall` removes the persistent router URL, so subsequent `claude`
sessions go directly to their previous endpoint. `cc-delegate` and every other
plugin remain untouched. Timestamped settings backups stay in
`~/.claude/backups/`.

## Development

```bash
dg test                   # or: PYTHONPATH="src;tests" python -m unittest discover -s tests -t tests
```

Standard library only, no runtime dependencies. Tests use fake workers, fake
Codex streams, a fake Claude Code process and temporary git repos - no real
quota is ever consumed.
