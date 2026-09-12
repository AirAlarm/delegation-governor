# One-shot code writing

Use this for a small, self-contained generated file when the full WRITE task
flow would add more ceremony than review value. Use a normal ledger task and
worktree for multi-file changes, changes that need integration or review, and
work that other tasks depend on.

Ask the worker for the complete file content and capture its response in a
content file. Worker output must always pass through `dg safewrite`; never let
a worker write directly into the repository.

```bash
dg safewrite path/to/generated.py --from path/to/worker-output.txt
```

The command writes through a temporary file in the target directory and then
atomically replaces the target. It refuses to overwrite an existing path. Use
`--force` only after checking the existing file and deciding replacement is
intentional.

If the response is wrapped in one outer triple-backtick code fence, safewrite
removes that pair. Triple-backtick lines inside the generated file are kept.
Unfenced content, including its byte content and newline style, is unchanged.

Call dg-worker's MCP `run_dev_task` yourself when it supplies the content.
`dg safewrite` never invokes a worker or records a ledger task; it is only the
disk-write safety layer after generation.
