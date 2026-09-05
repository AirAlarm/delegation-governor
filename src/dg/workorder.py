"""Bounded work orders (spec 10).

A work order is the *only* thing a worker sees. No transcript, no chat history,
no Claude reasoning -- just enough for the worker to inspect the repo itself and
finish autonomously, and written so a different worker can pick it up unchanged
after a fallback.
"""
from __future__ import annotations

from typing import Any

TEMPLATE = """# Work order {id} ({mode})

## Goal
{goal}

## Repository
{repo}
Working directory: {cwd}
{paths}
## Known current state
{state}

## Constraints
{constraints}

## Non-goals
{non_goals}

## Acceptance criteria
{acceptance}

## Required tests
{tests}

## Deliverable
{deliverable}
"""

_WRITE_DELIVERABLE = (
    "Edit files in the working directory above. It is an isolated git worktree; "
    "commit nothing, push nothing, and do not touch any path outside it. "
    "Finish with a two-to-five line summary of what changed and why."
)
_READ_DELIVERABLE = (
    "Change no files. Answer with findings only: the specific files, symbols and "
    "line references that support each conclusion."
)
_BASE_CONSTRAINTS = [
    "Stay inside the listed paths.",
    "Match the surrounding code's style; add no dependencies without saying so.",
    "Do not reformat, rename or refactor anything the goal did not ask for.",
]


def build(
    task: dict[str, Any],
    cwd: str,
    state: str = "",
    constraints: list[str] | None = None,
    non_goals: list[str] | None = None,
    acceptance: list[str] | None = None,
    tests: str = "",
) -> str:
    paths = task.get("paths") or []
    return TEMPLATE.format(
        id=task["id"],
        mode=task["mode"],
        goal=task.get("goal") or task["title"],
        repo=task.get("repo") or "(none)",
        cwd=cwd,
        paths=("Paths you own:\n" + "\n".join(f"- {p}" for p in paths) + "\n") if paths else "",
        state=state or "Not stated; inspect the repository to establish it.",
        constraints=_bullets((constraints or []) + _BASE_CONSTRAINTS),
        non_goals=_bullets(non_goals or ["Anything not named in the goal."]),
        acceptance=_bullets(acceptance or ["The goal above is met and nothing else changed."]),
        tests=tests or "None specified; if the repo has a test suite for the touched code, run it.",
        deliverable=_WRITE_DELIVERABLE if task["mode"] == "WRITE" else _READ_DELIVERABLE,
    )


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"
