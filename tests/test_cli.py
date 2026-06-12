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


def test_add_warns_when_daemon_not_running(ccnight_home, capsys):
    assert main(["add", "night task", "--repo", "/tmp"]) == 0
    captured = capsys.readouterr()
    assert "queued task" in captured.out
    assert "daemon is NOT running" in captured.err
    assert "caffeinate" in captured.err


def test_doctor_shows_guardrails(ccnight_home, capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "unattended guardrails" in out
    assert "Bash(git commit:*)" in out   # a deny entry is shown
    assert "Bash(./gradlew:*)" in out    # an allow entry is shown


def test_doctor_reports_disabled_guardrails(ccnight_home, capsys, monkeypatch):
    import json
    (ccnight_home).mkdir(parents=True, exist_ok=True)
    (ccnight_home / "config.json").write_text(json.dumps({"guardrails": False}))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "DISABLED" in out


def test_build_plist_shape():
    from pathlib import Path
    from ccnight.service import build_plist, LABEL

    p = build_plist(
        ccnight_bin="/usr/local/bin/ccnight", window="22:00-08:00", reserve=None,
        home=Path("/home/x/.config/ccnight"), log_path=Path("/home/x/.config/ccnight/daemon.log"),
        path_env="/usr/bin:/bin",
    )
    assert p["Label"] == LABEL
    assert p["ProgramArguments"] == ["/usr/local/bin/ccnight", "daemon", "--window", "22:00-08:00"]
    assert p["RunAtLoad"] is True
    assert p["KeepAlive"] == {"SuccessfulExit": False}
    assert p["EnvironmentVariables"]["CCNIGHT_HOME"] == "/home/x/.config/ccnight"
    assert p["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"


def test_build_plist_includes_reserve_when_set():
    from pathlib import Path
    from ccnight.service import build_plist

    p = build_plist(
        ccnight_bin="ccnight", window=None, reserve=20,
        home=Path("/h"), log_path=Path("/h/log"), path_env="",
    )
    assert "--reserve" in p["ProgramArguments"]
    assert "--window" not in p["ProgramArguments"]
