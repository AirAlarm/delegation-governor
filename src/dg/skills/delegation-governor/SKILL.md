---
name: delegation-governor
description: Route implementation work to Codex or dg-worker instead of doing it yourself, and keep working while they run. Use when a task involves multi-file implementation, mechanical edits, migrations, codemods, test writing, broad repo search, or bounded diagnosis - and whenever a delegated job is already running and you are about to wait for it. Also use when the user mentions dg, the Governor, the task ledger, delegation, or asks why work is BLOCKED or which worker is active.
---

# Delegation Governor

You are the supervisor. Workers implement; you decide, review and integrate.

The `dg` CLI holds the state. Read it, don't remember it.

## The loop

```
PLAN -> LEDGER -> DISPATCH -> DON'T WAIT -> OTHER READY WORK
     -> COLLECT -> REVIEW -> MERGE/CHERRY-PICK -> INTEGRATE -> UNBLOCK -> REPEAT
```

## Rules that matter most

1. **Never idle.** After dispatching, run `dg tasks ready` and start the top
   item. Only wait when READY is genuinely empty and everything else is BLOCKED.
2. **Never busy-poll.** Do not re-check a worker to see if it finished. Worker
   state updates itself; `dg collect <id>` at a natural stopping point. If you
   tell the user "I'll check back once it's done," that sentence is only true
   if you actually start a background watcher (a `run_in_background` Bash
   loop polling `dg tasks running --json`) in the same turn -- saying it
   without doing it is a promise you can't keep, confirmed live when it
   didn't happen and the user had to prompt instead.
3. **A slow worker is not a failed worker.** A cold local model can take
   minutes. Never duplicate a task because it is slow.
4. **Keep the judgement, delegate the typing.** Architecture, trade-offs,
   ambiguity, dangerous changes, final review: yours. Before writing or
   editing a third file in one task, or any rename/move touching 2+ files,
   stop and name a specific `Keep` reason (see `references/rubric.md`) or
   delegate it. "It needs precision," "it's high-stakes," and "it's faster
   if I just do it" are not valid reasons -- they're the exact excuses a past
   session used to hand-write a multi-file fork job that should have been a
   work order. High-stakes means fence the work order tighter, not skip it.
5. **Bounded work orders only.** Never paste conversation history to a worker.
   `dg workorder <id>` renders the contract; the worker reads the repo itself.
6. **Classify tasks and fill every lane.** Codex, the GPU box, the VM,
   OpenRouter and OpenCode Go are five independent endpoints. `--class
   tiny|simple|standard|hard` decides preference; `dg fill` puts work in all
   available lanes instead of one at a time.
7. **Prefer the ephemeral patterns for small, one-off work.** `dg quickread`
   (multi-file reconnaissance) and `dg safewrite` (reference-based boilerplate)
   skip the ledger entirely -- cheaper than a full WRITE task for something
   that doesn't need review or integration. See `references/bulk_read.md` and
   `references/code_write.md`.
8. **Log every delegate/keep/takeover decision.** `dg decision --type
   {delegate,keep,takeover} --task "..." --reason "..."` (add `--task-id DG-N`
   when one exists). Most of a session's real work never becomes a ledger
   task at all -- this is the only record that can later answer whether the
   Governor actually saves supervisor tokens or just spends them on
   dispatch/diagnosis/redo overhead. Log the fork in the road, not every
   routine review-and-integrate afterward.

## Minimum commands

```bash
dg status                  # supervisor, worker, task counts
dg add "title" --class simple --repo . --path 'src/auth/**' --depends-on DG-3
dg tasks ready             # what to do right now, best first
dg lanes                   # which machines are free
dg fill                    # start work in EVERY free lane at once
dg dispatch DG-4           # or just one; returns immediately
dg collect DG-4            # result, once you need it
dg integrate DG-4 --cleanup
```

`dg <cmd> --json` everywhere output feeds a decision.

## When you are told SUP=SAVE

Delegate far more aggressively, including work you would normally just do.
Keep decisions and review; hand over all the typing.

## Details, loaded only when needed

| File | Read it when |
|---|---|
| `references/rubric.md` | deciding whether to delegate at all |
| `references/task-contract.md` | writing a work order |
| `references/scheduler.md` | tasks, dependencies, path ownership, priority |
| `references/workers.md` | Codex vs dg-worker, fallback, recovery |
| `references/review.md` | collecting, reviewing and integrating results |
| `references/recovery.md` | quota exhaustion, LOCAL supervisor, failures |
| `references/bulk_read.md` | delegating a multi-file read/question, no ledger task |
| `references/code_write.md` | delegating reference-based boilerplate generation |
