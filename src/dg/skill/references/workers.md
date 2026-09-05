# Workers

Preference: **Codex first, cc-delegate when Codex cannot serve.**

`dg route` says which one you would get and why. `dg dispatch` picks for you.

## Codex

Started by `dg dispatch <id>`. Returns immediately with a pid and a log path;
a detached runner owns the run and writes the outcome into the ledger itself.

WRITE tasks get a git worktree outside your repo, branched from the current
commit. Your working tree is never touched.

## cc-delegate

cc-delegate is an MCP server, so **you** start it, not `dg`:

```bash
dg dispatch DG-4              # prints the work order when cc-delegate is chosen
```
then call the MCP tool `run_dev_task` with that work order as `spec`, and:
```bash
dg attach DG-4 <task-id-returned-by-run_dev_task>
```

After that the Governor tracks it by reading cc-delegate's own job file - no
MCP calls, no polling, no tokens. Do not call `get_task_status` in a loop.

Its profiles (`station-main` and friends) are unchanged and still yours to
choose; the Governor never edits cc-delegate's configuration.

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
