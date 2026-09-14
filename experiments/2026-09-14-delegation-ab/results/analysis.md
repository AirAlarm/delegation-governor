# Analysis of run 1 (§3 of the runbook)

Run 1 went arm B first, then arm A, on 2026-09-13 at 20:22–21:08 UTC. This analysis ran in a fresh session.

## Verdict: INCONCLUSIVE, leaning towards no saving

| Rule clause | Arm A | Arm B | Holds? |
|---|---:|---:|---|
| A's 5 h delta ≤ 0.8 × B's | 3 pp | 4 pp | yes on raw integers (3 ≤ 3.2), but within quantisation, see below |
| A's `generated` < B's | 231 218 | 217 118 | **no** (+6.5 %) |
| Quality parity | 1 blocking, 8/10 | 1 blocking, 6/10 | yes |

- **Not SUPPORTED:** the token clause fails.
- **Not REJECTED:** A's raw 5 h delta is below B's.
- **Result:** INCONCLUSIVE. The runbook says to repeat with the order swapped (A first).

## Claude usage

| Metric | Arm A (delegated) | Arm B (Claude only) | A / B |
|---|---:|---:|---:|
| 5 h window | 8 → 11 (3 pp) | 3 → 7 (4 pp) | 0.75 |
| 7 d window | 9 → 10 (1 pp) | 9 → 9 (0 pp) | n/a |
| Statusline `total_cost_usd` (API-equivalent) | $10.20 | $9.45 | **1.08** |
| `generated` tokens | 231 218 | 217 118 | 1.065 |
| `processed` tokens | 13 557 230 | 12 617 714 | 1.074 |
| Output tokens | 63 705 | 71 302 | 0.89 |
| Cache writes | 167 299 | 145 582 | 1.15 |
| API calls | 106 | 117 | 0.91 |
| Wall clock | 21.9 min | 22.0 min | 1.00 |

- **The 5 h delta can't separate the arms.** `used_percentage` is an integer, and the 7 → 8 tick happened at the arm boundary:
  - arm B's last tick came at $6.33 of its $9.45 spend;
  - arm A's first reading was already 8, before A had spent anything.
  - So the tick belongs to B's tail, and A's own tail ($0.87 after its last tick) was never observed.
  - B's true delta is between 4 and 5, and A's between 3 and 4.
- **Cost tracks the ticks.** Across both arms, 5 h ticks came every $1.1–5.0 of statusline cost, about $2.5/pp on average. At that rate the costs work out to about 3.8 pp for B and 4.1 pp for A.
- **All the fine-grained metrics favour B by 6–8 %.** Delegation cut Claude's output by 11 %, but added more in context: +15 % cache writes and +7 % cache reads.
- **Controls held:**
  - no other Claude session logged assistant turns during the arms;
  - no subagents ran in either arm;
  - no window reset happened mid-arm.

### Where arm A's Claude spend went (statusline cost by phase)

| Phase | Minutes | Cost | Share |
|---|---:|---:|---:|
| Planning, Yandex fetch, media/data scaffold, design system, first work order | 0–9.1 | $3.81 | 37 % |
| Dispatch 3 lanes, review and merge prices | 9.1–12.4 | $2.58 | 25 % |
| Integrate landing and akcii, wait for gallery, polling loops | 12.4–19.4 | $2.37 | 23 % |
| Collect gallery, fixes, commit, summary | 19.4–21.9 | $1.44 | 14 % |

- **Code split:** the supervisor wrote about 540 text lines itself (scaffold, including 283 lines of `prices.json`, plus integration fixes). Workers wrote about 2 340.
- **Work orders:** four went out:
  - landing page on Codex;
  - prices on minimax-m3;
  - akcii on longcat-2.0;
  - gallery on qwen3.6-plus.
- **Supervision cost:** handing off about 80 % of the lines did not reduce Claude's usage. Writing specs, reviewing diffs and fixing integration cost about as much as writing the pages.

## Cost moved to worker quotas (arm A only)

| Quota | Before | After | Delta |
|---|---:|---:|---:|
| Codex 5 h | 0 % | 6 % | +6 pp |
| Codex 7 d | 11 % | 12 % | +1 pp |
| OpenCode Go 5 h | 0 % | 6.4 % | +6.4 pp ($0.77) |
| OpenCode Go weekly | 44.3 % | 46.9 % | +2.6 pp |

## Quality

### `check_site.py`: both raw scores 9/10, but B's failure is a false negative

- **Arm A fails site weight:** 31.1 MB. `video/massazh-spiny.mp4` alone is 10 MB, though the videos use `preload="none"`.
- **Arm B fails the price check at 3/37, which is a checker artefact.** B renders prices with JS from `js/services.js`.
  - Parsed against the xlsx, that file has all 37 rows with the same name and price multiset.
  - The one rename is intentional: «Массаж шей (шейно-воротниковой зоны)» → «Массаж шейно-воротниковой зоны».
- **Corrected scores:** A 9/10, B 10/10.
- **Checker fix needed:** `check_site.py` should render JS-built pages, or read `window.SERVICES`, before run 2.

### Blind review

- **Setup:**
  - both archives were copied to `site-1` / `site-2` with a random mapping;
  - `.git`, `.cc-delegate` and `.gitignore` were stripped;
  - the reviewer ran as a separate agent given only the brief, the sites and `assets/`.
- **Checks it made:** rendered both sites at 1280 px and 375 px, read the xlsx, looked at the promo and story images, and compared contacts with Yandex.
- **Mapping, revealed only after the review:** site-1 = **arm A**, site-2 = **arm B**.

| | Arm A (site-1) | Arm B (site-2) |
|---|---|---|
| Score | **8/10**, "would show the client" | 6/10 |
| BLOCKING | 3 durations taken from story images instead of the xlsx: стопы 30 (xlsx 20), баночный 60 (45), перкуссионный 60 (45) | Invented promo rule «Привилегии не суммируются между собой» (`akcii.html:40`) |
| MAJOR | Story thumbnails contradict list prices with no note; «окрашивание» added to «Оформление бровей» | Promo terms reworded; invented amenities and claims (tea/coffee, "no plasterboard walls", abonements); contradictory directions; raw SEO descriptions, including one that contradicts itself; heavy home page (3.2 MB autoplay hero, 1600 px thumbnails) |
| Prices | 37/37 correct | 37/37 correct, durations exact |
| Strengths | More polished and cozy design, better copy, lightbox with navigation | Price data generated from the xlsx, note that list prices win over the story cards, README and build script, 22 interior photos |

- **I re-checked both BLOCKING items against the sources and confirmed them.**
- **Parity holds:** one blocking defect each. The reviewer preferred the delegated site.

## Conclusions

1. **In this run, delegation did not save Claude subscription usage.** It cost about 8 % more by API-equivalent cost and 6–7 % more by tokens. On top of that it spent 6 pp of Codex 5 h and 6.4 pp of OpenCode Go 5 h. The raw 5 h delta that favours A is within quantisation.
2. **Quality did not suffer, and may have improved.** The blind reviewer scored the delegated site higher, 8 vs 6, with the same number of blocking defects.
3. **The saving is eaten by supervision overhead.** 37 % of A's spend came before the first dispatch. The rest went to reviewing and integrating four parallel lanes, plus polling loops.
   - Delegating judgement-light bulk earlier and with fewer review round-trips is the lever to test.
   - F7/F14 (integrate friction) are the likely cost centres.

## Before run 2 (order swapped: A first)

- Make `check_site.py` see JS-rendered prices.
- Record a 5 h reading **after** each arm's tail: send one trivial prompt in a fresh session, or use `dg quota --force` a few minutes after exit. That way the boundary tick isn't misattributed.
- Keep statusline `total_cost_usd` as a reported secondary metric. It is the finest proxy that tracked the 5 h ticks.
