# AstrBot 中转站机器人

面向 QQ OneBot v11 / NapCat 的 AstrBot 新人礼插件。插件会记录实际入群的新人，在新人于同一群中 `@机器人` 并发送指定口令后，通过 QQ 群临时会话发放一个兑换码。

## 功能

- 新人入群自动欢迎，欢迎内容和领取口令可配置。
- 只有插件记录到的本群新人可以领取。
- 同一 QQ 号在机器人覆盖的所有群中只能成功领取一次。
- 兑换码仅在 QQ 群临时会话中发送，不在群内公开。
- 发送成功后从库存删除；发送失败时事务回滚并退回库存。
- AstrBot Plugin Page 提供批量导入、库存查看、复制、删除和领取记录。
- SQLite 持久化新人资格、库存和领取历史。

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

安装后重载插件，并在插件配置中设置欢迎内容、领取口令和可选群白名单。

## 使用

1. 从插件详情进入 **新人礼管理** 页面。
2. 在批量导入框中每行填写一个兑换码。
3. 新成员入群后，机器人发送欢迎消息并记录资格。
4. 新成员在该群发送：

   ```text
   @机器人 领取新人礼
   ```

5. 机器人通过该群的 QQ 临时会话发送兑换码，群内仅提示发送结果。

## 重要规则

- 插件安装前已经在群内的成员不会自动获得资格。
- `enabled_group_ids` 为空时对所有 QQ 群生效。
- 领取口令采用去除首尾空白后的完全匹配，附加文字、图片或 `@` 其他成员不会触发发放。
- 若 NapCat 报错，兑换码会退回库存。网络超时存在“QQ 实际已送达但本地回滚”的协议边界风险。
- 完整兑换码只在未领取库存中保存；领取记录仅保留尾号和 SHA-256 摘要。

更完整的部署、备份和排障说明见 [`docs/USAGE.md`](docs/USAGE.md)。

## 开发验证

```powershell
python -m pytest -q
python -m py_compile main.py page_api.py storage.py
node --check pages/gift_codes/app.js
ruff check .
ruff format --check .
```

## License

MIT
