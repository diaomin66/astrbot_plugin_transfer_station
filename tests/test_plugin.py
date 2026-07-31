from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import load_plugin_module

main = load_plugin_module("main")
campaign_utils = load_plugin_module("campaign_utils")
newapi_module = load_plugin_module("newapi_client")
Comp = main.Comp
TransferStationPlugin = main.TransferStationPlugin


class FakeContext:
    def __init__(self, bot=None):
        self.routes = []
        platform_insts = []
        if bot is not None:
            platform_insts.append(FakePlatform(bot))
        self.platform_manager = SimpleNamespace(platform_insts=platform_insts)

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler, methods, description))


class FakePlatform:
    def __init__(self, bot):
        self.bot = bot

    def meta(self):
        return SimpleNamespace(name="aiocqhttp")

    def get_client(self):
        return self.bot


class FakeBot:
    def __init__(
        self,
        error: Exception | None = None,
        action_results: dict | None = None,
        action_errors: dict | None = None,
    ):
        self.error = error
        self.action_results = action_results or {}
        self.action_errors = action_errors or {}
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action in self.action_errors:
            raise self.action_errors[action]
        if self.error and action == "send_private_msg":
            raise self.error
        if action in self.action_results:
            result = self.action_results[action]
            return result(**params) if callable(result) else result
        return {"message_id": 1}


class FakeEvent:
    def __init__(
        self,
        *,
        raw=None,
        group_id="100",
        sender_id="200",
        self_id="999",
        messages=None,
        message_str="",
        bot=None,
    ):
        self.message_obj = SimpleNamespace(raw_message=raw)
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._messages = messages or []
        self._message_str = message_str
        self.bot = bot or FakeBot()
        self.sent = []
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_messages(self):
        return self._messages

    def get_message_str(self):
        return self._message_str

    def chain_result(self, chain):
        return chain

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    return TransferStationPlugin(FakeContext(), {})


class FakeNewApi:
    def __init__(self):
        self.added = []

    async def status_snapshot(self):
        return newapi_module.QuotaSnapshot(
            "USD",
            Decimal(500000),
            Decimal("7.2"),
            Decimal(1),
        )

    async def get_user(self, user_id):
        return newapi_module.NewApiUser(int(user_id), f"user-{user_id}", 1)

    async def add_quota(self, user_id, raw_quota):
        self.added.append((int(user_id), int(raw_quota)))

    async def close(self):
        return None


def test_config_and_exact_claim_matching(plugin):
    valid = FakeEvent(messages=[Comp.At(qq="999"), Comp.Plain(" 领取新人礼 ")])
    extra_text = FakeEvent(messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼 谢谢")])
    other_at = FakeEvent(messages=[Comp.At(qq="888"), Comp.Plain("领取新人礼")])
    image = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼"), Comp.Image(file="x")]
    )

    assert plugin._is_claim_message(valid) is True
    assert plugin._is_claim_message(extra_text) is False
    assert plugin._is_claim_message(other_at) is False
    assert plugin._is_claim_message(image) is False
    assert plugin._welcome_content().endswith("“领取新人礼”即可领取新人礼。")
    assert plugin._group_enabled("100") is True
    assert plugin._reserved_lottery_keyword("领取新人礼") is True
    assert plugin._reserved_lottery_keyword("确认 补偿") is True
    assert plugin._reserved_lottery_keyword("领奖 7") is True
    assert plugin._reserved_lottery_keyword("确认领奖") is True
    assert plugin._reserved_lottery_keyword("领取补偿 8") is True
    assert plugin._reserved_lottery_keyword("确认补偿") is True
    assert plugin._reserved_lottery_keyword("参与抽奖") is False


def test_all_bot_messages_are_configurable(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {
            "claim_phrase": "拿礼物",
            "welcome_content": "欢迎，发送 {claim_phrase}",
            "gift_message_content": "专属内容：{code}；口令：{claim_phrase}",
            "claim_success_content": "发送成功",
            "already_claimed_content": "已经领过",
            "not_eligible_content": "没有资格",
            "no_codes_content": "暂时缺货",
            "temporary_chat_failed_content": "先私聊，再发送 {claim_phrase}",
            "claim_failed_content": "未知失败",
        },
    )

    assert plugin._welcome_content() == "欢迎，发送 拿礼物"
    assert (
        plugin._gift_message_content("CODE-001") == "专属内容：CODE-001；口令：拿礼物"
    )
    assert plugin._outcome_content("success") == "发送成功"
    assert plugin._outcome_content("already_claimed") == "已经领过"
    assert plugin._outcome_content("not_eligible") == "没有资格"
    assert plugin._outcome_content("no_codes") == "暂时缺货"
    assert plugin._outcome_content("send_failed") == "先私聊，再发送 拿礼物"
    assert plugin._outcome_content("unexpected") == "未知失败"


def test_newapi_error_placeholder_never_leaks_literal_marker(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"newapi_error_content": "New API 操作失败：{error}"},
    )

    content = plugin._campaign_content(campaign_utils.ActionResult("newapi_error"))

    assert content == "New API 操作失败：连接或配置不可用"
    assert "{error}" not in content


@pytest.mark.asyncio
async def test_newapi_test_command_reports_legacy_auth_requirement(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"newapi_error_content": "New API 操作失败：{error}"},
    )

    class LegacyFailure:
        async def test_connection(self):
            raise newapi_module.NewApiError(
                "New-Api-User header is required",
                status_code=200,
            )

    plugin._newapi_client = LegacyFailure()
    event = FakeEvent()

    await plugin.newapi_test(event)

    assert event.stopped is True
    assert len(event.sent) == 1
    assert "用户数字 ID" in event.sent[0]
    assert "{error}" not in event.sent[0]


def test_custom_gift_message_without_placeholder_still_includes_code(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"gift_message_content": "这是你的新人礼"},
    )

    assert plugin._gift_message_content("CODE-001") == "这是你的新人礼\nCODE-001"


@pytest.mark.asyncio
async def test_lottery_publish_rechecks_current_newcomer_phrase(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {
            "claim_phrase": "参与抽奖",
            "lottery_enabled": True,
        },
    )
    plugin._newapi_client = FakeNewApi()
    now = campaign_utils.utc_now()
    await plugin.lottery_storage.create_draft("100", "口令冲突", "1", now=now)
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")

    result = await plugin._lottery_service(require_newapi=True).publish("100")

    assert result.key == "lottery_keyword_reserved"
    assert (await plugin.lottery_storage.get_active("100"))["status"] == "draft"


@pytest.mark.asyncio
async def test_group_increase_records_once_and_sends_welcome(plugin):
    raw = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "group_id": 100,
        "user_id": 200,
        "self_id": 999,
    }
    bot = FakeBot(
        action_results={
            "get_group_member_list": [
                {"user_id": 300},
            ],
        },
    )
    assert await plugin._sync_group_baseline(bot, "100") is True
    event = FakeEvent(raw=raw, bot=bot)

    await plugin.handle_aiocqhttp_event(event)
    await plugin.handle_aiocqhttp_event(event)

    assert await plugin.storage.is_eligible("100", "200") is True
    assert await plugin.storage.is_eligible("100", "300") is False
    assert (await plugin.storage.summary())["known_users"] == 2
    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Comp.At)
    assert str(event.sent[0][0].qq) == "200"


@pytest.mark.asyncio
async def test_join_before_baseline_completion_is_not_eligible(plugin):
    raw = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "group_id": 100,
        "user_id": 200,
        "self_id": 999,
    }
    event = FakeEvent(
        raw=raw,
        bot=FakeBot(
            action_results={
                "get_group_member_list": [
                    {"user_id": 200},
                    {"user_id": 300},
                ],
            },
        ),
    )

    await plugin.handle_aiocqhttp_event(event)

    assert event.sent == []
    assert await plugin.storage.is_eligible("100", "200") is False
    assert await plugin.storage.register_newcomer("100", "200") == "known"


@pytest.mark.asyncio
async def test_first_join_after_restart_is_not_absorbed_into_existing_baseline(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    first = TransferStationPlugin(FakeContext(), {})
    await first.storage.record_group_baseline("100", ["111"])

    bot = FakeBot(
        action_results={
            "get_group_member_list": [
                {"user_id": 111},
                {"user_id": 222},
            ],
        }
    )
    reloaded = TransferStationPlugin(FakeContext(bot), {})
    event = FakeEvent(
        raw={
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 222,
            "self_id": 999,
        },
        bot=bot,
    )

    await reloaded.handle_aiocqhttp_event(event)

    assert await reloaded.storage.is_eligible("100", "222") is True
    assert len(event.sent) == 1


@pytest.mark.asyncio
async def test_restart_baseline_sync_cannot_absorb_concurrent_join(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    first = TransferStationPlugin(FakeContext(), {})
    await first.storage.record_group_baseline("100", ["111"])
    member_list_entered = asyncio.Event()
    release_member_list = asyncio.Event()

    class BlockingBot(FakeBot):
        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_group_member_list":
                member_list_entered.set()
                await release_member_list.wait()
                return [{"user_id": 111}, {"user_id": 222}]
            return {"message_id": 1}

    bot = BlockingBot()
    reloaded = TransferStationPlugin(FakeContext(bot), {})
    sync_task = asyncio.create_task(reloaded._sync_group_baseline(bot, "100"))
    await member_list_entered.wait()
    event = FakeEvent(
        raw={
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 222,
            "self_id": 999,
        },
        bot=bot,
    )
    event_task = asyncio.create_task(reloaded.handle_aiocqhttp_event(event))
    while "222" not in reloaded._pending_group_increases.get("100", set()):
        await asyncio.sleep(0)
    release_member_list.set()
    await asyncio.gather(sync_task, event_task)

    assert await reloaded.storage.is_eligible("100", "222") is True
    assert len(event.sent) == 1


@pytest.mark.asyncio
async def test_concurrent_events_during_first_baseline_stay_ineligible(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    member_list_entered = asyncio.Event()
    release_member_list = asyncio.Event()

    class BlockingBot(FakeBot):
        async def call_action(self, action, **params):
            self.calls.append((action, params))
            if action == "get_group_member_list":
                member_list_entered.set()
                await release_member_list.wait()
                return [{"user_id": 200}, {"user_id": 201}]
            return {"message_id": 1}

    bot = BlockingBot()
    plugin = TransferStationPlugin(FakeContext(bot), {})
    events = [
        FakeEvent(
            raw={
                "post_type": "notice",
                "notice_type": "group_increase",
                "group_id": 100,
                "user_id": user_id,
                "self_id": 999,
            },
            sender_id=str(user_id),
            bot=bot,
        )
        for user_id in (200, 201)
    ]
    tasks = [
        asyncio.create_task(plugin.handle_aiocqhttp_event(event)) for event in events
    ]
    await member_list_entered.wait()
    while len(plugin._pending_group_increases.get("100", set())) < 2:
        await asyncio.sleep(0)
    release_member_list.set()
    await asyncio.gather(*tasks)

    assert await plugin.storage.is_eligible("100", "200") is False
    assert await plugin.storage.is_eligible("100", "201") is False
    assert [event.sent for event in events] == [[], []]


@pytest.mark.asyncio
async def test_late_join_event_cannot_promote_permanently_known_baseline_user(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    first = TransferStationPlugin(FakeContext(), {})
    await first.storage.record_group_baseline("100", ["111"])
    bot = FakeBot(
        action_results={
            "get_group_member_list": [{"user_id": 111}, {"user_id": 222}],
        }
    )
    reloaded = TransferStationPlugin(FakeContext(bot), {})
    assert await reloaded._sync_group_baseline(bot, "100") is True
    assert await reloaded.storage.register_newcomer("100", "222") == "known"
    event = FakeEvent(
        raw={
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 222,
            "self_id": 999,
        },
        sender_id="222",
        bot=bot,
    )

    await reloaded.handle_aiocqhttp_event(event)

    assert await reloaded.storage.is_eligible("100", "222") is False
    assert await reloaded.storage.register_newcomer("100", "222") == "known"
    assert event.sent == []


@pytest.mark.asyncio
async def test_bot_join_and_unlisted_group_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"enabled_group_ids": ["100"]},
    )
    bot_join = FakeEvent(
        raw={
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 100,
            "user_id": 999,
            "self_id": 999,
        }
    )
    other_group = FakeEvent(
        raw={
            "post_type": "notice",
            "notice_type": "group_increase",
            "group_id": 101,
            "user_id": 200,
            "self_id": 999,
        }
    )

    await plugin.handle_aiocqhttp_event(bot_join)
    await plugin.handle_aiocqhttp_event(other_group)

    assert bot_join.sent == []
    assert other_group.sent == []
    assert (await plugin.storage.summary())["eligible_members"] == 0


@pytest.mark.asyncio
async def test_claim_sends_temporary_chat_and_consumes_code(plugin):
    await plugin.storage.record_group_baseline("100", [])
    assert await plugin.storage.register_newcomer("100", "200") == "eligible"
    plugin._ready_group_ids.add("100")
    await plugin.storage.import_codes(["WELCOME-001"])
    bot = FakeBot()
    event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼")],
        bot=bot,
    )

    await plugin.handle_aiocqhttp_event(event)

    assert event.stopped is True
    assert bot.calls == [
        (
            "send_private_msg",
            {
                "user_id": 200,
                "group_id": 100,
                "message": "欢迎领取新人礼！你的兑换码是：WELCOME-001",
            },
        )
    ]
    assert event.sent == ["新人礼已通过群临时会话发送，请查收。"]
    assert (await plugin.storage.summary())["available_codes"] == 0
    assert (await plugin.storage.summary())["claimed_users"] == 1


@pytest.mark.asyncio
async def test_claim_failure_guides_user_and_retry_can_succeed(plugin):
    class ActionFailed(RuntimeError):
        retcode = 1200

    await plugin.storage.record_group_baseline("100", [])
    assert await plugin.storage.register_newcomer("100", "200") == "eligible"
    plugin._ready_group_ids.add("100")
    await plugin.storage.import_codes(["WELCOME-001"])
    failed_event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼")],
        bot=FakeBot(ActionFailed("rejected")),
    )

    await plugin.handle_aiocqhttp_event(failed_event)

    assert "先主动私聊机器人发送任意消息" in failed_event.sent[0]
    assert "重新 @机器人并发送“领取新人礼”" in failed_event.sent[0]
    assert "已退回库存" in failed_event.sent[0]
    assert (await plugin.storage.summary())["available_codes"] == 1
    assert (await plugin.storage.summary())["claimed_users"] == 0

    retry_bot = FakeBot()
    retry_event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼")],
        bot=retry_bot,
    )
    await plugin.handle_aiocqhttp_event(retry_event)

    assert retry_bot.calls[0][1]["message"].endswith("WELCOME-001")
    assert retry_event.sent == ["新人礼已通过群临时会话发送，请查收。"]
    assert (await plugin.storage.summary())["available_codes"] == 0
    assert (await plugin.storage.summary())["claimed_users"] == 1


@pytest.mark.asyncio
async def test_claim_timeout_freezes_code_for_manual_review(plugin):
    await plugin.storage.record_group_baseline("100", [])
    assert await plugin.storage.register_newcomer("100", "200") == "eligible"
    plugin._ready_group_ids.add("100")
    await plugin.storage.import_codes(["WELCOME-REVIEW"])
    event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼")],
        bot=FakeBot(TimeoutError("timeout")),
    )

    await plugin.handle_aiocqhttp_event(event)

    assert "发送结果暂时无法确认" in event.sent[0]
    summary = await plugin.storage.summary()
    assert summary["available_codes"] == 0
    assert summary["gift_manual_reviews"] == 1
    assert summary["claimed_users"] == 0


@pytest.mark.asyncio
async def test_startup_baseline_marks_current_members_as_ineligible(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    bot = FakeBot(
        action_results={
            "get_group_member_list": [
                {"user_id": 200},
                {"user_id": 201},
            ],
        }
    )
    plugin = TransferStationPlugin(
        FakeContext(bot),
        {"enabled_group_ids": ["100"]},
    )

    await plugin.initialize()
    assert plugin._baseline_task is not None
    await plugin._baseline_task

    assert await plugin.storage.is_group_baselined("100") is True
    assert await plugin.storage.register_newcomer("100", "200") == "known"
    assert await plugin.storage.register_newcomer("100", "202") == "eligible"
    await plugin.terminate()


@pytest.mark.asyncio
async def test_empty_whitelist_baselines_every_joined_group(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)

    def member_list(group_id):
        return [{"user_id": group_id + 1000}]

    bot = FakeBot(
        action_results={
            "get_group_list": [{"group_id": 100}, {"group_id": 101}],
            "get_group_member_list": member_list,
        }
    )
    plugin = TransferStationPlugin(FakeContext(bot), {})

    await plugin.initialize()
    assert plugin._baseline_task is not None
    await plugin._baseline_task

    assert plugin._ready_group_ids == {"100", "101"}
    assert (await plugin.storage.summary())["known_users"] == 2
    await plugin.terminate()


@pytest.mark.asyncio
async def test_claim_is_blocked_until_group_baseline_succeeds(plugin):
    event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼")],
        bot=FakeBot(
            action_errors={"get_group_member_list": TimeoutError("offline")},
        ),
    )
    await plugin.storage.import_codes(["WELCOME-001"])

    await plugin.handle_aiocqhttp_event(event)

    assert event.stopped is True
    assert "成员基线" in event.sent[0]
    assert not any(action == "send_private_msg" for action, _ in event.bot.calls)
    assert (await plugin.storage.summary())["available_codes"] == 1


@pytest.mark.asyncio
async def test_lottery_user_registration_and_payout_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"lottery_enabled": True},
    )
    fake_newapi = FakeNewApi()
    plugin._newapi_client = fake_newapi
    await plugin.lottery_storage.initialize()
    service = plugin._lottery_service(require_newapi=True)
    now = campaign_utils.utc_now()
    activity = await plugin.lottery_storage.create_draft(
        "100", "测试抽奖", "1", now=now
    )
    await plugin.lottery_storage.add_prize("100", "一等奖", 1, "10")
    await plugin.lottery_storage.publish(
        "100",
        await fake_newapi.status_snapshot(),
        now=now,
    )

    join_event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("参与抽奖")],
    )
    await plugin.handle_aiocqhttp_event(join_event)
    assert join_event.stopped is True
    assert "报名成功" in join_event.sent[0]

    await service.draw(
        activity
        | {
            "display_type": "USD",
            "quota_per_unit": "500000",
            "usd_exchange_rate": "7.2",
            "custom_currency_exchange_rate": "1",
            "custom_currency_symbol": "",
        },
        ["200"],
        now=now,
    )
    target_event = FakeEvent(message_str="/领奖 7")
    await plugin.lottery_claim_command(target_event)
    assert "用户名：user-7" in target_event.sent[0]

    confirm_event = FakeEvent(message_str="/确认领奖")
    await plugin.lottery_confirm_claim_command(confirm_event)
    assert "发放成功" in confirm_event.sent[0]
    assert fake_newapi.added == [(7, 5000000)]


@pytest.mark.asyncio
async def test_compensation_flow_and_exact_budget_close(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"compensation_enabled": True},
    )
    fake_newapi = FakeNewApi()
    plugin._newapi_client = fake_newapi
    await plugin.compensation_storage.initialize()
    service = plugin._compensation_service(require_newapi=True)
    opened = await service.open(
        "100",
        "10",
        None,
        "10",
        "测试补偿",
        "1",
    )
    assert opened.key == "comp_opened"

    target_event = FakeEvent(message_str="/领取补偿 8")
    await plugin.compensation_claim_command(target_event)
    assert "用户名：user-8" in target_event.sent[0]

    confirm_event = FakeEvent(message_str="/确认补偿")
    await plugin.compensation_confirm_claim_command(confirm_event)
    assert "补偿发放成功" in confirm_event.sent[0]
    assert fake_newapi.added == [(8, 5000000)]
    assert await plugin.compensation_storage.get_active("100") is None


@pytest.mark.asyncio
async def test_campaign_systems_work_when_newcomer_gift_is_disabled(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {
            "enabled": False,
            "lottery_enabled": True,
        },
    )
    plugin._newapi_client = FakeNewApi()
    now = campaign_utils.utc_now()
    await plugin.lottery_storage.create_draft("100", "独立抽奖", "1", now=now)
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")
    await plugin.lottery_storage.publish(
        "100",
        await plugin._newapi_client.status_snapshot(),
        now=now,
    )
    event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("参与抽奖")],
    )

    await plugin.handle_aiocqhttp_event(event)

    assert "报名成功" in event.sent[0]


@pytest.mark.asyncio
async def test_scheduler_filters_lottery_participants_who_left_group(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    bot = FakeBot(action_results={"get_group_member_list": [{"user_id": 200}]})
    plugin = TransferStationPlugin(
        FakeContext(bot),
        {"lottery_enabled": True},
    )
    fake_newapi = FakeNewApi()
    plugin._newapi_client = fake_newapi
    await plugin.lottery_storage.initialize()
    start = campaign_utils.utc_now() - timedelta(hours=2)
    activity = await plugin.lottery_storage.create_draft(
        "100", "离群过滤", "1", now=start
    )
    await plugin.lottery_storage.add_prize("100", "奖品", 2, "1")
    await plugin.lottery_storage.publish(
        "100",
        await fake_newapi.status_snapshot(),
        now=start,
    )
    await plugin.lottery_storage.register(
        "100", "200", "参与抽奖", now=start + timedelta(minutes=1)
    )
    await plugin.lottery_storage.register(
        "100", "201", "参与抽奖", now=start + timedelta(minutes=1)
    )

    await plugin._campaign_scheduler_once()

    winners = await plugin.lottery_storage.winners(int(activity["id"]))
    assert [winner["user_id"] for winner in winners] == ["200"]
    assert any(action == "send_group_msg" for action, _ in bot.calls)


@pytest.mark.asyncio
async def test_scheduler_delays_draw_when_member_list_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    bot = FakeBot(action_results={"get_group_member_list": []})
    plugin = TransferStationPlugin(
        FakeContext(bot),
        {"lottery_enabled": True},
    )
    fake_newapi = FakeNewApi()
    plugin._newapi_client = fake_newapi
    start = campaign_utils.utc_now() - timedelta(hours=2)
    activity = await plugin.lottery_storage.create_draft(
        "100",
        "空成员列表延迟开奖",
        "1",
        now=start,
    )
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")
    await plugin.lottery_storage.publish(
        "100",
        await fake_newapi.status_snapshot(),
        now=start,
    )
    await plugin.lottery_storage.register(
        "100",
        "200",
        "参与抽奖",
        now=start + timedelta(minutes=1),
    )

    await plugin._campaign_scheduler_once()

    current = await plugin.lottery_storage.get_activity(int(activity["id"]))
    assert current["status"] == "open"
    assert await plugin.lottery_storage.winners(int(activity["id"])) == []


@pytest.mark.asyncio
async def test_page_draw_rejects_empty_member_list(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    bot = FakeBot(action_results={"get_group_member_list": []})
    plugin = TransferStationPlugin(
        FakeContext(bot),
        {"lottery_enabled": True},
    )
    now = campaign_utils.utc_now()
    activity = await plugin.lottery_storage.create_draft(
        "100",
        "Page 空成员列表",
        "1",
        now=now,
    )
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")
    await plugin.lottery_storage.publish(
        "100",
        await FakeNewApi().status_snapshot(),
        now=now,
    )
    await plugin.lottery_storage.register(
        "100",
        "200",
        "参与抽奖",
        now=now,
    )

    result = await plugin._draw_lottery_from_page(int(activity["id"]))

    assert result.key == "lottery_member_list_failed"
    assert (await plugin.lottery_storage.get_activity(int(activity["id"])))[
        "status"
    ] == "open"
    assert await plugin.lottery_storage.winners(int(activity["id"])) == []


@pytest.mark.asyncio
async def test_notification_timeout_releases_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(main, "CAMPAIGN_NOTIFICATION_TIMEOUT_SECONDS", 0.01)
    plugin = TransferStationPlugin(FakeContext(), {"lottery_enabled": True})
    now = campaign_utils.utc_now()
    activity = await plugin.lottery_storage.create_draft(
        "100",
        "超时通知",
        "1",
        now=now,
    )
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")
    await plugin.lottery_storage.publish(
        "100",
        await FakeNewApi().status_snapshot(),
        now=now,
    )
    release = asyncio.Event()

    async def sender(_result):
        await release.wait()

    try:
        with pytest.raises(TimeoutError):
            await plugin._send_persisted_notification(
                campaign_utils.ActionResult(
                    "lottery_published",
                    {"activity_id": str(activity["id"])},
                ),
                sender,
            )
    finally:
        release.set()

    reclaimed = await plugin.lottery_storage.claim_notification(
        int(activity["id"]),
        "lottery_published",
    )
    assert reclaimed is not None


@pytest.mark.asyncio
async def test_campaign_write_gate_limits_concurrency(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(main, "CAMPAIGN_WRITE_QUEUE_TIMEOUT_SECONDS", 0.01)
    plugin = TransferStationPlugin(
        FakeContext(),
        {
            "enabled": False,
            "lottery_enabled": True,
        },
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    active = 0
    maximum = 0

    class BlockingService:
        async def confirm(self, _group_id, _user_id):
            nonlocal calls, active, maximum
            calls += 1
            active += 1
            maximum = max(maximum, active)
            if active == main.CAMPAIGN_WRITE_CONCURRENCY:
                entered.set()
            try:
                await release.wait()
                return campaign_utils.ActionResult("lottery_paid")
            finally:
                active -= 1

    service = BlockingService()
    monkeypatch.setattr(plugin, "_lottery_service", lambda **_kwargs: service)
    events = [
        FakeEvent(
            sender_id=str(200 + index),
            messages=[Comp.At(qq="999"), Comp.Plain("确认 抽奖")],
        )
        for index in range(8)
    ]
    rejected = 0
    all_rejected = asyncio.Event()
    for event in events:
        original_send = event.send

        async def tracked_send(result, *, send=original_send):
            nonlocal rejected
            await send(result)
            if "当前发放请求较多" in str(result):
                rejected += 1
                if rejected == len(events) - main.CAMPAIGN_WRITE_CONCURRENCY:
                    all_rejected.set()

        event.send = tracked_send
    tasks = [
        asyncio.create_task(plugin.handle_aiocqhttp_event(event)) for event in events
    ]
    await entered.wait()
    await all_rejected.wait()
    release.set()
    await asyncio.gather(*tasks)

    assert calls == main.CAMPAIGN_WRITE_CONCURRENCY
    assert maximum == main.CAMPAIGN_WRITE_CONCURRENCY
    assert (
        sum("当前发放请求较多" in event.sent[0] for event in events)
        == len(events) - main.CAMPAIGN_WRITE_CONCURRENCY
    )


@pytest.mark.asyncio
async def test_processing_recovery_runs_periodically(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(main, "CAMPAIGN_SCHEDULER_SECONDS", 0.01)
    plugin = TransferStationPlugin(FakeContext(), {})
    calls = 0
    repeated = asyncio.Event()

    async def recover():
        nonlocal calls
        calls += 1
        if calls >= 2:
            repeated.set()
        return 0, 0

    monkeypatch.setattr(plugin, "_recover_stale_processing", recover)
    task = asyncio.create_task(plugin._processing_recovery_loop())
    try:
        await repeated.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls >= 2


@pytest.mark.asyncio
async def test_processing_recovery_continues_after_one_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(main, "CAMPAIGN_SCHEDULER_SECONDS", 0)
    plugin = TransferStationPlugin(FakeContext(), {})
    calls = 0
    second_call = asyncio.Event()

    async def recover():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first recovery failed")
        second_call.set()
        return 0, 0

    monkeypatch.setattr(plugin, "_recover_stale_processing", recover)
    task = asyncio.create_task(plugin._processing_recovery_loop())
    try:
        await second_call.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls >= 2


@pytest.mark.asyncio
async def test_gift_recovery_continues_after_one_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    monkeypatch.setattr(main, "CAMPAIGN_SCHEDULER_SECONDS", 0)
    plugin = TransferStationPlugin(FakeContext(), {})
    calls = 0
    second_call = asyncio.Event()

    async def recover_reserved(*, stale_before):
        nonlocal calls
        assert stale_before
        calls += 1
        if calls == 1:
            raise RuntimeError("first gift recovery failed")
        second_call.set()
        return 0

    monkeypatch.setattr(plugin.storage, "recover_reserved", recover_reserved)
    task = asyncio.create_task(plugin._gift_recovery_loop())
    try:
        await second_call.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls >= 2


@pytest.mark.asyncio
async def test_processing_recovery_uses_fixed_maximum_request_window(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    fixed_now = campaign_utils.utc_now()
    monkeypatch.setattr(main, "utc_now", lambda: fixed_now)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"newapi_timeout_seconds": 1},
    )
    cutoffs = []

    async def lottery_recover(*, now, stale_before):
        assert now == fixed_now
        cutoffs.append(stale_before)
        return 0

    async def compensation_recover(*, now, stale_before):
        assert now == fixed_now
        cutoffs.append(stale_before)
        return 0

    monkeypatch.setattr(plugin.lottery_storage, "recover_processing", lottery_recover)
    monkeypatch.setattr(
        plugin.compensation_storage,
        "recover_processing",
        compensation_recover,
    )

    await plugin._recover_stale_processing()

    expected = fixed_now - timedelta(
        seconds=main.MAX_NEWAPI_TIMEOUT_SECONDS + main.CAMPAIGN_SCHEDULER_SECONDS * 2
    )
    assert cutoffs == [expected, expected]


@pytest.mark.asyncio
async def test_notification_finalize_survives_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(FakeContext(), {"lottery_enabled": True})
    now = campaign_utils.utc_now()
    activity = await plugin.lottery_storage.create_draft(
        "100",
        "取消保护",
        "1",
        now=now,
    )
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")
    await plugin.lottery_storage.publish(
        "100",
        await FakeNewApi().status_snapshot(),
        now=now,
    )
    finalize_entered = asyncio.Event()
    release_finalize = asyncio.Event()
    original_mark = plugin.lottery_storage.mark_notification_sent

    async def blocked_mark(notification_id, lease_marker):
        finalize_entered.set()
        await release_finalize.wait()
        return await original_mark(notification_id, lease_marker)

    monkeypatch.setattr(
        plugin.lottery_storage,
        "mark_notification_sent",
        blocked_mark,
    )

    async def sender(_result):
        return None

    task = asyncio.create_task(
        plugin._send_persisted_notification(
            campaign_utils.ActionResult(
                "lottery_published",
                {"activity_id": str(activity["id"])},
            ),
            sender,
        )
    )
    await finalize_entered.wait()
    task.cancel()
    release_finalize.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await plugin.lottery_storage.list_pending_notifications() == []


@pytest.mark.asyncio
async def test_campaign_group_notifications_use_plain_text_segments(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    bot = FakeBot()
    plugin = TransferStationPlugin(
        FakeContext(bot),
        {"lottery_enabled": True},
    )
    now = campaign_utils.utc_now()
    await plugin.lottery_storage.create_draft(
        "100",
        "[CQ:at,qq=all]",
        "1",
        now=now,
    )
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")
    await plugin.lottery_storage.publish(
        "100",
        await FakeNewApi().status_snapshot(),
        now=now,
    )

    await plugin._flush_campaign_notifications()

    message = next(
        params["message"] for action, params in bot.calls if action == "send_group_msg"
    )
    assert message[0]["type"] == "text"
    assert "[CQ:at,qq=all]" in message[0]["data"]["text"]


@pytest.mark.asyncio
async def test_disabled_lottery_does_not_flush_old_group_notifications(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    bot = FakeBot()
    plugin = TransferStationPlugin(
        FakeContext(bot),
        {
            "lottery_enabled": False,
            "compensation_enabled": True,
        },
    )
    now = campaign_utils.utc_now()
    await plugin.lottery_storage.create_draft("100", "旧通知", "1", now=now)
    await plugin.lottery_storage.add_prize("100", "奖品", 1, "1")
    await plugin.lottery_storage.publish(
        "100",
        await FakeNewApi().status_snapshot(),
        now=now,
    )

    await plugin._campaign_scheduler_once()

    assert not any(action == "send_group_msg" for action, _ in bot.calls)
    assert (await plugin.lottery_storage.list_pending_notifications())[0][
        "event_key"
    ] == "lottery_published"


@pytest.mark.asyncio
async def test_terminate_cannot_leave_or_restart_campaign_scheduler(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"lottery_enabled": True},
    )
    await plugin.initialize()
    assert plugin._campaign_task is not None

    callback = asyncio.create_task(plugin._reconcile_campaign_scheduler())
    await plugin.terminate()
    await callback

    assert plugin._campaign_task is None
    assert plugin._plugin_initialized is False
    assert plugin._terminating is True


@pytest.mark.asyncio
async def test_terminate_waits_for_initialize_and_leaves_no_background_tasks(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _name: tmp_path)
    plugin = TransferStationPlugin(
        FakeContext(),
        {"lottery_enabled": True},
    )
    initialize_entered = asyncio.Event()
    release_initialize = asyncio.Event()
    original_initialize = plugin.storage.initialize

    async def blocked_initialize():
        initialize_entered.set()
        await release_initialize.wait()
        await original_initialize()

    monkeypatch.setattr(plugin.storage, "initialize", blocked_initialize)
    initialize_task = asyncio.create_task(plugin.initialize())
    await initialize_entered.wait()
    terminate_task = asyncio.create_task(plugin.terminate())
    await asyncio.sleep(0)
    assert terminate_task.done() is False

    release_initialize.set()
    await asyncio.gather(initialize_task, terminate_task)

    assert plugin._plugin_initialized is False
    assert plugin._terminating is True
    assert plugin._baseline_task is None
    assert plugin._campaign_task is None
    assert plugin._processing_recovery_task is None
    assert plugin._gift_recovery_task is None
