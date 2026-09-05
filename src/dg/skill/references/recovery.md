# Recovery

## Codex out of quota

Automatic. Routing goes to cc-delegate; the reset time shows in `dg status`
and the statusline. Once it passes, the next decision probes the account at
zero inference cost and returns to Codex only on confirmation.

`dg quota` shows every window. Reset credits, if any, are displayed and
**never consumed automatically** - that needs an explicit instruction.

## Codex auth or network failure

These are not quota. `dg status` names which one. Both fall back to
cc-delegate and set a cooldown so the failure is not rediscovered before
every task. Auth needs you: tell the user to run `codex login`.

## Anthropic approaching its limit

`SUP=SAVE`: delegate aggressively, keep decisions and review. Nothing else
changes - you are still on Anthropic.

## Anthropic exhausted

`SUP=LOCAL`. Claude Code cannot change backend inside a running process, so
the switch happens when the session exits:

1. the `StopFailure` hook records the hard limit;
2. you finish or the user quits - no tool call is ever interrupted;
3. `dg launch` relaunches automatically against LM Studio, resuming the
   **same session id** with the local model selected explicitly.

The task ledger, running Codex jobs and running cc-delegate jobs all survive:
they live in the Governor's database and in the workers' own processes, not in
the session.

If the user started with plain `claude` rather than `dg launch`, there is no
supervisor to relaunch them - tell them to exit and run `dg launch`.

## Anthropic recovers

When the reset passes, the next `dg launch` decision probes Anthropic for
real and relaunches the same session back on first-party Claude. A timestamp
passing is never treated as recovery on its own.

## LM Studio unavailable while LOCAL

`dg launch` refuses to start against a dead backend and prints the recovery
path rather than restart-looping. Options: start LM Studio and load the
configured model, `dg override supervisor claude`, or `dg probe claude`.

## Escape hatches

```bash
dg override supervisor claude|local|auto
dg override worker codex|cc-delegate|auto
dg clear-override
dg doctor
claude                    # stock Claude Code, Governor bypassed entirely
```
