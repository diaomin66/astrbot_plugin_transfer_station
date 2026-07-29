from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import load_plugin_module

main = load_plugin_module("main")
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
        bot=None,
    ):
        self.message_obj = SimpleNamespace(raw_message=raw)
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._messages = messages or []
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
    await plugin.storage.record_group_baseline("100", [])
    assert await plugin.storage.register_newcomer("100", "200") == "eligible"
    plugin._ready_group_ids.add("100")
    await plugin.storage.import_codes(["WELCOME-001"])
    failed_event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼")],
        bot=FakeBot(TimeoutError("timeout")),
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
