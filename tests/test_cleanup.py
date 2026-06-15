"""Log retention: prune old task logs by age, cap daemon.log by size."""

import os
import time

from nightcrew import cleanup
from nightcrew.daemon import run_daemon


def _touch(path, *, age_days=0.0, size=0):
    path.write_bytes(b"x" * size)
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))


# --- prune_task_logs --------------------------------------------------------


def test_prune_removes_old_keeps_fresh(tmp_path):
    old = tmp_path / "old.jsonl"
    fresh = tmp_path / "fresh.jsonl"
    _touch(old, age_days=30)
    _touch(fresh, age_days=1)

    removed = cleanup.prune_task_logs(tmp_path, retention_days=14)

    assert removed == [old]
    assert not old.exists()
    assert fresh.exists()


def test_prune_disabled_when_retention_zero(tmp_path):
    old = tmp_path / "old.jsonl"
    _touch(old, age_days=999)
    assert cleanup.prune_task_logs(tmp_path, retention_days=0) == []
    assert old.exists()


def test_prune_only_touches_jsonl(tmp_path):
    log = tmp_path / "old.jsonl"
    other = tmp_path / "old.txt"
    _touch(log, age_days=30)
    _touch(other, age_days=30)

    cleanup.prune_task_logs(tmp_path, retention_days=14)

    assert not log.exists()
    assert other.exists()  # not a .jsonl -- never our business


def test_prune_missing_dir_is_noop(tmp_path):
    assert cleanup.prune_task_logs(tmp_path / "nope", retention_days=14) == []


# --- trim_daemon_log --------------------------------------------------------


def test_trim_keeps_tail_over_cap(tmp_path):
    log = tmp_path / "daemon.log"
    body = bytes(range(256)) * 80  # 20480 bytes, deterministic content
    log.write_bytes(body)

    reclaimed = cleanup.trim_daemon_log(log, max_bytes=4096)

    kept = log.read_bytes()
    assert len(kept) == 2048  # max_bytes // 2
    assert kept == body[-2048:]  # the most recent bytes survive
    assert reclaimed == len(body) - 2048


def test_trim_noop_under_cap(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_bytes(b"small")
    assert cleanup.trim_daemon_log(log, max_bytes=4096) == 0
    assert log.read_bytes() == b"small"


def test_trim_disabled_when_zero(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_bytes(b"y" * 10_000)
    assert cleanup.trim_daemon_log(log, max_bytes=0) == 0
    assert len(log.read_bytes()) == 10_000


def test_trim_missing_file_is_noop(tmp_path):
    assert cleanup.trim_daemon_log(tmp_path / "nope.log", max_bytes=4096) == 0


# --- daemon integration -----------------------------------------------------


def test_daemon_prunes_old_logs_at_startup(config):
    """run_daemon cleans before its first loop, so a restart reclaims space."""
    old = config.logs_dir / "stale.jsonl"
    _touch(old, age_days=30)  # config default retention is 14 days

    run_daemon(config, max_loops=0, caffeinate=False)

    assert not old.exists()
