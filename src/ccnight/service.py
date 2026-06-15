"""Install ccnight's daemon as a macOS LaunchAgent (always-on background service).

With the agent loaded, the scheduling daemon runs continuously and is restarted
by launchd if it ever dies, so adding a task is all the user has to do — there
is no "remember to start the daemon" step. This is macOS-only; other platforms
should run ``ccnight daemon`` under their own supervisor (systemd, etc).
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config

LABEL = "com.ccnight.daemon"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(
    *, ccnight_bin: str, home: Path, log_path: Path, path_env: str,
) -> dict:
    # The daemon reads window/reserve/model/etc from config.json, so the plist
    # is stable: changing your schedule means editing config.json, not the
    # service definition.
    return {
        "Label": LABEL,
        "ProgramArguments": [ccnight_bin, "daemon"],
        "RunAtLoad": True,
        # Restart the daemon if it ever exits abnormally (crash, OOM); a clean
        # exit (the user ran `ccnight uninstall-service`) is left alone.
        "KeepAlive": {"SuccessfulExit": False},
        # PYTHONUNBUFFERED so the daemon's log lines hit daemon.log in real
        # time; without it launchd block-buffers stdout and the log looks empty.
        "EnvironmentVariables": {
            "PATH": path_env,
            "CCNIGHT_HOME": str(home),
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "ProcessType": "Background",
    }


def install(config: Config, *, window: str | None, reserve: int | None) -> int:
    if sys.platform != "darwin":
        print("ccnight: install-service is macOS-only; on Linux run the daemon "
              "under systemd. See the README.", file=sys.stderr)
        return 2

    # A manually-started daemon would fight the agent over the pid lock.
    from .daemon import read_daemon_pid
    running = read_daemon_pid(config)
    if running is not None:
        print(f"ccnight: a daemon is already running (pid {running}). Stop it "
              "first (Ctrl-C in its terminal), then re-run install-service.",
              file=sys.stderr)
        return 2

    ccnight_bin = shutil.which("ccnight") or os.path.abspath(sys.argv[0])
    config.ensure_dirs()
    # Persist schedule into config.json so it stays the single source of truth.
    updates = {}
    if window is not None:
        updates["window"] = window
    if reserve is not None:
        updates["reserve"] = reserve
    if updates:
        _merge_config(config.config_path, updates)

    log_path = config.home / "daemon.log"
    plist = build_plist(
        ccnight_bin=ccnight_bin,
        home=config.home, log_path=log_path, path_env=os.environ.get("PATH", ""),
    )
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as fh:
        plistlib.dump(plist, fh)

    # Reload cleanly: unload any previous version, then load the new one.
    subprocess.run(["launchctl", "unload", str(target)],
                   capture_output=True, text=True)
    result = subprocess.run(["launchctl", "load", "-w", str(target)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ccnight: launchctl load failed: {result.stderr.strip()}",
              file=sys.stderr)
        return 1

    loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    ok = LABEL in loaded.stdout
    effective = Config.load(home=config.home)
    print(f"ccnight: service installed ({target})")
    print(f"  status:  {'loaded and running' if ok else 'written but not visible in launchctl list'}")
    print(f"  window:  {effective.window or 'always'} (edit config.json to change)")
    print(f"  logs:    {log_path}")
    print("  the daemon now starts on login and stays running - just `ccnight add`.")
    return 0 if ok else 1


def _merge_config(path: Path, updates: dict) -> None:
    """Merge *updates* into the JSON config file, preserving existing keys."""
    import json
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
    data.update(updates)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def uninstall() -> int:
    if sys.platform != "darwin":
        print("ccnight: install-service is macOS-only.", file=sys.stderr)
        return 2
    target = plist_path()
    subprocess.run(["launchctl", "unload", str(target)],
                   capture_output=True, text=True)
    existed = target.exists()
    if existed:
        target.unlink()
    print("ccnight: service removed" if existed else "ccnight: no service was installed")
    return 0
