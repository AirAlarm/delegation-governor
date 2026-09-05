# Ledger and scheduler

## States

Stored: `PLANNED QUEUED RUNNING SUCCEEDED FAILED QUOTA_FAILED AUTH_FAILED
SUPERSEDED CANCELLED INTEGRATED`

Computed, never stored: **`READY`** and **`BLOCKED`**. A `PLANNED` task is
READY when its dependencies are satisfied, no active write job owns
overlapping paths, and there is capacity. Otherwise BLOCKED, with a reason.

This is why you ask `dg tasks ready` instead of remembering the plan: the
answer is recomputed from the graph every time and cannot go stale.

## Building a graph

```bash
A=$(dg add "backend endpoint"   --repo . --path 'src/api/**')
B=$(dg add "update docs"        --repo . --path 'docs/**')
C=$(dg add "integration tests"  --repo . --path 'tests/**' --depends-on $A)
D=$(dg add "frontend copy"      --repo . --path 'web/**')
E=$(dg add "final review" --mode READ_ONLY --repo . --depends-on $A --depends-on $C)
```

`dg graph` prints the edges. `dg graph DG-4` prints just that task's closure.

## Dependency rules

- A dependency counts as satisfied only when `SUCCEEDED` or `INTEGRATED`.
- A dependency that `FAILED`, `QUOTA_FAILED`, `AUTH_FAILED` or was `CANCELLED`
  blocks its dependants **permanently**. Fix or `dg fallback` the parent; the
  child will not quietly unblock.
- A missing dependency blocks. It is never treated as satisfied.

## Path ownership

Two WRITE tasks in the same repo with overlapping paths never run together.

- `src/auth/**` vs `src/auth/**` - conflict.
- `src/**` vs `src/auth/login.py` - conflict, one contains the other.
- `src/backend/**` vs `docs/**` - fine, both can run.
- `src/auth/**` vs `src/authz/**` - fine, not a prefix match.
- **No paths declared** - owns the whole repo, conflicts with everything.

So always pass `--path`. It is what buys you parallelism.

## Priority

READY tasks are ordered: explicit `--priority`, then how many tasks each
unlocks (critical path), then age. Deterministic, no cleverness.

A task that unlocks three others outranks one that unlocks none - but the
second is still READY and can run alongside it if capacity allows.

## Capacity (configurable)

| Limit | Default |
|---|---|
| Codex WRITE jobs per repo | 1 |
| cc-delegate WRITE jobs per repo | 1 |
| total WRITE jobs per repo | 2 |
| READ_ONLY jobs | 3 |
