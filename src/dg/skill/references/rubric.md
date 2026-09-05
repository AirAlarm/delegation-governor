# What to delegate

> Delegate when the work is both **specifiable** and **worth** delegating.

Specifiable: you could write the acceptance criteria without another round of
questions. Worth it: the work is bigger than the cost of describing it.

## Delegate

- multi-file implementation of an architecture you already chose
- mechanical edits, migrations, codemods, dependency bumps
- writing tests; test/fix loops
- straightforward refactors with a clear end state
- searching many files and summarising the findings (`--mode READ_ONLY`)
- documentation generated from repository state
- bounded diagnosis: "find why X fails, don't fix it"
- independent code review of a change you already made

## Keep

- ambiguous requirements, and anything needing another question to the user
- architecture and design choices; trade-offs
- dangerous or irreversible changes
- one-line fixes, where writing the order costs more than the edit
- final review and the decision to integrate
- talking to the user

## Mode thresholds

| Supervisor | Delegate when |
|---|---|
| `NORMAL` | the work is more than a few minutes of typing |
| `SAVE` | anything specifiable, including work you would normally just do |
| `LOCAL` | as SAVE; you are on a smaller model, so lean on workers harder and keep your own reasoning short |

## The honest test

Before doing implementation work yourself, ask: *could I write this as an
acceptance criterion instead?* If yes, and it is more than a few lines, it
belongs to a worker.
