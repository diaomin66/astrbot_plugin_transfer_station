# 安装、配置与故障排查

## 1. 安装与更新

支持环境：

- AstrBot `>=4.26,<5`
- Python 3.12（已验证）
- QQ OneBot v11 `aiocqhttp`
- NapCatQQ

安装地址：

```text
https://github.com/diaomin66/astrbot_plugin_transfer_station
```

安装或更新后重启 AstrBot。`v1.4.2` 会保留现有数据库，不会清空新人资格、永久用户、兑换码或活动历史。

## 2. 新人礼配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 新人礼总开关 |
| `enabled_group_ids` | `[]` | 启用群白名单，空列表表示全部群 |
| `welcome_content` | 内置欢迎语 | 支持 `{claim_phrase}` |
| `gift_message_content` | 内置私聊内容 | 支持 `{code}`、`{claim_phrase}` |
| `claim_success_content` | 内置文案 | 兑换码成功发送并完成记账后的群内提示 |
| `already_claimed_content` | 内置文案 | QQ 已在任意群领取过 |
| `not_eligible_content` | 内置文案 | QQ 已在永久用户库或没有当前群资格 |
| `no_codes_content` | 内置文案 | 没有可用兑换码 |
| `temporary_chat_failed_content` | 内置文案 | OneBot 明确拒绝主动临时会话后的引导 |
| `gift_manual_review_content` | 内置文案 | 发送结果未知且兑换码已冻结 |
| `baseline_pending_content` | 内置文案 | 当前群成员基线尚未完成 |
| `claim_failed_content` | 内置文案 | 其他未知领取结果 |
| `claim_phrase` | `领取新人礼` | 群内精确领取口令 |
| `mention_new_member` | `true` | 欢迎消息是否自动 `@新人` |

抽奖、补偿和 New API 配置见 [`CAMPAIGNS.md`](CAMPAIGNS.md)。

## 3. 新人礼管理 Page

在 AstrBot 插件管理中打开“新人礼管理”：

1. 查看可用兑换码、已领取人数和待领取新人数。
2. 查看永久用户数据库人数与当日新人数量。
3. 批量粘贴兑换码，每行一个；单次最多 10000 行。
4. 浏览、显示、复制或两次点击删除未使用兑换码。
5. 查看最近领取记录。
6. 处理发送结果未知的人工核查记录。

人工核查前必须先核对 QQ/NapCat 日志：

- **确认送达**：完成领取记账并永久删除兑换码。
- **确认未送达**：释放领取占用并把兑换码退回库存。

页面默认定时刷新。修改库存或核查结果后会立即重新读取统计。

## 4. 抽奖与补偿调度台

在 AstrBot 插件管理中打开“抽奖与补偿调度台”：

### 连接与范围

- 配置 New API 根地址、超时、TLS 验证和 HTTP 例外。
- 配置抽奖、补偿总开关和群白名单。
- 查看令牌、用户名和密码是否已经在 AstrBot 插件设置中配置。
- 测试 New API 状态、身份及管理权限。

Page 不回显、不接收、不保存 New API 令牌或密码。敏感凭据只能在 AstrBot 插件设置中维护。

如果使用 New API `v0.13.x` 等旧版本，请在插件设置中填写超管用户数字 ID；新版 New API 可以留空。该 ID 不是 API 密钥，也不是 New API 用户的模型令牌。

如果 Page 表单存在未保存修改：

- 自动刷新不会覆盖本地内容。
- 远端配置改变后会提示冲突。
- 旧 revision 的保存请求会被拒绝，需刷新后重新确认。
- 设置、抽奖草稿和奖项提交期间控件会暂时锁定；如果同时切换活动，旧请求不会覆盖新活动详情。

### 抽奖

- 创建草稿后，在详情中编辑标题、描述、口令、开始/开奖时间和领奖时限。
- 添加多个奖项并设置人数与额度。
- 发布前 Page 会通过 New API 读取额度换算快照。
- “立即开奖”会实时读取 QQ 群成员列表并过滤退群参与者。
- 当前活动和历史活动均可打开详情。
- `manual_review` 中奖流水可选择“确认到账”或“确认失败”。

删除奖项、发布、开奖、取消和人工核查均使用两次点击确认。

### 补偿

- 填写群号、标题、每人额度、持续时间和总金额。
- 持续时间或总金额使用 `-` 表示不限，但不能同时不限。
- 当前活动可手动关闭。
- 历史活动及所有领取明细永久可查。
- `manual_review` 补偿流水可选择“确认到账”或“确认失败”。

## 5. 新人资格与永久防重

插件按以下顺序处理启用群：

1. 调用 OneBot `get_group_list` 获取机器人所在群。
2. 对启用群调用 `get_group_member_list`。
3. 将当前所有成员 QQ ID 写入永久 `known_users`。
4. 基线完成后收到新的 `group_increase`，才把数据库从未出现过的 QQ ID 标记为新人。
5. 新人只在入群事件所属群具有领取资格。
6. 成功领取后写入全局唯一领取记录。

永久规则：

- `known_users` 中的 QQ ID 不删除。
- `enabled_group_ids` 从白名单移除后，历史成员记录仍保留。
- 退群不会删除永久用户记录。
- 退群重进不会重新获得新人资格。
- 同一 QQ 加入另一个启用群，也不会再次成为新人。
- `claims.user_id` 全局唯一，同一 QQ 在所有群只能成功领取一次。

现有群成员不会自动获得新人礼资格。只有基线完成后收到真实入群事件的新 QQ ID 才可能获得资格。

## 6. QQ 群临时会话

插件调用 OneBot：

```text
send_private_msg
user_id=<新人QQ>
group_id=<新人所在群>
```

同时传入 `user_id` 与 `group_id`，让 NapCat 以群临时会话上下文发送。

### 明确拒绝

NapCat/OneBot 明确返回失败时：

1. 当前兑换码退回库存。
2. 新人资格保留。
3. 群内发送 `temporary_chat_failed_content`。
4. 用户可先主动私聊机器人建立会话，再回群重新领取。

### 未知结果

以下情况不能证明“未送达”：

- 请求超时。
- 网络连接中断。
- 任务取消。
- OneBot 返回结构无法确认。
- QQ 已送达，但本地数据库提交失败。

插件会：

1. 将记录标记为 `gift_reservations.manual_review`。
2. 冻结兑换码，不自动退库。
3. 冻结当前领取，避免重复发送。
4. 在新人礼 Page 等待管理员核查。

这避免了“QQ 实际收到，但超时后兑换码被再次分配”的重复泄露风险。

## 7. 数据库与备份

默认目录：

```text
data/plugin_data/astrbot_plugin_transfer_station/
├── gifts.db
├── lottery.db
└── compensation.db
```

`gifts.db` 主要表：

- `known_users`：永久 QQ 用户库。
- `group_baselines`：群成员基线状态。
- `eligible_members`：新人资格。
- `gift_codes`：尚未发放的完整兑换码。
- `gift_reservations`：发送占用与人工核查。
- `claims`：已完成领取；不保存完整兑换码。

`lottery.db` 保存活动、奖项、参与者、中奖者、发放流水、查号次数和持久通知。

`compensation.db` 保存活动、领取流水、查号次数和持久通知。

### 备份

1. 停止 AstrBot。
2. 复制整个 `astrbot_plugin_transfer_station` 数据目录。
3. 确认同时复制可能存在的 `*.db-wal` 和 `*.db-shm`。
4. 启动 AstrBot。

不要在 AstrBot 运行时只复制主数据库文件，否则可能遗漏 WAL 中尚未合并的数据。

## 8. 常见故障

### 新人没有收到欢迎消息

检查：

- `enabled` 是否开启。
- 当前群是否在 `enabled_group_ids`。
- NapCat 是否上报 `notice_type=group_increase`。
- AstrBot 平台是否为 `aiocqhttp`。
- 群成员基线是否已经完成。

### 一直提示基线未完成

检查 NapCat 的 `get_group_list` 与 `get_group_member_list` 权限及日志。失败后插件会按周期重试；永久用户库不会因短暂失败而清空。

### 群临时会话失败

检查：

- 机器人和新人是否仍在同一群。
- NapCat 登录账号是否正常。
- `send_private_msg` 是否同时收到 `user_id` 和 `group_id`。
- 对方 QQ 是否允许临时会话。
- 用户是否先主动私聊机器人建立会话。

### Page 删除按钮无效

本插件要求 AstrBot `>=4.26`，新人礼 Page 使用 Plugin Page Bridge 的 `POST codes/delete` 接口。确认 AstrBot 版本和浏览器缓存，并重启 AstrBot 后重新打开 Page。

### Page 显示“数据可能已过期”

表示定时刷新至少一个接口失败，页面保留了上一次成功数据。点击“刷新数据”重试，并查看 AstrBot 日志中的 API、SQLite 或 OneBot 错误类型。

### New API 测试失败

依次检查：

1. `newapi_base_url` 是否为根地址且没有查询参数。
2. 生产环境是否使用 HTTPS。
3. 访问令牌是否为管理员或超管令牌，而不是普通用户 API Key。
4. 用户名密码账号是否启用了 2FA。
5. AstrBot 主机能否访问 New API。
6. TLS 证书是否有效。

### 发放进入人工核查

不要直接重试。先在 New API 管理端按用户 ID、额度和时间核对：

- 已到账：核查为成功。
- 确认未到账：核查为失败，释放资格或预算。
- 无法确认：继续保持 `manual_review`。

详细状态与并发策略见 [`CAMPAIGNS.md`](CAMPAIGNS.md)。

## 9. 升级检查清单

1. 停止 AstrBot 并备份三个数据库。
2. 更新插件。
3. 启动 AstrBot，确认版本为 `v1.4.2`。
4. 打开两个 Plugin Page，检查统计与历史活动。
5. 执行 New API 连接测试。
6. 在测试群完成一次小额度抽奖和补偿。
7. 确认新人礼库存、永久用户人数和历史领取记录保持不变。
