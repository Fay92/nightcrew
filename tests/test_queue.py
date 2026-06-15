"""Tests for the JSON task queue, including lock-protected concurrency."""

import json
import threading

import pytest

from ccnight.queue import (
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_RUNNING,
    AmbiguousTaskId,
    StaleTask,
    Task,
    TaskNotFound,
    TaskQueue,
)


@pytest.fixture
def queue(ccnight_home):
    return TaskQueue(ccnight_home)


def test_add_and_roundtrip(queue):
    task = queue.add("write tests", "/tmp", "--model claude-sonnet-4-5")
    assert task.status == STATUS_PENDING
    assert task.created_at
    loaded = TaskQueue(queue.home).all()
    assert len(loaded) == 1
    assert loaded[0].id == task.id
    assert loaded[0].prompt == "write tests"
    assert loaded[0].repo == "/tmp"
    assert loaded[0].claude_args == "--model claude-sonnet-4-5"


def test_queue_file_is_versioned_json(queue):
    queue.add("a", "/tmp")
    raw = json.loads(queue.path.read_text())
    assert raw["version"] == 1
    assert isinstance(raw["tasks"], list)


def test_get_by_prefix(queue):
    task = queue.add("a", "/tmp")
    assert queue.get(task.id[:4]).id == task.id
    with pytest.raises(TaskNotFound):
        queue.get("zzzzzzzz")


def test_get_ambiguous_prefix(queue, monkeypatch):
    # Force two tasks with a shared prefix by writing them directly.
    tasks = [
        Task(id="abcd1111", prompt="x", repo="/tmp", created_at="2026-01-01T00:00:00+00:00"),
        Task(id="abcd2222", prompt="y", repo="/tmp", created_at="2026-01-01T00:00:00+00:00"),
    ]
    queue._write(tasks)
    with pytest.raises(AmbiguousTaskId):
        queue.get("abcd")


def test_update_fields(queue):
    task = queue.add("a", "/tmp")
    updated = queue.update(task.id, status=STATUS_DONE, session_id="s-1")
    assert updated.status == STATUS_DONE
    assert queue.get(task.id).session_id == "s-1"


def test_update_unknown_field_rejected(queue):
    task = queue.add("a", "/tmp")
    with pytest.raises(ValueError):
        queue.update(task.id, not_a_field=1)


def test_update_expect_status_guards_claims(queue):
    task = queue.add("a", "/tmp")
    queue.update(task.id, status=STATUS_RUNNING, expect_status=STATUS_PENDING)
    with pytest.raises(StaleTask):
        queue.update(task.id, status=STATUS_RUNNING, expect_status=STATUS_PENDING)


def test_unknown_json_fields_are_tolerated(queue):
    """Forward compatibility: extra fields written by a newer version."""
    queue.add("a", "/tmp")
    raw = json.loads(queue.path.read_text())
    raw["tasks"][0]["future_field"] = {"x": 1}
    queue.path.write_text(json.dumps(raw))
    assert len(queue.all()) == 1


def test_concurrent_adds_do_not_corrupt(queue):
    """Many writers racing through the lock leave a valid, complete queue."""
    threads = []
    errors = []

    def add_many(n):
        try:
            q = TaskQueue(queue.home)  # each thread gets its own handle/fd
            for i in range(5):
                q.add(f"task-{n}-{i}", "/tmp")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    for n in range(8):
        threads.append(threading.Thread(target=add_many, args=(n,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    tasks = queue.all()
    assert len(tasks) == 40
    assert len({t.id for t in tasks}) == 40
    json.loads(queue.path.read_text())  # file must still be valid JSON


def test_remove_by_prefix_and_unknown(ccnight_home):
    from ccnight.queue import TaskNotFound, TaskQueue

    q = TaskQueue(ccnight_home)
    task = q.add("doomed", "/tmp")
    removed = q.remove(task.id[:4])
    assert removed.id == task.id
    assert q.all() == []
    try:
        q.remove("ffffffff")
        raise AssertionError("expected TaskNotFound")
    except TaskNotFound:
        pass


def test_remove_running_requires_force(ccnight_home):
    from ccnight.queue import STATUS_RUNNING, StaleTask, TaskQueue

    q = TaskQueue(ccnight_home)
    task = q.add("busy", "/tmp")
    q.update(task.id, status=STATUS_RUNNING)
    try:
        q.remove(task.id)
        raise AssertionError("expected StaleTask")
    except StaleTask:
        pass
    assert q.remove(task.id, force=True).id == task.id


def test_requeue_failed_resets_state(ccnight_home):
    from ccnight.queue import TaskQueue, STATUS_FAILED, STATUS_PENDING

    q = TaskQueue(ccnight_home)
    t = q.add("job", "/tmp")
    q.update(t.id, status=STATUS_FAILED, error="boom", session_id="s1",
             started_at="x", finished_at="y")
    done = q.requeue(None, all_failed=True)
    assert len(done) == 1
    back = q.get(t.id)
    assert back.status == STATUS_PENDING
    assert back.error is None and back.session_id is None
    assert back.started_at is None and back.finished_at is None
