# Recovery

## Codex out of quota

Automatic. Routing goes to dg-worker; the reset time shows in `dg status`
and the statusline. Once it passes, the next decision probes the account at
zero inference cost and returns to Codex only on confirmation.

`dg quota` shows every window. Reset credits, if any, are displayed and
**never consumed automatically** - that needs an explicit instruction.

## Codex auth or network failure

These are not quota. `dg status` names which one. Both fall back to
dg-worker and set a cooldown so the failure is not rediscovered before
every task. Auth needs you: tell the user to run `codex login`.

## Anthropic approaching its limit

`SUP=SAVE`: delegate aggressively, keep decisions and review. Nothing else
changes - you are still on Anthropic.

## Anthropic exhausted

`SUP=LOCAL`. Claude Code remains attached to the loopback router; switching is
performed between requests:

1. the proxy or `StopFailure` hook records the hard limit;
2. the failed request ends normally;
3. the next request tries local Qwen, then Oracle if Qwen is unavailable or
   returns a retryable/malformed-tool-call failure.

The task ledger, running Codex jobs and running dg-worker jobs all survive:
they live in the Governor's database and in the workers' own processes, not in
the session.

Plain `claude` and `dg launch` both use the router after `dg install --proxy`.

## Anthropic recovers

When the reset passes, the router probes Anthropic for real and a later request
returns to it. A timestamp passing is never treated as recovery on its own.

## LM Studio unavailable while LOCAL

The router cools down the failed tier and tries Oracle. Options: start LM
Studio and load the configured model, force Anthropic with
`dg override supervisor claude`, or inspect `dg proxy --status`.

## Escape hatches

```bash
dg override supervisor claude|local|auto
dg override worker codex|cc-delegate|auto
dg clear-override
dg doctor
dg proxy --status
```
