# OpenCode Go model matrix

Which models this plugin can actually delegate to, and why. Measured 2026-09-13
against `https://opencode.ai/zen/go`. Quotas are from
[the OpenCode Go docs](https://opencode.ai/docs/go/#usage-limits); everything
else here is measured, because the docs say nothing about tool calling.

## How to read this

A model is only usable if it survives a **real multi-turn delegated task**.
Cheaper checks each answer a narrower question and each one passed on models
that later failed:

| Check | What it proves | What it misses |
|---|---|---|
| plain request → 200 | model is served on that endpoint | tool calling |
| request with `tools` → 200 | accepts tool definitions | forced tool choice |
| forced `tool_choice` → 200 | deepagents' agent loop can start | multi-turn replay |
| **real delegated task** | **actually usable** | — |

Only the last row is trusted below. `deepseek-v4-flash` passes all three probes
and still fails a real task.

## Tier A — validated by a real multi-turn task

| Model | Endpoint | `api_base` | $/mo | req/mo |
|---|---|---|---|---|
| `longcat-2.0` | OpenAI | `…/zen/go/v1` | 60 | 57,200 |
| `minimax-m2.7` | Anthropic | `…/zen/go` | 60 | 17,000 |
| `qwen3.6-plus` | Anthropic | `…/zen/go` | 60 | 16,300 |
| `minimax-m3` | either | per provider | 60 | 16,000 |
| `minimax-m2.5` | Anthropic | `…/zen/go` | 60 | — |
| `glm-5.3` | OpenAI | `…/zen/go/v1` | 15 | 1,080 |
| `qwen3.7-max` | **both**, verified separately | per provider | 30 | 840 |
| `qwen/qwen3.5-9b` (station) | LM Studio | `http://<host>:1234/v1` | local | — |

## Tier B — forced tool choice works, multi-turn NOT yet tested

Plausible, unproven. Validate before use.

| Model | Endpoint | $/mo | req/mo |
|---|---|---|---|
| `glm-5.3-flash` | OpenAI | 60 | 31,580 |
| `mimo-v2.5-pro` | OpenAI | 15 | 16,300 |
| `glm-5.2` | OpenAI | 60 | 4,300 |
| `glm-5.1` | OpenAI | 60 | 4,300 |
| `kimi-k3` | OpenAI | 15 | 490 |

## Tier C — reject forced tool choice (400), cannot run the agent loop

`deepseek-v4-pro`, `deepseek-v4.1-flash`, `deepseek-v4-flash-vision-exp`,
`kimi-k2.7-code`, `qwen3.8-max`, `qwen3.8-flash`, `qwen3.7-plus`.

Note `qwen3.8-max` here: an earlier config chose it for the reviewer lane, but
it cannot run a delegated task at all.

## Tier D — excluded

| Model | Why |
|---|---|
| `deepseek-v4-flash` | **The trap.** Best quota on the plan (65,000 req/mo) and passes every probe, but it is thinking-mode: the provider requires `reasoning_content` replayed on the next call and litellm does not do it, so it dies on turn 2. Revisit if litellm adds replay. |
| `grok-4.6` | 401 — no access on this plan |
| `gpt-5.6-luna` | 500 on both endpoints |
| `kimi-k2.6`, `mimo-v2.5`, `hy3`, `hy4-preview`, `muse-spark-*` | 403 — not entitled |

## Two traps worth knowing

**1. `api_base` is not portable across providers.** litellm appends a different
suffix per provider, so the same endpoint needs two different strings:

- `litellm:anthropic/<model>` → `https://opencode.ai/zen/go` (adds `/v1/messages`)
- `litellm:openai/<model>` → `https://opencode.ai/zen/go/v1` (adds `/chat/completions`)

Getting this wrong returns an HTML 404 wrapped in `litellm.NotFoundError`, which
reads like the model does not exist.

**2. A 500 usually means wrong endpoint, not a broken model.** Most of the
catalogue serves `/v1/chat/completions`; only 8 serve `/v1/messages`. `glm-5.3`
500s on the latter and works fine on the former. An earlier version of
`config.py` recorded several models as broken on exactly this mistake and
under-selected the catalogue for months.

## Rubric grading

`RubricMiddleware` returns `grader_error` on **every** OpenCode Go model, on both
endpoints. It works on LM Studio (station), which returns real verdicts. Since
`worker.py` synthesises a rubric from `--test-command` as well as
`--definition-of-done`, OpenCode lanes hit it easily. As of v0.6.0 a grader error
is no longer fatal: the run reports `succeeded` with `ungraded` set.

So today, **station is the only lane with a working acceptance gate.**

## Re-validating

Probes are not enough. Run a real task per model:

```bash
export DELEGATE_API_KEY=$(python3 -c "import json,pathlib;print(json.loads((pathlib.Path.home()/'.delegation-governor/credentials.json').read_text())['OPENCODE_GO_API_KEY'])")
uv run worker/worker_runtime/worker.py \
  --worktree /tmp/scratch-repo --spec "Add subtract(a,b) to calc.py returning a - b." \
  --session-id validate --model litellm:openai/<model> \
  --api-key-env-var OPENCODE_GO_API_KEY --api-base https://opencode.ai/zen/go/v1 \
  --test-command "python3 -m unittest discover -q" \
  --recursion-limit 400 --rubric-max-iterations 1 --command-timeout 60
```

`--recursion-limit 400` is the production default (`DELEGATE_RECURSION_LIMIT`).
A lower value produces a spurious `GraphRecursionError` that looks like a model
defect. Always verify the produced code yourself rather than trusting the
reported status.
