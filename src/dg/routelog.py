"""Append-only log of worker lane routing decisions."""
from __future__ import annotations

import json
import time
from typing import Any

from . import config


def append_route(record: dict[str, Any]) -> dict[str, Any]:
    routed = dict(record)
    routed["ts"] = time.time()
    config.HOME.mkdir(parents=True, exist_ok=True)
    with (config.HOME / "routing.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(routed) + "\n")
    return routed


def read_routes() -> list[dict[str, Any]]:
    path = config.HOME / "routing.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text("utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
