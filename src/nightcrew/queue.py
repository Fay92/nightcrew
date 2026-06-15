"""Persistent task queue backed by a JSON file plus a lock file.

The queue lives at ``<home>/queue.json``. Every read-modify-write cycle
takes an exclusive advisory lock on ``<home>/queue.lock`` so that
``nightcrew add`` and the daemon can never corrupt the file by writing
concurrently. Writes are atomic (temp file + ``os.replace``).

Stdlib only: ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_BLOCKED_LIMIT = "blocked_limit"
STATUS_RETRY = "retry"  # transient platform/network error — backing off
STATUS_DONE = "done"
STATUS_FAILED = "failed"
ALL_STATUSES = (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_BLOCKED_LIMIT,
    STATUS_RETRY,
    STATUS_DONE,
    STATUS_FAILED,
)

QUEUE_FILE_NAME = "queue.json"
LOCK_FILE_NAME = "queue.lock"
QUEUE_FORMAT_VERSION = 1


class TaskNotFound(KeyError):
    """No task matches the given id or prefix."""


class AmbiguousTaskId(ValueError):
    """A task id prefix matches more than one task."""


class StaleTask(RuntimeError):
    """The task changed status under us (lost a claim race)."""


class QueueLockTimeout(RuntimeError):
    """Could not acquire the queue lock in time."""


def utcnow_iso() -> str:
    """UTC timestamp in ISO-8601, the format used for all task fields."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    prompt: str
    repo: str
    claude_args: str | None = None
    status: str = STATUS_PENDING
    session_id: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    log_file: str | None = None
    # ISO timestamp at which the usage limit is expected to lift
    # (None when the limit message could not be parsed).
    reset_at: str | None = None
    # When the task entered blocked_limit; drives the 30-minute probe
    # backoff when reset_at is unknown.
    blocked_at: str | None = None
    # Consecutive transient-error retries; drives exponential backoff.
    retry_count: int = 0
    # ISO timestamp of the next retry attempt (set on transient errors).
    retry_at: str | None = None
    # Failure reason, for display in list/status.
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Task":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


class FileLock:
    """Exclusive advisory file lock (context manager).

    Polls with a non-blocking acquire so we can enforce a timeout instead
    of hanging forever on a wedged peer.
    """

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fh = None

    def _acquire_nonblocking(self) -> None:
        if os.name == "nt":  # pragma: no cover - exercised on Windows only
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        if os.name == "nt":  # pragma: no cover
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire_nonblocking()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise QueueLockTimeout(
                        f"could not lock {self.path} within {self.timeout}s"
                    ) from None
                time.sleep(0.05)

    def __exit__(self, *exc_info) -> None:
        if self._fh is not None:
            try:
                self._release()
            except OSError:
                pass
            self._fh.close()
            self._fh = None


class TaskQueue:
    """JSON-file task queue with locked read-modify-write operations."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.path = home / QUEUE_FILE_NAME
        self.lock_path = home / LOCK_FILE_NAME

    # -- raw storage -----------------------------------------------------

    def _read(self) -> list[Task]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        items = raw.get("tasks", []) if isinstance(raw, dict) else raw
        return [Task.from_dict(item) for item in items]

    def _write(self, tasks: list[Task]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": QUEUE_FORMAT_VERSION,
            "tasks": [t.to_dict() for t in tasks],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def _lock(self) -> FileLock:
        return FileLock(self.lock_path)

    # -- public API ------------------------------------------------------

    def add(self, prompt: str, repo: str, claude_args: str | None = None) -> Task:
        with self._lock():
            tasks = self._read()
            existing = {t.id for t in tasks}
            task_id = uuid.uuid4().hex[:8]
            while task_id in existing:
                task_id = uuid.uuid4().hex[:8]
            task = Task(
                id=task_id,
                prompt=prompt,
                repo=repo,
                claude_args=claude_args,
                created_at=utcnow_iso(),
            )
            tasks.append(task)
            self._write(tasks)
            return task

    def all(self) -> list[Task]:
        with self._lock():
            return self._read()

    def get(self, ref: str) -> Task:
        """Look up a task by full id or unique id prefix."""
        tasks = self.all()
        exact = [t for t in tasks if t.id == ref]
        if exact:
            return exact[0]
        matches = [t for t in tasks if t.id.startswith(ref)]
        if not matches:
            raise TaskNotFound(f"no task matches {ref!r}")
        if len(matches) > 1:
            ids = ", ".join(t.id for t in matches)
            raise AmbiguousTaskId(f"{ref!r} matches several tasks: {ids}")
        return matches[0]

    def remove(self, ref: str, *, force: bool = False) -> Task:
        """Delete a task by id or unique prefix and return the removed task.

        Running tasks are protected unless *force* is set: deleting one from
        under an active daemon would only desync the queue from reality.
        """
        with self._lock():
            tasks = self._read()
            matches = [t for t in tasks if t.id == ref] or [
                t for t in tasks if t.id.startswith(ref)
            ]
            if not matches:
                raise TaskNotFound(f"no task matches {ref!r}")
            if len(matches) > 1:
                ids = ", ".join(t.id for t in matches)
                raise AmbiguousTaskId(f"{ref!r} matches several tasks: {ids}")
            task = matches[0]
            if task.status == STATUS_RUNNING and not force:
                raise StaleTask(
                    f"task {task.id} is running; stop the daemon first "
                    "or pass --force"
                )
            self._write([t for t in tasks if t.id != task.id])
            return task

    def requeue(self, ref: str | None, *, all_failed: bool = False) -> list[Task]:
        """Reset failed/done tasks back to pending for a fresh retry.

        Clears the prior run's state (error, session id, timestamps). Running
        tasks are never touched.
        """
        with self._lock():
            tasks = self._read()
            if all_failed:
                targets = [t for t in tasks if t.status == STATUS_FAILED]
            else:
                matches = [t for t in tasks if t.id == ref] or [
                    t for t in tasks if ref and t.id.startswith(ref)
                ]
                if not matches:
                    raise TaskNotFound(f"no task matches {ref!r}")
                if len(matches) > 1:
                    ids = ", ".join(t.id for t in matches)
                    raise AmbiguousTaskId(f"{ref!r} matches several tasks: {ids}")
                if matches[0].status == STATUS_RUNNING:
                    raise StaleTask(f"task {matches[0].id} is running")
                targets = matches
            for t in targets:
                t.status = STATUS_PENDING
                t.error = None
                t.session_id = None
                t.started_at = t.finished_at = t.reset_at = t.blocked_at = None
            if targets:
                self._write(tasks)
            return targets

    def update(
        self, task_id: str, *, expect_status: str | None = None, **changes
    ) -> Task:
        """Atomically apply field changes to one task.

        When *expect_status* is given the update only succeeds if the task
        still has that status; otherwise :class:`StaleTask` is raised. This
        is how the daemon claims a task without racing ``run-once``.
        """
        valid = {f.name for f in fields(Task)}
        unknown = set(changes) - valid
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        with self._lock():
            tasks = self._read()
            for task in tasks:
                if task.id != task_id:
                    continue
                if expect_status is not None and task.status != expect_status:
                    raise StaleTask(
                        f"task {task_id} is {task.status!r}, expected {expect_status!r}"
                    )
                for key, value in changes.items():
                    setattr(task, key, value)
                self._write(tasks)
                return task
        raise TaskNotFound(f"no task with id {task_id!r}")
