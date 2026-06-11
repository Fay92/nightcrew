"""Notifications: macOS Notification Center plus an optional webhook.

Notification failures must never take the daemon down, so every channel
swallows its own errors. On non-macOS platforms the desktop notification
quietly degrades to the log line that is always printed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

from .config import Config
from .queue import utcnow_iso


def notify(config: Config, title: str, message: str) -> None:
    """Fan a notification out to every configured channel."""
    print(f"[notify] {title}: {message}", flush=True)
    if sys.platform == "darwin":
        _macos(title, message)
    if config.webhook_url:
        _webhook(config.webhook_url, title, message)


def _applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _macos(title: str, message: str) -> None:
    script = (
        f'display notification "{_applescript_escape(message)}" '
        f'with title "{_applescript_escape(title)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _webhook(url: str, title: str, message: str) -> None:
    payload = json.dumps(
        {
            "source": "ccnight",
            "title": title,
            "message": message,
            "timestamp": utcnow_iso(),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "ccnight"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except Exception as exc:  # noqa: BLE001 - any webhook error is non-fatal
        print(f"[notify] webhook delivery failed: {exc}", file=sys.stderr)
