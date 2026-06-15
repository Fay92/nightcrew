"""Command line interface for nightcrew.

Subcommands: add, list, status, daemon, run-once, logs, remove, doctor,
install-service, uninstall-service.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, daemon as scheduler, runner
from .config import Config
from .queue import (
    STATUS_BLOCKED_LIMIT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    AmbiguousTaskId,
    StaleTask,
    TaskNotFound,
    TaskQueue,
    utcnow_iso,
)


def _fmt_local(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _one_line(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


# ---------------------------------------------------------------------------
# Subcommand handlers (each returns a process exit code)
# ---------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace, config: Config) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"nightcrew: error: --repo {repo} is not a directory", file=sys.stderr)
        return 2
    queue = TaskQueue(config.home)
    task = queue.add(args.prompt, str(repo), args.claude_args)
    print(f"queued task {task.id}")
    print(f"  prompt: {_one_line(task.prompt, 70)}")
    print(f"  repo:   {task.repo}")
    if task.claude_args:
        print(f"  args:   {task.claude_args}")
    pid = scheduler.read_daemon_pid(config)
    if pid:
        print(f"daemon: running (pid {pid}) - it will pick this task up")
    else:
        print(
            "\n"
            "  WARNING: the daemon is NOT running - queued tasks will never start.\n"
            "  Before you walk away, launch it (plugged in, lid open):\n"
            '    caffeinate -i nightcrew daemon --window "23:30-08:00"\n',
            file=sys.stderr,
        )
    return 0


def cmd_list(args: argparse.Namespace, config: Config) -> int:
    tasks = TaskQueue(config.home).all()
    if not tasks:
        print("queue is empty - add a task with: nightcrew add \"<prompt>\" --repo <path>")
        return 0
    header = f"{'ID':<10}{'STATUS':<15}{'CREATED':<18}{'REPO':<28}PROMPT"
    print(header)
    print("-" * len(header))
    home = str(Path.home())
    for task in tasks:
        repo = task.repo.replace(home, "~", 1)
        print(
            f"{task.id:<10}"
            f"{task.status:<15}"
            f"{_fmt_local(task.created_at):<18}"
            f"{_one_line(repo, 26):<28}"
            f"{_one_line(task.prompt, 50)}"
        )
    return 0


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    queue = TaskQueue(config.home)
    tasks = queue.all()
    counts = {
        status: sum(1 for t in tasks if t.status == status)
        for status in (
            STATUS_PENDING,
            STATUS_RUNNING,
            STATUS_BLOCKED_LIMIT,
            STATUS_DONE,
            STATUS_FAILED,
        )
    }
    summary = ", ".join(f"{n} {status}" for status, n in counts.items())
    print(f"queue:  {len(tasks)} task(s) - {summary}")

    pid = scheduler.read_daemon_pid(config)
    print(f"daemon: running (pid {pid})" if pid else "daemon: not running")

    blocked, wake = scheduler.limit_block_state(tasks, scheduler.local_now())
    if blocked:
        if wake is not None:
            print(f"limit:  BLOCKED - next wake {wake.astimezone():%Y-%m-%d %H:%M %Z}")
        else:
            print("limit:  BLOCKED - reset time unknown, probing every 30 min")
    else:
        print("limit:  not blocked")
    print(f"home:   {config.home}")
    return 0


def cmd_daemon(args: argparse.Namespace, config: Config) -> int:
    window = None
    window_str = args.window or config.window  # --window overrides config file
    if window_str:
        try:
            window = scheduler.parse_window(window_str)
        except ValueError as exc:
            print(f"nightcrew: error: {exc}", file=sys.stderr)
            return 2
    reserve = args.reserve if args.reserve is not None else config.reserve
    if reserve is not None and not 0 <= reserve <= 99:
        print("nightcrew: error: reserve must be between 0 and 99", file=sys.stderr)
        return 2
    return scheduler.run_daemon(
        config,
        window=window,
        reserve=reserve,
        dry=args.dry_run,
        caffeinate=not args.no_caffeinate,
    )


def cmd_run_once(args: argparse.Namespace, config: Config) -> int:
    queue = TaskQueue(config.home)
    try:
        task = queue.get(args.task_id)
    except (TaskNotFound, AmbiguousTaskId) as exc:
        print(f"nightcrew: error: {exc}", file=sys.stderr)
        return 2
    if task.status == STATUS_RUNNING:
        print(f"nightcrew: error: task {task.id} is already running", file=sys.stderr)
        return 2
    if not scheduler.preflight_ok(config):
        print("nightcrew: preflight failed (e.g. IP/VPN not ready); not running",
              file=sys.stderr)
        return 2
    claimed = queue.update(
        task.id,
        status=STATUS_RUNNING,
        started_at=task.started_at or utcnow_iso(),
        expect_status=task.status,
    )
    resume = bool(claimed.session_id)
    print(f"running task {claimed.id} ({'resume' if resume else 'fresh run'})...")
    outcome = runner.run_task(config, claimed)
    applied = scheduler.apply_outcome(config, queue, claimed, outcome)
    print(f"task {applied.id} -> {applied.status}")
    if outcome.detail:
        print(f"  detail: {_one_line(outcome.detail, 200)}")
    if outcome.reset_at:
        print(f"  reset at: {outcome.reset_at:%Y-%m-%d %H:%M %Z}")
    print(f"  log: {runner.log_path_for(config, applied)}")
    return 0 if applied.status == STATUS_DONE else 1


def cmd_install_service(args: argparse.Namespace, config: Config) -> int:
    from . import service
    return service.install(config, window=args.window, reserve=args.reserve)


def cmd_uninstall_service(args: argparse.Namespace, config: Config) -> int:
    from . import service
    return service.uninstall()


def cmd_retry(args: argparse.Namespace, config: Config) -> int:
    queue = TaskQueue(config.home)
    try:
        if args.all:
            done = queue.requeue(None, all_failed=True)
        elif args.task_id:
            done = queue.requeue(args.task_id)
        else:
            print("nightcrew: error: give a task id or --all", file=sys.stderr)
            return 2
    except (TaskNotFound, AmbiguousTaskId, StaleTask) as exc:
        print(f"nightcrew: error: {exc}", file=sys.stderr)
        return 2
    if not done:
        print("no failed tasks to retry")
    for t in done:
        print(f"requeued {t.id} -> pending")
    return 0


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """Print the effective configuration and unattended guardrails."""
    allow = config.allow_tools if config.allow_tools is not None else runner.DEFAULT_ALLOW_TOOLS
    deny = config.deny_tools if config.deny_tools is not None else runner.DEFAULT_DENY_TOOLS

    print("nightcrew configuration")
    print(f"  home:            {config.home}")
    print(f"  config file:     {config.config_path}"
          + ("" if config.config_path.exists() else "  (none yet, using defaults)"))
    claude_path = shutil.which(config.claude_bin)
    print(f"  claude binary:   {claude_path or config.claude_bin + ' (NOT FOUND on PATH)'}")
    print(f"  model:           {config.model or '(CLI default)'}")
    print(f"  window:          {config.window or 'always (no window set)'}")
    print(f"  preflight:       {config.preflight_command or '(none)'}")
    print(f"  permission mode: {config.permission_mode or '(none)'}")
    timeout = f"{config.task_timeout_seconds}s" if config.task_timeout_seconds else "none"
    print(f"  task timeout:    {timeout}")
    stall = f"{config.stall_timeout_seconds}s (kill if no output)" if config.stall_timeout_seconds else "off"
    print(f"  stall watchdog:  {stall}")
    if config.append_system_prompt:
        first = config.append_system_prompt.strip().splitlines()[0]
        print(f"  work protocol:   on ({first[:50]}...)")
    else:
        print("  work protocol:   off (no --append-system-prompt)")

    print("\nnotifications")
    if config.notify_command:
        print(f"  command:         {config.notify_command}")
    if config.webhook_url:
        print(f"  webhook:         {config.webhook_url}  (format: {config.webhook_format})")
    if not config.notify_command and not config.webhook_url:
        print("  (none configured - desktop notification only on macOS)")

    print("\nunattended guardrails")
    if not config.guardrails:
        print("  DISABLED (guardrails=false) - relying on the project's own permissions")
        return 0
    src = "custom" if config.allow_tools is not None else "built-in default"
    print(f"  allow ({src}, {len(allow)}): commands claude may run unattended")
    for tool in allow:
        print(f"    + {tool}")
    src = "custom" if config.deny_tools is not None else "built-in default"
    print(f"  deny ({src}, {len(deny)}): always blocked (deny wins over allow)")
    for tool in deny:
        print(f"    - {tool}")
    return 0


def cmd_remove(args: argparse.Namespace, config: Config) -> int:
    queue = TaskQueue(config.home)
    try:
        task = queue.remove(args.task_id, force=args.force)
    except (TaskNotFound, AmbiguousTaskId, StaleTask) as exc:
        print(f"nightcrew: error: {exc}", file=sys.stderr)
        return 2
    print(f"removed task {task.id} ({task.status})")
    return 0


def cmd_logs(args: argparse.Namespace, config: Config) -> int:
    queue = TaskQueue(config.home)
    try:
        task = queue.get(args.task_id)
    except (TaskNotFound, AmbiguousTaskId) as exc:
        print(f"nightcrew: error: {exc}", file=sys.stderr)
        return 2
    log_path = Path(task.log_file) if task.log_file else runner.log_path_for(config, task)
    if not log_path.exists():
        print(f"no log yet for task {task.id} (expected at {log_path})")
        return 1
    print(log_path.read_text(encoding="utf-8"), end="")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nightcrew",
        description=(
            "Quota-aware task queue for Claude Code: queue prompts, run them "
            "unattended, pause on usage limits and resume when the quota window "
            "resets."
        ),
        epilog="Run 'nightcrew <command> --help' for details on a command.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p_add = sub.add_parser(
        "add",
        help="queue a task for the daemon to run",
        description="Queue a prompt to run headlessly inside a repository.",
    )
    p_add.add_argument("prompt", help="the prompt to send to claude -p")
    p_add.add_argument(
        "--repo",
        required=True,
        help="directory the claude process will run in (the project to work on)",
    )
    p_add.add_argument(
        "--claude-args",
        default=None,
        metavar="ARGS",
        help=(
            "extra arguments appended to the claude invocation, e.g. "
            "\"--model claude-sonnet-4-5 --permission-mode plan\" "
            "(overrides the default --permission-mode)"
        ),
    )
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser(
        "list",
        help="list queued tasks and their states",
        description="List every task in the queue with status and timestamps.",
    )
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser(
        "status",
        help="show queue, daemon and limit status",
        description=(
            "Show queue counts, whether the daemon is running, whether a usage "
            "limit currently blocks the queue, and the next wake-up time."
        ),
    )
    p_status.set_defaults(func=cmd_status)

    p_daemon = sub.add_parser(
        "daemon",
        help="run the scheduling loop in the foreground",
        description=(
            "Process the queue: run pending tasks, pause on usage limits, "
            "resume sessions when the limit resets, send notifications. "
            "Runs in the foreground - keep it in tmux or launchd."
        ),
    )
    p_daemon.add_argument(
        "--window",
        metavar="HH:MM-HH:MM",
        default=None,
        help="only run tasks inside this daily time window, e.g. 00:00-08:00 "
        "(may cross midnight, e.g. 23:00-07:00)",
    )
    p_daemon.add_argument(
        "--reserve",
        type=int,
        default=None,
        metavar="PERCENT",
        help="keep this percentage of the current 5h usage window for "
        "interactive use; the queue holds once the ccusage estimate crosses "
        "100-PERCENT (requires ccusage, degrades gracefully without it)",
    )
    p_daemon.add_argument(
        "--dry-run",
        action="store_true",
        help="print the scheduling decision for the current queue and exit "
        "without calling claude",
    )
    p_daemon.add_argument(
        "--no-caffeinate",
        action="store_true",
        help="do not auto-prevent system sleep (macOS keeps awake by default "
        "so overnight tasks are not frozen by sleep)",
    )
    p_daemon.set_defaults(func=cmd_daemon)

    p_once = sub.add_parser(
        "run-once",
        help="run a single task immediately (debugging)",
        description=(
            "Run one task right now, ignoring the daemon window and reserve. "
            "Accepts a full task id or a unique prefix."
        ),
    )
    p_once.add_argument("task_id", help="task id (or unique prefix) to run")
    p_once.set_defaults(func=cmd_run_once)

    p_logs = sub.add_parser(
        "logs",
        help="print the stream-json log of a task",
        description="Print the captured stream-json log for a task.",
    )
    p_logs.add_argument("task_id", help="task id (or unique prefix)")
    p_logs.set_defaults(func=cmd_logs)

    p_remove = sub.add_parser(
        "remove",
        help="delete a task from the queue",
        description="Delete a task by id. Running tasks need --force.",
    )
    p_remove.add_argument("task_id", help="task id (or unique prefix)")
    p_remove.add_argument(
        "--force",
        action="store_true",
        help="also remove a task that is currently running",
    )
    p_remove.set_defaults(func=cmd_remove)

    p_retry = sub.add_parser(
        "retry",
        help="re-queue failed tasks so the daemon runs them again",
        description="Reset a failed task back to pending (fresh run). Use --all "
        "to re-queue every failed task at once.",
    )
    p_retry.add_argument("task_id", nargs="?", help="task id or unique prefix")
    p_retry.add_argument("--all", action="store_true", help="re-queue all failed tasks")
    p_retry.set_defaults(func=cmd_retry)

    p_doctor = sub.add_parser(
        "doctor",
        help="show the effective config and unattended guardrails",
        description="Print where state lives, notification setup, and the "
        "allow/deny tool guardrails applied to every unattended run.",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_install = sub.add_parser(
        "install-service",
        help="(macOS) run the daemon as an always-on background service",
        description="Install a LaunchAgent so the scheduling daemon starts on "
        "login and stays running. After this, just `nightcrew add` - no need to "
        "start the daemon manually.",
    )
    p_install.add_argument("--window", metavar="HH:MM-HH:MM", default=None,
                           help="daily run window for the service (e.g. 22:00-08:00)")
    p_install.add_argument("--reserve", type=int, default=None, metavar="PERCENT",
                           help="interaction reserve percentage (see daemon --reserve)")
    p_install.set_defaults(func=cmd_install_service)

    p_uninstall = sub.add_parser(
        "uninstall-service",
        help="(macOS) remove the always-on background service",
        description="Unload and delete the nightcrew LaunchAgent.",
    )
    p_uninstall.set_defaults(func=cmd_uninstall_service)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load()
    config.ensure_dirs()
    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        print("\nnightcrew: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
