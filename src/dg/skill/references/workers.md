# Workers and lanes

A **lane** is an independently limited execution endpoint, not a tool. Four of
them can run at the same time:

| Lane | Machine | Good for |
|---|---|---|
| `codex` | cloud | `hard`, `standard` |
| `station` | the GPU box (one model resident) | `simple`, and anything |
| `oracle` | a separate CPU VM, slow | `tiny`, `simple` |
| `openrouter` | metered cloud API | `standard`, `hard`, overflow |

`station`, `oracle`, and `openrouter` are reached through cc-delegate but have
separate capacity, so they never contend with each other or Codex.

## Classify every task

```bash
dg add "port 14 call sites" --class simple --path 'src/api/**'
dg add "fix the auth race"  --class hard   --path 'src/auth/**'
```

`tiny | simple | standard | hard`. This is the whole point: a trivial edit sent
to `codex` burns the slot a hard task needs, and leaves both local boxes idle.
Guess `standard` only when you genuinely cannot tell.

## Fill every lane

```bash
dg fill              # start one READY task in each free lane
dg lanes             # who is busy, who is down, which classes go where
```

`dg fill` starts Codex work through the official Codex Claude plugin and hands back a work order for each
cc-delegate lane, with the profile to use and the `dg attach ... --lane` to run
afterwards. Submit those with `run_dev_task`, then attach.

`dg dispatch <id>` still does one task, choosing its lane the same way.

Preference is by class, then capacity, then availability -- so a busy or dead
lane falls through to the next rather than blocking.

## Codex

Started by `dg dispatch <id>` through `codex@openai-codex`'s companion runtime.
It returns immediately with the plugin job id and log path; `/codex:status`
sees the same job and the Governor reconciles the plugin's durable result.
There is deliberately no raw `codex exec` fallback: if the official plugin is
missing, disabled, incompatible, or unauthenticated, the Codex lane is down.

WRITE tasks get a git worktree outside your repo, branched from the current
commit. Your working tree is never touched.

## cc-delegate

cc-delegate is an MCP server, so **you** start it, not `dg`:

```bash
dg dispatch DG-4              # prints the work order when cc-delegate is chosen
```
then call the MCP tool `run_dev_task` with that work order as `spec`, and:
```bash
dg attach DG-4 <task-id-returned-by-run_dev_task> --attempt <attempt-id>
```

After that the Governor tracks it by reading cc-delegate's own job file - no
MCP calls, no polling, no tokens. Do not call `get_task_status` in a loop.

Its profiles (`station-main`, `oracle-coder`, `openrouter-coder`, and friends)
are still yours to choose; the Governor never edits cc-delegate configuration.
OpenRouter is down until `openrouter-coder` exists and its
`OPENROUTER_API_KEY` is available. That lane uses one metered slot and is never
selected as the Claude supervisor.

## Fallback

If Codex is already known exhausted, `dg dispatch` goes straight to
cc-delegate - **no Codex request is spent rediscovering it**. That is the
entire point of the quota cache.

If Codex dies of quota mid-task:

```
DG-42  QUOTA_FAILED          <- attempt preserved, partial work kept on disk
dg fallback DG-42            <- DG-42 becomes SUPERSEDED, a fresh task appears
```

The retry starts from the original clean base. Partial output from the failed
attempt is kept for diagnosis and never reused or auto-integrated.

## Recovery

Codex returns by itself: once the reset time passes, the next routing decision
probes the account (zero inference) and only marks it READY on confirmation.
You do nothing.

## Overrides

```bash
dg override worker codex        # or cc-delegate
dg clear-override
```
Forced mode is visible in `dg status` and the statusline.

Codex, Station, Oracle, and OpenRouter reservations are independent. Submit all
three cc-delegate handoffs immediately after `dg fill`; none waits for Codex or
another profile.
