"""Log retention: prune old per-task logs and cap the daemon log.

Two artifacts grow without bound in a tool that runs every night:

- one ``<task-id>.jsonl`` per task under ``logs_dir`` (the captured
  stream-json), kept forever;
- ``daemon.log`` -- the launchd stdout/stderr redirect -- which only ever
  appends.

This module prunes the first by age and caps the second by size, never
touching anything still in use. It is called at daemon startup, once a day
while the daemon runs, and on demand via ``nightcrew clean``.
"""

from __future__ import annotations

import time
from pathlib import Path


def prune_task_logs(
    logs_dir: Path, retention_days: int, *, now: float | None = None
) -> list[Path]:
    """Delete ``*.jsonl`` files last modified more than *retention_days* ago.

    Pruning by mtime (not queue membership) means a running or just-finished
    task -- whose log was written seconds ago -- is never touched, while logs
    orphaned by a removed task are still reclaimed. ``retention_days <= 0``
    disables pruning. Returns the paths removed.
    """
    if retention_days <= 0 or not logs_dir.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    removed: list[Path] = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue  # racing daemon write / permission -- skip, retry next pass
    return removed


def trim_daemon_log(log_path: Path, max_bytes: int) -> int:
    """Trim *log_path* to its last ~half of *max_bytes* once it exceeds it.

    Keeping the tail (rather than truncating to empty) preserves recent
    context. The same inode is reused, so launchd's open ``O_APPEND``
    descriptor stays valid and simply resumes appending at the new, smaller
    EOF. ``max_bytes <= 0`` disables the cap. Returns bytes reclaimed.

    There is a tiny race if launchd writes between our read and truncate, at
    worst dropping a few in-flight log bytes -- acceptable for an operational
    log, and the per-task ``.jsonl`` files (the logs that matter) are never
    touched here.
    """
    if max_bytes <= 0:
        return 0
    try:
        size = log_path.stat().st_size
    except OSError:
        return 0
    if size <= max_bytes:
        return 0
    keep = max_bytes // 2
    try:
        with open(log_path, "r+b") as fh:
            fh.seek(size - keep)
            tail = fh.read()
            fh.seek(0)
            fh.write(tail)
            fh.truncate()
    except OSError:
        return 0
    return size - len(tail)


def run_cleanup(config) -> tuple[int, int]:
    """Apply both retention policies from *config*.

    Returns ``(task_logs_removed, daemon_log_bytes_reclaimed)``.
    """
    removed = prune_task_logs(config.logs_dir, config.log_retention_days)
    reclaimed = trim_daemon_log(config.daemon_log_path, config.daemon_log_max_bytes)
    return len(removed), reclaimed
