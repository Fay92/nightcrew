"""The scheduler: decide what to run next and react to usage limits.

The daemon is a plain foreground process (run it under tmux, launchd or
systemd - see the README for a launchd example). Each loop iteration
takes a snapshot of the queue and computes a single :class:`Decision`;
``--dry-run`` prints the decision for the current snapshot and exits
without calling Claude or mutating any state.

Scheduling rules, in priority order:

1. A task already running (e.g. via ``run-once``) - wait.
2. Outside the configured time window - sleep until the window opens.
3. Interaction reserve engaged (ccusage estimate above threshold) - hold.
4. Limit-blocked tasks govern the queue globally: resume the earliest one
   when its reset time has passed (or probe every 30 minutes when the
   reset time is unknown); otherwise sleep until the earliest reset.
5. Run the oldest pending task.
6. Queue empty - idle poll.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

from . import runner
from .config import Config
from .notify import notify
from .queue import (
    STATUS_BLOCKED_LIMIT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    StaleTask,
    Task,
    TaskQueue,
    utcnow_iso,
)

POLL_SECONDS = 30
IDLE_POLL_SECONDS = 120
PROBE_INTERVAL = timedelta(minutes=30)
RESERVE_RECHECK_SECONDS = 300
CCUSAGE_TIMEOUT_SECONDS = 60


def local_now() -> datetime:
    return datetime.now().astimezone()


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------


@dataclass
class TimeWindow:
    """A daily clock window; start > end means it crosses midnight."""

    start: dtime
    end: dtime

    def contains(self, moment: dtime) -> bool:
        if self.start == self.end:  # degenerate spec means "always open"
            return True
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end

    def next_start(self, now: datetime) -> datetime:
        candidate = now.replace(
            hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_window(spec: str) -> TimeWindow:
    """Parse ``"HH:MM-HH:MM"`` (e.g. ``"00:00-08:00"``, ``"23:00-07:00"``)."""
    try:
        raw_start, raw_end = spec.split("-", 1)
        start = dtime.fromisoformat(raw_start.strip())
        end = dtime.fromisoformat(raw_end.strip())
    except ValueError as exc:
        raise ValueError(
            f"invalid window {spec!r}, expected HH:MM-HH:MM (e.g. 00:00-08:00)"
        ) from exc
    return TimeWindow(start, end)


# ---------------------------------------------------------------------------
# Interaction reserve via ccusage (optional)
# ---------------------------------------------------------------------------


def usage_percent_from_blocks(data: object) -> float | None:
    """Best-effort percentage of the current 5h billing window already used.

    ccusage exposes token counts but no hard limit, so the baseline is the
    largest completed block on record - the same heuristic as ccusage's
    ``--token-limit max``. Returns ``None`` when no estimate is possible.
    """
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return None
    active: float | None = None
    baseline = 0.0
    for block in blocks:
        if not isinstance(block, dict) or block.get("isGap"):
            continue
        tokens = block.get("totalTokens")
        if not isinstance(tokens, (int, float)):
            continue
        if block.get("isActive"):
            active = float(tokens)
        else:
            baseline = max(baseline, float(tokens))
    if active is None:
        return 0.0  # no active block: fresh window, nothing used yet
    if baseline <= 0:
        return None  # no history to compare against
    return min(100.0, active / baseline * 100.0)


def _ccusage_command() -> list[str] | None:
    if shutil.which("ccusage"):
        return ["ccusage"]
    if shutil.which("npx"):
        return ["npx", "-y", "ccusage"]
    return None


def estimate_usage_percent(timeout: float = CCUSAGE_TIMEOUT_SECONDS) -> float | None:
    """Run ccusage and estimate current-window usage; None on any failure."""
    cmd = _ccusage_command()
    if cmd is None:
        return None
    try:
        proc = subprocess.run(
            cmd + ["blocks", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return usage_percent_from_blocks(data)


# ---------------------------------------------------------------------------
# Decision making
# ---------------------------------------------------------------------------

ACTION_RUN = "run"
ACTION_RESUME = "resume"
ACTION_WAIT_WINDOW = "wait_window"
ACTION_HOLD_RESERVE = "hold_reserve"
ACTION_WAIT_RESET = "wait_reset"
ACTION_BUSY = "busy"
ACTION_IDLE = "idle"


@dataclass
class Decision:
    action: str
    task: Task | None = None
    wake_at: datetime | None = None
    reason: str = ""


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _next_wake_for_blocked(task: Task, now: datetime) -> datetime:
    reset = _parse_iso(task.reset_at)
    if reset is not None:
        return reset
    blocked = _parse_iso(task.blocked_at)
    if blocked is not None:
        return blocked + PROBE_INTERVAL
    return now  # no information at all: probe immediately


def decide(
    tasks: list[Task],
    *,
    now: datetime,
    window: TimeWindow | None = None,
    reserve: int | None = None,
    usage_percent: float | None = None,
) -> Decision:
    """Compute the single next scheduling action for a queue snapshot."""
    running = [t for t in tasks if t.status == STATUS_RUNNING]
    pending = sorted(
        (t for t in tasks if t.status == STATUS_PENDING), key=lambda t: t.created_at
    )
    blocked = [t for t in tasks if t.status == STATUS_BLOCKED_LIMIT]

    if running:
        return Decision(
            ACTION_BUSY,
            task=running[0],
            wake_at=now + timedelta(seconds=POLL_SECONDS),
            reason=f"task {running[0].id} is already running",
        )
    if not pending and not blocked:
        return Decision(ACTION_IDLE, reason="queue has no runnable tasks")

    if window is not None and not window.contains(now.time()):
        wake = window.next_start(now)
        return Decision(
            ACTION_WAIT_WINDOW,
            wake_at=wake,
            reason=f"outside window {window}, next start {wake:%Y-%m-%d %H:%M}",
        )

    if reserve is not None and usage_percent is not None:
        threshold = 100 - reserve
        if usage_percent >= threshold:
            return Decision(
                ACTION_HOLD_RESERVE,
                wake_at=now + timedelta(seconds=RESERVE_RECHECK_SECONDS),
                reason=(
                    f"current window usage {usage_percent:.0f}% >= {threshold}% "
                    f"threshold (reserve {reserve}% for interactive use)"
                ),
            )

    if blocked:
        # A usage limit is account-wide: while any task is limit-blocked,
        # starting a pending task would hit the same wall.
        due = [t for t in blocked if _next_wake_for_blocked(t, now) <= now]
        if due:
            task = min(due, key=lambda t: _next_wake_for_blocked(t, now))
            if task.reset_at:
                reason = f"reset time {task.reset_at} has passed"
            else:
                reason = "reset time unknown - probing (30 min backoff)"
            return Decision(ACTION_RESUME, task=task, reason=reason)
        wake = min(_next_wake_for_blocked(t, now) for t in blocked)
        return Decision(
            ACTION_WAIT_RESET,
            wake_at=wake,
            reason=f"usage limit active, waking at {wake:%Y-%m-%d %H:%M}",
        )

    return Decision(ACTION_RUN, task=pending[0], reason="next pending task (FIFO)")


def limit_block_state(tasks: list[Task], now: datetime) -> tuple[bool, datetime | None]:
    """Used by ``ccnight status``: (limit currently blocking?, next wake)."""
    blocked = [t for t in tasks if t.status == STATUS_BLOCKED_LIMIT]
    if not blocked:
        return False, None
    return True, min(_next_wake_for_blocked(t, now) for t in blocked)


# ---------------------------------------------------------------------------
# Outcome application (shared by daemon and run-once)
# ---------------------------------------------------------------------------


def apply_outcome(
    config: Config, queue: TaskQueue, task: Task, outcome: runner.RunOutcome
) -> Task:
    """Persist a run outcome onto the task and send notifications."""
    log_file = str(runner.log_path_for(config, task))
    short_prompt = task.prompt[:60] + ("..." if len(task.prompt) > 60 else "")
    if outcome.status == runner.COMPLETED:
        updated = queue.update(
            task.id,
            status=STATUS_DONE,
            session_id=outcome.session_id,
            finished_at=utcnow_iso(),
            log_file=log_file,
            reset_at=None,
            blocked_at=None,
            error=None,
        )
        notify(config, "ccnight: task done", f"[{task.id}] {short_prompt}")
    elif outcome.status == runner.HIT_LIMIT:
        reset_iso = outcome.reset_at.isoformat() if outcome.reset_at else None
        updated = queue.update(
            task.id,
            status=STATUS_BLOCKED_LIMIT,
            session_id=outcome.session_id,
            reset_at=reset_iso,
            blocked_at=utcnow_iso(),
            log_file=log_file,
        )
        if outcome.reset_at:
            when = f"{outcome.reset_at:%Y-%m-%d %H:%M}"
            message = f"queue paused, resuming at {when}"
        else:
            message = "queue paused, reset time unknown (probing every 30 min)"
        notify(config, "ccnight: usage limit hit", message)
    else:
        updated = queue.update(
            task.id,
            status=STATUS_FAILED,
            session_id=outcome.session_id,
            finished_at=utcnow_iso(),
            log_file=log_file,
            error=outcome.detail[:500],
        )
        notify(config, "ccnight: task failed", f"[{task.id}] {outcome.detail[:120]}")
    return updated


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_daemon_pid(config: Config) -> int | None:
    """Return the live daemon pid, or None (cleans up stale pid files)."""
    try:
        pid = int(config.pid_path.read_text().strip())
    except (OSError, ValueError):
        return None
    if _pid_alive(pid):
        return pid
    try:
        config.pid_path.unlink()
    except OSError:
        pass
    return None


def _recover_stale_running(queue: TaskQueue) -> None:
    """Reset tasks left in 'running' by a crashed daemon back to pending.

    Their session id is kept, so the next run resumes instead of starting
    over.
    """
    for task in queue.all():
        if task.status == STATUS_RUNNING:
            print(f"[daemon] recovering stale running task {task.id} -> pending")
            try:
                queue.update(task.id, status=STATUS_PENDING, expect_status=STATUS_RUNNING)
            except StaleTask:
                pass


def _counts_line(tasks: list[Task]) -> str:
    by_status = {status: 0 for status in
                 (STATUS_PENDING, STATUS_RUNNING, STATUS_BLOCKED_LIMIT, STATUS_DONE, STATUS_FAILED)}
    for task in tasks:
        by_status[task.status] = by_status.get(task.status, 0) + 1
    return ", ".join(f"{count} {status}" for status, count in by_status.items())


def _describe_execution(config: Config, task: Task) -> tuple[str, str]:
    """Human-readable (command, cwd) preview for a task."""
    cmd = runner.build_command(config, task, resume=bool(task.session_id))
    return shlex.join(cmd), task.repo


def dry_run(
    config: Config,
    *,
    window: TimeWindow | None = None,
    reserve: int | None = None,
) -> int:
    """Evaluate the scheduler once, print the decision, change nothing."""
    queue = TaskQueue(config.home)
    tasks = queue.all()
    now = local_now()
    usage = estimate_usage_percent() if reserve is not None else None

    print("ccnight daemon (dry-run)")
    print(f"  home:     {config.home}")
    print(f"  window:   {window if window else 'always (no --window)'}")
    if reserve is not None:
        usage_text = f"{usage:.0f}%" if usage is not None else "unavailable (ccusage missing or unparseable)"
        print(f"  reserve:  {reserve}% (current window usage: {usage_text})")
    else:
        print("  reserve:  disabled")
    print(f"  queue:    {_counts_line(tasks)}")

    decision = decide(tasks, now=now, window=window, reserve=reserve, usage_percent=usage)
    print(f"  decision: {decision.action} - {decision.reason}")
    if decision.task is not None and decision.action in (ACTION_RUN, ACTION_RESUME):
        command, cwd = _describe_execution(config, decision.task)
        print(f"  task:     {decision.task.id} \"{decision.task.prompt[:60]}\"")
        print(f"  command:  {command}")
        print(f"  cwd:      {cwd}")
    if decision.wake_at is not None:
        print(f"  wake at:  {decision.wake_at:%Y-%m-%d %H:%M:%S}")
    print("  (dry-run: nothing was executed, queue unchanged)")
    return 0


def run_daemon(
    config: Config,
    *,
    window: TimeWindow | None = None,
    reserve: int | None = None,
    dry: bool = False,
    poll_seconds: float = POLL_SECONDS,
    idle_seconds: float = IDLE_POLL_SECONDS,
    max_loops: int | None = None,
) -> int:
    """Foreground scheduling loop. *max_loops* exists for tests."""
    if dry:
        return dry_run(config, window=window, reserve=reserve)

    existing = read_daemon_pid(config)
    if existing is not None:
        print(f"ccnight: daemon already running (pid {existing})", file=sys.stderr)
        return 1
    config.ensure_dirs()
    config.pid_path.write_text(str(os.getpid()))

    queue = TaskQueue(config.home)
    _recover_stale_running(queue)
    print(
        f"[daemon] started (pid {os.getpid()}), window={window or 'always'}, "
        f"reserve={reserve if reserve is not None else 'off'}, home={config.home}"
    )

    last_logged = ""
    loops = 0
    try:
        while max_loops is None or loops < max_loops:
            loops += 1
            tasks = queue.all()
            now = local_now()
            usage = estimate_usage_percent() if reserve is not None else None
            decision = decide(
                tasks, now=now, window=window, reserve=reserve, usage_percent=usage
            )

            if decision.action in (ACTION_RUN, ACTION_RESUME):
                last_logged = ""
                task = decision.task
                assert task is not None
                print(f"[daemon] {decision.action} task {task.id}: {decision.reason}")
                try:
                    claimed = queue.update(
                        task.id,
                        status=STATUS_RUNNING,
                        started_at=task.started_at or utcnow_iso(),
                        expect_status=task.status,
                    )
                except StaleTask:
                    continue  # someone else (run-once) grabbed it
                outcome = runner.run_task(config, claimed)
                applied = apply_outcome(config, queue, claimed, outcome)
                print(
                    f"[daemon] task {applied.id} -> {applied.status}"
                    + (f" ({outcome.detail[:120]})" if outcome.detail else "")
                )
                continue  # re-evaluate immediately

            log_line = f"[daemon] {decision.action}: {decision.reason}"
            if log_line != last_logged:
                print(log_line)
                last_logged = log_line

            if decision.action == ACTION_IDLE:
                sleep_for = idle_seconds
            elif decision.wake_at is not None:
                sleep_for = min(
                    poll_seconds, max(1.0, (decision.wake_at - now).total_seconds())
                )
            else:
                sleep_for = poll_seconds
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\n[daemon] interrupted, exiting")
    finally:
        try:
            config.pid_path.unlink()
        except OSError:
            pass
    return 0
