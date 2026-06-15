"""Configuration loading and shared filesystem paths.

All nightcrew state lives under a single directory, resolved in this order:

1. ``$NIGHTCREW_HOME`` (also how the test suite isolates itself)
2. ``$XDG_CONFIG_HOME/nightcrew``
3. ``~/.config/nightcrew``

User-tunable settings are read from ``<home>/config.json``. Unknown keys
are ignored and a broken file falls back to defaults with a warning, so a
bad edit can never brick the daemon.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE_NAME = "config.json"


def config_home() -> Path:
    """Return the nightcrew state directory (not guaranteed to exist yet)."""
    override = os.environ.get("NIGHTCREW_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "nightcrew"


@dataclass
class Config:
    """Runtime configuration. Field names match the config.json keys."""

    home: Path
    # Claude CLI executable. Override when claude is not on PATH.
    claude_bin: str = "claude"
    # Model passed as --model on every run. Defaults to Opus so unattended work
    # gets the strongest model; the interactive default (which may be a 1M or
    # preview model id that headless rejects) is deliberately NOT inherited.
    # Set to "" to inherit the CLI default instead.
    model: str = "claude-opus-4-8"
    # Nightly run window "HH:MM-HH:MM" (may cross midnight). The daemon reads
    # this when --window is not passed on the command line.
    window: str | None = None
    # Interaction reserve percent (keep this much of the 5h window for yourself,
    # needs ccusage). Read when --reserve is not passed on the command line.
    reserve: int | None = None
    # Optional preflight command run before every claude call (e.g. an IP/VPN
    # check). Non-zero exit refuses that run instead of risking it. Keeps the
    # guard working under launchd, where the interactive shell wrapper is absent.
    preflight_command: str | None = None
    # Stall watchdog: if a running task produces no new log output for this many
    # seconds it is considered hung, killed, and the daemon moves on. None = off.
    stall_timeout_seconds: int | None = 1200
    # Passed as --permission-mode unless the task's claude_args already set
    # one. Use an empty string to never pass the flag.
    permission_mode: str = "acceptEdits"
    # Prompt sent when resuming a limit-blocked session.
    continue_prompt: str = "continue"
    # Optional working protocol injected via --append-system-prompt on every
    # run, so unattended tasks follow a consistent method (analyse, plan,
    # review, execute, self-check) without baking it into each task's text.
    append_system_prompt: str | None = None
    # Built-in unattended guardrails: inject a safe --allowedTools /
    # --disallowedTools preset (see runner.DEFAULT_*_TOOLS) into every run so
    # nightcrew ships safe by default with no settings.json edits. Set false to
    # rely entirely on the project's own permission config.
    guardrails: bool = True
    # Override the built-in allow / deny tool presets. None = use the default.
    # An empty list disables that half. Project-specific build commands go here.
    allow_tools: list[str] | None = None
    deny_tools: list[str] | None = None
    # Escape hatch: extra raw arguments appended to every claude invocation.
    claude_extra_args: str | None = None
    # Optional URL that receives a JSON POST for every notification.
    webhook_url: str | None = None
    # Webhook payload shape: "auto" (detect Feishu/Lark and Slack from the
    # URL), "feishu", "slack" or "generic".
    webhook_format: str = "auto"
    # Optional shell command executed for every notification, with the
    # details exposed as $NIGHTCREW_TITLE and $NIGHTCREW_MESSAGE. Lets users
    # plug in any messenger CLI without nightcrew knowing about it.
    notify_command: str | None = None
    # Extra regexes (case-insensitive) appended to the built-in
    # usage-limit detection patterns.
    extra_limit_patterns: list[str] = field(default_factory=list)
    # Hard cap for a single claude run, in seconds. None means no cap.
    task_timeout_seconds: int | None = None

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def pid_path(self) -> Path:
        return self.home / "daemon.pid"

    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_FILE_NAME

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, home: Path | None = None) -> "Config":
        """Load config.json from *home* (default: ``config_home()``)."""
        home = home or config_home()
        cfg = cls(home=home)
        path = cfg.config_path
        if not path.exists():
            return cfg
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"nightcrew: warning: ignoring invalid config file {path}: {exc}",
                file=sys.stderr,
            )
            return cfg
        if not isinstance(raw, dict):
            print(
                f"nightcrew: warning: {path} must contain a JSON object; using defaults",
                file=sys.stderr,
            )
            return cfg
        for key in (
            "claude_bin",
            "model",
            "window",
            "reserve",
            "preflight_command",
            "stall_timeout_seconds",
            "permission_mode",
            "continue_prompt",
            "append_system_prompt",
            "guardrails",
            "claude_extra_args",
            "webhook_url",
            "webhook_format",
            "notify_command",
            "task_timeout_seconds",
        ):
            if key in raw:
                setattr(cfg, key, raw[key])
        for key in ("allow_tools", "deny_tools", "extra_limit_patterns"):
            value = raw.get(key)
            if isinstance(value, list):
                setattr(cfg, key, [str(p) for p in value])
        return cfg
