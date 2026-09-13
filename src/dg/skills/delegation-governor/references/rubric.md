# What to delegate

> Delegate when the work is both **specifiable** and **worth** delegating.

Specifiable: you could write the acceptance criteria without another round of
questions. Worth it: the work is bigger than the cost of describing it.

**Track record, stated plainly so it can't be waved away as theoretical:** in
the session that built most of this system, the supervisor delegated a
handful of tasks and then hand-wrote nearly everything else directly --
including a multi-file cc-delegate fork/rename job (dozens of files, a
mechanical rename with precisely statable constraints) that belonged to a
worker and never went to one. That is the failure this rubric now exists to
make harder to repeat.

## The gate (apply BEFORE starting, not as a retrospective excuse)

Before writing or editing a **third file** in one task, or before any
rename/move/mechanical-transform touching **2 or more files**, stop and
answer in one sentence: *why is this not a work order?*

If the honest answer is one of the ones below, it is not a valid answer --
delegate instead:

- "It needs precision" -- precision is what acceptance criteria and named
  constraints are *for*. Write them down; that is the job, not a reason to
  skip the worker.
- "It's high-stakes / could break something live" -- that is a reason to
  fence the work order tighter (name the exact files it may touch, name what
  it must never touch), not a reason to do it yourself. "Never touch
  `~/.delegation-governor/`" is one sentence in a work order.
- "It's faster if I just do it" -- almost always true in the moment, and
  exactly the shortcut that makes the Governor pointless. The cost that
  matters is supervisor tokens and context, not wall-clock time on this one
  task.
- "It's mostly mechanical, hardly worth a work order" -- mechanical-and-wide
  is the single best case for delegating, not an exemption from it.

A valid reason looks like one of the **Keep** items below, named specifically
-- not a vague sense that this particular instance is somehow different.

## Delegate

- multi-file implementation of an architecture you already chose
- mechanical edits, migrations, codemods, dependency bumps
- renames and moves across more than one file, however precise the mapping
- writing tests; test/fix loops
- straightforward refactors with a clear end state
- searching many files and summarising the findings (`--mode READ_ONLY`)
- documentation generated from repository state
- bounded diagnosis: "find why X fails, don't fix it"
- independent code review of a change you already made

## Keep

- ambiguous requirements, and anything needing another question to the user
- architecture and design choices; trade-offs -- the *decision*, not the
  edits that implement it once the decision is made
- dangerous or irreversible changes themselves (e.g. the actual `rm`, the
  actual force-push) -- but writing the surrounding mechanical change that
  leads up to that decision point is still delegatable
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
belongs to a worker. If you find yourself justifying "yes, but this one
time..." -- that sentence is the tell. Delegate it.
