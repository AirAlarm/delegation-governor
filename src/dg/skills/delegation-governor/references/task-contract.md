# Bounded work orders

A worker sees **only** the work order. Never the conversation, never your
reasoning, never the user's messages.

`dg workorder <id>` renders it. `dg dispatch <id>` renders and sends it.

## Shape

```
Task ID + mode (READ_ONLY | WRITE)
Goal
Repository + working directory + paths you own
Known current state
Constraints
Non-goals
Acceptance criteria
Required tests
Deliverable
```

## Writing a good one

```bash
dg dispatch DG-7 \
  --state "Handlers live in src/api/*.py; each duplicates the same auth check." \
  --constraint "Keep the existing decorator name; callers depend on it." \
  --non-goal "Do not touch the session store." \
  --acceptance "Every handler in src/api uses the shared decorator." \
  --acceptance "No behaviour change for unauthenticated requests." \
  --tests "python -m pytest tests/api -q"
```

**Known current state** is the highest-value field. It is the one thing the
worker cannot cheaply discover, and it stops it re-deriving what you already
know. Two or three sentences.

## Rules

- State facts, not narrative. "X is in file Y" beats "we were discussing X".
- Acceptance criteria must be checkable by the worker without asking you.
- Name the paths. Path ownership is what lets two write jobs run at once.
- Write it so a *different* worker could pick it up unchanged - a Codex task
  may be re-run on dg-worker after a quota failure, with no edits.
- No secrets, no tokens, no absolute paths outside the repo.

## Read-only orders

`--mode READ_ONLY` gets no worktree and must change nothing. Ask for findings
with file and line references, not opinions.
