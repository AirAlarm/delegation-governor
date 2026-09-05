"""Minimal git helpers. Worktrees only -- the user's working tree is never touched.

Worktrees live under the Governor's own state directory, not inside the repo:
a `.dg/` directory in the repo would show up as untracked in the user's
`git status` and would need a .gitignore entry written into their project.
Outside the repo, the user's tree stays byte-for-byte clean.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from . import config


def git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p


def is_repo(path: str) -> bool:
    return subprocess.run(["git", "-C", path, "rev-parse", "--git-dir"],
                          capture_output=True).returncode == 0


def head_commit(repo: str) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def worktree_root(repo: str) -> Path:
    """Per-repo worktree area inside the Governor's state dir.

    The basename keeps it recognisable; the hash keeps two same-named repos
    from colliding.
    """
    resolved = str(Path(repo).resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
    return config.HOME / "worktrees" / f"{Path(resolved).name}-{digest}"


def create_worktree(repo: str, task_id: str, base: str | None = None) -> dict[str, str]:
    """Disposable branch + worktree off `base` (default: current HEAD commit).

    Basing on the commit rather than the branch name means a later checkout in
    the user's tree cannot move the ground under a running worker.
    """
    repo = str(Path(repo).resolve())
    base = base or head_commit(repo)
    branch = f"dg/{task_id.lower()}"
    wt = worktree_root(repo) / task_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(wt), base)
    return {"worktree": str(wt), "branch": branch, "baseCommit": base}


def diff(worktree: str) -> str:
    """Everything the worker changed, staged or not, including new files."""
    git(worktree, "add", "-A", check=False)
    return git(worktree, "diff", "--cached", check=False).stdout


def changed_files(worktree: str) -> list[str]:
    out = git(worktree, "status", "--porcelain", check=False).stdout
    return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]


def remove_worktree(repo: str, worktree: str, branch: str | None = None) -> dict[str, bool]:
    res = {"worktreeRemoved": False, "branchDeleted": False}
    if Path(worktree).exists():
        res["worktreeRemoved"] = git(repo, "worktree", "remove", "--force", worktree,
                                     check=False).returncode == 0
    git(repo, "worktree", "prune", check=False)
    if branch:
        res["branchDeleted"] = git(repo, "branch", "-D", branch, check=False).returncode == 0
    return res
