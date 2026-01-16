# AstrBot Life Scheduler Plugin

`astrbot_plugin_life_scheduler` 是一个为 AstrBot 设计的拟人化生活日程生成插件。它利用 LLM 的能力，根据日期、节日、历史日程和近期对话记录，自动为 Bot 生成每日的穿搭和日程安排，并将其注入到系统提示词（System Prompt）中，使 Bot 拥有更加真实、连续的“生活”状态。

## ✨ 功能特性

- **📅 拟人化日程生成**: 结合日期、节日（中国节假日）、历史日程和近期对话，生成富有生活气息的日程表。
- **👗 每日穿搭推荐**: 根据设定生成符合人设的每日穿搭描述。
- **🧠 System Prompt 注入**: 自动将当日穿搭和当前时间段的日程状态（刚开始、进行中、即将结束）注入 LLM 上下文。Bot 在日常对话中会“记得”自己今天穿了什么，正在做什么。
- **🔗 上下文感知**: 生成日程时会参考过去几天的安排和指定的最近聊天记录，保持人设的一致性和记忆连贯性。
- **⚡️ 懒加载机制**: 如果当天未到达生成时间或未生成，在首次对话时会自动生成，确保人设数据始终可用。

## 💿 安装与依赖

插件依赖以下 Python 库，请在 AstrBot 环境中安装：

```bash
pip install holidays APScheduler
```

或者在插件目录下运行：

```bash
pip install -r requirements.txt
```

## ⚙️ 配置说明

配置文件 `config.json` 支持通过 AstrBot 管理面板配置。

### 主要配置项

| 字段 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `schedule_time` | string | `"07:00"` | 每日自动生成日程并播报的时间 (HH:MM)。 |
| `reference_history_days` | int | `3` | 生成今日日程时，参考过去几天的历史日程。 |
| `reference_chats` | list | `[]` | 生成时参考的近期会话列表。详见下方示例。 |
| `outfit_desc` | string | *(见配置)* | 指导 LLM 生成穿搭的提示词要求。 |
| `prompt_template` | string | *(见配置)* | 自定义 LLM 生成日程的完整 Prompt 模板。 |

### `reference_chats` 配置示例

配置此项可以让 Bot 根据最近聊过的话题来安排今天的日程（例如昨天答应了群友今天要去看电影）。

> **提示**: `umo` 字段为 Unified Message Origin (统一消息来源)。
>
> **如何获取 UMO？**
> 对 Bot 发送 `/sid` 指令，Bot 会返回当前会话的详细信息，其中包含 `UMO` 字段。
>
> 示例格式：
> - QQ 私聊：`QQ:FriendMessage:123456`
> - QQ 群聊：`QQ:GroupMessage:654321`

```json
"reference_chats": [
  {
    "umo": "QQ:GroupMessage:123456",
    "count": 20
  },
  {
    "umo": "QQ:FriendMessage:987654",
    "count": 10
  }
]
```

## 📝 指令列表

| 指令 | 说明 |
| :--- | :--- |
| `/life show` | 查看 Bot 今日的日程安排和穿搭。 |
| `/life regenerate` | 强制重新生成今日日程（会覆盖已有数据，消耗 LLM Token）。 |

## 🧩 进阶说明

### 人设注入机制

插件会在 AstrBot 处理 LLM 请求时，动态向 System Prompt 追加如下信息：

```text
[今日生活状态 (进行中)]
穿搭：白色T恤搭配浅蓝色牛仔裤，背着帆布包。
日程：上午去市图书馆查阅资料；中午和朋友在附近的咖啡厅简餐；下午回来整理笔记。
请在回答中体现这些生活状态。
```

* `(进行中)` 状态会根据当前时间自动判断：`刚开始` (<9点)、`进行中`、`即将结束` (>22点)。

### 懒加载机制

如果到了预定的生成时间（默认 07:00）Bot 未运行或生成失败，插件会在当天的第一次对话时自动触发“懒加载”生成。这确保了无论 Bot 何时启动，只要有对话发生，人设数据都是可用的。

## ⚠️ 注意事项

1. **Token 消耗**: 每日生成日程会调用一次 LLM，消耗一定的 Token。配置的参考历史天数和聊天记录越多，消耗越大。
2. **依赖安装**: 务必确保安装了 `holidays` 库，否则节日判断功能将失效（默认为中国节日）。

本插件开发QQ群：215532038

<img width="1284" height="2289" alt="qrcode_1767584668806" src="https://github.com/user-attachments/assets/113ccf60-044a-47f3-ac8f-432ae05f89ee" />
