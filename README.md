# Delegation Governor

Keeps Claude Code as supervisor, pushes implementation to Codex (falling back
to `cc-delegate`), and keeps a dependency-aware ledger so Claude never sits
idle waiting on a worker. When Anthropic quota runs out it relaunches the same
session against LM Studio and brings it back when the window resets.

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
   so no router or translation layer is needed - CCR was dropped.
2. **Claude Code fixes its backend at process start** from `process.env`, so
   failover is a process lifecycle, not a runtime switch. `dg launch`
   supervises that lifecycle and switches only at Claude Code's own exit,
   which is the one boundary where no tool call can be interrupted.

## Requirements

Windows/macOS/Linux, git, Python 3.11+ (via `uv`), Claude Code 2.1+.
Optional: Codex CLI logged in; cc-delegate plugin; LM Studio for local
supervision. Missing pieces degrade gracefully - `dg doctor` says what is
absent and what it costs you.

## Install

```bash
git clone <this repo> && cd delegation-governor
uv venv --python 3.14 && uv pip install -e .
uv tool install --force .          # puts `dg` on PATH

dg install --dry-run               # show every change first
dg install
dg doctor
```

`dg install` backs up `~/.claude/settings.json` to `~/.claude/backups/` with a
timestamp, then **merges**: existing hooks, permissions, plugins and model
settings are preserved. An existing statusline is stashed and restored on
uninstall. `cc-delegate` is never modified.

It adds exactly three integration points:

| Where | What | Cost |
|---|---|---|
| `statusLine` | `dg hook statusline` | zero tokens; also feeds Claude quota in |
| `UserPromptSubmit` | `dg hook prompt` | one short line, silent when idle |
| `StopFailure` (`rate_limit`) | `dg hook stopfailure` | records the hard limit |

plus the skill at `~/.claude/skills/delegation-governor/`.

## Use

```bash
dg launch                     # lifecycle-supervised session (recommended)
claude                        # stock Claude Code, Governor bypassed
```

Inside a session, Claude drives the ledger itself via the skill. By hand:

```bash
A=$(dg add "backend endpoint"  --repo . --path 'src/api/**')
C=$(dg add "integration tests" --repo . --path 'tests/**' --depends-on $A)

dg tasks ready                # what can run now, best first
dg dispatch $A                # claim + start a worker, returns immediately
dg tasks                      # A RUNNING, C BLOCKED
dg collect $A                 # result + which tasks just unblocked
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
| `dg add` / `dg set` | create / force a status |
| `dg workorder <id>` | render the bounded contract |
| `dg dispatch <id>` | claim + start, non-blocking |
| `dg attach <id> <cc-task-id>` | register a cc-delegate job |
| `dg collect <id>` / `dg integrate <id>` | result / accept (`--cleanup` keeps unmerged branches) |
| `dg fallback <id>` | clean retry after a failure |
| `dg worker-status` / `dg logs [<id>]` | live attempts / worker logs |
| `dg probe codex\|claude` | confirm a provider really recovered |
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
    "codex":       { "maxWriteJobsPerRepo": 1 },
    "cc-delegate": { "maxWriteJobsPerRepo": 1 },
    "totalWriteJobsPerRepo": 2,
    "maxReadOnlyJobs": 3,
    "slowAfterSeconds": 900,                   // flagged SLOW, never killed
    "hardTimeoutSeconds": 0,                   // 0 = no hard kill
    "minCheckSpacingSeconds": 60
  },
  "lmstudio": {
    "baseUrl": "http://127.0.0.1:1234",
    "model": "openai/gpt-oss-20b",             // 12GB: fits alongside 128k ctx
    "smallModel": "openai/gpt-oss-20b",
    "tokenEnvVar": "LMSTUDIO_API_KEY",         // name only, never the value
    "contextLength": 131072,                   // loaded via `lms load -c`
    "minContextLength": 40960,                 // Claude Code's prompt is ~34k
    "loadTimeoutSeconds": 600,
    "ttlSeconds": 3600,
    "autoLoad": true                           // false = you manage residency
  }
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

- Path ownership: two WRITE tasks with overlapping globs in the same repo never
  run together. A task with no declared paths owns the whole repo.
- Capacity: per-worker and per-repo write limits, plus a read-only cap.
- Atomic claiming: `BEGIN IMMEDIATE`, so two sessions cannot claim one task.
- WRITE work runs in a git worktree **outside** your repo, branched from the
  current commit. Your working tree is never touched, not even on failure.
- Cleanup never loses work: loose worker output is committed to its branch, and
  an unmerged branch is kept (with a note) unless you pass `--discard`.

## Quota behaviour

| Situation | What happens |
|---|---|
| Codex healthy | Codex gets the work |
| Codex exhausted | cc-delegate immediately; **no Codex request wasted** |
| Codex quota mid-task | attempt `QUOTA_FAILED`, reset captured, partial work preserved, `dg fallback` retries clean |
| Codex auth/network error | distinguished from quota; cooldown; actionable warning |
| Codex reset passes | probed at zero inference; READY only on confirmation |
| Claude at SAVE threshold | delegate harder; same backend |
| Claude hard rate limit | hook records it; next exit relaunches on LM Studio, same session |
| Anthropic recovers | probed for real; same session relaunched on Anthropic |
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
| `dg launch` refuses to start | LM Studio unreachable or its model will not load while LOCAL; the message lists the fixes |
| `exceed_context_size_error` on LOCAL | the model is loaded with too little context; `dg launch` reloads it, or `lms load <model> -c 131072 -y` |
| cc-delegate times out while LOCAL | both share one LM Studio model slot - see the `dg doctor` warning; delegate to Codex or an `oracle-*` profile instead |
| Worker seems hung | `dg worker-status` - `SLOW` is normal for a cold local model |

## Upgrade

```bash
git pull && uv pip install -e . && uv tool install --force .
dg install          # re-merges hooks idempotently, backs up again
dg doctor
```

The database migrates forward automatically and refuses to open a schema newer
than the installed `dg` rather than corrupting it.

## Rollback

```bash
dg uninstall              # removes hooks + skill, restores previous statusline
dg uninstall --purge      # also deletes the ledger and logs
```

Or just run `claude` - the Governor is bypassed entirely. `cc-delegate` and
every other plugin remain untouched either way. Timestamped settings backups
stay in `~/.claude/backups/`.

## Development

```bash
dg test                   # or: PYTHONPATH="src;tests" python -m unittest discover -s tests -t tests
```

Standard library only, no runtime dependencies. Tests use fake workers, fake
Codex streams, a fake Claude Code process and temporary git repos - no real
quota is ever consumed.
