"""Onboarding helpers: install the bundled skill, and an interactive setup.

These make first-run painless: `install-skill` drops the Claude Code skill into
place, and `setup` walks the user through the two things that can't have a
sensible default - their nightly window and (optionally) a notification webhook.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .config import Config
from .daemon import parse_window
from .service import _merge_config

SKILL_DEST = Path.home() / ".claude" / "skills" / "nightcrew"


def bundled_skill() -> Path:
    return Path(__file__).parent / "skill" / "SKILL.md"


def install_skill() -> int:
    src = bundled_skill()
    if not src.exists():
        print(f"nightcrew: bundled skill missing at {src}", file=sys.stderr)
        return 1
    SKILL_DEST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, SKILL_DEST / "SKILL.md")
    print(f"nightcrew: skill installed to {SKILL_DEST}/SKILL.md")
    print("  it activates in your next Claude Code session - then just say "
          '"queue this for tonight".')
    return 0


def setup(config: Config) -> int:
    """Interactively fill in window + notification. No-ops gracefully if stdin
    is not a TTY (e.g. piped installer), leaving defaults in place."""
    config.ensure_dirs()
    if not sys.stdin.isatty():
        print("nightcrew setup: non-interactive stdin, keeping defaults "
              f"(window={config.window or 'unset'}). Edit {config.config_path} "
              "to customize.")
        return 0

    print("nightcrew setup - press Enter to accept the [default].\n")
    updates: dict = {}
    try:
        cur = config.window or "22:00-08:00"
        win = input(f"Nightly run window HH:MM-HH:MM [{cur}]: ").strip() or cur
        try:
            parse_window(win)
        except ValueError:
            print(f"  not a valid window, keeping {cur}")
            win = cur
        updates["window"] = win

        ans = input("Send a notification when tasks finish/fail? [y/N]: ").strip().lower()
        if ans.startswith("y"):
            print("  Feishu: target group -> settings -> bots -> add custom bot "
                  "-> copy the webhook URL.")
            print("  (Slack incoming webhooks work too; see docs for other channels.)")
            url = input("  Webhook URL (blank to skip): ").strip()
            if url:
                updates["webhook_url"] = url
    except EOFError:
        print("\n(input closed; skipping the rest)")

    if updates:
        _merge_config(config.config_path, updates)
    print(f"\nSaved to {config.config_path}. Review with: nightcrew doctor")
    return 0
