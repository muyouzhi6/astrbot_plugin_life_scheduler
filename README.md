# AstrBot Life Scheduler Plugin

为 AstrBot 设计的拟人化生活日程插件。利用 LLM 根据日期、节日、历史日程和近期对话，自动生成每日穿搭和日程安排，并注入到 System Prompt，让 Bot 拥有连续的"生活"状态。

## 功能特性

- **日程生成**: 结合日期、节日、历史日程和近期对话，生成拟人化日程
- **穿搭推荐**: 根据创意池随机选取风格，生成每日穿搭描述
- **System Prompt 注入**: 自动将当日状态注入 LLM 上下文，Bot 会"记得"自己今天穿了什么、在做什么
- **懒加载**: 未到生成时间时，首次对话自动触发生成
- **补充要求**: 重写日程时可附加自定义要求，让生成更符合预期

## 安装

```bash
pip install holidays APScheduler
```

## 指令列表

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `查看日程` | 所有人 | 查看今日日程和穿搭 |
| `重写日程` | 管理员 | 重新生成今日日程 |
| `重写日程 <补充要求>` | 管理员 | 带补充要求重新生成，例如：`重写日程 今天穿黑色连衣裙，安排一个下午茶` |
| `日程时间 <HH:MM>` | 管理员 | 设置每日自动生成时间 |

别名：`life show`、`life renew`、`life time`

## 配置项

| 字段 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `schedule_time` | string | `07:00` | 每日自动生成日程的时间 |
| `reference_history_days` | int | `3` | 生成时参考的历史日程天数 (1-7) |
| `reference_recent_count` | int | `10` | 生成时参考的近期会话数量，0 表示不参考 |
| `pool` | object | - | 创意池，每次生成随机选取 |
| `prompt_template` | text | - | LLM 生成日程的 Prompt 模板 |

### 创意池 (pool)

每次生成日程时，从各池随机选取一项融入提示词：

| 池 | 示例 |
| :--- | :--- |
| `daily_themes` | 探索日、社交日、宅家日、工作日、运动日... |
| `mood_colors` | 慵懒、活力、优雅、俏皮、温柔、冷艳... |
| `outfit_styles` | 知性学院风、街头休闲风、温柔淑女风、酷飒中性风... |
| `schedule_types` | 户外活动型、社交聚会型、独处充电型、随性漫游型... |

### Prompt 模板占位符

| 占位符 | 说明 |
| :--- | :--- |
| `{date_str}` | 日期，如 2026年01月22日 |
| `{weekday}` | 星期几 |
| `{holiday}` | 节日信息（中国节日） |
| `{persona_desc}` | Bot 人设描述 |
| `{daily_theme}` | 从创意池选取的今日主题 |
| `{mood_color}` | 从创意池选取的心情色彩 |
| `{outfit_style}` | 从创意池选取的穿搭风格 |
| `{schedule_type}` | 从创意池选取的日程类型 |
| `{history_schedules}` | 历史日程记录 |
| `{recent_chats}` | 近期对话记录 |

## 注入机制

插件在 LLM 请求时自动注入当前状态：

```xml
<character_state>
时间: 下午
穿着: 白色针织衫搭配米色阔腿裤...
日程: 上午整理房间，下午去咖啡厅看书...
</character_state>
```

## 注意事项

1. 每日生成消耗 LLM Token，参考天数和对话数越多消耗越大
2. 需安装 `holidays` 库才能识别中国节日
3. 重写日程会覆盖当日已有数据



本插件开发QQ群：215532038

<img width="1284" height="2289" alt="qrcode_1767584668806" src="https://github.com/user-attachments/assets/113ccf60-044a-47f3-ac8f-432ae05f89ee" />

