# Analysis of run 1 + run 2 (§3 of the runbook)

Run in a fresh session on 2026-09-14. Run 1's analysis is in `../analysis.md`; the run 2 blind review is in `blind-review.md`.

## Combined verdict: REJECTED — delegation did not save Claude usage in either run

| Rule clause | Run 1 (A / B) | Run 2 (A2r / B2) |
|---|---|---|
| A's 5 h delta ≤ 0.8 × B's | 3 / 4 pp: within quantisation | 7 / 4 pp raw. **Contaminated**; A2r's own share ≈ 3.7–4.1 pp ≈ B2's |
| A's `generated` < B's | **no**: 231k / 217k | **no**: 267k / 262k |
| Quality parity | yes: 1 / 1 blocking, blind 8 / 6 | **no**: 2 / 0 blocking, blind 6 / 8 |
| Run verdict | INCONCLUSIVE | REJECTED raw; after correction A ≈ B, and the token and quality clauses fail anyway |

- **SUPPORTED:** not reachable in either run. The token clause fails in both, and in run 2 quality parity fails too.
- **Primary metric:**
  - run 1 favours A by 1 pp, within integer quantisation;
  - run 2, corrected for contamination, puts A at B's level or above.
- **Every token and cost total has A ≥ B in both runs.** API-equivalent cost is 1.08× in run 1 and 1.99× in run 2.
- **Delegation also spent worker quota:**
  - run 1: Codex 5 h +6 pp, OpenCode Go 5 h +6.4 pp;
  - run 2: Codex 5 h +39 pp, 7 d +6 pp, plus 13 pp Codex burned by the aborted A2.
- **This describes the Governor as of 0.7.3.** Supervision overhead and ledger friction cost more than the hand-off saved.

## Run 2 validity

### Control violated: a concurrent Claude session during arm A2r

- **What overlapped:** a desktop-app `/design-review` session in CryptAndHearth (`68af4556…`, Opus 5) was active 10:06–10:59 UTC. That overlaps A2r (10:23–11:15) for 36 minutes.
- **How it was found:** I scanned every transcript under `~/.claude/projects` for assistant turns inside each arm's window.
  - B2's window (08:14–08:35) was clean.
  - A2r's window had 53 API calls from that session.
- **Its size inside A2r's window:**
  - about 186k generated tokens (A2r used 267k) and 18.4M cache reads;
  - about $11.5 at Opus list price (A2r ≈ $13.2 by the same formula, $14.92 by statusline).
- **The ticks confirm it:**
  - A2r's 5 h reading went 1 → 8 by 10:59:35, five seconds before the other session's last turn;
  - A2r then spent another **$5.39 with no further tick**, and R4 at 11:22 still read 8.
- **Attribution:** splitting the 7 pp by each session's share of the window's usage gives A2r **3.7 pp** by cost or **4.1 pp** by generated tokens. B2 was 4 pp. The integer ends of each bracket add ±1 pp to both.
- **Consequence:** the raw "7 ≥ 4 → REJECTED" overstates A2r. The honest primary reading is **A ≈ B**. That is still ≥ B at the point estimate, and far from the ≤ 0.8 × B needed for SUPPORTED.
- **Other checks:**
  - **B2:** R1 → R2 brackets are clean.
  - **A2r:** R3 → R4 is inside one fresh 5 h window, with no reset mid-arm.
  - **7 d window:** 0 pp in both arms.
- **Order:** A2r did not run first as planned. It ran alone after the 5 h reset, after B2, because the first A2 attempt was aborted by a driver race.

### Claude usage, clean per-session metrics

These come from each arm's own transcript and statusline, so the concurrent session doesn't affect them.

| Metric | A2r (delegated) | B2 (Claude only) | A / B | Run 1 A / B |
|---|---:|---:|---:|---:|
| 5 h window (bracketed) | 1 → 8 (7 pp, ≈ 4 own) | 8 → 12 (4 pp) | ≈ 1.0 corrected | 0.75 |
| 7 d window | 13 → 13 | 11 → 11 | n/a | n/a |
| Statusline `total_cost_usd` | $14.92 | $7.51 | **1.99** | 1.08 |
| `generated` tokens | 266 962 | 262 445 | 1.02 | 1.065 |
| `processed` tokens | 20 726 748 | 8 040 675 | 2.58 | 1.074 |
| Output tokens | 70 320 | 65 452 | 1.07 | 0.89 |
| Cache writes | 196 362 | 196 875 | 1.00 | 1.15 |
| Cache reads | 20 459 786 | 7 778 230 | 2.63 | 1.07 |
| API calls | 139 | 59 | 2.36 | 0.91 |
| Wall clock | 52.5 min | 17.6 min | 2.98 | 1.00 |

- **Not counted above:** the aborted A2 spent $2.82 and 107k generated tokens that produced nothing usable.
- **A2r's supervisor produced as much output as B2 building the whole site:** 70k vs 65k tokens.
  - A2r's output went into `BRIEF.md` and the media script (21k chars of Write), work orders (16k chars), `dg` management and polling commands (19k chars), and other Bash.
  - B2's output went mostly into writing pages (40k chars of Write).
- **A2r's cost doubled on cache reads.** Its run was 3× longer and made 2.4× the API calls, each re-reading a large context.
- **Which proxy tracks the subscription limit is unresolved:**
  - `generated` says A ≈ B;
  - cost says A ≈ 2 × B.
  - The window-level fits (5 h ticks against all sessions' usage) are too noisy at integer resolution to pick one: $1.9–2.8 per pp, or 58–110k generated tokens per pp.
  - **Either way, neither proxy shows a saving.**

### Where arm A2r's Claude spend went (statusline cost by phase)

| Phase (UTC) | Cost | Share |
|---|---:|---:|
| 10:23–10:29 Intake, Yandex fetch, media pipeline, `BRIEF.md`, baseline commit | $2.85 | 19 % |
| 10:29–10:31 Queue and dispatch landing (Codex), write page work orders | $0.91 | 6 % |
| 10:31–10:44 Wait for Codex landing | $0.17 | 1 % |
| 10:44–10:59 Review and merge landing; polish order; `dg dispatch` refused, then `release` and a direct `run_dev_task` for akcii/gallery; collect and merge akcii and polish | $5.48 | 37 % |
| 10:59–11:06 Uslugi polish; gallery worker (qwen fallback) looping; cancel, cleanup, path conflict, reading dg source, redispatch as rin-website-19 | $3.55 | 24 % |
| 11:06–11:15 SendFeedback, wait, merge gallery, CSS fix, final checks, summary | $1.96 | 13 % |

- **Scale:** 7 work orders (rin-website-13 … 19) and 6 worker merges.
- **Ledger and worker friction cost about $4 (≈ 27 %).** It covers 10:46–10:55 (dispatch refused, `--help`, release, MCP fallback) and 11:04–11:06 (cancel and cleanup not releasing the attempt, `dg set`, reading `store.py` and `cli.py`, `recover-handoffs`).
  - The bug is already filed through SendFeedback: a cc-delegate attempt stays RUNNING after MCP cancel+cleanup.
  - Without this friction, A2r would cost about $11, still 1.45× B2.

## Quality

### `check_site.py`

| Check | A2r | B2 |
|---|---|---|
| Raw score | 8/10 | 7/10 |
| Prices | FAIL 33/37 | FAIL 33/37 |
| Contacts | pass | FAIL (no booking link) |
| Weight | FAIL 27.6 MB | FAIL 36.1 MB |
| **Corrected** | **9/10** | **8/10** |

- **The price failures are a checker artefact, the same on both sites.** The checker matches exact xlsx names, and both sites shorten the same 4:
  - «Эндосфера и ручной массаж восстановительный»
  - «Эндосфера и проработка проблемных зон»
  - «Эндосфера и миостимуляция»
  - «Массаж шей (шейно-воротниковой зоны)»
- **The checker can't see durations,** and durations are where the real defects are.

### Blind review

- **Setup:** random mapping; dev files stripped. Full report in `blind-review.md`.
- **Revealed after the report:** site-1 = B2, site-2 = A2r.
- **Blinding was reviewer-side only.** The orchestrating session could tell the sites apart by their folder layout.

| | A2r (delegated) | B2 (Claude only) |
|---|---|---|
| Score | 6/10, "wouldn't show the client until the blockers are fixed" | **8/10**, "would show the client" |
| BLOCKING | Invented promo rule «Привилегии не суммируются между собой» (`akcii.html:167`); durations in no source: стоп 30/1800 (xlsx 20), перкуссионный 60/3500 (xlsx 45); plus баночный 60 (xlsx 45) | none |
| MAJOR | 6 durations missing; altered Yandex review quote; full-size JPG thumbnails | no online booking (`tel:` only); «отдельные кабинеты» unsupported |
| Rows fully correct | 28/37 | 37/37 |
| Strengths | Most polished visuals, yclients link, good ARIA | Exact data, optimised media, Yandex map and reviews widget |

- **I checked both A2r BLOCKING items against the xlsx and the site after the reveal, and confirmed them.**
- **Parity fails for A in run 2** (2 blocking vs 0). In run 1 it held (1 vs 1), and the delegated site scored higher.

### The quality gap is not an arm effect

The same defects recur across runs, and they swap arms:
- **Durations from the story images:** стопы 30, баночный 60, перкуссионный 60. These appeared in run 1 **arm A** and again in run 2 **arm A2r**.
- **«Привилегии не суммируются между собой»:** invented in run 1 **arm B**, and again in run 2 **arm A2r**.

Both come from the brief and the sources: 7 conflicts between the price stories and the xlsx, and no stated stacking rule. They don't come from delegation. With n = 1 per arm, blind scores of 8/6 then 6/8 mean **quality is a wash**. What delegation does not do is buy quality.

## Conclusions

1. **No saving, two runs, both orders.**
   - Delegated arms used about the same Claude tokens (1.02–1.07× `generated`) and more cost (1.08–1.99×).
   - They also spent 6–39 pp of Codex 5 h and OpenCode Go quota.
   - The one reading that favoured A (run 1's 3 vs 4 pp) is inside quantisation.
2. **Why:** supervision costs about as much as building. The pre-dispatch phase alone is 19–37 % of A's spend (writing a brief good enough to delegate). Review and merge round-trips, polling and ledger friction take most of the rest. In run 2 the supervisor's own output (70k tokens) matched B2's whole build.
3. **This task is too small to amortise the overhead.** B2 built the site in 17.6 min and $7.51. A task where Claude-alone would cost several times the brief-writing is where delegation could pay. That is untested.
4. **Fix before any run 3:**
   - **dg bugs:** cc-delegate attempt stays RUNNING after cancel+cleanup; `dg dispatch` refusal and the path conflict on fallback. They cost about 27 % of A2r.
   - **Driver:** `autorun.py` should check for concurrent Claude sessions during an arm and flag the arm, using the same all-transcripts scan as this analysis.
   - **Checker:** `check_site.py` should compare durations and allow renamed services, e.g. by normalising «и» / «+» and dropping parentheticals.
   - **Brief:** add «xlsx wins over story images; don't invent promo rules». Otherwise both arms keep hitting the same content traps and the quality comparison stays noise.
