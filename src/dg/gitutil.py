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
from typing import Any

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


def current_branch(repo: str) -> str | None:
    p = git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    return p.stdout.strip() or None


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


def is_merged(repo: str, branch: str) -> bool:
    """Is every commit on `branch` already reachable from HEAD?"""
    return git(repo, "merge-base", "--is-ancestor", branch, "HEAD",
               check=False).returncode == 0


def _patch_id(repo: str, commit: str) -> str | None:
    shown = git(repo, "show", "--pretty=format:", "--binary", commit, check=False)
    if shown.returncode != 0:
        return None
    p = subprocess.run(["git", "patch-id", "--stable"], input=shown.stdout,
                       capture_output=True, text=True)
    return p.stdout.split()[0] if p.returncode == 0 and p.stdout.split() else None


def integration_state(repo: str, branch: str, base: str) -> dict[str, Any]:
    """Verify a merge by ancestry or a cherry-pick by stable patch identity."""
    if is_merged(repo, branch):
        return {"integrated": True, "method": "merge", "missingPatchIds": []}
    worker = git(repo, "rev-list", "--reverse", "--no-merges", f"{base}..{branch}",
                 check=False).stdout.split()
    main = git(repo, "rev-list", "--no-merges", f"{base}..HEAD", check=False).stdout.split()
    worker_ids = [p for c in worker if (p := _patch_id(repo, c))]
    main_ids = {p for c in main if (p := _patch_id(repo, c))}
    missing = [p for p in worker_ids if p not in main_ids]
    return {"integrated": bool(worker_ids) and not missing, "method": "cherry-pick",
            "missingPatchIds": missing, "workerCommits": worker}


def snapshot(worktree: str, message: str) -> bool:
    """Commit whatever is loose in the worktree onto its branch. True if it did.

    A worker leaves its output uncommitted, so removing the worktree would
    throw it away. Committing first means the branch is a durable copy.
    """
    if not changed_files(worktree):
        return False
    git(worktree, "add", "-A", check=False)
    return git(worktree, "-c", "user.email=dg@localhost", "-c", "user.name=delegation-governor",
               "commit", "-m", message, check=False).returncode == 0


def remove_worktree(repo: str, worktree: str, branch: str | None = None,
                    force: bool = False) -> dict[str, Any]:
    """Retire a worktree without losing work.

    Loose changes are committed to the branch first, and the branch itself is
    only deleted once its commits are reachable from HEAD -- otherwise cleanup
    would silently destroy output nobody has merged yet. `force` overrides.
    """
    res: dict[str, Any] = {"worktreeRemoved": False, "branchDeleted": False,
                           "snapshotted": False, "keptBranch": None}
    if Path(worktree).exists():
        res["snapshotted"] = snapshot(worktree, "wip(dg): snapshot before cleanup")
        res["worktreeRemoved"] = git(repo, "worktree", "remove", "--force", worktree,
                                     check=False).returncode == 0
    git(repo, "worktree", "prune", check=False)
    if branch:
        if force or is_merged(repo, branch):
            res["branchDeleted"] = git(repo, "branch", "-D", branch,
                                       check=False).returncode == 0
        else:
            res["keptBranch"] = branch
            res["reason"] = (f"{branch} is not merged into HEAD; kept so the work is not "
                             f"lost. Merge it, or re-run with --force to discard.")
    return res
