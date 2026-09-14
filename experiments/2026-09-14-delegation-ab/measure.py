"""Measure one experiment arm: Claude tokens from its transcript, 5h/7d deltas from usage.jsonl.

Usage: python3 measure.py <session-id> [<session-id> ...]

Tokens are reported raw per type (no price weighting) plus two views, because how
subscription limits count cached context is not published:
  processed = input + cache writes + cache reads + output
  generated = input + cache writes + output        (cache reads treated as free)
The 5h/7d utilisation deltas are the ground truth those views are checked against.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
PROJECTS = Path.home() / ".claude" / "projects"


def transcript(session_id: str) -> Path:
    hits = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    if not hits:
        sys.exit(f"no transcript for {session_id}")
    return hits[0]


def tokens(path: Path) -> dict:
    calls: dict[str, dict] = {}
    tools = collections.Counter()
    stamps = []
    for line in path.open():
        d = json.loads(line)
        if "timestamp" in d:
            stamps.append(d["timestamp"])
        if d.get("type") != "assistant":
            continue
        m = d["message"]
        calls[m["id"]] = m["usage"]  # last line per message carries the final usage
        for b in m.get("content", []):
            if b.get("type") == "tool_use":
                tools[b["name"]] += 1
    t = collections.Counter()
    for u in calls.values():
        cc = u.get("cache_creation") or {}
        t["input"] += u.get("input_tokens", 0)
        t["cache_write"] += cc.get("ephemeral_1h_input_tokens", 0) + cc.get("ephemeral_5m_input_tokens", 0)
        t["cache_read"] += u.get("cache_read_input_tokens", 0)
        t["output"] += u.get("output_tokens", 0)
    t["processed"] = t["input"] + t["cache_write"] + t["cache_read"] + t["output"]
    t["generated"] = t["input"] + t["cache_write"] + t["output"]
    t["api_calls"] = len(calls)
    start, end = (dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for s in (min(stamps), max(stamps)))
    return {"tokens": dict(t), "wall_minutes": round((end - start).total_seconds() / 60, 1),
            "tool_calls": dict(tools.most_common()), "start": min(stamps), "end": max(stamps)}


def snapshots(session_id: str) -> list[dict]:
    path = HERE / "usage.jsonl"
    rows = [json.loads(l) for l in path.open()] if path.exists() else []
    return [r for r in rows if r.get("session_id") == session_id]


def _deltas(first: dict, last: dict) -> dict:
    out = {}
    for w in ("five_hour", "seven_day"):
        a, b = first.get(w) or {}, last.get(w) or {}
        ua, ub = a.get("utilization", a.get("used_percentage")), b.get("utilization", b.get("used_percentage"))
        out[w] = {"start": ua, "end": ub,
                  "delta": (ub - ua) if isinstance(ua, (int, float)) and isinstance(ub, (int, float)) else None,
                  "window_reset_during_arm": a.get("resets_at") != b.get("resets_at")}
    return out


def window_deltas(session_id: str) -> dict:
    rows = [r for r in snapshots(session_id) if r.get("rate_limits")]
    if len(rows) < 2:
        return {"note": "fewer than 2 statusline snapshots with rate_limits for this session"}
    out = _deltas(rows[0]["rate_limits"], rows[-1]["rate_limits"])
    out["snapshots"] = len(rows)
    out["raw_first"] = rows[0]["rate_limits"]
    return out


def cost_usd(session_id: str) -> float | None:
    """Statusline total_cost_usd is cumulative per session, so the last value is the arm's cost."""
    costs = [(r.get("cost") or {}).get("total_cost_usd") for r in snapshots(session_id)]
    costs = [c for c in costs if isinstance(c, (int, float))]
    return round(costs[-1], 4) if costs else None


def reading(session_id: str) -> dict | None:
    """A bracket reading session's first rate_limits: the window state before/after an arm."""
    rows = [r for r in snapshots(session_id) if r.get("rate_limits")]
    return rows[0]["rate_limits"] if rows else None


if __name__ == "__main__":
    args = sys.argv[1:]
    before = after = None
    if "--before" in args:
        i = args.index("--before"); before = args[i + 1]; del args[i:i + 2]
    if "--after" in args:
        i = args.index("--after"); after = args[i + 1]; del args[i:i + 2]
    for sid in args:
        out = {"session_id": sid, **tokens(transcript(sid)),
               "cost_usd": cost_usd(sid), "claude_windows": window_deltas(sid)}
        if before and after:
            rb, ra = reading(before), reading(after)
            out["bracketed_windows"] = (_deltas(rb, ra) if rb and ra else
                                        {"note": "a bracket reading session has no rate_limits snapshot"})
        print(json.dumps(out, indent=2, ensure_ascii=False))
