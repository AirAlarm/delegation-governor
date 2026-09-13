# A/B experiment: does delegation-governor save Claude subscription usage?

## Question and decision rule (fixed before running)

**Statement under test:** "Supervising the rin-website build through the Governor uses less of the Claude
subscription's limits than Claude building it alone."

| Metric | Source | Role |
|---|---|---|
| Claude 5 h window utilisation delta (pp) | statusline `rate_limits` → `usage.jsonl` | **primary** |
| Claude 7 d window utilisation delta (pp) | same | confirms primary |
| `generated` tokens (input + cache writes + output) | transcript → `measure.py` | secondary |
| `processed` tokens (all, incl. cache reads) | transcript → `measure.py` | secondary |
| Worker quota spent (Codex 5 h/7 d %, OpenCode Go usage) | `snapshot.sh` + OpenCode dashboard | cost moved, not saved |
| Wall-clock minutes | transcript | secondary |
| Quality: `check_site.py` score + defects found in a blind review | outputs | parity check |

**Verdicts:**
- **SUPPORTED:** arm A's 5 h delta ≤ 0.8 × arm B's, AND A's `generated` tokens < B's, AND quality parity
  (same or better `check_site.py` score, no extra blocking defects in blind review).
- **REJECTED:** arm A's 5 h delta ≥ arm B's.
- **INCONCLUSIVE:** anything else, or a window reset happened mid-arm, or the 5 h delta is too coarse
  (both ≤ 2 pp). Then repeat with the arm order swapped.

## Controls

- **Same task:** identical `task.md` for both arms. Each arm differs only in its one-paragraph instruction
  (`arm-a.md` / `arm-b.md`) and its settings file.
- **Same model and effort:** `claude-opus-5`, `--effort high`, in a fresh terminal session each. Use the same
  permission mode in both arms.
- **Same starting state:** `rin-website/` contains only `assets/` (`reset.sh` enforces this between arms).
- **Isolation, verified 2026-09-13:**
  - arm B's settings disable the delegation-governor and codex plugins — the model sees none of their tools or skills;
  - arm A sees all of them.
- **Measurement adds no Claude usage:** `snapshot.sh`, `measure.py` and `check_site.py` run outside Claude.
- **Nothing else uses Claude during an arm:** keep the desktop app and other sessions idle. Other usage
  would show up in the 5 h delta.
- **No human steering:** don't type into an arm except to approve permission prompts.

## Steps

Run everything from `~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab`
(the `claude` commands from `~/Projects/rin-website`).

### 0. Preconditions

```bash
dg tasks running
```

```bash
ls -A ~/Projects/rin-website
```

The first should print nothing. The second should list only `.DS_Store` and `assets`.

```bash
mkdir -p results
```

### 1. Arm B: Claude only (run first)

```bash
./snapshot.sh arm-b-start
```

```bash
cd ~/Projects/rin-website && claude --session-id 1c1134b1-34f9-4132-8355-f529bafecbff --model claude-opus-5 --effort high --settings ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-b.settings.json "$(cat ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/task.md ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-b.md)"
```

- **Wait:** let it run to its final summary. The statusline must show at least once after the last turn;
  press Enter on an empty prompt if needed.
- **Exit:** then leave the session.

```bash
./snapshot.sh arm-b-end
```

```bash
python3 measure.py 1c1134b1-34f9-4132-8355-f529bafecbff > results/arm-b.json
```

```bash
python3 check_site.py ~/Projects/rin-website > results/arm-b-check.txt
```

```bash
./reset.sh arm-b
```

### 2. Arm A: delegated

Note the OpenCode Go dashboard usage, then:

```bash
./snapshot.sh arm-a-start
```

```bash
cd ~/Projects/rin-website && claude --session-id 9e854f96-130c-458c-986e-3fb50c5a045b --model claude-opus-5 --effort high --settings ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-a.settings.json "$(cat ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/task.md ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-a.md)"
```

When it has finished and exited, note the OpenCode Go dashboard usage again, then:

```bash
./snapshot.sh arm-a-end
```

```bash
python3 measure.py 9e854f96-130c-458c-986e-3fb50c5a045b > results/arm-a.json
```

```bash
python3 check_site.py ~/Projects/rin-website > results/arm-a-check.txt
```

```bash
./reset.sh arm-a
```

### 3. Analysis

- **Where to run it:** start a *fresh* session for the analysis, not an arm session. Point it at `results/`,
  `snapshots.jsonl`, `usage.jsonl` and the archives in `~/Projects/rin-website-archive/arm-{a,b}`.
- **Blind review:** give it both sites under neutral names (copy them to `site-1`/`site-2`) and have it list
  defects before revealing which arm is which.

## Known limitations

- **n = 1 per arm:** one run shows a direction, not a distribution. If the result is INCONCLUSIVE or close,
  repeat with the order swapped (A first).
- **Coarse primary metric:** 5 h utilisation has limited resolution, and small arms may move only a few points.
  Token counts are the fine-grained backup.
- **Shared prompt cache:** Anthropic's cache can carry over between the two sessions within an hour, but the
  tool and plugin lists differ between arms, so little of the prompt prefix is shared.
- **Product as-is:** the Governor is tested at 0.7.3 with its open issues (manual cc-delegate handoff, F7/F14
  integrate friction, F19 quota blindness). The result describes the product today, not its ceiling.
- **Unconfirmed field name:** the statusline field name for utilisation is unconfirmed. `measure.py` reads
  `utilization` or `used_percentage`; check `raw_first` in the results if a delta is `null`.
