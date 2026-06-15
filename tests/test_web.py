"""Web viewer: stream-json rendering (pure) + a live loopback server."""

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from nightcrew import web
from nightcrew.queue import TaskQueue


# --- log rendering ----------------------------------------------------------


def _write_log(path, *events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_log_entries_renders_mixed_stream(tmp_path):
    log = tmp_path / "t.jsonl"
    _write_log(
        log,
        {"type": "nightcrew.run", "resume": False, "task_id": "t"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Analysing the gift module"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "gradle build"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "BUILD SUCCESSFUL"},
        ]}},
        {"type": "result", "subtype": "success", "result": "Done, 3 files changed",
         "num_turns": 7, "total_cost_usd": 0.1234, "duration_ms": 42000},
        {"type": "nightcrew.outcome", "status": "done", "detail": "ok"},
    )
    entries = web.log_entries(log)
    roles = [e["role"] for e in entries]

    assert roles == ["meta", "claude", "tool", "tool_result", "result", "result_text", "outcome"]
    assert "fresh run" in entries[0]["text"]
    assert entries[1]["text"] == "Analysing the gift module"
    assert "gradle build" in entries[2]["text"]
    assert entries[3]["text"] == "BUILD SUCCESSFUL"
    assert "success" in entries[4]["text"] and "$0.1234" in entries[4]["text"]
    assert entries[5]["text"] == "Done, 3 files changed"
    assert "status=done" in entries[6]["text"]


def test_log_entries_marks_tool_errors(tmp_path):
    log = tmp_path / "t.jsonl"
    _write_log(log, {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "boom", "is_error": True},
    ]}})
    assert web.log_entries(log) == [{"role": "tool_result_err", "text": "boom"}]


def test_log_entries_missing_file_and_garbage(tmp_path):
    assert web.log_entries(tmp_path / "nope.jsonl") == []
    log = tmp_path / "g.jsonl"
    log.write_text("not json at all\n", encoding="utf-8")
    assert web.log_entries(log) == [{"role": "raw", "text": "not json at all"}]


# --- live server ------------------------------------------------------------


@pytest.fixture
def server(config):
    httpd, token, _url = web.make_server(config, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, token, config
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(base, path):
    return json.loads(urlopen(base + path, timeout=5).read())


def test_server_lists_and_reads_logs(server):
    base, _token, config = server
    queue = TaskQueue(config.home)
    task = queue.add("refactor gift panel", "/tmp/repo")
    _write_log(config.logs_dir / f"{task.id}.jsonl",
               {"type": "assistant", "message": {"content": [
                   {"type": "text", "text": "hello from claude"}]}})

    tasks = _get(base, "/api/tasks")["tasks"]
    assert [t["id"] for t in tasks] == [task.id]
    assert tasks[0]["prompt"] == "refactor gift panel"

    entries = _get(base, f"/api/tasks/{task.id}/log")["entries"]
    assert {"role": "claude", "text": "hello from claude"} in entries

    # the page embeds the delete token so the UI can mutate
    page = urlopen(base + "/", timeout=5).read().decode()
    assert _token in page


def test_delete_requires_token(server):
    base, token, config = server
    task = TaskQueue(config.home).add("doomed", "/tmp/repo")

    req = Request(base + f"/api/tasks/{task.id}/delete", method="POST")
    with pytest.raises(HTTPError) as excinfo:
        urlopen(req, timeout=5)
    assert excinfo.value.code == 403
    assert TaskQueue(config.home).all(), "must not delete without a token"

    req = Request(base + f"/api/tasks/{task.id}/delete", method="POST",
                  headers={"X-Nightcrew-Token": token})
    assert json.loads(urlopen(req, timeout=5).read())["ok"] is True
    assert TaskQueue(config.home).all() == []


def test_non_loopback_host_rejected(server):
    base, _token, _config = server
    req = Request(base + "/api/tasks", headers={"Host": "evil.example.com"})
    with pytest.raises(HTTPError) as excinfo:
        urlopen(req, timeout=5)
    assert excinfo.value.code == 403
