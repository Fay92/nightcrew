"""Execute one queued task with the Claude Code CLI in headless mode.

Runs ``claude -p ... --output-format stream-json --verbose`` inside the
task's repository, streams every stdout line into the per-task log file,
captures the session id, and classifies the outcome as completed /
hit_limit / failed.

Only error-ish text is scanned for limit messages: non-JSON stdout lines,
stderr, and ``result`` events that signal an error. Assistant transcript
content is deliberately *not* scanned, so the model merely talking about
limits cannot trigger a false positive.
"""

from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import limits
from .config import Config
from .queue import Task, utcnow_iso

COMPLETED = "completed"
HIT_LIMIT = "hit_limit"
FAILED = "failed"

# Built-in guardrails for unattended runs. Since nobody is at the keyboard to
# approve a prompt, headless claude silently *denies* anything not allowed, so
# we both deny dangerous commands outright (deny wins over allow) and allow the
# build/read-only commands a task needs to verify itself. This ships safe by
# default — installing nightcrew is enough, no settings.json edits required.
DEFAULT_DENY_TOOLS: tuple[str, ...] = (
    # never commit / push / rewrite history or trash uncommitted work
    "Bash(git commit:*)", "Bash(git push:*)", "Bash(git add:*)",
    "Bash(git reset:*)", "Bash(git checkout:*)", "Bash(git restore:*)",
    "Bash(git stash:*)", "Bash(git clean:*)", "Bash(git rebase:*)",
    # never delete
    "Bash(rm:*)", "Bash(rmdir:*)",
    # never download / install / escalate
    "Bash(curl:*)", "Bash(wget:*)", "Bash(git clone:*)", "Bash(brew:*)",
    "Bash(npm install:*)", "Bash(pnpm install:*)", "Bash(yarn add:*)",
    "Bash(pip install:*)", "Bash(pip3 install:*)", "Bash(sudo:*)",
)
DEFAULT_ALLOW_TOOLS: tuple[str, ...] = (
    "Read", "Write", "Edit", "Grep", "Glob",
    # build / test runners
    "Bash(./gradlew:*)", "Bash(gradle:*)", "Bash(mvn:*)",
    "Bash(npm run:*)", "Bash(pnpm run:*)", "Bash(yarn:*)", "Bash(make:*)",
    "Bash(cargo:*)", "Bash(go build:*)", "Bash(go test:*)", "Bash(pytest:*)",
    # read-only inspection
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git show:*)", "Bash(git branch:*)",
    "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(ls:*)",
    "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(echo:*)",
)


@dataclass
class RunOutcome:
    """Structured result of a single claude run."""

    status: str  # completed | hit_limit | failed
    session_id: str | None = None
    reset_at: datetime | None = None
    detail: str = ""


def build_command(config: Config, task: Task, *, resume: bool) -> list[str]:
    """Build the claude invocation for *task*.

    Fresh run:   claude -p "<prompt>" --output-format stream-json --verbose
    Resumed run: claude -p --resume <session_id> "<continue_prompt>" ...

    ``--permission-mode`` is appended from config unless the task's own
    ``claude_args`` already set one (user args win).
    """
    cmd = [config.claude_bin, "-p"]
    if resume and task.session_id:
        cmd += ["--resume", task.session_id, config.continue_prompt]
    else:
        cmd.append(task.prompt)
    cmd += ["--output-format", "stream-json", "--verbose"]
    user_args = shlex.split(task.claude_args) if task.claude_args else []
    if config.model and "--model" not in user_args:
        cmd += ["--model", config.model]
    if config.permission_mode and "--permission-mode" not in user_args:
        cmd += ["--permission-mode", config.permission_mode]
    if config.append_system_prompt and "--append-system-prompt" not in user_args:
        cmd += ["--append-system-prompt", config.append_system_prompt]
    if config.guardrails and "--allowedTools" not in user_args:
        allow = config.allow_tools if config.allow_tools is not None else DEFAULT_ALLOW_TOOLS
        deny = config.deny_tools if config.deny_tools is not None else DEFAULT_DENY_TOOLS
        if allow:
            cmd += ["--allowedTools", *allow]
        if deny:
            cmd += ["--disallowedTools", *deny]
    if config.claude_extra_args:
        cmd += shlex.split(config.claude_extra_args)
    cmd += user_args
    return cmd


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the subprocess and everything it spawned (gradle, tests, ...).

    The child runs in its own process group (start_new_session), so killing
    the group reaps orphaned build processes that would otherwise keep burning
    CPU and quota after a timeout. Falls back to killing just the parent.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def log_path_for(config: Config, task: Task) -> Path:
    return config.logs_dir / f"{task.id}.jsonl"


def _meta(log, kind: str, **payload) -> None:
    """Write a nightcrew-namespaced JSON line into the task log."""
    log.write(json.dumps({"type": f"nightcrew.{kind}", "at": utcnow_iso(), **payload}) + "\n")


def run_task(config: Config, task: Task) -> RunOutcome:
    """Run *task* once (resuming when it already has a session id)."""
    resume = bool(task.session_id)
    cmd = build_command(config, task, resume=resume)
    log_path = log_path_for(config, task)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    session_id = task.session_id
    result_event: dict | None = None
    suspicious: list[str] = []  # lines eligible for limit scanning

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=task.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Own process group so a timeout can kill the whole tree (claude
            # plus any gradle/test child it spawned), not just the parent.
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        return RunOutcome(FAILED, session_id, None, f"failed to launch {cmd[0]}: {exc}")

    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line.rstrip("\n"))

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    timed_out = threading.Event()
    watchdog: threading.Timer | None = None
    if config.task_timeout_seconds:

        def _kill() -> None:
            timed_out.set()
            _kill_process_tree(proc)

        watchdog = threading.Timer(config.task_timeout_seconds, _kill)
        watchdog.daemon = True
        watchdog.start()

    # Stall watchdog: if no new output arrives for stall_timeout_seconds the
    # task is hung (or spinning silently) -- kill it so the daemon moves on.
    stalled = threading.Event()
    last_output = [time.monotonic()]
    stall_stop = threading.Event()
    stall_seconds = config.stall_timeout_seconds
    if stall_seconds:

        def _stall_monitor() -> None:
            tick = max(5.0, min(30.0, stall_seconds / 4))
            while not stall_stop.wait(tick):
                if time.monotonic() - last_output[0] > stall_seconds:
                    stalled.set()
                    _kill_process_tree(proc)
                    return

        threading.Thread(target=_stall_monitor, daemon=True).start()

    with open(log_path, "a", encoding="utf-8") as log:
        _meta(log, "run", command=cmd, resume=resume, task_id=task.id)
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            last_output[0] = time.monotonic()
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            log.write(line + "\n")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                suspicious.append(line)
                continue
            if not isinstance(event, dict):
                suspicious.append(line)
                continue
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            if event.get("type") == "result":
                result_event = event
                subtype = str(event.get("subtype", ""))
                if event.get("is_error") or subtype.startswith("error"):
                    for key in ("result", "error", "message"):
                        value = event.get(key)
                        if isinstance(value, str):
                            suspicious.append(value)

        returncode = proc.wait()
        if watchdog is not None:
            watchdog.cancel()
        stall_stop.set()
        stderr_thread.join(timeout=5)
        for line in stderr_lines:
            if line.strip():
                _meta(log, "stderr", text=line)
                suspicious.append(line)

        outcome = _classify(
            config,
            returncode=returncode,
            result_event=result_event,
            suspicious=suspicious,
            session_id=session_id,
            timed_out=timed_out.is_set(),
            stalled=stalled.is_set(),
        )
        _meta(
            log,
            "outcome",
            status=outcome.status,
            session_id=outcome.session_id,
            reset_at=outcome.reset_at.isoformat() if outcome.reset_at else None,
            detail=outcome.detail,
        )
    return outcome


def _classify(
    config: Config,
    *,
    returncode: int,
    result_event: dict | None,
    suspicious: list[str],
    session_id: str | None,
    timed_out: bool,
    stalled: bool = False,
) -> RunOutcome:
    if stalled:
        return RunOutcome(
            FAILED,
            session_id,
            None,
            f"stalled: no output for {config.stall_timeout_seconds}s, killed and skipped",
        )
    if timed_out:
        return RunOutcome(
            FAILED,
            session_id,
            None,
            f"timed out after {config.task_timeout_seconds}s and was killed",
        )

    if (
        returncode == 0
        and result_event is not None
        and not result_event.get("is_error")
        and result_event.get("subtype") == "success"
    ):
        snippet = str(result_event.get("result", ""))[:200]
        return RunOutcome(COMPLETED, session_id, None, snippet)

    scan_text = "\n".join(suspicious)
    hit = limits.find_limit(scan_text, config.extra_limit_patterns)
    if hit is not None:
        return RunOutcome(HIT_LIMIT, session_id, hit.reset_at, hit.matched_text)

    tail = suspicious[-1][:200] if suspicious else ""
    detail = f"claude exited with code {returncode}"
    if result_event is not None and result_event.get("subtype"):
        detail += f" (result subtype: {result_event.get('subtype')})"
    if tail:
        detail += f": {tail}"
    return RunOutcome(FAILED, session_id, None, detail)
