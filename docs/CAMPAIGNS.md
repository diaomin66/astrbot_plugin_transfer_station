# 抽奖与补偿系统

插件 `v1.3.0` 将新人礼、抽奖和补偿拆成三个互不读取业务数据的系统：

- 新人礼继续使用 `gifts.db`；
- 抽奖使用 `lottery.db`；
- 补偿使用 `compensation.db`；
- 三者只共享 New API 客户端，不共享资格、用户去重或兑换码。

每个 QQ 群最多同时存在一场未结束抽奖和一场进行中的补偿。活动记录只做逻辑归档，不物理删除。

## 1. New API 配置

在插件配置中填写：

| 配置项 | 说明 |
| --- | --- |
| `newapi_base_url` | New API 根地址，生产环境使用 HTTPS |
| `newapi_access_token` | 管理员/超管访问令牌，优先使用 |
| `newapi_username` / `newapi_password` | 没有令牌时的兼容登录方式 |
| `newapi_timeout_seconds` | 请求超时，默认 10 秒 |
| `newapi_verify_ssl` | TLS 证书校验，默认开启 |
| `newapi_allow_insecure_http` | 是否允许 HTTP，默认关闭 |

访问令牌必须具备管理员或超管权限。密码登录遇到 2FA 会停止并提示改用令牌；插件不记录凭据、令牌或完整远端响应。

先在群内使用：

```text
/newapi 测试
```

测试会检查站点状态、当前身份和管理员接口权限，不会修改任何用户额度。

### 额度换算

发布活动时读取 `/api/status` 并保存快照，活动期间修改 New API 汇率不会改变已发布奖励：

- `USD`：填写金额 × `quota_per_unit`
- `CNY`：填写金额 ÷ `usd_exchange_rate` × `quota_per_unit`
- `CUSTOM`：填写金额 ÷ `custom_currency_exchange_rate` × `quota_per_unit`
- `TOKENS`：填写值直接作为原始 quota

使用 `Decimal` 和四舍五入为整数。到账调用 `/api/user/manage` 的原子 `add_quota` 增量接口，不覆盖用户现有余额。

## 2. 抽奖

开启 `lottery_enabled`，可用 `lottery_enabled_group_ids` 限制群白名单（空列表表示全部群）。管理员指令：

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
```

时间支持 `YYYY-MM-DDTHH:mm`（按上海时区）、`now`、`+30m`、`+2h`、`+1d`。新草稿默认为立即开始、1 小时后开奖、口令“参与抽奖”、领奖期限 24 小时。
报名口令不能与新人礼口令、抽奖领奖确认或补偿领取指令冲突。

报名必须在活动开放期间发送：

```text
@机器人 参与抽奖
```

同一 QQ 每场只能报名一次。开奖前会重新读取群成员列表，已经退群的报名者被排除；读取失败时保留活动并由调度器重试。开奖使用系统随机源，无重复抽取，按奖项顺序分配。

中奖者发送 `@机器人 抽奖 <New API用户ID>` 后，机器人显示用户信息和额度；再发送 `@机器人 确认 抽奖` 才会到账。确认 5 分钟失效，领奖截止后不补抽。不同中奖者可以选择同一个有效 New API ID。

## 3. 补偿

开启 `compensation_enabled`，可用 `compensation_enabled_group_ids` 限制群白名单。管理员指令：

```text
/补偿 开启 <每人额度> <持续时间|-> <总金额|-> [标题]
/补偿 状态
/补偿 记录 [页码]
/补偿 关闭 [原因]
/补偿 核查 <流水号> <成功|失败>
```

持续时间和总金额至少填写一个，`-` 表示不限制。例如：

```text
/补偿 开启 10 2h 1000 服务异常补偿
/补偿 开启 5 - 500
/补偿 开启 3 1d -
```

用户发送 `@机器人 补偿 <New API用户ID>`，确认方式为 `@机器人 确认 补偿`。一个活动内 QQ 和 New API ID 均只能成功到账一次。剩余预算不足一份完整补偿时活动自动结束，不发放部分额度。

管理员关闭、到时或预算结束后，待确认申请会取消，历史记录保留；下一场活动使用新的活动编号，旧活动的去重记录不会污染新活动。

## 4. 失败与人工核查

领取状态统一使用：

```text
pending_confirmation
processing
paid
failed
manual_review
cancelled
expired
```

- 明确的 4xx 或参数错误：标记 `failed`，释放资格/预算，活动仍开放时可重新提交；
- 超时、断线或 5xx：标记 `manual_review`，不自动重试，冻结资格/预算；
- 重启时遗留 `processing` 自动转为 `manual_review`；
- 管理员使用“核查 成功”完成记账，或“核查 失败”释放资格。

插件不会因为远端超时而假定到账成功。由于网络协议无法证明超时请求是否已在 New API 生效，人工核查前禁止自动重试。

## 5. 数据、备份与回滚

数据目录：

```text
data/plugin_data/astrbot_plugin_transfer_station/
├── gifts.db
├── lottery.db
└── compensation.db
```

停止 AstrBot 后备份整个目录。降级时应先恢复与旧版本匹配的数据库备份，再回滚代码；不要仅回滚 Python 文件而继续使用新数据库结构。

所有聊天文案（包括帮助、状态、确认、成功、失败、人工核查和调度通知）均有对应的 `*_content` 配置项。占位符按默认文案中的名称填写，例如 `{title}`、`{amount}`、`{user_id}`、`{username}`、`{serial}`、`{claim_deadline}`。
