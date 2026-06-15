# nightcrew

**Quota-aware task queue for Claude Code — use the quota you already paid for while you sleep.**

![status](https://img.shields.io/badge/status-alpha-orange) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![deps](https://img.shields.io/badge/runtime%20deps-zero-brightgreen) ![license](https://img.shields.io/badge/license-MIT-green)

Queue up coding tasks in the evening. While you sleep, `nightcrew` runs them one by one through the official `claude` CLI in headless mode. When it hits your usage limit, it parses the reset time, pauses, and resumes the **same session** the moment your quota window resets. When everything is done (or stuck), it notifies you.

```
9pm   nightcrew add "migrate the test suite to pytest" --repo ~/code/app
9pm   nightcrew add "write API docs for the v2 endpoints" --repo ~/code/app
9pm   nightcrew daemon --window "23:00-08:00" --reserve 20
3am   [usage limit hit -> parsed "resets 6am" -> sleeping]
6am   [limit reset -> resuming session...]
8am   macOS notification: "nightcrew: task done"
```

## Why

If you pay for Claude Pro or Max, your 5-hour usage windows keep refilling around the clock — including the eight hours you spend asleep. That quota is part of what you pay for, and it evaporates unused.

This is one of the most-requested Claude Code features: [anthropics/claude-code#13354](https://github.com/anthropics/claude-code/issues/13354) (queued tasks / auto-resume after usage limits, 100+ upvotes, open for months with no official solution). The existing workarounds are one-off hacks: tmux keystroke injectors, personal gists, stop-hook experiments. `nightcrew` is the missing piece as a real tool: a persistent multi-task queue, limit-aware scheduling, session resume, and completion notifications.

## Compliance

> **nightcrew does NOT bypass any rate limit, does NOT rotate accounts, and does NOT proxy subscriptions.**
>
> It only *schedules* work you already paid for — exactly like setting an alarm clock. It drives the official `claude` CLI under your own single login, fully respects every limit it encounters, and simply waits until your quota window resets before continuing. Nothing more.

## Install

**One-shot (recommended)** — installs the CLI, the always-on service (macOS),
the Claude Code skill, and runs interactive setup:

```bash
git clone https://github.com/Fay92/nightcrew && cd nightcrew && ./install.sh
```

After this you only ever run `nightcrew add` — the daemon is registered with
launchd (starts on login, restarts on crash) and runs your queue every night.

**Manual / piecemeal:**

```bash
pipx install git+https://github.com/Fay92/nightcrew   # the CLI
nightcrew install-service        # macOS: always-on background daemon
nightcrew install-skill          # Claude Code skill (queue tasks by chatting)
nightcrew setup                  # pick your nightly window + notifications
# for development:
git clone https://github.com/Fay92/nightcrew && cd nightcrew && pip install -e ".[dev]"
```

Requirements: Python 3.11+, the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) logged in with your subscription. Zero third-party runtime dependencies (stdlib only). The always-on service is macOS-only (launchd); on Linux run `nightcrew daemon` under systemd or tmux.

## Quickstart

```bash
# 1. queue tasks
nightcrew add "refactor the auth module and make all tests pass" --repo ~/code/app
nightcrew add "add input validation to every public endpoint" --repo ~/code/api \
    --claude-args "--model claude-sonnet-4-5"

# 2. inspect the queue
nightcrew list
nightcrew status

# 3. see what the scheduler would do, without calling claude
nightcrew daemon --dry-run

# 4. run the scheduler (foreground; keep it in tmux/launchd)
nightcrew daemon --window "00:00-08:00" --reserve 20

# debugging helpers
nightcrew run-once <task-id>      # run one task right now
nightcrew logs <task-id>          # dump the captured stream-json log
```

## How it works

Each task moves through a small state machine, persisted in `~/.config/nightcrew/queue.json`:

```
                       ┌────────────────────────────────────────────┐
                       │   usage limit hit: parse reset time,       │
                       │   keep session id                          │
                       ▼                                            │
 add ──▶ pending ──▶ running ──┬──▶ done ───▶ notification          │
            ▲                  ├──▶ failed ─▶ notification          │
            │                  └────────────────────────────────────┘
            │                       │
            │                blocked_limit
            │                       │  reset time reached
            └───────────────────────┘  (or 30-min probe when unknown)
                resume: claude -p --resume <session_id> "continue"
```

- **Run**: the daemon picks the oldest pending task and runs
  `claude -p "<prompt>" --output-format stream-json --verbose` inside the task's repo,
  streaming the full transcript to `~/.config/nightcrew/logs/<task-id>.jsonl` and capturing the session id.
- **Hit the wall**: limit messages are detected in error output (multiple known formats, see
  *Known limitations*), the reset time is parsed when present, and the whole queue pauses —
  a usage limit is account-wide, so starting another task would just hit the same wall.
- **Resume**: at the reset time the daemon continues the interrupted session via
  `--resume <session_id>` (configurable continue prompt). A task that hit the wall before
  producing a session id is simply re-run from scratch. If the reset time could not be
  parsed, the daemon probes every 30 minutes instead.
- **Notify**: completion, failure and limit-pause events go to macOS Notification Center
  and/or a webhook of your choice.

## Unattended permissions

**Read this section before queueing anything.**

Headless runs cannot ask you for confirmation, so by default nightcrew passes
`--permission-mode acceptEdits`: Claude may create and edit files inside the task repo
without prompting, but actions outside that mode (e.g. arbitrary shell commands not
covered by your allowlist) still fail rather than silently proceed.

This is a real trade-off. An unattended agent with edit permissions can damage a working
tree. Recommendations:

- **Only queue tasks in repos you trust and that are fully committed** (clean `git status`),
  so every change is reviewable and revertible the next morning.
- **Prefer an allowlist** over broader permission modes. Put one into the repo's
  `.claude/settings.json` so unattended runs can do exactly what they need and nothing else:

  ```json
  {
    "permissions": {
      "allow": [
        "Edit(./src/**)",
        "Edit(./tests/**)",
        "Bash(npm test:*)",
        "Bash(npm run build)"
      ],
      "deny": [
        "Read(./.env)",
        "Read(./secrets/**)",
        "WebFetch"
      ]
    }
  }
  ```

- **Overriding the default**: anything you pass via `--claude-args` wins. For example
  `--claude-args "--permission-mode plan"` for a read-only planning run, or — if you fully
  understand the risk — `--claude-args "--dangerously-skip-permissions"`. nightcrew never
  passes that flag itself.
- Review unattended work like you would review a teammate's overnight PR: `git diff` first,
  `nightcrew logs <task-id>` when something looks odd.

## Scheduling window and interaction reserve

Two independent, optional mechanisms keep nightcrew from eating the quota you want for
yourself:

1. **Time window** (`--window "00:00-08:00"`): the queue only runs inside this daily window
   (it may cross midnight, e.g. `23:00-07:00`). Outside it, the daemon just waits. This is
   the simple, dependency-free option: give nightcrew the hours when you sleep.

2. **Usage reserve** (`--reserve 20`): keep N% of the current 5-hour window for interactive
   use. If [ccusage](https://github.com/ryoppippi/ccusage) is available (`ccusage` on PATH,
   or `npx` to fetch it), the daemon estimates how much of the current window is already
   burned and holds the queue once usage crosses `100 - N` percent. The estimate compares
   the active block's token count against your largest historical block (the same `max`
   heuristic ccusage itself uses) — it is approximate by nature. If ccusage is missing,
   times out, or its output cannot be parsed, the reserve check degrades gracefully and
   only the time window applies.

Both are off by default; combine them as you like.

## Notifications

- **macOS**: native Notification Center banners via `osascript` (no setup).
- **Other platforms**: desktop notification degrades to a log line; use the webhook.
- **Webhook**: set `webhook_url` in the config and every event is POSTed as JSON —
  point it at Slack, Discord, ntfy.sh, Feishu, or your own endpoint:

  ```json
  {"source": "nightcrew", "title": "nightcrew: task done", "message": "[1a2b3c4d] refactor...", "timestamp": "2026-06-11T07:58:00+00:00"}
  ```

## Configuration

`~/.config/nightcrew/config.json` (all keys optional):

```json
{
  "claude_bin": "claude",
  "permission_mode": "acceptEdits",
  "continue_prompt": "continue",
  "webhook_url": "https://hooks.example.com/nightcrew",
  "extra_limit_patterns": ["my org's custom limit message"],
  "task_timeout_seconds": null
}
```

| key | default | meaning |
| --- | --- | --- |
| `claude_bin` | `"claude"` | Claude CLI executable (set when not on PATH) |
| `permission_mode` | `"acceptEdits"` | default `--permission-mode`; `""` disables passing it |
| `continue_prompt` | `"continue"` | prompt sent when resuming a limit-blocked session |
| `webhook_url` | `null` | POST target for notifications |
| `extra_limit_patterns` | `[]` | extra case-insensitive regexes for limit detection |
| `task_timeout_seconds` | `null` | hard kill switch for a single run (null = unlimited) |

State lives in the same directory: `queue.json`, `logs/<task-id>.jsonl`, `daemon.pid`.
Set `NIGHTCREW_HOME` to relocate everything.

## Running as a service (launchd)

The daemon is a plain foreground process by design — put it under your favorite supervisor.
macOS example, `~/Library/LaunchAgents/com.nightcrew.daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nightcrew.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/nightcrew</string>
    <string>daemon</string>
    <string>--window</string>
    <string>00:00-08:00</string>
    <string>--reserve</string>
    <string>20</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/nightcrew.daemon.log</string>
  <key>StandardErrorPath</key><string>/tmp/nightcrew.daemon.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.nightcrew.daemon.plist
```

Or just: `tmux new -s nightcrew 'nightcrew daemon --window "00:00-08:00"'`.

## Known limitations

- **Limit-message coverage is evolving.** Claude Code's wall messages vary across versions
  and limit types; nightcrew ships patterns for the publicly reported formats, but
  battle-tested format coverage is evolving. When a message is detected but its reset time
  cannot be parsed, nightcrew falls back to a 30-minute probe loop — slower, but safe. If you
  see an unrecognized message, add a regex to `extra_limit_patterns` and please open an
  issue with the exact text.
- **Reset times are only knowable after hitting the wall.** The CLI does not expose the next
  reset in advance, so v0 is reactive by design (see Roadmap for predictive scheduling).
- **The reserve estimate is a heuristic.** It needs ccusage plus at least one completed
  historical block, and "percent of window" is relative to your own past peak usage, not an
  official quota figure.
- **One task at a time, FIFO.** No priorities, dependencies or parallelism yet.
- **Desktop notifications are macOS-only** (webhook covers everything else).
- **Timezone abbreviations** in reset messages are mapped for UTC/GMT and common US zones;
  IANA names work everywhere, anything else falls back to local time.
- A task with no `task_timeout_seconds` that genuinely hangs will stall the queue until the
  daemon is restarted.

## Roadmap

- **v1: predictive scheduling** — learn your reset cadence and start long tasks right after
  a window opens instead of mid-window.
- **Smarter quota estimation** — use official usage signals if/when the CLI exposes them;
  per-model burn-rate awareness.
- Task priorities, retries with budgets, and simple dependencies (`--after <task-id>`).
- Native Linux (`notify-send`) and Windows toast notifications.
- Weekly-cap awareness alongside the 5-hour window.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite never calls the real `claude`: a scripted stub at `tests/fakes/claude`
plays success / limit-wall / garbage scenarios.

## License

MIT
