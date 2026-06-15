---
name: nightcrew
description: 把任务排进 nightcrew 夜间队列（配额感知任务队列，睡觉时自动用满 Claude 配额），或查询队列状态/任务日志。当用户说"放夜间队列 / 夜里跑 / 排队跑 / 睡觉时跑 / 过夜任务 / 明早要 / nightcrew / 查夜间队列 / 昨晚跑得怎么样"时使用。
argument-hint: "[任务描述] | status | logs <task-id>"
allowed-tools: Bash(nightcrew *), Bash(git *)
---

# nightcrew — 夜间任务队列集成

nightcrew 是本机已装的独立 CLI，数据在 `~/.config/nightcrew/`。本 skill 让会话内可以直接入队/查询。

## 核心行为准则：入队前必须把任务改写成「自包含 prompt」

夜里跑的是**全新的 claude 会话，没有当前对话的任何记忆**。直接把用户口语扔进队列 = 夜里跑出垃圾。入队前必须改写，要素：

1. **目标**：做什么，一句话
2. **范围**：涉及的具体文件/模块路径（从当前会话上下文提取，写成绝对可定位的路径）
3. **完成标准**：怎么算做完（测试命令通过 / 文件存在 / 构建成功）
4. **约束**：不要动什么（如"不改公共 API"、"不升级依赖"）

改写后把最终 prompt 展示给用户，然后入队。

## 操作映射

### 入队

```bash
nightcrew add "<改写后的自包含 prompt>" --repo <仓库绝对路径>
```

- `--repo` 默认取当前会话 cwd 所在 git 仓库根（`git rev-parse --show-toplevel`）；用户指定了别的项目就用用户的
- 用户一次说了多个任务 → 逐个改写、逐条 add
- 入队成功后跑 `nightcrew status`：
  - 若 `daemon: running` → 已就绪，到夜班窗口自动跑，无需用户做任何事
  - 若 `daemon: not running` → 提醒用户：装常驻服务（一次即可，开机自起）`nightcrew install-service`，或临时手动 `nightcrew daemon`

### 查询

| 用户意图 | 命令 |
|---|---|
| 队列里有什么 / 跑得怎么样 | `nightcrew status` 和 `nightcrew list` |
| 某任务细节/失败原因 | `nightcrew logs <task-id>`（输出大时只摘关键错误和 result 段） |
| 立即试跑某任务（调试） | `nightcrew run-once <task-id>`（提醒：这会立即消耗配额） |
| 重排失败的任务 | `nightcrew retry <task-id>` 或 `nightcrew retry --all` |
| 查看生效配置/护栏 | `nightcrew doctor` |

## 配置（用户说"配置 / 改夜班时间 / 配通知 / 收不到通知"时）

所有配置在用户自己的 `~/.config/nightcrew/config.json`，不在本 skill 里（skill 无状态可共享，个人配置归个人文件）。常用项：

- **夜班时间**：`"window": "22:00-08:00"`（按作息自由改，改完重启 daemon 生效）
- **模型**：`"model": "claude-opus-4-8"`（默认 Opus；可改 sonnet 省配额）
- **飞书/通知**：最简单是群机器人 webhook —— `{"webhook_url": "<飞书群自定义机器人地址>"}`，nightcrew 识别 open.feishu.cn 域名自动用飞书格式。想私聊或接其他渠道见 `nightcrew setup` 或仓库内 `docs/notifications-feishu-zh.md`（三种方式）。**任何配置文件里都不放密钥**。

不确定怎么填时，引导用户跑 `nightcrew setup`（交互式引导填 window/通知）。

## 行为细则

- **歧义确认**：用户措辞含"直接执行 / 现在跑 / 马上做"却又触发了本 skill → 先确认："是排进夜间队列（配额重置后无人值守跑），还是现在就在本会话执行？" 不要默认任何一边
- **入队后必须复核**：`nightcrew list` 确认任务真的进了队列，把任务 ID 报给用户——禁止只凭 add 命令没报错就宣布成功
- **大计划必须拆**：用户丢过来多里程碑计划（M1/M2/M3…）→ 一个里程碑一个任务分别入队，并提醒：队列按 FIFO 顺序执行但**无依赖检查**——前面任务失败后面仍会跑，有强依赖的后续里程碑建议在 prompt 里写明"若前置未完成先报告"
- 入队的任务权限默认 `acceptEdits`（自动改文件）+ nightcrew **内置无人值守护栏**（零配置自带：允许 gradle/mvn/npm/pytest 等构建 + git 只读 + grep/find 等；硬拦 git commit/push/rm/curl/install/sudo）。所以"夜里编译命令被拒"的问题已根治，装完即安全，**不需要改任何 settings.json**
- 若某项目的构建命令不在默认允许集（例：自定义脚本 `./build.sh`），让用户在 config 加 `"allow_tools"` 覆盖默认集（注意：覆盖即全量替换，要把需要的构建命令一并列全），或临时用 `nightcrew add --claude-args '--allowedTools "Bash(./build.sh:*)"'`
- 入**重要仓库**的任务提醒一句：建议 prompt 里写明先开分支再动手（护栏拦了提交，但改动仍落在当前工作区，开分支更干净）
- **working tree 警告**：任务 repo 若是用户白天正在工作的 checkout，提醒夜间任务会直接改动同一工作区——未提交的白天工作可能被搅乱；建议 prompt 里写明在新分支干活
- 不要替用户启动 daemon / 装服务（这些是用户自己一次性操作）
- "昨晚跑得怎么样"类查询：status + list 汇总后，对 done 任务给一句话结果摘要，对 failed/blocked_limit 任务主动看 logs 找原因
- 队列文件损坏 / 命令报错 → 原样展示错误，不要自行改 `~/.config/nightcrew/` 下的文件
