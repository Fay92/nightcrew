"""Notification tests: webhook against a real local HTTP server, and the
macOS path with subprocess captured (so test runs never pop banners)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ccnight import notify as notify_mod
from ccnight.config import Config


@pytest.fixture
def webhook_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # keep test output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook", received
    server.shutdown()


def test_webhook_delivery(ccnight_home, webhook_server, monkeypatch):
    url, received = webhook_server
    monkeypatch.setattr("sys.platform", "linux")  # skip the osascript branch
    cfg = Config(home=ccnight_home, webhook_url=url)
    notify_mod.notify(cfg, "ccnight: task done", "demo finished")
    assert len(received) == 1
    assert received[0]["source"] == "ccnight"
    assert received[0]["title"] == "ccnight: task done"
    assert received[0]["message"] == "demo finished"
    assert "timestamp" in received[0]


def test_webhook_failure_is_swallowed(ccnight_home, monkeypatch, capsys):
    monkeypatch.setattr("sys.platform", "linux")
    cfg = Config(home=ccnight_home, webhook_url="http://127.0.0.1:1/unreachable")
    notify_mod.notify(cfg, "t", "m")  # must not raise
    assert "webhook delivery failed" in capsys.readouterr().err


def test_macos_notification_invokes_osascript(ccnight_home, monkeypatch):
    calls = []
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        notify_mod.subprocess,
        "run",
        lambda *a, **k: calls.append(a[0]),
    )
    cfg = Config(home=ccnight_home)
    notify_mod.notify(cfg, 'task "x"', "done\\now")
    assert len(calls) == 1
    assert calls[0][0] == "osascript"
    script = calls[0][2]
    assert 'with title "task \\"x\\""' in script  # quotes escaped


def test_non_macos_degrades_to_log_line(ccnight_home, monkeypatch, capsys):
    monkeypatch.setattr("sys.platform", "linux")
    cfg = Config(home=ccnight_home)
    notify_mod.notify(cfg, "title", "message")
    assert "[notify] title: message" in capsys.readouterr().out


def test_webhook_payload_auto_detects_feishu():
    from ccnight.notify import webhook_payload

    p = webhook_payload(
        "https://open.feishu.cn/open-apis/bot/v2/hook/abc", "auto", "t", "m"
    )
    assert p == {"msg_type": "text", "content": {"text": "t\nm"}}


def test_webhook_payload_auto_detects_slack():
    from ccnight.notify import webhook_payload

    p = webhook_payload("https://hooks.slack.com/services/x/y/z", "auto", "t", "m")
    assert p == {"text": "*t*\nm"}


def test_webhook_payload_generic_default_and_explicit_override():
    from ccnight.notify import webhook_payload

    generic = webhook_payload("https://example.com/hook", "auto", "t", "m")
    assert generic["source"] == "ccnight" and generic["title"] == "t"
    forced = webhook_payload("https://example.com/hook", "feishu", "t", "m")
    assert forced["msg_type"] == "text"


def test_notify_command_receives_env(ccnight_home, tmp_path):
    from ccnight.config import Config
    from ccnight.notify import notify

    out = tmp_path / "captured.txt"
    cfg = Config(
        home=ccnight_home,
        notify_command=f'printf "%s|%s" "$CCNIGHT_TITLE" "$CCNIGHT_MESSAGE" > {out}',
    )
    notify(cfg, "hello", "world")
    assert out.read_text() == "hello|world"


def test_webhook_payload_feishu_automation_url_stays_generic():
    from ccnight.notify import webhook_payload

    p = webhook_payload(
        "https://open.feishu.cn/anycross/trigger/callback/x", "auto", "t", "m"
    )
    assert "msg_type" not in p and p["title"] == "t"
