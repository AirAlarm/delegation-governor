# Token audit — does the Governor save Claude's tokens? (field test 2026-09-13)

**The resource being saved is the Claude subscription's usage limits** (5-hour and weekly windows), not API spend.
Codex and OpenCode Go are also subscriptions with their own windows.

**Verdict for this session: no.** The Governor did not measurably save Claude subscription usage.

**Real limit usage was never recorded.** The Governor reads `rate_limits.five_hour/seven_day` from the
Claude Code statusline, but the desktop app never runs a statusline. `claudeQuota` was never written,
`dg status` showed "no quota data yet" all session, and SUP=SAVE could never trigger (F19).
Codex's windows *were* recorded: 44 % of its 5 h window and 11 % weekly were spent on this workload.

**Estimate instead.** Anthropic doesn't publish how cached context counts against subscription limits, so the
comparison uses three weightings of the same measured tokens. "Delegated" is the supervisor's
orchestration + review calls; "self" is the estimated cost of the supervisor writing the same 41.6 k tokens itself.

| Weighting | Delegation cost | Self-write estimate | Delegated ÷ self |
|---|---:|---:|---:|
| A. output tokens only (context is free) | 0.06 M | 0.06 M | **0.86×** (saves ~14 %) |
| B. API price ratios (cache read 0.1×, output 5×) | 2.10 M | 1.19 M | **1.77×** |
| C. every processed token equal | 16.4 M | 7.9 M | **2.06×** |

Under the one weighting where context is free, delegation saved ~14 % of the delegated slice.
Under any weighting where re-reading context counts, it cost ~1.8–2× more.

- **Main cost driver.** The supervisor's calls per delegated task (~8–10 here), each re-reading a ~190 k-token context.
- **What paid off.** Only the two largest deliverables (landing + shared CSS/JS, price page) saved under B.
- **Worker-side cost.** The workers' own windows (Codex 5 h: 44 %; OpenCode Go monthly) were spent on top.

## Method

- **Supervisor tokens:** measured from the session transcript
  (`~/.claude/projects/…/aab45e7c-….jsonl`): 166 unique Opus API calls, per-call usage.
- **Categories:** each call is labelled by the tools it emitted (heuristic keyword classifier, ±10 %).
- **Weighting:** "weighted" = input-equivalent tokens with Anthropic's price ratios
  (cache read 0.1×, 1 h cache write 2×, output 5×).
- **Worker tokens:** Codex runs on its own subscription and logs no tokens. cc-delegate reported
  `total_tokens` for 2 of its 4 jobs (1.67 M minimax-m3, 201 k qwen3.6-plus). The other two were lost
  when the MCP server restarted (F16).
- **Counterfactual** (supervisor writes it itself, same session):
  - `output×1.3×5` (write + edits)
  - `+ output×2` (cache write)
  - `+ output×90×0.1` (the written text stays in context for ~90 later calls)
  - `+ 3 verify calls × 21.5 k`

## Where the supervisor's tokens went

| Category | Calls | Output | Cache read | Weighted | Share |
|---|---:|---:|---:|---:|---:|
| Review worker output | 33 | 21.7 k | 7.71 M | 959 k | 23 % |
| Investigation / debug (plugin bugs, MCP, key) | 39 | 19.1 k | 6.11 M | 841 k | 20 % |
| Orchestrate (add/fill/handoff/attach/integrate/merge/watch) | 35 | 23.6 k | 6.07 M | 821 k | 20 % |
| Release (0.7.2, 0.7.3) | 14 | 11.9 k | 3.77 M | 475 k | 11 % |
| Website, own work (content, assets, brief) | 18 | 11.9 k | 2.09 M | 373 k | 9 % |
| Governor, own implementation + tests | 14 | 11.2 k | 2.54 M | 340 k | 8 % |
| Reports to user | 11 | 8.1 k | 2.08 M | 289 k | 7 % |
| Logging / memory | 2 | 1.6 k | 0.48 M | 58 k | 1 % |
| **Total** | **166** | **109 k** | **30.9 M** | **4.16 M** | |

- **Main cost driver is context size, not output.** The average call re-read **190 k** cached tokens (peak 321 k),
  so every supervisor call costs about 21.5 k weighted tokens before it writes anything.
- **Output was small by comparison.** All output in the session was 109 k tokens, 13 % of the weighted cost.

## Per delegated task

| Task (lane) | Worker output | Supervisor calls | Delegated cost | Self estimate | Result |
|---|---:|---:|---:|---:|---|
| rw-1 landing + CSS/JS (codex) | 15.2 k | 11 | 243 k | 330 k | saved 87 k |
| rw-2 price (codex) | 7.9 k | 6 | 172 k | 202 k | saved 30 k |
| rw-3 akcii (opencode-main) | 4.1 k | 8 | 249 k | 136 k | lost 113 k |
| rw-4 gallery (opencode-main-fallback) | 3.7 k | 8 | 199 k | 130 k | lost 70 k |
| dg-1 routing log (codex) | 2.6 k | 10 | 315 k | 110 k | lost 205 k |
| dg-2 + dg-3 distribution (opencode-main → smart) | 8.2 k | 16 | 438 k | 208 k | lost 230 k |
| Shared watchers / multi-task fill | — | 16 | 373 k | — | overhead |
| **Total** | **41.6 k** | **75** | **1.99 M** | **1.12 M** | **+874 k (+78 %)** |

**Break-even.** Self-writing costs roughly 17.5 weighted tokens per output token. Delegation costs roughly
(calls per task) × (context per call).
- **This session:** 8 calls per task at a 190 k context puts break-even at a deliverable of about 6 k output tokens.
- **Lean pipeline:** 3 calls per task (dispatch, collect+integrate, one review) at a 60 k context puts it at about 1 k.

**The plugin can save tokens, but only when calls per task are low and the supervisor context stays small.**
Neither held here.

## Why calls per task were high (avoidable)

| Cause | Extra calls | Ref |
|---|---:|---|
| cc-delegate handoff is manual: `dg workorder` → read → retype the spec into `run_dev_task` (~1.2 k output tokens each) → `dg attach` | ~3 per OpenCode task (×5) | — |
| Integrate: merge before the lazy worker commit merged nothing; supervisor edits broke patch-id verification | ~6 | F7, F14 |
| Weak worker results needed re-routing or scope surgery | ~12 | F8, F11, F15 |
| `--json` rejected; watcher restarts; one non-harness loop that never woke the session | ~5 | F9 |
| Pages 2–4 serialised behind the landing page (supervisor plan) | wall-clock, not tokens | plan decision |

## What the supervisor kept, and why

| Work | Why kept | Justified? |
|---|---|---|
| Plugin investigation (MCP failure, missing key, test failures) — 39 calls, 20 % | needed session and host context no work order carries | yes, but half of it was multi-file reading that `dg quickread` exists for and was never used |
| Content extraction: xlsx → JSON, promo text from 5 images, Yandex contacts | workers can't see images; judgement on price conflicts | yes |
| Asset optimisation (sips/ffmpeg), `BRIEF.md` design/content contract | one shell loop; the brief is the judgement | yes |
| v13 migration, stall-timeout tests, `plan` type, capacity fix, reveal fix | each ≤ ~30 lines, fully traced; a work order would be longer | yes. Capacity fix touched 4 files, which rule 4 says to delegate; kept with a logged reason |
| Review fixes (test leak, scope stubs, merges) | review is the supervisor's job | yes; review caught 4 defects no worker gate caught |
| Releases, field log, audit | outward/irreversible or judgement | yes |

**Decision log gaps.** `dg decisions` has 8 records, but there were about 20 real forks. Unlogged keeps
include the reveal fix, review takeovers on rw-3 and dg-3, and the website content prep.

## Recommendations (ordered by tokens saved)

1. **Remove the manual cc-delegate handoff:** `dg fill`/`dispatch` should start OpenCode jobs itself (as it
   does for Codex), or `run_dev_task` should take a ledger task id and read the work order. This saves ~3 calls
   and ~1.2 k output tokens per task.
2. **One-call integrate:** `dg integrate` commits the worker result, merges it (or applies the patch), verifies,
   and cleans up. Accept supervisor edits made on top.
3. **Record tokens in the ledger** (F16/F17): store worker `total_tokens` at attach/collect, and count supervisor
   calls per task id from a PostToolUse hook, so `dg distribution` can show cost per lane. That answers this
   question without transcript forensics.
4. **Keep the supervisor context lean:** do investigation through `dg quickread`, avoid reading whole diffs into
   context (review with test gates and targeted greps), and start a fresh session per workload.
5. **Delegate only above the break-even size** (~6 k output tokens at today's overhead), or batch small pages into
   one task.

## New findings

| # | Severity | Finding |
|---|---|---|
| F16 | medium | cc-delegate job results live only in the MCP server's memory: after a session restart `fetch_task_result` returns `unknown task_id`, and `total_tokens` is lost (the job JSON and log don't carry it). |
| F17 | medium | No token accounting anywhere in the Governor (supervisor or worker), so "does delegation save tokens" can't be answered from `dg` data. |
| F18 | low | The skill's `dg quickread` / `dg safewrite` were never used in a session with 39 investigation calls; nothing prompts the supervisor to use them. |
| F19 | high | **Blind to Claude's own limits in the desktop app.** Supervisor quota comes only from the Claude Code statusline payload, which the desktop Code tab never executes. `claudeQuota` is never written, SUP stays CLAUDE_NORMAL with "no quota data yet", and the SAVE/LOCAL transitions that exist to protect the subscription can't fire. The core purpose of the plugin is unmeasured in this client. |
