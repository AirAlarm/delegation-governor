# Collecting, reviewing, integrating

## Don't interrupt yourself

A finished worker is not an interrupt. Reach a natural stopping point in what
you are doing, then collect.

## Collect

```bash
dg collect DG-4
```

Returns status, every attempt, the summary, the changed file list, a path to
the diff, and `unblockedNow` - the tasks that just became READY.

## Review

Read the diff, not the worker's summary of the diff.

- Does it meet the acceptance criteria you wrote?
- Did it stay inside its declared paths?
- Did it change anything you did not ask for?
- Do the tests actually run, and did they pass for the right reason?

You own this judgement. A worker reporting success is a claim, not evidence.

## Integrate

```bash
dg integrate DG-4 --cleanup      # marks INTEGRATED, removes the worktree
```

The work is in a worktree on branch `dg/dg-4`. Merge, cherry-pick or copy it
into your tree as the situation needs - that is a decision, so it is yours.
Integrate before `--cleanup`: cleanup deletes the branch.

Dependants become READY the moment the parent is `SUCCEEDED`; `INTEGRATED`
also satisfies them.

## Not accepting the result

```bash
dg set DG-4 FAILED --reason "ignored the path constraint"
dg fallback DG-4                 # clean retry, original attempt kept visible
```

Never edit a worker's output into shape silently and call it integrated - the
ledger should show what actually happened.
