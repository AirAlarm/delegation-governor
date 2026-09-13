#!/usr/bin/env bash
# Experiment statusline: log every rate-limit snapshot, feed the Governor, keep ponytail's badge.
# Claude Code allows one statusLine command, so this chains both instead of replacing either.
here="$(cd "$(dirname "$0")" && pwd)"
payload="$(cat)"

printf '%s' "$payload" | python3 -c '
import json, sys, time
d = json.load(sys.stdin)
print(json.dumps({"ts": time.time(), "session_id": d.get("session_id"),
                  "rate_limits": d.get("rate_limits"), "cost": d.get("cost"),
                  "model": (d.get("model") or {}).get("id")}))
' >> "$here/usage.jsonl" 2>/dev/null

dg_line="$(printf '%s' "$payload" | dg hook statusline 2>/dev/null)"
pony="$(printf '%s' "$payload" | bash "$HOME/.claude/plugins/cache/ponytail/ponytail/4.8.4/hooks/ponytail-statusline.sh" 2>/dev/null)"
printf '%s %s' "$dg_line" "$pony"
