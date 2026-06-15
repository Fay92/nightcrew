"""Per-repo git worktree isolation for unattended runs.

When enabled, a repo's nightly tasks run in a sibling worktree named
``<repo>_worktree`` on the ``nightcrew-work`` branch (branched from the repo's
current branch). Tasks on the same repo share that worktree so dependent
milestones accumulate, while the user's daytime checkout is never touched.
Review and merge back are deliberately manual.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

WORK_BRANCH = "nightcrew-work"


def worktree_path(repo: str) -> Path:
    """Sibling worktree path: ``<parent>/<repo-name>_worktree``."""
    p = Path(repo)
    return p.parent / f"{p.name}_worktree"


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def ensure_worktree(repo: str, branch: str = WORK_BRANCH) -> Path:
    """Create or reuse the sibling worktree for *repo*; return its path.

    Raises RuntimeError on any git failure so the caller can fail the task
    rather than silently fall back to the main checkout (which would defeat
    the isolation the user asked for).
    """
    wt = worktree_path(repo)
    listing = _git(repo, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        raise RuntimeError(f"{repo} is not a git repo: {listing.stderr.strip()}")
    if str(wt.resolve()) in listing.stdout or str(wt) in listing.stdout:
        return wt  # already registered — reuse (dependent tasks accumulate)
    if wt.exists():
        raise RuntimeError(f"{wt} exists but is not a registered worktree")

    base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
    branch_exists = _git(
        repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0
    add = (
        ["worktree", "add", str(wt), branch] if branch_exists
        else ["worktree", "add", str(wt), "-b", branch, base]
    )
    res = _git(repo, *add)
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {res.stderr.strip()}")
    return wt


def existing_worktrees(repos: list[str]) -> list[tuple[str, Path]]:
    """For each distinct repo, return (repo, worktree_path) if it exists."""
    out: list[tuple[str, Path]] = []
    for r in dict.fromkeys(repos):  # de-dupe, preserve order
        wt = worktree_path(r)
        if wt.exists():
            out.append((r, wt))
    return out
