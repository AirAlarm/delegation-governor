# Analysis of round 3 (bigger task, dg 0.7.4)

This round was run on 2026-09-15 and analysed in the same session, which stayed idle during both arms. Blind review: `blind-review.md`. Runs 1–2: `../analysis.md`, `../run2/analysis.md`.

## Verdict: INCONCLUSIVE — first measured Claude saving, but quality parity fails

| Rule clause | A3 (delegated) | B3 (Claude only) | Holds? |
|---|---:|---:|---|
| A's 5 h delta ≤ 0.8 × B's | 5 pp | 7 pp | **yes**: 5 ≤ 5.6 on the bracketed integer readings |
| A's `generated` < B's | 370 320 | 429 541 | **yes** (0.86×) |
| Quality parity | `check_r3` 26/26; blind 6.5/10, 2 BLOCKING | `check_r3` 26/26; blind 8/10, 0 BLOCKING | **no**: two extra blocking defects |

- **Not SUPPORTED:** quality parity fails.
- **Not REJECTED:** A's delta is below B's.
- **The saving is real but smaller than the raw readings suggest.** The token-weighted estimate (below) gives A/B ≈ **0.84**. The raw integer readings give 0.71, but each carries ±1 pp.
- **Next step, per the runbook:** repeat with the order swapped (B first).

## Validity

- **Arm A3:** 08:26–09:33 MSK, 67 min. Its 5 h window started at 6 % with 190+ min left.
- **Arm B3:** 20:16–20:52 MSK, 32 min. It ran in a fresh window, starting at 0 %.
- **No mid-arm reset in either arm.** Each arm was bracketed by its own before/after reading, taken 2 min after the arm.
- **`other_claude_activity` is `{}` for both arms.** Every transcript on the machine was scanned. This session's messages fell between the arms (09:36–12:20) and after B3.
- **Driver stop between the arms:** a pending Xcode license broke `/usr/bin/python3`, which `statusline.sh` uses. The B3-before reading at 12:23 logged nothing and the driver stopped.
  - After the license was accepted, the resumed driver skipped A3 and ran B3 alone, starting with a fresh reading.
  - A3's data was not affected.
- **Plugin version:** 0.7.4 was installed and asserted by the driver.

## Claude usage

| Metric | A3 (delegated) | B3 (Claude only) | A / B |
|---|---:|---:|---:|
| 5 h window (bracketed) | 6 → 11 (5 pp) | 0 → 7 (7 pp) | 0.71 |
| 5 h, token-weighted estimate (below) | 5.4 pp | 6.4 pp | **0.84** |
| 7 d window | 25 → 26 | 26 → 27 | 1.0 |
| `generated` tokens | 370 320 | 429 541 | 0.86 |
| Output tokens | 103 406 | 136 967 | **0.75** |
| Input + cache writes | 266 914 | 292 574 | 0.91 |
| Cache reads | 23 717 719 | 12 716 065 | 1.87 |
| Statusline `total_cost_usd` | $18.44 | $12.72 | 1.45 |
| API calls | 129 | 75 | 1.72 |
| Wall clock | 67.0 min | 32.4 min | 2.07 |

### What the 5 h limit actually counts

In earlier runs, API-equivalent cost and generated tokens gave different answers. Round 3's clean ticks settle it.

- **Method:**
  - Each tick-to-tick interval inside an arm is exactly 1 pp (one interval is 2 pp). That gives 9 intervals across A3 and B3.
  - I fitted `pp = a·(input+cache writes) + b·output + c·cache reads` to them.
  - Best fit: **0.6 pp per 100k input+cache-write, 3.2 pp per 100k output, 0.02 pp per 1M cache read**.
  - Output weighs about 5× input, the same ratio as API prices, but cache reads count about 25× less than their API price suggests.
- **How the candidate metrics compare** (sum of squared errors over the 9 intervals):

  | Metric | Error |
  |---|---:|
  | Fitted weights | 0.77 |
  | `generated` tokens, equal weights | 1.06 |
  | API cost | 4.24 (4× worse) |

- **Checked against runs 1–2** (the fit used round 3 only):

  | Run | Arm | Estimate | Reading |
  |---|---|---:|---:|
  | 1 | A | 3.3 | 3 |
  | 1 | B | 3.4 | 4 |
  | 2 | B2 | 3.4 | 4 |
  | 2 | A2r | 3.8 | 7 raw; the contamination split in run 2 gave 3.7–4.1 |
  | 3 | A3 | 5.4 | 5 |
  | 3 | B3 | 6.4 | 7 |

- **Consequence 1:** statusline cost is the wrong proxy for subscription usage. Delegated arms run long sessions that re-read their context, which inflates cost (A3 1.45×) but not the limit.
- **Consequence 2:** the comparable A/B ratio across all three runs is the token-weighted estimate:

  | Run | A/B (token-weighted) |
  |---|---:|
  | 1 | 0.97 |
  | 2 | 1.12 |
  | 3 | **0.84** |

### Where A3's Claude spend went (statusline cost by phase)

| Phase (MSK) | Cost | Share |
|---|---:|---:|
| 08:26–08:37 Skill, Yandex, xlsx reader, `data/site.json`, `docs/SPEC.md` | $4.01 | 22 % |
| 08:37–08:57 Dispatch 4 lanes; OpenCode failures (3 fallbacks, 1 FAILED) and supervisor writing `tools/media.py`; integrate EN copy and the Codex checker | $8.70 | 47 % |
| 08:57–09:15 Wait for the Codex site generator (all 84 pages) | $0.45 | 2 % |
| 09:15–09:26 Merge, screenshot review, Codex fix order, README, integrate | $4.37 | 24 % |
| 09:26–09:33 Final fixes, summary | $0.91 | 5 % |

- **Codex carried the build:**
  - `tools/check.py`, rin-website-22;
  - `tools/build.py` plus every page, rin-website-27;
  - the design fixes, rin-website-28.
- **OpenCode mostly failed:** 4 of 5 jobs failed (a provider `APIConnectionError`, two stalls, one empty result). The fifth wrote the EN service copy, which the supervisor corrected. The supervisor then wrote the media pipeline itself.
  - This is where most of the 47 % phase went.
  - With working OpenCode lanes, A3 would likely have been cheaper still.
- **The 0.7.4 ledger fixes held:** 3 fallbacks, a `dg set FAILED` and a cancel went through with no path conflicts. "Awaiting handoff" appeared twice, as designed.
- **The saving comes from output:** the supervisor wrote 25 % fewer output tokens than B3, which wrote the generator and all the copy itself. Output is the most heavily weighted component of the limit.

## Cost moved to worker quotas (A3 only)

| Quota | Before | After | Delta |
|---|---:|---:|---:|
| Codex 5 h | 0 % | 47 % | **+47 pp** |
| Codex 7 d | 20 % | 27 % | +7 pp |
| OpenCode Go 5 h | — | 16.8 % ($0.87) | +16.8 pp (window empty before; `opencode-go-arm-a3.md`) |

## Quality

- **`check_r3.py`:** both 26/26 after two checker fixes that affected both arms.
  - EN prices are formatted "3,200 ₽".
  - B3's price list lives at `uslugi/index.html`.
  - Raw scores at arm end were A3 25, B3 24.
- **Prices:** both sites are 37/37 on RU and EN service pages, price lists, booking dropdowns and JSON-LD.
- **Blind review:** B3 8/10 vs A3 6.5/10. Both A3 BLOCKING items are confirmed in the archive:
  1. **Real-looking domain:** `https://raz-i-navsegda.ru` everywhere. It was set in the supervisor's own Foundation commit, not by a worker, and it is a live, unrelated site. B3 used an honest `.example` placeholder.
  2. **Contradictory brow descriptions published:** the xlsx describes «…без окрашивания» as «с окрашиванием», and A3 copied that. B3 left those two descriptions out.
- **Both defects were disclosed.** A3's final summary listed both as open questions; the reviewer didn't see it, because summaries aren't part of the served site. They are still defects in the shipped site, so parity fails as the rule is written.
- **Both come from supervisor decisions** (the facts file, and verbatim copy from data), not from worker output. As in runs 1–2 (8/6, then 6/8), the quality gap swaps between arms and looks like per-run variance rather than a delegation effect. One run can't settle it.

## Conclusions

1. **On a bigger task (Claude-only 32 min vs 18 min in run 2), delegation produced its first measured Claude saving.**
   - The token-weighted estimate is about 16 % less 5 h usage (0.84×); the raw readings show 29 %.
   - All of it comes from Codex writing the bulk output.
   - The price: 2.1× wall clock, 47 pp of Codex's 5 h window, 17 % of OpenCode Go's 5 h window, and 1.45× API-equivalent cost (which the subscription barely counts).
2. **The saving is below the pre-registered 20 % bar on fine-grained numbers, and quality parity failed.** Hence INCONCLUSIVE rather than SUPPORTED.
3. **Measure subscription impact with weighted tokens, not cost.** Cache reads don't count.
4. **For a repeat (order swapped, B first):**
   - Fix or remove the failing OpenCode lanes. They cost the supervisor about $4, and the media pipeline came back to Claude.
   - Keep the task; B3 finished in 32 min, so a bigger task would widen the gap further.
   - Add to the brief: "use a placeholder domain" and "flag contradictory source cells instead of publishing them". Both defects were supervisor judgement calls that a clearer brief removes for both arms.
