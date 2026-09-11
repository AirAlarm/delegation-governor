# One-shot bulk reading

Use this for read-only reconnaissance when the question needs several files or
one file large enough that reading it directly would crowd your context. Use
`Read` for small files, targeted excerpts, and exact line/value verification.
Open a normal `READ_ONLY` ledger task when the work must be scheduled, tracked,
retried, reviewed, or used as a dependency.

## Prepare the corpus

```bash
dg quickread path/to/one.py "path/with spaces/two.py"
```

Stdout is an XML-delimited corpus with one `<file path="...">` block per file.
Per-file line/byte counts and the approximate input-token total go to stderr.
The command validates and reads every path before printing stdout, so an error
never leaves a plausible-looking partial corpus.

## Delegate it

Call cc-delegate's MCP `run_dev_task` yourself. Put the read-only question and
the corpus in `spec`, select the appropriate profile, and ask for analysis only.
`dg quickread` prepares input; it never calls the MCP tool.

Each call is ephemeral and independent. There is no worktree, branch, task id,
or server-side conversation to resume. Repeat the command and send the files
again for a follow-up. These calls are intentionally absent from the dg task
ledger; use the full task flow whenever durable observability matters.

Do not use quickread for editing. Before making a later edit, `Read` the exact
lines and values on which the change depends.
