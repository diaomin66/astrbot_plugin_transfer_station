## 2026-07-29 - Task: 实现 AstrBot 中转站新人礼插件并准备 GitHub 发布

### What was done

- 完成 QQ OneBot/NapCat 新人入群资格记录、可配置欢迎消息和精确口令识别。
- 完成全局一次领取、群临时会话发码、成功删除兑换码及失败事务回滚。
- 完成 SQLite 持久化、并发串行化和不保留已领取完整兑换码的隐私处理。
- 完成 AstrBot Plugin Page 兑换码批量导入、库存分页、遮罩显示、复制、删除、统计和领取记录。
- 完成插件配置、元数据、中英文 Page 标题、安装使用文档和自动化测试。

### Testing

- `python -m pytest -q`：14 项测试全部通过。
- `python -m ruff check .`：通过。
- `python -m ruff format --check .`：通过。
- `python -m py_compile main.py page_api.py storage.py`：通过。
- `node --check pages/gift_codes/app.js`：通过。
- AstrBot 4.26.7 非侵入式兼容验证：元数据加载、插件类识别和 `gift_codes` Page 发现均通过。
- 未执行真实 QQ 群临时会话端到端发放；该项需要在线 NapCat 账号、测试群和实际新人账号。

### Notes

- `main.py`：新增入群欢迎、资格判断、精确领取口令和 NapCat 群临时会话发码流程。
- `storage.py`：新增 SQLite 初始化、库存、新人资格、领取记录、并发锁和事务回滚。
- `page_api.py`：新增 Plugin Page 统计、库存、导入、删除和领取记录 API。
- `pages/gift_codes/index.html`：新增新人礼管理页面结构。
- `pages/gift_codes/style.css`：新增响应式明暗主题管理页面样式。
- `pages/gift_codes/app.js`：新增 Page Bridge 数据读取、导入、遮罩、复制、删除和分页交互。
- `metadata.yaml`：新增插件身份、版本、平台兼容和 Page 元数据。
- `_conf_schema.json`：新增总开关、群白名单、欢迎内容、领取口令和新人提醒配置。
- `.astrbot-plugin/i18n/zh-CN.json`：新增中文插件与 Page 标题。
- `.astrbot-plugin/i18n/en-US.json`：新增英文插件与 Page 标题。
- `requirements.txt`：声明 SQLite 异步驱动依赖。
- `README.md`：新增功能、安装、使用、限制和验证说明。
- `docs/USAGE.md`：新增配置、运营、备份、临时会话要求和故障排查文档。
- `tests/conftest.py`：新增仓库目录无关的插件模块加载辅助。
- `tests/test_storage.py`：新增存储、回滚、全局一次领取和并发库存测试。
- `tests/test_plugin.py`：新增入群、白名单、口令、发码和失败回滚测试。
- `tests/test_page_api.py`：新增 Page API 导入、分页、删除和参数验证测试。
- `tests/test_static_contract.py`：新增元数据、配置和前端安全渲染契约测试。
- `.gitignore`：新增 Python、测试、IDE 和运行时数据库忽略规则。
- `LICENSE`：新增 MIT 许可证。
- `progress.md`：追加本轮实现、验证、文件清单和回滚说明。
- 回滚方式：发布后在仓库执行 `git revert <本次初始提交哈希>`；若已安装到 AstrBot，可同时删除 `data/plugins/astrbot_plugin_transfer_station`，需要清除业务数据时再手动删除 `data/plugin_data/astrbot_plugin_transfer_station`。
