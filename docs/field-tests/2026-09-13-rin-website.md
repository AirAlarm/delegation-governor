# Field test 2026-09-13 — rin-website draft + governor fixes

Supervisor: Claude Opus 5 (desktop, Code tab). Plugin 0.7.0 from cache during the session; fixes released in 0.7.2.
Workload: a 4-page static site for a beauty salon (`~/Projects/rin-website`), plus governor
fixes found along the way (run in parallel in this repo).

Raw records: `dg decisions` (supervisor decisions, `~/.claude/delegation-governor/decisions.jsonl`),
`dg show <id>` (attempts per task). Lane routing was not persisted before this session — see F4.

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| F1 | high | Saved config kept `tokenFile: ~/.cc-delegate/credentials.json` after the home-store move (56564aa); `_merge` lets it override the default, so all 7 OpenCode lanes reported `set OPENCODE_GO_API_KEY` with the key present. | fixed: schema v13 migration (d008f61) |
| F2 | low | A session opened in this repo loads the root `.mcp.json` as a *project* server; `${CLAUDE_PLUGIN_ROOT}` is unexpanded there, so a second `dg-worker` fails with CONNECTION_CLOSED. The plugin copy connects fine. Misread by the supervisor as "worker down". | open: document, or name the dev-repo server differently |
| F3 | low | `TestStallTimeout` faked only the Windows `reg query` branch; on macOS 2 failed, 2 passed by coincidence. | fixed (8822bf3) |
| F4 | high | Lane choices (`preference`, skipped lanes + reasons, rank) only existed in `dg fill` stdout — no way to audit distribution or balance afterwards. | in progress: `routing.jsonl` (delegation-governor-1) + `dg distribution` (delegation-governor-2/3) |
| F5 | high | **Lane capacity was per repo, not per lane.** `has_capacity` counts in-flight attempts per repo, so `maxWriteJobs: 1` lanes were double-booked across repos: codex ran rin-website-2 + delegation-governor-1, opencode-main ran rin-website-3 + delegation-governor-2 at the same time. Contradicts `lanes.py`'s "a lane is a machine". | fixed: lane slots counted across all repos in routing, reservation and `dg lanes`; `totalWriteJobsPerRepo` stays per repo; routing-log `in_flight` is now global |
| F6 | medium | Worktree location differs by lane: codex → `~/.claude/delegation-governor/worktrees/…`, cc-delegate → `<repo>/.cc-delegate/worktrees/…` (must be gitignored in every target repo). Confused the user ("why is it outside the project?"). | open |
| F7 | medium | `dg integrate` does not merge; it verifies and tells the supervisor to merge/cherry-pick first. Codex lane commits in its worktree (`feat(dg): …`); cc-delegate lane leaves changes staged + a `.diff` in `.cc-delegate/patches/`. Two integration paths. | open |
| F8 | medium | opencode-main (minimax-m3) returned delegation-governor-2 without the required test file, with a filler summary ("I'll start by exploring…"), 1.67M tokens, and `rubric grader errored` → reported `succeeded`. The test_command did not require the new test file to exist, so the gate passed on the old suite. | mitigated: test_command now asserts the file exists; fix re-routed to opencode-smart |
| F9 | low | Skill says "`dg <cmd> --json` everywhere", but `dg add` rejects `--json` in both positions. | open |
| F10 | low | New CLI features are unusable by the supervisor until a release: `dg` is an editable install of the *plugin cache* (`…/cache/…/0.7.0/src`), not the repo. `dg decision --type plan` failed until run via `PYTHONPATH=src`. | open |

## Supervisor orchestration decisions

| Time | Type | Decision | Outcome |
|---|---|---|---|
| 22:16 | keep | v13 migration myself (1 branch + test + bump) | done, suite green |
| 22:21 | delegate | rin-website pages → workers; supervisor kept content extraction (xlsx → `data/prices.json`, promo transcription from images, Yandex contacts), asset optimisation, `BRIEF.md` | landing by codex in 11 min, good quality |
| 22:21 | plan | **mistake**: pages 2–4 `--depends-on` the landing task so they reuse its CSS → 3 tasks BLOCKED, one lane busy for 11 min while 6 OpenCode lanes idled. Better: a small CSS-contract task (or supervisor-written tokens/class list in the brief), then all 4 pages in parallel. | cost ≈ 11 min of wall-clock |
| 22:30 | plan | routing log + distribution report as 2 parallel tasks with disjoint paths against a fixed record schema | ran on codex + opencode-main simultaneously |
| 22:40 | takeover | delegation-governor-2 draft accepted, bugs (task vs attempt status, `since` not applied to attempts) + missing tests re-routed as class hard → opencode-smart (different model) | running |

## Lane placement this session

| Task | Class | Lane (model) | Notes |
|---|---|---|---|
| rin-website-1 landing + CSS/JS | hard | codex | 682 s, integrated |
| delegation-governor-1 routing log | standard | codex | shared codex with rin-website-2 (F5) |
| delegation-governor-2 distribution | standard | opencode-main (minimax-m3) | fast, incomplete (F8) |
| rin-website-2 price | standard | codex | 2nd concurrent codex job (F5) |
| rin-website-3 akcii | standard | opencode-main (minimax-m3) | concurrent with delegation-governor-2 (F5) |
| rin-website-4 gallery | standard | opencode-main-fallback (qwen3.6-plus) | |
| delegation-governor-3 distribution fixes | hard | opencode-smart (glm-5.3) | codex at capacity in this repo |

Idle all session: opencode-fast, opencode-fast-fallback (no tiny/simple work was created),
opencode-bulk, opencode-reviewer (not routable by class). station down (LM Studio off).
