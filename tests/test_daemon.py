"""Scheduler tests: window math, reserve estimation, decisions, dry-run,
and a full daemon mini-loop against the fake claude binary."""

import datetime as dt

import pytest

from ccnight import daemon
from ccnight.queue import (
    STATUS_BLOCKED_LIMIT,
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_RUNNING,
    Task,
    TaskQueue,
)

NOW = dt.datetime(2026, 6, 11, 2, 0, 0).astimezone()  # 02:00 local


def make_task(status=STATUS_PENDING, **kw):
    defaults = dict(
        id="aaaa0001",
        prompt="demo",
        repo="/tmp",
        status=status,
        created_at="2026-06-11T00:00:00+00:00",
    )
    defaults.update(kw)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------


def test_parse_window():
    w = daemon.parse_window("00:00-08:00")
    assert w.contains(dt.time(2, 0))
    assert not w.contains(dt.time(9, 0))


def test_parse_window_cross_midnight():
    w = daemon.parse_window("23:00-07:00")
    assert w.contains(dt.time(23, 30))
    assert w.contains(dt.time(3, 0))
    assert not w.contains(dt.time(12, 0))


def test_parse_window_invalid():
    with pytest.raises(ValueError):
        daemon.parse_window("8am-4pm")


def test_window_next_start():
    w = daemon.parse_window("23:00-07:00")
    now = NOW  # 02:00
    nxt = w.next_start(now)
    assert nxt > now
    assert (nxt.hour, nxt.minute) == (23, 0)


# ---------------------------------------------------------------------------
# ccusage-based reserve estimation
# ---------------------------------------------------------------------------


def blocks_payload(active_tokens, history_tokens):
    blocks = [
        {"isActive": False, "isGap": False, "totalTokens": t} for t in history_tokens
    ]
    if active_tokens is not None:
        blocks.append({"isActive": True, "isGap": False, "totalTokens": active_tokens})
    blocks.append({"isActive": False, "isGap": True, "totalTokens": 0})
    return {"blocks": blocks}


def test_usage_percent_basic():
    pct = daemon.usage_percent_from_blocks(blocks_payload(50, [100, 80]))
    assert pct == pytest.approx(50.0)


def test_usage_percent_caps_at_100():
    pct = daemon.usage_percent_from_blocks(blocks_payload(250, [100]))
    assert pct == 100.0


def test_usage_percent_no_active_block_means_fresh_window():
    assert daemon.usage_percent_from_blocks(blocks_payload(None, [100])) == 0.0


def test_usage_percent_without_history_is_unknown():
    assert daemon.usage_percent_from_blocks(blocks_payload(50, [])) is None


def test_usage_percent_garbage_payloads():
    assert daemon.usage_percent_from_blocks(None) is None
    assert daemon.usage_percent_from_blocks({"nope": 1}) is None
    assert daemon.usage_percent_from_blocks({"blocks": "x"}) is None


# ---------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------


def test_decide_empty_queue_idles():
    d = daemon.decide([], now=NOW)
    assert d.action == daemon.ACTION_IDLE


def test_decide_runs_oldest_pending_first():
    t_old = make_task(id="aaaa0001", created_at="2026-06-11T00:00:00+00:00")
    t_new = make_task(id="aaaa0002", created_at="2026-06-11T01:00:00+00:00")
    d = daemon.decide([t_new, t_old], now=NOW)
    assert d.action == daemon.ACTION_RUN
    assert d.task.id == "aaaa0001"


def test_decide_waits_outside_window():
    window = daemon.parse_window("03:00-05:00")  # NOW is 02:00
    d = daemon.decide([make_task()], now=NOW, window=window)
    assert d.action == daemon.ACTION_WAIT_WINDOW
    assert (d.wake_at.hour, d.wake_at.minute) == (3, 0)
    assert d.wake_at > NOW


def test_decide_holds_when_reserve_threshold_crossed():
    d = daemon.decide(
        [make_task()], now=NOW, reserve=20, usage_percent=85.0
    )
    assert d.action == daemon.ACTION_HOLD_RESERVE
    assert "85%" in d.reason


def test_decide_runs_below_reserve_threshold():
    d = daemon.decide([make_task()], now=NOW, reserve=20, usage_percent=42.0)
    assert d.action == daemon.ACTION_RUN


def test_decide_runs_when_usage_unknown():
    """ccusage unavailable -> reserve degrades gracefully to 'run'."""
    d = daemon.decide([make_task()], now=NOW, reserve=20, usage_percent=None)
    assert d.action == daemon.ACTION_RUN


def test_decide_waits_for_known_reset_in_future():
    reset = (NOW + dt.timedelta(hours=2)).isoformat()
    blocked = make_task(status=STATUS_BLOCKED_LIMIT, reset_at=reset, session_id="s1")
    pending = make_task(id="bbbb0001")
    d = daemon.decide([blocked, pending], now=NOW)
    # The limit is account-wide: do not start pending work while blocked.
    assert d.action == daemon.ACTION_WAIT_RESET
    assert d.wake_at == dt.datetime.fromisoformat(reset)


def test_decide_resumes_after_reset_passed():
    reset = (NOW - dt.timedelta(minutes=1)).isoformat()
    blocked = make_task(status=STATUS_BLOCKED_LIMIT, reset_at=reset, session_id="s1")
    d = daemon.decide([blocked], now=NOW)
    assert d.action == daemon.ACTION_RESUME
    assert d.task.id == blocked.id


def test_decide_unknown_reset_probes_after_backoff():
    recently = (NOW - dt.timedelta(minutes=5)).isoformat()
    blocked = make_task(status=STATUS_BLOCKED_LIMIT, blocked_at=recently)
    d = daemon.decide([blocked], now=NOW)
    assert d.action == daemon.ACTION_WAIT_RESET  # only 5 min since block
    long_ago = (NOW - dt.timedelta(minutes=31)).isoformat()
    blocked = make_task(status=STATUS_BLOCKED_LIMIT, blocked_at=long_ago)
    d = daemon.decide([blocked], now=NOW)
    assert d.action == daemon.ACTION_RESUME
    assert "probing" in d.reason


def test_decide_busy_when_task_already_running():
    d = daemon.decide(
        [make_task(status=STATUS_RUNNING), make_task(id="bbbb0001")], now=NOW
    )
    assert d.action == daemon.ACTION_BUSY


def test_limit_block_state_for_status():
    reset = (NOW + dt.timedelta(hours=1)).isoformat()
    blocked, wake = daemon.limit_block_state(
        [make_task(status=STATUS_BLOCKED_LIMIT, reset_at=reset)], NOW
    )
    assert blocked is True
    assert wake == dt.datetime.fromisoformat(reset)
    assert daemon.limit_block_state([make_task()], NOW) == (False, None)


# ---------------------------------------------------------------------------
# dry-run and live loop
# ---------------------------------------------------------------------------


def test_dry_run_prints_decision_and_changes_nothing(config, capsys):
    queue = TaskQueue(config.home)
    task = queue.add("demo task", "/tmp")
    rc = daemon.run_daemon(config, dry=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out
    assert f"decision: run - next pending task (FIFO)" in out
    assert task.id in out
    assert "--output-format stream-json --verbose" in out
    assert "--permission-mode acceptEdits" in out
    # nothing mutated, nothing executed
    assert queue.get(task.id).status == STATUS_PENDING
    assert not list(config.logs_dir.glob("*.jsonl"))


def test_dry_run_outside_window(config, capsys, monkeypatch):
    TaskQueue(config.home).add("demo task", "/tmp")
    fixed = dt.datetime(2026, 6, 11, 12, 0).astimezone()
    monkeypatch.setattr(daemon, "local_now", lambda: fixed)
    daemon.run_daemon(config, dry=True, window=daemon.parse_window("00:00-08:00"))
    out = capsys.readouterr().out
    assert "wait_window" in out
    assert "next start" in out


def test_daemon_loop_runs_pending_task_to_done(config, monkeypatch, capsys):
    """Two loop iterations: run the task, then idle - with notifications
    captured instead of hitting macOS Notification Center."""
    sent = []
    monkeypatch.setattr(daemon, "notify", lambda cfg, t, m: sent.append((t, m)))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    queue = TaskQueue(config.home)
    task = queue.add("demo task", "/tmp")

    rc = daemon.run_daemon(config, max_loops=2, poll_seconds=0.01, idle_seconds=0.01)
    assert rc == 0

    final = queue.get(task.id)
    assert final.status == STATUS_DONE
    assert final.session_id == "fake-sess-0001"
    assert final.log_file and final.log_file.endswith(f"{task.id}.jsonl")
    assert any("task done" in title for title, _ in sent)
    assert not config.pid_path.exists()  # pidfile cleaned up


def test_daemon_loop_blocks_on_limit_then_waits(config, monkeypatch):
    sent = []
    monkeypatch.setattr(daemon, "notify", lambda cfg, t, m: sent.append((t, m)))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit_json")
    monkeypatch.setenv("FAKE_CLAUDE_LIMIT_TEXT", "5-hour limit reached ∙ resets 3am")
    queue = TaskQueue(config.home)
    task = queue.add("demo task", "/tmp")

    daemon.run_daemon(config, max_loops=2, poll_seconds=0.01, idle_seconds=0.01)

    final = queue.get(task.id)
    assert final.status == STATUS_BLOCKED_LIMIT
    assert final.session_id == "fake-sess-0001"  # kept for the future resume
    assert final.reset_at is not None
    assert final.blocked_at is not None
    assert any("usage limit" in title for title, _ in sent)


def test_daemon_recovers_stale_running_tasks(config, monkeypatch):
    monkeypatch.setattr(daemon, "notify", lambda cfg, t, m: None)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    queue = TaskQueue(config.home)
    task = queue.add("demo task", "/tmp")
    queue.update(task.id, status=STATUS_RUNNING, session_id="sess-orphan")

    daemon.run_daemon(config, max_loops=2, poll_seconds=0.01, idle_seconds=0.01)

    final = queue.get(task.id)
    assert final.status == STATUS_DONE  # recovered to pending, then resumed


def test_daemon_refuses_second_instance(config, monkeypatch, capsys):
    config.ensure_dirs()
    import os

    config.pid_path.write_text(str(os.getpid()))  # "alive" daemon (our own pid)
    rc = daemon.run_daemon(config, max_loops=1)
    assert rc == 1
    assert "already running" in capsys.readouterr().err


def test_shift_report_summary():
    from ccnight.daemon import shift_report
    from ccnight.queue import Task

    def t(id_, status, start, end):
        task = Task(id=id_, prompt="p", repo="/tmp", status=status)
        task.started_at = start
        task.finished_at = end
        return task

    report = shift_report(
        [
            t("aaaa1111", "done", "2026-06-12T00:00:00+00:00", "2026-06-12T01:30:00+00:00"),
            t("bbbb2222", "done", "2026-06-12T01:30:00+00:00", "2026-06-12T03:00:00+00:00"),
            t("cccc3333", "failed", "2026-06-12T03:00:00+00:00", "2026-06-12T04:12:00+00:00"),
        ]
    )
    assert "3 task(s): 2 done, 1 failed" in report
    assert "wall 4.2h" in report
    assert "[cccc3333] failed" in report


def test_daemon_sends_shift_report_when_queue_drains(config, monkeypatch):
    from ccnight import daemon as scheduler
    from ccnight.queue import TaskQueue

    calls = []
    monkeypatch.setattr(
        "ccnight.daemon.notify", lambda cfg, title, msg: calls.append((title, msg))
    )
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    queue = TaskQueue(config.home)
    queue.add("night job", "/tmp")

    rc = scheduler.run_daemon(config, window=None, reserve=None, idle_seconds=0, max_loops=2)
    assert rc == 0
    titles = [t for t, _ in calls]
    assert any("task started" in t for t in titles)
    assert any("task done" in t for t in titles)
    report = [m for t, m in calls if "night shift report" in t]
    assert report and "1 task(s): 1 done, 0 failed" in report[0]


def test_daemon_starts_and_stops_caffeinate(config, monkeypatch):
    from ccnight import daemon as scheduler

    started = {}
    class FakeProc:
        def terminate(self):
            started["terminated"] = True
    def fake_start():
        started["started"] = True
        return FakeProc()
    monkeypatch.setattr(scheduler, "_start_caffeinate", fake_start)

    scheduler.run_daemon(config, window=None, reserve=None, max_loops=1, idle_seconds=0)
    assert started.get("started") and started.get("terminated")


def test_daemon_no_caffeinate_flag(config, monkeypatch):
    from ccnight import daemon as scheduler

    called = {"n": 0}
    monkeypatch.setattr(scheduler, "_start_caffeinate", lambda: called.__setitem__("n", called["n"] + 1))
    scheduler.run_daemon(config, window=None, reserve=None, caffeinate=False, max_loops=1, idle_seconds=0)
    assert called["n"] == 0


def test_no_resume_storm_when_reset_in_past():
    """A reset time in the past must not cause immediate back-to-back resumes."""
    import datetime as dt
    from ccnight.daemon import _next_wake_for_blocked, MIN_RESUME_BACKOFF
    from ccnight.queue import Task

    now = dt.datetime(2026, 6, 12, 4, 0, 0, tzinfo=dt.timezone.utc)
    just_blocked = (now - dt.timedelta(seconds=10)).isoformat()
    past_reset = (now - dt.timedelta(hours=1)).isoformat()  # bogus past reset
    task = Task(id="b1", prompt="p", repo="/tmp", status="blocked_limit")
    task.blocked_at = just_blocked
    task.reset_at = past_reset

    wake = _next_wake_for_blocked(task, now)
    # must be pushed at least MIN_RESUME_BACKOFF past the block, not "now"
    assert wake >= dt.datetime.fromisoformat(just_blocked) + MIN_RESUME_BACKOFF
    assert wake > now  # i.e. NOT due immediately


def test_future_reset_is_respected_as_is():
    import datetime as dt
    from ccnight.daemon import _next_wake_for_blocked
    from ccnight.queue import Task

    now = dt.datetime(2026, 6, 12, 1, 0, 0, tzinfo=dt.timezone.utc)
    future_reset = (now + dt.timedelta(hours=2)).isoformat()
    task = Task(id="b2", prompt="p", repo="/tmp", status="blocked_limit")
    task.blocked_at = now.isoformat()
    task.reset_at = future_reset
    assert _next_wake_for_blocked(task, now) == dt.datetime.fromisoformat(future_reset)


def test_preflight_holds_when_failing(config, monkeypatch):
    """A failing preflight must hold the task (stay pending), not run it."""
    from ccnight import daemon as scheduler
    from ccnight.queue import TaskQueue

    config.preflight_command = "exit 1"  # always fails
    monkeypatch.setattr("ccnight.daemon._preflight_ok", lambda c: False)
    ran = {"n": 0}
    monkeypatch.setattr(scheduler.runner, "run_task",
                        lambda *a, **k: ran.__setitem__("n", ran["n"] + 1))
    q = TaskQueue(config.home)
    q.add("job", "/tmp")
    scheduler.run_daemon(config, window=None, reserve=None, caffeinate=False,
                         max_loops=2, idle_seconds=0, poll_seconds=0)
    assert ran["n"] == 0                       # never ran
    assert q.all()[0].status == "pending"      # held, not failed


def test_window_close_notifies_unfinished(config, monkeypatch):
    from ccnight.daemon import _preflight_ok  # noqa: ensure import path
    from ccnight.daemon import TimeWindow
    import ccnight.daemon as scheduler
    from ccnight.queue import TaskQueue
    import datetime as dt

    calls = []
    monkeypatch.setattr("ccnight.daemon.notify",
                        lambda c, t, m: calls.append((t, m)))
    # force "now" outside a window that we pretend we were just inside
    q = TaskQueue(config.home)
    q.add("leftover", "/tmp")
    # window 22:00-08:00; pick a daytime now -> outside
    win = scheduler.parse_window("22:00-08:00")
    # local 14:00 (naive -> attach local tz, keeps the wall-clock time)
    monkeypatch.setattr("ccnight.daemon.local_now",
                        lambda: dt.datetime(2026, 6, 15, 14, 0).astimezone())
    scheduler.run_daemon(config, window=win, reserve=None, caffeinate=False,
                         max_loops=1, idle_seconds=0, poll_seconds=0)
    assert any("window closed" in t for t, _ in calls)
