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

---

# Run 2 — order swapped (arm A first)

Run 1 was INCONCLUSIVE (`results/analysis.md`), so run 2 swaps the order. It changes three things:

1. **Order:** arm A runs first.
2. **Bracket readings:** a one-prompt Opus session runs before, between and after the arms. Its first
   statusline gives the 5 h / 7 d state with no arm tail missing. This prevents run 1's misattributed boundary tick.
3. **Cost metric:** statusline `total_cost_usd` per arm is reported as a secondary metric (about $2.5 per 5 h pp
   in run 1), and `check_site.py` now also reads JS/JSON price data.

The decision rule above is unchanged. It uses the **bracketed** 5 h deltas: A = R1 − R0, B = R2 − R1.

Session ids:

| Session | Id |
|---|---|
| R0 reading (before A) | `aa95fbc3-a809-4471-9c43-6f8dd4460fa4` |
| Arm A2 | `6a016168-8d57-408d-abc9-8fe293fa8e4f` |
| R1 reading (after A, before B) | `b3a086d2-524a-4377-8429-112a4397acf0` |
| Arm B2 | `1ecfb531-6c9e-42e9-8eb9-452364ca0fbd` |
| R2 reading (after B) | `ce41867d-f008-40d4-8f34-7e25869cea76` |

**How to take a reading:**
- Run the command. When "OK" appears and the statusline shows, type `/exit`.
- Before readings R1 and R2, wait 2 minutes after the arm exits, so usage accounting catches up.
- A reading costs one short Opus turn (about 0.1 pp of the 5 h window). It is identical in every bracket, so it cancels out.

## Run 2 steps

From `~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab`:

### 0. Preconditions

```bash
dg tasks running
```

```bash
ls -A ~/Projects/rin-website
```

```bash
mkdir -p results/run2
```

### 1. Reading R0

```bash
./snapshot.sh run2-R0
```

```bash
claude --session-id aa95fbc3-a809-4471-9c43-6f8dd4460fa4 --model claude-opus-5 --settings ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-b.settings.json "Reply with OK."
```

### 2. Arm A2 (delegated)

Note the OpenCode Go dashboard, then:

```bash
cd ~/Projects/rin-website && claude --session-id 6a016168-8d57-408d-abc9-8fe293fa8e4f --model claude-opus-5 --effort high --settings ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-a.settings.json "$(cat ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/task.md ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-a.md)"
```

When it has finished and exited, note the OpenCode Go dashboard again. Then, back in the experiment folder:

```bash
cd ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab
```

```bash
python3 check_site.py ~/Projects/rin-website > results/run2/arm-a-check.txt
```

```bash
./reset.sh arm-a2
```

### 3. Reading R1 (wait 2 minutes after arm A2 exits)

```bash
./snapshot.sh run2-R1
```

```bash
claude --session-id b3a086d2-524a-4377-8429-112a4397acf0 --model claude-opus-5 --settings ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-b.settings.json "Reply with OK."
```

### 4. Arm B2 (Claude only)

```bash
cd ~/Projects/rin-website && claude --session-id 1ecfb531-6c9e-42e9-8eb9-452364ca0fbd --model claude-opus-5 --effort high --settings ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-b.settings.json "$(cat ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/task.md ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-b.md)"
```

```bash
cd ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab
```

```bash
python3 check_site.py ~/Projects/rin-website > results/run2/arm-b-check.txt
```

```bash
./reset.sh arm-b2
```

### 5. Reading R2 (wait 2 minutes after arm B2 exits)

```bash
./snapshot.sh run2-R2
```

```bash
claude --session-id ce41867d-f008-40d4-8f34-7e25869cea76 --model claude-opus-5 --settings ~/Projects/delegation-governor/experiments/2026-09-14-delegation-ab/arm-b.settings.json "Reply with OK."
```

### 6. Measure

```bash
python3 measure.py 6a016168-8d57-408d-abc9-8fe293fa8e4f --before aa95fbc3-a809-4471-9c43-6f8dd4460fa4 --after b3a086d2-524a-4377-8429-112a4397acf0 > results/run2/arm-a.json
```

```bash
python3 measure.py 1ecfb531-6c9e-42e9-8eb9-452364ca0fbd --before b3a086d2-524a-4377-8429-112a4397acf0 --after ce41867d-f008-40d4-8f34-7e25869cea76 > results/run2/arm-b.json
```

**Checks before analysis:**
- **Readings:** each `results/run2/arm-*.json` must contain `bracketed_windows` with numeric deltas and no
  mid-arm window reset.
- **5 h window:** it must not reset between R0 and R2. Run 2 takes about 50 minutes, so start it at least
  1 hour before the reset shown in the statusline.
- **If a check fails:** re-run that arm with a new id (`uuidgen`).

Analysis: a fresh session with §3 of this runbook, comparing run 1 and run 2 together. Blind-review the
run 2 sites too.
