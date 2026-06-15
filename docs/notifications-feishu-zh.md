# nightcrew 飞书通知配置指南

nightcrew 在任务 **开始 / 完成 / 失败 / 撞配额暂停（含恢复时间）** 四个时机发通知。
所有配置写在你自己的 `~/.config/nightcrew/config.json`，与他人无关、不含任何共享凭证。

按需求三选一：

---

## 方式一：群机器人 webhook（最简单，30 秒，消息发到群）

适合：能接受消息进群（也可以建一个只有自己的小群，体验接近私聊）。

1. 目标群 → **设置 → 群机器人 → 添加机器人 → 自定义机器人**，复制 webhook 地址；
2. 写进 `~/.config/nightcrew/config.json`：

```json
{ "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx" }
```

完事。nightcrew 识别群机器人地址后自动使用飞书消息格式，无需其他配置。

---

## 方式二：多维表格自动化（私聊发给自己，不建群）

适合：想要机器人**私聊**通知、又不想找管理员要任何东西。原理：飞书多维表格的
自动化流程支持「**收到 Webhook 时**」作为触发器，动作支持「**发送飞书消息**」给指定成员
（参考官方文档：[使用自动化流程的 webhook 触发](https://www.feishu.cn/hc/zh-CN/articles/612376356355)、
[自动化 webhook 参数的详细说明](https://www.feishu.cn/hc/zh-CN/articles/383585269199)）。

1. 飞书 → 新建 → **多维表格**（内容随意，它只是自动化流程的宿主）；
2. 表格右上角 **自动化 → 新建自动化流程**；
3. 触发器选 **「收到 Webhook 时」**，复制飞书生成的 webhook URL；
4. 按界面引导定义参数。nightcrew 发送的 JSON 长这样，可直接作为示例粘贴：

```json
{
  "source": "nightcrew",
  "title": "nightcrew: task done",
  "message": "[3da094eb] refactor the settings module...",
  "timestamp": "2026-06-12T02:11:08+00:00"
}
```

   把 `title` 和 `message` 声明为参数；
5. 添加动作 **「发送飞书消息」** → 接收人选**自己** → 消息内容引用上面两个参数（如 `{{title}}：{{message}}`）；
6. 保存并启用流程，把第 3 步的 URL 写进 config：

```json
{ "webhook_url": "<自动化流程给的 webhook URL>" }
```

> 说明：此类 URL 走 nightcrew 的 generic JSON 格式（即第 4 步那个结构），正好是流程参数需要的。
> 若消息格式异常，在 config.json 里显式加一行 `"webhook_format": "generic"`。
>
> ⚠️ 本节步骤依据官方文档梳理，个别界面措辞可能随版本变化。第一位配置的同学如发现
> 与实际界面不符，请反馈修正本文档。

---

## 方式三：notify_command 自定义命令（高级，接任意通知渠道）

适合：自己有顺手的通知工具（自建机器人 CLI、ntfy、邮件脚本等）。

```json
{ "notify_command": "/path/to/your-notify-script.sh" }
```

nightcrew 每次通知会执行该命令，标题和内容通过环境变量传入：

- `$NIGHTCREW_TITLE` — 如 `nightcrew: task done`
- `$NIGHTCREW_MESSAGE` — 如 `[3da094eb] refactor ...`

脚本里调什么、发给谁完全自定义；凭证由你的脚本自己管理，**不要写进 nightcrew 的任何配置**。

验证命令（不消耗 Claude 配额）：

```bash
NIGHTCREW_TITLE=test NIGHTCREW_MESSAGE=hello /path/to/your-notify-script.sh
```
