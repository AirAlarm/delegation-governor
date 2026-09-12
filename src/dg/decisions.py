"""Append-only log of supervisor delegation decisions."""
from __future__ import annotations

import json
import time
from typing import Any

from . import config


DECISION_TYPES = ("delegate", "keep", "takeover")


def append_decision(
        decision: str, task: str, reason: str,
        related_task_id: str | None = None) -> dict[str, Any]:
    if decision not in DECISION_TYPES:
        raise ValueError(f"unknown decision type: {decision}")
    record = {
        "ts": time.time(),
        "decision": decision,
        "task": task,
        "reason": reason,
        "related_task_id": related_task_id,
    }
    config.HOME.mkdir(parents=True, exist_ok=True)
    with (config.HOME / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def read_decisions() -> list[dict[str, Any]]:
    path = config.HOME / "decisions.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text("utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
