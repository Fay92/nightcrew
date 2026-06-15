"""Worktree isolation: sibling path, create/reuse, failure handling."""

import subprocess

import pytest

from nightcrew import worktree


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def gitrepo(tmp_path):
    repo = tmp_path / "myproj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "f.txt").write_text("hi")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_worktree_path_is_sibling():
    assert str(worktree.worktree_path("/a/b/myproj")) == "/a/b/myproj_worktree"


def test_ensure_worktree_creates_on_work_branch(gitrepo):
    wt = worktree.ensure_worktree(str(gitrepo))
    assert wt.exists()
    assert wt.name == "myproj_worktree"
    head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == worktree.WORK_BRANCH
    # main checkout is untouched (still on main)
    main_head = subprocess.run(
        ["git", "-C", str(gitrepo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert main_head == "main"


def test_ensure_worktree_reuses_existing(gitrepo):
    wt1 = worktree.ensure_worktree(str(gitrepo))
    wt2 = worktree.ensure_worktree(str(gitrepo))  # must not error
    assert wt1 == wt2


def test_ensure_worktree_non_git_raises(tmp_path):
    with pytest.raises(RuntimeError):
        worktree.ensure_worktree(str(tmp_path / "not-a-repo"))
