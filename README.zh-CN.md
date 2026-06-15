# nightcrew

**面向 Claude Code 的配额感知任务队列 —— 趁你睡觉，把你已经付费的配额用起来。**

![status](https://img.shields.io/badge/status-alpha-orange) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![deps](https://img.shields.io/badge/runtime%20deps-zero-brightgreen) ![license](https://img.shields.io/badge/license-MIT-green)

[English](README.md) | **简体中文**

傍晚把编码任务排进队列。你睡觉时，`nightcrew` 通过官方 `claude` CLI 以无头模式逐个执行。撞到用量上限时，它解析出重置时间、暂停，并在配额窗口重置的那一刻**续跑同一个会话**。全部完成（或卡住）时通知你。

```
21:00  nightcrew add "把测试套件迁移到 pytest" --repo ~/code/app
21:00  nightcrew add "给 v2 接口写 API 文档" --repo ~/code/app
21:00  nightcrew daemon --window "23:00-08:00" --reserve 20
03:00  [撞到用量上限 -> 解析出 "6 点重置" -> 休眠中]
06:00  [上限重置 -> 正在续跑会话...]
08:00  macOS 通知:"nightcrew: task done"
```

## 为什么做这个

如果你订阅了 Claude Pro 或 Max，你的 5 小时用量窗口会全天候不断回满 —— 包括你睡着的那八个小时。那份配额是你花钱买的一部分，却在没用上的情况下白白蒸发了。

这是 Claude Code 呼声最高的功能之一：[anthropics/claude-code#13354](https://github.com/anthropics/claude-code/issues/13354)（任务排队 / 撞上限后自动续跑，100+ 赞，开了好几个月仍无官方方案）。现有的绕法都是一次性的 hack：tmux 按键注入器、个人 gist、stop-hook 实验。`nightcrew` 把这块缺失补成了一个真正的工具：常驻的多任务队列、上限感知调度、会话续跑、完成通知。

## 合规说明

> **nightcrew 不绕过任何速率限制、不轮换账号、不代理订阅。**
>
> 它只是*调度*你已经付费的工作 —— 就跟设个闹钟一样。它在你自己的单一登录下驱动官方 `claude` CLI，完全尊重它遇到的每一个限制，只是在你的配额窗口重置前安静等待，仅此而已。

## 安装

**一站式（推荐）** —— 安装 CLI、常驻服务（macOS）、Claude Code skill，并运行交互式初始化：

```bash
git clone https://github.com/Fay92/nightcrew && cd nightcrew && ./install.sh
```

装完之后你就只用敲 `nightcrew add` —— daemon 已注册到 launchd（开机登录即启动、崩溃自动重启），每晚自动跑你的队列。

**手动 / 按需：**

```bash
pipx install git+https://github.com/Fay92/nightcrew   # CLI 本体
nightcrew install-service        # macOS:常驻后台 daemon
nightcrew install-skill          # Claude Code skill(用聊天排任务)
nightcrew setup                  # 选择你的每晚窗口 + 通知方式
# 开发用:
git clone https://github.com/Fay92/nightcrew && cd nightcrew && pip install -e ".[dev]"
```

环境要求：Python 3.11+、已用订阅登录的 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)。零第三方运行时依赖（仅用标准库）。常驻服务仅支持 macOS（launchd）；在 Linux 上用 systemd 或 tmux 跑 `nightcrew daemon`。

## 快速上手

```bash
# 1. 排入任务
nightcrew add "重构 auth 模块并让所有测试通过" --repo ~/code/app
nightcrew add "给每个公开接口加上输入校验" --repo ~/code/api \
    --claude-args "--model claude-sonnet-4-5"

# 2. 查看队列
nightcrew list
nightcrew status
nightcrew web                     # 在本地网页里浏览任务 + 读日志

# 3. 看看调度器会怎么做,但不真正调用 claude
nightcrew daemon --dry-run

# 4. 跑调度器(前台;放进 tmux/launchd 里)
nightcrew daemon --window "00:00-08:00" --reserve 20

# 调试辅助
nightcrew run-once <task-id>      # 立刻跑某一个任务
nightcrew logs <task-id>          # 打印捕获到的 stream-json 日志
```

## 排队任务

进同一个队列，两种方式：

**聊天方式 —— skill。** skill 装好后（`install.sh` 已帮你装好；若你是手动按需安装，跑一次 `nightcrew install-skill`），在 Claude Code 里用大白话描述任务：输入 `/nightcrew 把礼物模块迁移到新 API 并让测试通过`，或者直接说“这个放今晚跑”。夜间运行是**全新会话、没有当前对话的任何记忆**，所以 skill 会先把你的请求归结到本质、改写成一个自包含 prompt 再入队：

- **目标** —— 做什么，一句话
- **范围** —— 涉及的具体文件/模块，从对话上下文里提取成绝对路径
- **完成标准** —— 怎么算做完（测试通过 / 构建成功）
- **约束** —— 不要动什么（如“不改公共 API”）

它会把改写后的 prompt 给你看，然后入队 —— 你用自然语言思考，队列里存的是无记忆会话真正能执行的东西。

**CLI 方式。** 你更想自己写 prompt 时：

```bash
nightcrew add "<一个自包含的 prompt>" --repo <仓库路径>
```

## 工作原理

每个任务都走一个小型状态机，持久化在 `~/.config/nightcrew/queue.json`：

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

- **运行（Run）**：daemon 取出最早的 pending 任务，在该任务的仓库里运行
  `claude -p "<prompt>" --output-format stream-json --verbose`，
  把完整记录流式写入 `~/.config/nightcrew/logs/<task-id>.jsonl`，并记下 session id。
- **撞墙（Hit the wall）**：从错误输出里检测上限消息（多种已知格式，见
  *已知限制*），能解析出重置时间就解析，整个队列暂停 ——
  用量上限是账号级的，这时启动别的任务也只会撞上同一堵墙。
- **续跑（Resume）**：到重置时间时，daemon 通过 `--resume <session_id>`（续跑 prompt 可配置）
  继续被中断的会话。若任务在产生 session id 之前就撞墙，则直接从头重跑。
  如果重置时间无法解析，daemon 改为每 30 分钟探测一次。
- **通知（Notify）**：完成、失败、撞上限暂停等事件会发到 macOS 通知中心
  和/或你自己指定的 webhook。

## 无人值守权限

**排任何任务之前，先读这一节。**

无头运行没法向你二次确认，所以 nightcrew 默认传 `--permission-mode acceptEdits`：
Claude 可以在任务仓库内创建和编辑文件而不询问，但该模式之外的动作
（例如不在你 allowlist 里的任意 shell 命令）仍然会直接失败，而不是悄悄执行。

这是个实打实的权衡。一个有编辑权限的无人值守 agent 可能弄坏工作区。建议：

- **只在你信任、且已完整提交（`git status` 干净）的仓库里排任务**，
  这样第二天早上每处改动都可审、可回退。
- **优先用 allowlist** 而非更宽的权限模式。把它放进仓库的
  `.claude/settings.json`，让无人值守运行只能做它需要做的、别的都不行：

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

- **覆盖默认值**：你通过 `--claude-args` 传的任何东西都优先生效。例如
  `--claude-args "--permission-mode plan"` 做只读的规划运行；或者 —— 在你完全
  理解风险的前提下 —— `--claude-args "--dangerously-skip-permissions"`。nightcrew
  自己永远不会传这个 flag。
- 像 review 同事的隔夜 PR 那样 review 无人值守的产出：先 `git diff`，
  哪里看着不对就 `nightcrew logs <task-id>`。

## 工作准则（可选）

用 `append_system_prompt` 给夜间 agent 一套一致的方法：nightcrew 会在**每次**运行时通过 `--append-system-prompt` 注入它，这样每个任务都遵循同样的纪律，不用你在每条 prompt 里重复。一份可作为起点的准则：

```json
{
  "append_system_prompt": "动手前先读仓库自身规则（CLAUDE.md / .claude）并遵守。把任务归结到第一性原理、复述目标。不说废话、不旁白——基于代码里的事实而非臆测行事。先规划，再实现，再用构建和测试验证通过才算完成。严格围绕任务，不擅自扩散到无关改动。"
}
```

Claude Code 本来就会自动加载仓库的 `CLAUDE.md`，所以即使不配这个，项目规则也会生效 —— 这段准则只是把 agent 的方法显式化、可复用。它**出厂不设默认值**；当你想让每次运行都守住同一条线时再设置它。

## 调度窗口与交互保留

两个相互独立、都可选的机制，避免 nightcrew 吃掉你想留给自己的配额：

1. **时间窗口**（`--window "00:00-08:00"`）：队列只在这个每日窗口内运行
   （可跨午夜，如 `23:00-07:00`）。窗口之外，daemon 只是等待。这是最简单、
   无依赖的选项：把你睡觉的那几个小时给 nightcrew。

2. **用量保留**（`--reserve 20`）：给交互使用保留当前 5 小时窗口的 N%。若
   [ccusage](https://github.com/ryoppippi/ccusage) 可用（`ccusage` 在 PATH 上，
   或用 `npx` 拉取），daemon 会估算当前窗口已烧掉多少，一旦用量越过
   `100 - N` 百分比就 hold 住队列。这个估算是拿当前活跃块的 token 数跟你历史上
   最大的块比较（跟 ccusage 自己用的 `max` 启发式一样）—— 本质上是近似值。若
   ccusage 缺失、超时或输出无法解析，保留检查会优雅降级，只剩时间窗口生效。

两者默认都关；按你喜欢的方式组合。

## 通知

- **macOS**：通过 `osascript` 弹原生通知中心横幅（无需配置）。
- **其他平台**：桌面通知降级为一行日志；请用 webhook。
- **Webhook**：在配置里设 `webhook_url`，每个事件都会以 JSON POST 出去 ——
  指向 Slack、Discord、ntfy.sh、飞书，或你自己的端点：

  ```json
  {"source": "nightcrew", "title": "nightcrew: task done", "message": "[1a2b3c4d] refactor...", "timestamp": "2026-06-11T07:58:00+00:00"}
  ```

## 配置

`~/.config/nightcrew/config.json`（所有键均可选）：

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

| 配置项 | 默认值 | 含义 |
| --- | --- | --- |
| `claude_bin` | `"claude"` | Claude CLI 可执行文件（不在 PATH 上时设置） |
| `model` | `"claude-opus-4-8"` | 每次运行的 `--model`；`""` 表示沿用 CLI 默认 |
| `window` | `null` | 每晚运行窗口 `"HH:MM-HH:MM"`（可跨午夜）；未传 `--window` 时生效 |
| `reserve` | `null` | 给交互使用保留当前 5 小时窗口的 N%（需要 ccusage）；未传 `--reserve` 时生效 |
| `preflight_command` | `null` | 每次调用 claude 前运行的 shell 命令；非零退出则跳过该次运行（如 IP/VPN 检查） |
| `stall_timeout_seconds` | `1200` | 杀掉这么久没有输出的运行（卡死看门狗）；`null` 关闭 |
| `worktree_isolation` | `false` | 在兄弟目录 `<repo>_worktree`（`nightcrew-work` 分支）里跑每个仓库的任务，而非主工作区 |
| `permission_mode` | `"acceptEdits"` | 默认 `--permission-mode`；`""` 表示不传该 flag |
| `continue_prompt` | `"continue"` | 续跑被上限阻塞的会话时发送的 prompt |
| `append_system_prompt` | `null` | 每次运行通过 `--append-system-prompt` 注入的文本（如工作准则） |
| `guardrails` | `true` | 注入内置的安全 `--allowedTools`/`--disallowedTools` 预设；`false` 则依赖仓库自身的权限配置 |
| `allow_tools` | `null` | 覆盖内置 allow 预设（列表）；`null`=默认，`[]`=不允许任何 |
| `deny_tools` | `null` | 覆盖内置 deny 预设（列表）；deny 优先于 allow |
| `claude_extra_args` | `null` | 追加到每次 claude 调用的原始参数 |
| `webhook_url` | `null` | 通知的 POST 目标 |
| `webhook_format` | `"auto"` | webhook 载荷格式：`auto` / `feishu` / `slack` / `generic` |
| `notify_command` | `null` | 每条通知运行的 shell 命令，可用 `$NIGHTCREW_TITLE` / `$NIGHTCREW_MESSAGE` |
| `extra_limit_patterns` | `[]` | 额外的大小写不敏感正则，用于上限检测 |
| `task_timeout_seconds` | `null` | 单次运行的硬性超时开关（null = 不限） |
| `log_retention_days` | `14` | 删除超过这么多天（按 mtime）的每任务 `<id>.jsonl` 日志；`0` 永久保留 |
| `daemon_log_max_bytes` | `5000000` | 限制 `daemon.log` 大小（~5 MB），保留最近一半；`0` 关闭上限 |

状态都存在同一目录：`queue.json`、`logs/<task-id>.jsonl`、`daemon.pid`。
设置 `NIGHTCREW_HOME` 可整体迁移位置。

## 作为服务运行（launchd）

daemon 在设计上就是个普通的前台进程 —— 把它交给你喜欢的进程守护器。
macOS 示例，`~/Library/LaunchAgents/com.nightcrew.daemon.plist`：

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

或者直接：`tmux new -s nightcrew 'nightcrew daemon --window "00:00-08:00"'`。

## 已知限制

- **上限消息的覆盖仍在演进。** Claude Code 的撞墙消息因版本和上限类型而异；
  nightcrew 内置了公开报告过的格式的匹配模式，但实战验证的格式覆盖仍在完善中。
  当检测到消息却无法解析其重置时间时，nightcrew 退回到每 30 分钟探测一次的循环 ——
  更慢，但安全。若你看到无法识别的消息，请往 `extra_limit_patterns` 加一条正则，
  并附上确切文本提个 issue。
- **重置时间只有撞墙之后才能知道。** CLI 不会提前暴露下一次重置时间，所以 v0
  在设计上是被动响应式的（预测式调度见 Roadmap）。
- **保留估算是启发式的。** 它需要 ccusage 外加至少一个已完成的历史块，且"窗口百分比"
  是相对于你自己过去的峰值用量，不是官方配额数字。
- **一次一个任务，FIFO。** 暂无优先级、依赖或并行。
- **桌面通知仅限 macOS**（webhook 覆盖其余一切）。
- **时区缩写** 在重置消息中已映射 UTC/GMT 和常见美区；IANA 名称处处可用，
  其他则回退到本地时间。
- 没设 `task_timeout_seconds` 而又真的卡死的任务，会一直阻塞队列，直到 daemon 重启。

## Roadmap

- **v1：预测式调度** —— 学习你的重置节奏，在窗口一打开就启动长任务，
  而不是在窗口中段才开始。
- **更聪明的配额估算** —— 当 CLI 暴露官方用量信号时加以利用；按模型的烧速感知。
- 任务优先级、带预算的重试、以及简单依赖（`--after <task-id>`）。
- 原生 Linux（`notify-send`）和 Windows toast 通知。
- 在 5 小时窗口之外，加上周配额上限的感知。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

测试套件从不调用真实的 `claude`：`tests/fakes/claude` 处有个脚本化的桩，
会演练 成功 / 撞上限 / 垃圾输出 等场景。

## 许可证

MIT
