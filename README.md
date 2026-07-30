# AstrBot 中转站机器人

面向 QQ OneBot v11 / NapCat 的 AstrBot 中转站插件。除新人礼外，`v1.3.0` 增加了独立的抽奖和补偿系统，可通过 QuantumNous New API 兼容接口给用户原子增加额度。

## 功能

- 新人入群自动欢迎，领取口令和机器人全部聊天文案均可配置。
- 插件启动后自动同步启用群当前全部成员 QQ ID，建立永久成员基线。
- 只有永久用户库中从未出现过、且在基线完成后首次入群的 QQ ID 可以领取。
- 同一 QQ 号在机器人覆盖的所有群中只能成功领取一次。
- 已记录 QQ ID 和领取记录不提供删除功能，退群重进不会重置资格。
- 兑换码仅在 QQ 群临时会话中发送，不在群内公开。
- 发送成功后从库存删除；发送失败时事务回滚并退回库存。
- 主动临时会话失败时，引导新人先私聊机器人建立会话，再回群重新领取。
- AstrBot Plugin Page 提供批量导入、库存查看、复制、删除、领取记录、数据库人数和今日新人实时统计。
- SQLite 持久化新人资格、库存和领取历史。
- 抽奖和补偿分别使用独立 SQLite 数据库，与新人礼数据完全隔离。
- New API 支持管理员访问令牌、兼容用户名密码登录、额度换算快照和人工核查状态。
- 抽奖支持定时开奖、分级奖项、退群过滤、确认领奖和永久活动归档。
- 补偿支持按持续时间/总预算结束、QQ 与 New API ID 双重去重和并发预算保护。
- 抽奖、补偿和 New API 测试的机器人回复全部可配置。

## 环境要求

- AstrBot `>=4.16,<5`
- QQ OneBot v11 `aiocqhttp` 适配器
- NapCatQQ
- Python 3.12 或更高版本（按当前 AstrBot 运行要求）

## 安装

在 AstrBot WebUI 的插件管理中使用仓库地址安装：

```text
https://github.com/diaomin66/astrbot_plugin_transfer_station
```

更新前建议备份插件数据目录。安装或更新到 `v1.3.0` 后重载插件；新人礼配置保持不变。需要抽奖或补偿时，再配置 New API、对应系统开关和群白名单。

## 使用

1. 从插件详情进入 **新人礼管理** 页面。
2. 在批量导入框中每行填写一个兑换码。
3. 等待启用群成员基线同步完成。
4. 数据库从未记录过的新 QQ ID 入群后，机器人发送欢迎消息并记录资格。
5. 新成员在该群发送：

   ```text
   @机器人 领取新人礼
   ```

6. 机器人通过该群的 QQ 临时会话发送兑换码，群内仅提示发送结果。
7. 若机器人无法主动建立临时会话，新人按群内提示先主动私聊机器人发送任意消息，再回群重新执行第 5 步。

### 抽奖与补偿快速开始

1. 配置 `newapi_base_url` 和管理员 `newapi_access_token`，在群内执行 `/newapi 测试`。
2. 打开 `lottery_enabled` 或 `compensation_enabled`，按需填写各自群白名单。
3. 按 [`docs/CAMPAIGNS.md`](docs/CAMPAIGNS.md) 的管理员指令创建活动。
4. 首次联调请使用测试 New API 实例和测试用户，不要直接使用生产额度。

## 重要规则

- 更新后首次启动会把启用群当前成员以及旧版资格、领取记录迁移到永久用户库；这些 QQ ID 不会获得新资格。
- `enabled_group_ids` 为空时对所有 QQ 群生效。
- 插件每次启动都会重新核对启用群当前成员，只新增永久记录，不删除或重置已有记录。
- 为避免启动竞态被利用，成员基线完成前加入群的 QQ ID 会按当前成员写入基线，不补发新人资格。
- 同一 QQ ID 一旦进入永久用户库，在任何启用群退群重进都不会再次成为新人。
- 领取口令采用去除首尾空白后的完全匹配，附加文字、图片或 `@` 其他成员不会触发发放。
- 欢迎、私聊兑换码、领取成功、重复领取、无资格、库存不足、临时会话失败和其他失败文案均可配置。
- 私聊兑换码文案支持 `{code}`；若没有填写该占位符，插件会自动在文案末尾附加兑换码。
- 临时会话失败文案支持 `{claim_phrase}`，默认明确引导用户先主动私聊，再回群重新领取。
- 若 NapCat 报错，兑换码会退回库存且不会记录领取，用户建立会话后可以重试。网络超时仍存在“QQ 实际已送达但本地回滚”的协议边界风险。
- 抽奖/补偿的 New API 明确失败会释放资格；超时、断线或 5xx 会进入 `manual_review`，必须管理员核查，不会自动重试。
- 完整兑换码只在未领取库存中保存；领取记录仅保留尾号和 SHA-256 摘要。
- 插件无法识别更新时已经不在任何启用群、且旧数据库从未记录过的历史退群账号；该限制来自 QQ 群成员接口只能返回当前成员。

更完整的部署、备份和排障说明见 [`docs/USAGE.md`](docs/USAGE.md)。
抽奖、补偿和 New API 的完整运维说明见 [`docs/CAMPAIGNS.md`](docs/CAMPAIGNS.md)。

## 开发验证

```powershell
python -m pytest tests -q
python -m py_compile main.py page_api.py storage.py campaign_utils.py campaign_messages.py newapi_client.py lottery.py compensation.py
node --check pages/gift_codes/app.js
ruff check main.py page_api.py storage.py campaign_utils.py campaign_messages.py newapi_client.py lottery.py compensation.py tests
ruff format --check main.py page_api.py storage.py campaign_utils.py campaign_messages.py newapi_client.py lottery.py compensation.py tests
```

## License

MIT
