from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import load_plugin_module

main = load_plugin_module("main")
Comp = main.Comp
TransferStationPlugin = main.TransferStationPlugin


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler, methods, description))


class FakeBot:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error:
            raise self.error
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


@pytest.mark.asyncio
async def test_group_increase_records_once_and_sends_welcome(plugin):
    raw = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "group_id": 100,
        "user_id": 200,
        "self_id": 999,
    }
    event = FakeEvent(raw=raw)

    await plugin.handle_aiocqhttp_event(event)
    await plugin.handle_aiocqhttp_event(event)

    assert await plugin.storage.is_eligible("100", "200") is True
    assert len(event.sent) == 1
    assert isinstance(event.sent[0][0], Comp.At)
    assert str(event.sent[0][0].qq) == "200"


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
    await plugin.storage.add_eligible("100", "200")
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
async def test_claim_failure_returns_code_to_inventory(plugin):
    await plugin.storage.add_eligible("100", "200")
    await plugin.storage.import_codes(["WELCOME-001"])
    event = FakeEvent(
        messages=[Comp.At(qq="999"), Comp.Plain("领取新人礼")],
        bot=FakeBot(TimeoutError("timeout")),
    )

    await plugin.handle_aiocqhttp_event(event)

    assert "已退回库存" in event.sent[0]
    assert (await plugin.storage.summary())["available_codes"] == 1
    assert (await plugin.storage.summary())["claimed_users"] == 0
