"""CLI smoke tests: help for every subcommand plus the add/list/status flow."""

import pytest

from ccnight.cli import main

SUBCOMMANDS = ["add", "list", "status", "daemon", "run-once", "logs"]


def test_top_level_help_lists_all_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in out


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_each_subcommand_has_help(name, capsys):
    with pytest.raises(SystemExit) as exc:
        main([name, "--help"])
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_add_list_status_flow(ccnight_home, capsys):
    assert main(["add", "demo task", "--repo", "/tmp"]) == 0
    out = capsys.readouterr().out
    assert "queued task" in out

    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "demo task" in out
    assert "pending" in out

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "1 pending" in out
    assert "daemon: not running" in out
    assert "limit:  not blocked" in out


def test_add_rejects_missing_repo(ccnight_home, capsys):
    rc = main(["add", "x", "--repo", "/definitely/not/a/dir"])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


def test_list_empty_queue(ccnight_home, capsys):
    assert main(["list"]) == 0
    assert "queue is empty" in capsys.readouterr().out


def test_logs_for_unknown_task(ccnight_home, capsys):
    rc = main(["logs", "deadbeef"])
    assert rc == 2
    assert "no task matches" in capsys.readouterr().err


def test_run_once_unknown_task(ccnight_home, capsys):
    rc = main(["run-once", "deadbeef"])
    assert rc == 2


def test_run_once_executes_task(ccnight_home, fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    # Point the runner at the fake binary via config.json on disk, exactly
    # like a user would.
    ccnight_home.mkdir(parents=True, exist_ok=True)
    (ccnight_home / "config.json").write_text(
        '{"claude_bin": "%s"}' % fake_claude
    )
    # Avoid real macOS notification banners during the test run.
    monkeypatch.setattr("ccnight.notify._macos", lambda *a, **k: None)

    assert main(["add", "demo task", "--repo", "/tmp"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    capsys.readouterr()

    from ccnight.queue import TaskQueue

    task_id = TaskQueue(ccnight_home).all()[0].id
    rc = main(["run-once", task_id[:5]])  # prefix lookup
    out = capsys.readouterr().out
    assert rc == 0
    assert "-> done" in out

    assert main(["logs", task_id]) == 0
    out = capsys.readouterr().out
    assert '"type": "ccnight.outcome"' in out or "ccnight.outcome" in out
