#!/usr/bin/env bash
# nightcrew one-shot installer.
# Usage:  ./install.sh        (after git clone, or curl this file and run it)
# Installs the CLI, the always-on service (macOS), the Claude Code skill, and
# runs interactive setup. Re-runnable: safe to run again to update.
set -euo pipefail

REPO="git+https://github.com/Fay92/nightcrew"
say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
die() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

say "nightcrew installer"

# 1. Prerequisite: Claude Code
command -v claude >/dev/null 2>&1 || die \
  "Claude Code (the 'claude' command) was not found. Install it first: https://claude.com/claude-code"

# 2. Prerequisite: Python 3.11+
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found - install Python 3.11+ first."
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' \
  || die "Python 3.11+ required (found $($PY -V 2>&1))."

# 3. Install the CLI (pipx preferred for clean isolation)
say "[1/4] installing the nightcrew CLI..."
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$REPO"
else
  echo "  pipx not found; using 'pip install --user'. (pipx recommended: brew install pipx)"
  "$PY" -m pip install --user --upgrade "$REPO"
fi

NIGHTCREW="$(command -v nightcrew || echo "$HOME/.local/bin/nightcrew")"
[ -x "$NIGHTCREW" ] || die \
  "nightcrew installed but not on PATH. Add ~/.local/bin to PATH and re-run, or see pipx's note above."

# 4. Always-on service (macOS only)
if [ "$(uname)" = "Darwin" ]; then
  say "[2/4] registering the background service (launchd)..."
  "$NIGHTCREW" install-service
else
  say "[2/4] non-macOS: skip launchd. Run 'nightcrew daemon' under systemd/tmux (see README)."
fi

# 5. Claude Code skill
say "[3/4] installing the Claude Code skill..."
"$NIGHTCREW" install-skill

# 6. Interactive setup (window + notifications)
say "[4/4] setup..."
"$NIGHTCREW" setup

say "Done. Queue a task with:  nightcrew add \"<task>\" --repo ~/your-project"
echo "Then just go to sleep - it runs in your nightly window. Check: nightcrew status"
