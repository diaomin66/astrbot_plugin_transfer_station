# AstrBot 中转站机器人

面向 QQ OneBot v11 / NapCat 的 AstrBot 插件，包含三个相互独立的系统：

1. **新人礼**：入群资格、永久 QQ 用户库、一次性兑换码与群临时会话发放。
2. **抽奖**：分级奖项、定时开奖、退群过滤、New API 自动加额度与人工核查。
3. **补偿**：按持续时间或总预算运行，单场 QQ / New API ID 双重防重。

当前版本：`v1.4.1`。

## 功能概览

### 新人礼

- 插件启用或更新后，先同步白名单群现有成员的精确 QQ ID。
- 只有永久用户库中从未出现过的新入群 QQ ID 才能取得资格。
- 同一 QQ 在所有群中只能成功领取一次，退群重进无法重复领取。
- 新人必须在取得资格的群内准确发送 `@机器人 领取新人礼`。
- 兑换码仅通过 QQ 群临时会话发送，不会公开到群内。
- OneBot 明确拒绝发送时，兑换码立即退回库存。
- 超时、断线或未知发送结果会冻结兑换码并进入人工核查，不自动重发。
- “新人礼管理”Page 支持库存、领取记录、永久用户数、当日新人和发放核查。

### 抽奖

- 每个群最多同时存在一场未结束抽奖。
- 支持草稿、开始/开奖时间、报名口令、活动说明、领奖时限和多个分级奖项。
- 报名必须准确发送 `@机器人 <报名口令>`，同一 QQ 每场只能报名一次。
- 开奖前重新读取群成员，已退群报名者不进入抽奖池。
- 使用 `secrets.SystemRandom` 无重复抽取，结果与参与者快照永久保存。
- 中奖者提交 New API 用户 ID，确认后通过 `add_quota` 原子增加额度。
- 不同中奖者可以把奖励发到同一个有效 New API ID。

### 补偿

- 每个群最多同时运行一场补偿。
- 持续时间和总金额至少限制一项；同时设置时任一条件先满足即结束。
- 同一场活动内，一个 QQ 和一个 New API ID 均只能到账一次。
- 剩余预算不足完整一份时自动结束，不部分发放、不超预算。
- 活动结束后记录永久归档；下一场使用新活动编号重新计数。

### 抽奖与补偿调度台

`v1.4.0` 新增独立 Plugin Page；`v1.4.1` 增加旧版 New API 认证兼容与错误诊断：

- 查看 New API、抽奖与补偿启用状态。
- 配置两套系统开关、群白名单和 New API 非敏感连接参数。
- 测试 New API 状态、身份及管理权限。
- 创建、编辑、发布、开奖、取消抽奖，管理分级奖项。
- 开启、关闭补偿活动。
- 可视化当前活动、历史活动、中奖记录、补偿记录和人工核查。
- 活动 ID、New API ID 和 quota 均以字符串传到前端，避免 JavaScript 大整数精度损坏。
- Page 保存和活动操作期间会锁定表单，并忽略已切换活动的旧详情刷新。
- 可选填写 New API 超管用户数字 ID，兼容 v0.13.x 等要求 `New-Api-User` 请求头的旧版站点。
- New API 失败文案支持 `{error}`，仅显示脱敏后的认证、权限、地址或版本原因。
- Page 不回显、不接收、不保存 New API 密码或访问令牌；凭据仍在 AstrBot 插件设置中维护。

## 环境要求

- AstrBot `>=4.26,<5`
- Python 3.12（已验证）
- QQ OneBot v11 `aiocqhttp`
- NapCatQQ
- QuantumNous New API 或接口兼容实现

## 安装

在 AstrBot WebUI 的插件管理中通过仓库地址安装：

```text
https://github.com/diaomin66/astrbot_plugin_transfer_station
```

安装或更新后重启 AstrBot，并确认插件版本为 `v1.4.1`。

## 快速配置

1. 在 AstrBot 插件设置中配置新人礼开关、群白名单、欢迎内容和领取口令。
2. 打开“新人礼管理”，导入每行一个的兑换码。
3. 在插件设置中填写 `newapi_base_url`，并优先配置管理员访问令牌。
4. 打开“抽奖与补偿调度台”，配置抽奖/补偿开关和群白名单。
5. 在调度台执行 New API 连接测试。
6. 先使用测试 New API 实例和测试群完成真实加额度验收。

用户名密码仅作为兼容登录方式。启用 2FA 的 New API 管理账号必须改用访问令牌。

## 常用指令

### New API

```text
/newapi 测试
```

### 抽奖管理

```text
/抽奖 创建 <标题>
/抽奖 时间 <开始时间> <开奖时间>
/抽奖 口令 <报名关键词>
/抽奖 描述 <内容>
/抽奖 奖项添加 <奖项名> <人数> <额度>
/抽奖 奖项删除 <序号>
/抽奖 领奖时限 <时长>
/抽奖 发布
/抽奖 状态
/抽奖 参与者 [页码]
/抽奖 提前开奖
/抽奖 取消 [原因]
/抽奖 核查 <流水号> <成功|失败>
/抽奖 帮助
```

### 补偿管理

```text
/补偿 开启 <每人额度> <持续时间|-> <总金额|-> [标题]
/补偿 状态
/补偿 记录 [页码]
/补偿 关闭 [原因]
/补偿 核查 <流水号> <成功|失败>
/补偿 帮助
```

管理员指令均要求 AstrBot `ADMIN` 权限。

## 用户操作

```text
@机器人 <抽奖报名口令>
@机器人 抽奖 <New API用户ID>
@机器人 确认 抽奖
@机器人 取消 抽奖

@机器人 补偿 <New API用户ID>
@机器人 确认 补偿
@机器人 取消 补偿
```

提交 New API ID 后的确认状态 5 分钟失效。

## 失败与人工核查

- 明确的 4xx 或上游拒绝：标记失败并释放资格/预算，可重新提交。
- 超时、断线、5xx、未知 JSON 或取消中的写请求：标记 `manual_review`。
- `manual_review` 会冻结资格和预算，插件不会自动重试。
- 管理员核对 New API、QQ 或 NapCat 日志后，在指令或调度台选择“成功”或“失败”。
- 重启时遗留 `processing` 会在安全等待期后转入人工核查。

## 数据与备份

默认数据目录：

```text
data/plugin_data/astrbot_plugin_transfer_station/
├── gifts.db
├── lottery.db
└── compensation.db
```

三个数据库互不读取、互不修改。备份前请停止 AstrBot，并同时复制数据库主文件及可能存在的 `-wal`、`-shm` 文件。

## 开发验证

```powershell
python -m pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning
ruff check main.py page_api.py campaign_page_api.py storage.py sqlite_utils.py campaign_utils.py campaign_messages.py newapi_client.py lottery.py compensation.py tests
ruff format --check main.py page_api.py campaign_page_api.py storage.py sqlite_utils.py campaign_utils.py campaign_messages.py newapi_client.py lottery.py compensation.py tests
python -m py_compile main.py page_api.py campaign_page_api.py storage.py sqlite_utils.py campaign_utils.py campaign_messages.py newapi_client.py lottery.py compensation.py
node --check pages/gift_codes/app.js
node --check pages/campaigns/app.js
npm run test:pages
```

详细配置与故障排查见：

- [`docs/USAGE.md`](docs/USAGE.md)
- [`docs/CAMPAIGNS.md`](docs/CAMPAIGNS.md)

## License

MIT
