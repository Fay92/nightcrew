"""Integration tests: runner + limits against the fake claude binary.

The stub at tests/fakes/claude plays three scripted scenarios (success,
usage-limit wall, garbage output) selected by FAKE_CLAUDE_MODE.
"""

import datetime as dt
import json

import pytest

from ccnight import runner
from ccnight.queue import Task


def make_task(repo, **kw):
    defaults = dict(
        id="t1234567",
        prompt="demo task",
        repo=str(repo),
        created_at="2026-06-11T00:00:00+00:00",
    )
    defaults.update(kw)
    return Task(**defaults)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    return d


def test_successful_run(config, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    task = make_task(repo)
    outcome = runner.run_task(config, task)
    assert outcome.status == runner.COMPLETED
    assert outcome.session_id == "fake-sess-0001"
    assert outcome.reset_at is None
    assert "All done." in outcome.detail

    log = runner.log_path_for(config, task)
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    types = [l.get("type") for l in lines]
    assert "system" in types  # raw stream-json passthrough
    assert "result" in types
    assert types[0] == "ccnight.run"  # run metadata header
    assert types[-1] == "ccnight.outcome"


def test_limit_via_error_result_event(config, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit_json")
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT_TEXT", "5-hour limit reached ∙ resets 3am")
    outcome = runner.run_task(config, make_task(repo))
    assert outcome.status == runner.HIT_LIMIT
    assert outcome.session_id == "fake-sess-0001"
    assert outcome.reset_at is not None
    assert outcome.reset_at > dt.datetime.now().astimezone()
    assert outcome.reset_at.hour == 3


def test_limit_via_plain_text_with_epoch(config, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit_text")
    monkeypatch.setenv(
        "FAKE_CLAUDE_LIMIT_TEXT", "Claude AI usage limit reached|1893456000"
    )
    outcome = runner.run_task(config, make_task(repo))
    assert outcome.status == runner.HIT_LIMIT
    assert outcome.reset_at == dt.datetime.fromtimestamp(
        1893456000, tz=dt.timezone.utc
    )
    # Plain-text wall before any stream-json means no session id was seen.
    assert outcome.session_id is None


def test_limit_detected_but_time_unparseable(config, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit_json")
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT_TEXT", "You've reached your usage limit.")
    outcome = runner.run_task(config, make_task(repo))
    assert outcome.status == runner.HIT_LIMIT
    assert outcome.reset_at is None  # scheduler will fall back to probing


def test_garbage_output_is_failed_not_limit(config, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    outcome = runner.run_task(config, make_task(repo))
    assert outcome.status == runner.FAILED
    assert "exited with code 2" in outcome.detail


def test_missing_binary_is_failed(config, repo):
    config.claude_bin = "/nonexistent/claude-bin"
    outcome = runner.run_task(config, make_task(repo))
    assert outcome.status == runner.FAILED
    assert "failed to launch" in outcome.detail


def test_assistant_text_about_limits_is_not_a_false_positive(config, repo, monkeypatch):
    """The success transcript mentions limits; runner must not scan it."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    # The 'ok' scenario emits an assistant message and a successful result;
    # even if extra patterns are aggressive, success must win.
    config.extra_limit_patterns = ["all done"]
    outcome = runner.run_task(config, make_task(repo))
    assert outcome.status == runner.COMPLETED


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_fresh_command_shape(config, repo):
    task = make_task(repo)
    cmd = runner.build_command(config, task, resume=False)
    assert cmd[0] == config.claude_bin
    assert cmd[1:3] == ["-p", "demo task"]
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_resume_command_uses_session_and_continue_prompt(config, repo):
    task = make_task(repo, session_id="sess-42")
    cmd = runner.build_command(config, task, resume=True)
    assert cmd[cmd.index("--resume") + 1] == "sess-42"
    assert config.continue_prompt in cmd
    assert "demo task" not in cmd  # original prompt is not resent


def test_user_permission_mode_overrides_default(config, repo):
    task = make_task(repo, claude_args="--permission-mode plan")
    cmd = runner.build_command(config, task, resume=False)
    assert cmd.count("--permission-mode") == 1
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"


def test_resume_invocation_reaches_claude(config, repo, tmp_path, monkeypatch):
    """End to end: a task with a session id is resumed, not re-prompted."""
    args_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    task = make_task(repo, session_id="sess-42")
    outcome = runner.run_task(config, task)
    assert outcome.status == runner.COMPLETED
    argv = json.loads(args_file.read_text())
    assert argv[argv.index("--resume") + 1] == "sess-42"
    assert "continue" in argv


def test_build_command_appends_config_extra_args(config):
    from ccnight.queue import Task
    from ccnight.runner import build_command

    config.guardrails = False  # isolate the raw escape hatch
    config.claude_extra_args = '--disallowedTools "Bash(git commit:*)" "Bash(rm:*)"'
    task = Task(id="x1", prompt="do it", repo="/tmp", status="pending")
    cmd = build_command(config, task, resume=False)
    i = cmd.index("--disallowedTools")
    assert cmd[i + 1] == "Bash(git commit:*)" and cmd[i + 2] == "Bash(rm:*)"


def test_guardrails_on_by_default(config):
    from ccnight.queue import Task
    from ccnight.runner import build_command, DEFAULT_DENY_TOOLS, DEFAULT_ALLOW_TOOLS

    task = Task(id="g1", prompt="do it", repo="/tmp", status="pending")
    cmd = build_command(config, task, resume=False)
    assert "--allowedTools" in cmd and "--disallowedTools" in cmd
    assert "Bash(git commit:*)" in cmd  # a deny entry
    assert "Bash(./gradlew:*)" in cmd   # an allow entry
    # deny entries must all be present
    for d in DEFAULT_DENY_TOOLS:
        assert d in cmd


def test_guardrails_can_be_disabled(config):
    from ccnight.queue import Task
    from ccnight.runner import build_command

    config.guardrails = False
    task = Task(id="g2", prompt="x", repo="/tmp", status="pending")
    cmd = build_command(config, task, resume=False)
    assert "--allowedTools" not in cmd and "--disallowedTools" not in cmd


def test_custom_allow_deny_override_defaults(config):
    from ccnight.queue import Task
    from ccnight.runner import build_command

    config.allow_tools = ["Bash(./gradlew:*)", "Bash(custombuild:*)"]
    config.deny_tools = []
    task = Task(id="g3", prompt="x", repo="/tmp", status="pending")
    cmd = build_command(config, task, resume=False)
    i = cmd.index("--allowedTools")
    assert "Bash(custombuild:*)" in cmd
    assert "--disallowedTools" not in cmd  # empty deny list disables that half


def test_task_claude_args_allowedtools_suppresses_builtin(config):
    from ccnight.queue import Task
    from ccnight.runner import build_command

    task = Task(
        id="g4", prompt="x", repo="/tmp", status="pending",
        claude_args='--allowedTools "Bash(only:*)"',
    )
    cmd = build_command(config, task, resume=False)
    # task opted into its own allowlist; built-in preset stays out of the way
    assert cmd.count("--allowedTools") == 1
    assert "Bash(only:*)" in cmd


def test_append_system_prompt_injected(config):
    from ccnight.queue import Task
    from ccnight.runner import build_command

    config.append_system_prompt = "Work protocol: analyse, plan, execute, self-check."
    task = Task(id="sp1", prompt="do it", repo="/tmp", status="pending")
    cmd = build_command(config, task, resume=False)
    i = cmd.index("--append-system-prompt")
    assert cmd[i + 1] == "Work protocol: analyse, plan, execute, self-check."


def test_append_system_prompt_absent_by_default(config):
    from ccnight.queue import Task
    from ccnight.runner import build_command

    task = Task(id="sp2", prompt="x", repo="/tmp", status="pending")
    assert "--append-system-prompt" not in build_command(config, task, resume=False)
