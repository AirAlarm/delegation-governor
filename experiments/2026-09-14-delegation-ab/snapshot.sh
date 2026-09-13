#!/usr/bin/env bash
# Record Claude + Codex quota at an experiment boundary. Uses no Claude tokens.
# Usage: ./snapshot.sh <label>    e.g. ./snapshot.sh arm-b-start
here="$(cd "$(dirname "$0")" && pwd)"
label="${1:?label required}"
dg quota --json --force 2>/dev/null | python3 -c '
import json, sys, time
try:
    q = json.load(sys.stdin)
except ValueError:
    q = {"error": "dg quota --json failed"}
print(json.dumps({"ts": time.time(), "label": sys.argv[1], "quota": q}))
' "$label" >> "$here/snapshots.jsonl"
tail -n 1 "$here/snapshots.jsonl" | cut -c1-300
echo
echo "Also note OpenCode Go usage from its dashboard for: $label"
