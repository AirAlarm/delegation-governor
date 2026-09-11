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
git merge --no-ff dg/dg-4        # or cherry-pick the commit shown by collect
dg integrate DG-4 --cleanup      # verifies, marks INTEGRATED, retires worktree
```

`integrate` does **not** merge for you. The work is on branch `dg/dg-4`; merge
or cherry-pick it first. The command then verifies ancestry or patch identity
before it records the decision.

`--cleanup` runs only after verification and removes the retired worktree and
branch. If a reviewed squash or manual copy is semantically equivalent but
patch identity cannot prove it, use `--accept-equivalent --note "reason"` so
the exceptional judgement is explicit in the ledger.

Dependants become READY only after the parent is `INTEGRATED`. `SUCCEEDED`
means worker output exists and is ready for review; it is not yet safe input
for a dependent task.

## Not accepting the result

```bash
dg set DG-4 FAILED --reason "ignored the path constraint"
dg fallback DG-4                 # clean retry, original attempt kept visible
```

Never edit a worker's output into shape silently and call it integrated - the
ledger should show what actually happened.
