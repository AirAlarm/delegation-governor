---
name: delegation-governor
description: Route implementation work to Codex or cc-delegate instead of doing it yourself, and keep working while they run. Use when a task involves multi-file implementation, mechanical edits, migrations, codemods, test writing, broad repo search, or bounded diagnosis - and whenever a delegated job is already running and you are about to wait for it. Also use when the user mentions dg, the Governor, the task ledger, delegation, or asks why work is BLOCKED or which worker is active.
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
   state updates itself; `dg collect <id>` at a natural stopping point.
3. **A slow worker is not a failed worker.** A cold local model can take
   minutes. Never duplicate a task because it is slow.
4. **Keep the judgement, delegate the typing.** Architecture, trade-offs,
   ambiguity, dangerous changes, final review: yours. See `references/rubric.md`.
5. **Bounded work orders only.** Never paste conversation history to a worker.
   `dg workorder <id>` renders the contract; the worker reads the repo itself.
6. **Classify tasks and fill every lane.** Codex, the GPU box, the VM, and
   OpenRouter are four independent endpoints. `--class
   tiny|simple|standard|hard` decides preference; `dg fill` puts work in all
   available lanes instead of one at a time.

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
| `references/workers.md` | Codex vs cc-delegate, fallback, recovery |
| `references/review.md` | collecting, reviewing and integrating results |
| `references/recovery.md` | quota exhaustion, LOCAL supervisor, failures |
