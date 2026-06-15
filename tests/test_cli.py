"""CLI smoke tests: help for every subcommand plus the add/list/status flow."""

import pytest

from nightcrew.cli import main

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


def test_add_list_status_flow(nightcrew_home, capsys):
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


def test_add_rejects_missing_repo(nightcrew_home, capsys):
    rc = main(["add", "x", "--repo", "/definitely/not/a/dir"])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().err


def test_list_empty_queue(nightcrew_home, capsys):
    assert main(["list"]) == 0
    assert "queue is empty" in capsys.readouterr().out


def test_logs_for_unknown_task(nightcrew_home, capsys):
    rc = main(["logs", "deadbeef"])
    assert rc == 2
    assert "no task matches" in capsys.readouterr().err


def test_run_once_unknown_task(nightcrew_home, capsys):
    rc = main(["run-once", "deadbeef"])
    assert rc == 2


def test_run_once_executes_task(nightcrew_home, fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    # Point the runner at the fake binary via config.json on disk, exactly
    # like a user would.
    nightcrew_home.mkdir(parents=True, exist_ok=True)
    (nightcrew_home / "config.json").write_text(
        '{"claude_bin": "%s"}' % fake_claude
    )
    # Avoid real macOS notification banners during the test run.
    monkeypatch.setattr("nightcrew.notify._macos", lambda *a, **k: None)

    assert main(["add", "demo task", "--repo", "/tmp"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    capsys.readouterr()

    from nightcrew.queue import TaskQueue

    task_id = TaskQueue(nightcrew_home).all()[0].id
    rc = main(["run-once", task_id[:5]])  # prefix lookup
    out = capsys.readouterr().out
    assert rc == 0
    assert "-> done" in out

    assert main(["logs", task_id]) == 0
    out = capsys.readouterr().out
    assert '"type": "nightcrew.outcome"' in out or "nightcrew.outcome" in out


def test_add_warns_when_daemon_not_running(nightcrew_home, capsys):
    assert main(["add", "night task", "--repo", "/tmp"]) == 0
    captured = capsys.readouterr()
    assert "queued task" in captured.out
    assert "daemon is NOT running" in captured.err
    assert "caffeinate" in captured.err


def test_doctor_shows_guardrails(nightcrew_home, capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "unattended guardrails" in out
    assert "Bash(git commit:*)" in out   # a deny entry is shown
    assert "Bash(./gradlew:*)" in out    # an allow entry is shown


def test_doctor_reports_disabled_guardrails(nightcrew_home, capsys, monkeypatch):
    import json
    (nightcrew_home).mkdir(parents=True, exist_ok=True)
    (nightcrew_home / "config.json").write_text(json.dumps({"guardrails": False}))
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "DISABLED" in out


def test_build_plist_shape():
    from pathlib import Path
    from nightcrew.service import build_plist, LABEL

    p = build_plist(
        nightcrew_bin="/usr/local/bin/nightcrew",
        home=Path("/home/x/.config/nightcrew"), log_path=Path("/home/x/.config/nightcrew/daemon.log"),
        path_env="/usr/bin:/bin",
    )
    assert p["Label"] == LABEL
    # plist is stable; schedule lives in config.json, not the service args
    assert p["ProgramArguments"] == ["/usr/local/bin/nightcrew", "daemon"]
    assert p["RunAtLoad"] is True
    assert p["KeepAlive"] == {"SuccessfulExit": False}
    assert p["EnvironmentVariables"]["NIGHTCREW_HOME"] == "/home/x/.config/nightcrew"
    assert p["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"


def test_merge_config_preserves_existing(tmp_path):
    import json
    from nightcrew.service import _merge_config
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"notify_command": "x", "window": "23:00-07:00"}))
    _merge_config(p, {"window": "22:00-08:00"})
    data = json.loads(p.read_text())
    assert data["window"] == "22:00-08:00"   # updated
    assert data["notify_command"] == "x"     # preserved


def test_install_skill_copies_bundled(tmp_path, monkeypatch):
    from nightcrew import onboard
    dest = tmp_path / "skills" / "nightcrew"
    monkeypatch.setattr(onboard, "SKILL_DEST", dest)
    assert onboard.install_skill() == 0
    skill = dest / "SKILL.md"
    assert skill.exists()
    text = skill.read_text()
    assert "name: nightcrew" in text          # frontmatter intact
    assert "自包含 prompt" in text             # core guidance shipped


def test_setup_non_interactive_keeps_defaults(nightcrew_home, monkeypatch):
    import io
    from nightcrew import onboard
    from nightcrew.config import Config
    monkeypatch.setattr("sys.stdin", io.StringIO())  # isatty() -> False
    cfg = Config.load(home=nightcrew_home)
    assert onboard.setup(cfg) == 0  # graceful no-op, no crash
