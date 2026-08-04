# astrbot_plugin_deepseek_balance DeepSeek 余额监控

**本插件由AI辅助完成，如有问题请前往[QQ交流群](https://qm.qq.com/q/2HuArULfbq)反馈**

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：每日定时查询 DeepSeek 账户余额，当余额低于阈值时向管理群聊 / 管理员私聊自动告警。告警文案支持由 LLM 以当前人格生成，并可检测 LLM 因余额耗尽而不可用的情况。

## 功能特性

- ⏰ **定时查询**：默认每日 06:00 与 18:00 各自动查询一次余额，时间可自定义。
- 🔑 **自动识别 Provider**：从 AstrBot 主配置文件（默认 `~/.config/astrbot/config.json`）的 `providers` 结构中自动定位 DeepSeek Provider 并提取 `api_key`，无需手动输入。
- 💴 **人民币阈值**：告警阈值以人民币（CNY）为单位，默认 70.00 元，按配置汇率（默认 7.2）自动换算 API 返回的美元余额进行比较。
- ⚠️ **低余额告警**：当换算后的人民币余额 ≤ 阈值时，向配置的管理群聊与管理员私聊发送告警。
- 🎭 **人格化告警**：告警文案可调用 LLM 以 AstrBot 当前设定的人格生成（默认开启），使告警语气与机器人人设一致。LLM 不可用时自动回退到固定模板。
- 🔕 **智能冷却**：同余额档位在冷却期内（默认 30 分钟）不会重复告警，防止刷屏。
- ❌ **失败也告警**：API 请求超时 / 返回异常时同样会向管理员通知。
- 🚨 **LLM 耗尽检测**：每次查询后自动检测 LLM 是否因余额耗尽（如 Insufficient Quota / 402 Payment Required）无法使用，若是则向管理员发送自定义告警文案（默认"API余额已经用完，请管理者及时充值。"）。
- 📊 **手动查询指令**：管理员可随时通过指令手动触发余额查询。
- 🗂️ **状态持久化**：运行状态保存到本地 JSON 文件，重启后可追溯上次查询结果。
- 🔒 **安全存储**：`api_key` 仅在内存中使用，不会写入持久化文件。
- 🔌 **多通道发送**：优先使用 AstrBot Context 主动发送接口，回退到 OneBot `send_group_msg` / `send_private_msg`，最后兜底到日志 + 事件补发队列。
- 🚫 **无 Provider 时自动跳过**：若配置中未找到 DeepSeek Provider，插件静默不执行任何查询，仅写日志提示。

## 环境要求

- AstrBot **>= 4.5.7**
- AstrBot 主配置文件（默认路径 `~/.config/astrbot/config.json`）中已配置至少一个 DeepSeek Provider（即在 `providers` 下存在包含 `deepseek` 关键字的条目，且带有 `api_key`）

## 安装

将本插件目录放入 AstrBot 的插件目录：

```
AstrBot/data/plugins/astrbot_plugin_deepseek_balance/
├── metadata.yaml
├── _conf_schema.json
├── main.py
└── README.md
```

> 建议目录名使用 `astrbot_plugin_deepseek_balance`。重启 AstrBot 或在 WebUI「插件」页面重新加载即可。

## 配置

在 WebUI「插件」页面点击本插件的配置按钮即可可视化编辑。主要配置项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 总开关，关闭后插件不执行任何定时查询 |
| `config_path` | `~/.config/astrbot/config.json` | AstrBot 主配置文件路径，自动展开 `~` |
| `provider_keyword` | `deepseek` | 识别 DeepSeek Provider 的关键字（大小写不敏感，匹配 `id` / `name` / `type` / `endpoint`） |
| `threshold_cny` | `70.00` | 告警阈值（人民币 CNY），当换算后的 CNY 余额 ≤ 此值时触发告警 |
| `usd_to_cny_rate` | `7.2` | 美元兑人民币汇率，用于将 API 返回的 USD 余额换算为 CNY 比较 |
| `check_times` | `["06:00", "18:00"]` | 每日检查时间（本地时间 `HH:MM` 格式列表），可自行增删 |
| `notify_groups` | `[]` | 告警群聊 ID 列表，支持 `platform_id:group_id` 多平台格式 |
| `notify_users` | `[]` | 告警管理员私聊 ID 列表，支持 `platform_id:user_id` 多平台格式 |
| `alert_on_query` | `false` | 开启后每次查询（即便余额充足）也会发送结果通知 |
| `cooldown_minutes` | `30` | 同一余额档位重复告警的冷却时间（分钟） |
| `api_endpoint` | `https://api.deepseek.com/user/balance` | DeepSeek 余额查询接口，通常无需修改 |
| `request_timeout` | `15` | HTTP 请求超时秒数 |
| `command_permission` | `admin` | 手动查询指令所需的权限级别 |
| `use_persona` | `true` | 告警文案是否使用 LLM 当前人格生成，关闭则使用固定模板 |
| `persona_provider_id` | 空（自动） | 用于生成人格告警文案的模型提供商 ID，留空则使用当前会话默认模型 |
| `depleted_message` | `API余额已经用完，请管理者及时充值。` | LLM 因余额耗尽无法使用时的告警文案，支持 `{provider}` 占位符 |

### notify_groups / notify_users 格式说明

每个条目支持两种写法：

- `platform_id:target_id`：指定平台，例如 `qq:123456789`（群）或 `qq:10000`（私聊）
- `target_id`：仅填 ID，由插件按可用平台自动尝试

```
notify_groups:
  - "qq:123456789"
  - "qq:987654321"

notify_users:
  - "qq:10000"
```

## 工作流程

1. AstrBot 加载完成后，插件启动后台调度循环。
2. 调度循环每分钟检查当前时间是否命中 `check_times` 配置，命中则异步触发一次查询。
3. 查询时：
   1. 读取 `config_path` 指定的配置文件；
   2. 在 `providers` 下按 `provider_keyword` 定位匹配的 Provider，提取 `api_key`；
   3. 若未找到 Provider，本次跳过并日志提示。
4. 使用 `api_key` 调用 `GET https://api.deepseek.com/user/balance`，请求头 `Authorization: Bearer <API_KEY>`。
5. 从响应中提取余额（兼容 `total_balance` / `available_balance` / `available` / `balance` / `usd_balance` / `remain` / `credits` 等多种字段及嵌套结构）。
6. 按 `usd_to_cny_rate` 将 USD 余额换算为 CNY，与 `threshold_cny` 比较。
7. 若 CNY 余额 ≤ 阈值或请求失败：
   - 检查冷却（同余额档位在冷却期内不重复告警）；
   - 若启用了人格告警（`use_persona=true`），调用 LLM 以当前 AstrBot 人设生成告警文案；
   - LLM 调用失败时自动回退到固定模板文案；
   - 通过多通道向配置的群 / 用户发送告警；
   - 若所有通道均未送达，告警文本进入积压队列，等待下一条任意消息兜底补发。
8. 每次查询（无论是否低余额）都会检测 LLM 是否因余额耗尽无法使用：
   - 通过一次最小化的 LLM ping 调用检测；
   - 若捕获到 Insufficient Quota / 402 / No Balance 等错误，向管理员发送 `depleted_message` 文案（带冷却）。
9. 更新持久化状态文件 `deepseek_balance_data.json`（不写入 `api_key`）。

### 余额解析兼容性

DeepSeek 余额接口的返回结构可能随版本调整，插件已兼容以下常见字段路径：

```json
{
  "total_balance": 100.00,        // 常见顶层字段
  "available_balance": 50.00,
  "balance": 80.00,
  "data": {
    "balance": 75.00              // 嵌套 data.balance
  }
}
```

只要响应中存在以上任一字段即可正确提取。

## 管理指令

> 以下指令仅 `command_permission` 配置的权限可用（默认 `admin`）。

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/deepseek_balance` | `/余额` | 手动触发一次余额查询并在当前会话返回结果 |

## 效果示例

### 手动查询

```
管理员: /余额
机器人: 正在查询 DeepSeek 余额…
机器人: ⚠️ 余额不足
        Provider: deepseek_api
        当前余额: $5.23（¥37.66）
        告警阈值：¥70.00（汇率 7.20）
        查询时间：2026-08-04 18:00:12
```

### 人格化低余额告警（定时触发）

```
（定时触发，LLM 以"管家"人格生成）
机器人在管理群:
⚠️ DeepSeek 余额告警
时间：2026-08-04 06:00:05
Provider：deepseek_api

主人，账户余额已低于预警线，请尽快充值以免影响服务运转。

当前余额：$5.23（¥37.66）  阈值：¥70.00
```

### LLM 耗尽告警

```
（检测到 LLM 因余额耗尽无法调用时）
机器人在管理群:
API余额已经用完，请管理者及时充值。

原因：Insufficient Quota (HTTP 402)
```

## 文件结构

```
astrbot_plugin_deepseek_balance/
├── metadata.yaml              # 插件元数据
├── _conf_schema.json          # 配置项 Schema（WebUI 可视化配置）
├── main.py                    # 插件主逻辑
├── README.md
└── deepseek_balance_data.json # 运行时自动生成，保存查询状态（不含 api_key）
```

## 常见问题

**Q: 插件启动后提示"未配置 DeepSeek Provider"？**
A: 请确认 AstrBot 主配置文件路径正确，且 `providers` 下存在包含 `deepseek` 关键字的条目并已配置 `api_key`。关键字匹配不区分大小写。

**Q: 查询返回"请求超时"怎么办？**
A: 检查网络是否可访问 `https://api.deepseek.com`，或适当调大 `request_timeout`。

**Q: 告警金额显示不正确？**
A: 请检查 `usd_to_cny_rate` 汇率是否为当前实际汇率。默认 7.2 为参考值，可按实际情况调整。

**Q: 人格化告警不生效？**
A: 请确认 `use_persona` 已开启，并在 AstrBot WebUI 「人格与情景」中设置了首选人格。若指定了 `persona_provider_id`，请确保该 Provider 可用。LLM 调用失败时会自动回退到固定模板。

**Q: LLM 耗尽检测会影响正常使用吗？**
A: 不会。检测仅在余额查询后触发一次最小化的 ping 调用，消耗极少 token。若 LLM 已耗尽，该 ping 会失败并触发告警文案，不会影响机器人的正常运行。

**Q: 告警没有送到群里？**
A: 请确认 `notify_groups` 中填写的群号格式正确（多平台需加 `platform_id:` 前缀）。若所有通道都未送达，告警会在你下次发消息时由机器人兜底补发。

**Q: 能否同时监控多个 DeepSeek Provider？**
A: 当前版本取第一个匹配到的 Provider。如需同时监控多个，可将它们的 `endpoint` 都指向 DeepSeek，或通过 `provider_keyword` 调整匹配规则。

## 许可证

MIT
