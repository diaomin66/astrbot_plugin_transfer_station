from __future__ import annotations

import json
from pathlib import Path

import yaml
from astrbot.core.star.star_handler import star_handlers_registry
from conftest import load_plugin_module

ROOT = Path(__file__).resolve().parents[1]
campaign_messages = load_plugin_module("campaign_messages")
load_plugin_module("main")


def test_metadata_and_config_contract():
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert metadata["name"] == "astrbot_plugin_transfer_station"
    assert metadata["version"] == "v1.4.3"
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["astrbot_version"] == ">=4.26,<5"
    assert [page["name"] for page in metadata["pages"]] == [
        "gift_codes",
        "campaigns",
    ]
    assert {
        "enabled",
        "enabled_group_ids",
        "welcome_content",
        "gift_message_content",
        "claim_success_content",
        "already_claimed_content",
        "not_eligible_content",
        "no_codes_content",
        "temporary_chat_failed_content",
        "baseline_pending_content",
        "claim_failed_content",
        "claim_phrase",
        "mention_new_member",
    }.issubset(schema)
    assert {
        "newapi_base_url",
        "newapi_access_token",
        "newapi_user_id",
        "newapi_username",
        "newapi_password",
        "newapi_timeout_seconds",
        "newapi_verify_ssl",
        "newapi_allow_insecure_http",
        "lottery_enabled",
        "lottery_enabled_group_ids",
        "compensation_enabled",
        "compensation_enabled_group_ids",
    }.issubset(schema)
    assert {
        f"{key}_content" for key in campaign_messages.CAMPAIGN_TEXT_DEFAULTS
    }.issubset(schema)
    for key, default in campaign_messages.CAMPAIGN_TEXT_DEFAULTS.items():
        assert schema[f"{key}_content"]["default"] == default
    assert not any(
        "?" in value
        for item in schema.values()
        for value in (
            item.get("description"),
            item.get("hint"),
            item.get("default"),
        )
        if isinstance(value, str)
    )


def test_page_uses_bridge_and_safe_dynamic_text_rendering():
    html = (ROOT / "pages" / "gift_codes" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "pages" / "gift_codes" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "pages" / "gift_codes" / "style.css").read_text(encoding="utf-8")

    assert "/api/plugin/page/bridge-sdk.js" in html
    assert 'class="code-table"' in html
    assert "window.AstrBotPluginPage" in script
    assert "bridge.apiGet" in script
    assert "bridge.apiPost" in script
    assert "确认删除" in script
    assert "window.confirm" not in script
    assert 'document.getElementById("knownUsers")' in script
    assert 'document.getElementById("todayNewcomers")' in script
    assert "window.setInterval" in script
    assert ".textContent" in script
    assert ".innerHTML" not in script
    assert ".code-cell {\n  display: flex" not in style
    assert "table-layout: fixed" in style
    assert 'id="knownUsers"' in html
    assert 'id="todayNewcomers"' in html


def test_campaign_page_uses_bridge_and_preserves_large_ids():
    html = (ROOT / "pages" / "campaigns" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "pages" / "campaigns" / "app.js").read_text(encoding="utf-8")

    assert "/api/plugin/page/bridge-sdk.js" in html
    assert "window.AstrBotPluginPage" in script
    assert "bridge.apiGet" in script
    assert "bridge.apiPost" in script
    assert ".textContent" in script
    assert ".innerHTML" not in script
    assert "Number(activity.id)" not in script
    assert "Number(item.id)" not in script
    assert "Number(prize.id)" not in script
    assert "settingsDirty" in script
    assert "formBaseRevision" in script
    assert "detailGeneration" in script
    assert "settingsReloadButton" in script
    assert 'id="settingsUserId"' in html
    assert "newapi_user_id" in script
    assert 'id="reasonModal"' in html
    assert "window.prompt" not in script
    assert "window.confirm" not in script
    assert "window.alert" not in script
    for endpoint in (
        "campaigns/summary",
        "campaigns/settings",
        "campaigns/settings/save",
        "campaigns/newapi/test",
        "campaigns/lotteries",
        "campaigns/lotteries/detail",
        "campaigns/lotteries/create",
        "campaigns/lotteries/update",
        "campaigns/lotteries/prizes/add",
        "campaigns/lotteries/prizes/delete",
        "campaigns/lotteries/publish",
        "campaigns/lotteries/draw",
        "campaigns/lotteries/cancel",
        "campaigns/lotteries/review",
        "campaigns/compensations",
        "campaigns/compensations/detail",
        "campaigns/compensations/open",
        "campaigns/compensations/close",
        "campaigns/compensations/review",
    ):
        assert endpoint in script


def test_campaign_command_groups_require_admin_permission():
    handlers = [
        handler
        for handler in star_handlers_registry
        if handler.handler_module_path == f"{ROOT.name}.main"
        and handler.handler_name
        in {"newapi_commands", "lottery_commands", "compensation_commands"}
    ]
    assert len(handlers) == 3
    assert {handler.handler_name for handler in handlers} == {
        "newapi_commands",
        "lottery_commands",
        "compensation_commands",
    }
    for handler in handlers:
        filter_names = {type(item).__name__ for item in handler.event_filters}
        assert "PermissionTypeFilter" in filter_names
        assert "CommandGroupFilter" in filter_names


def test_lottery_claim_commands_do_not_require_admin_permission():
    handlers = [
        handler
        for handler in star_handlers_registry
        if handler.handler_module_path == f"{ROOT.name}.main"
        and handler.handler_name
        in {"lottery_claim_command", "lottery_confirm_claim_command"}
    ]
    assert len(handlers) == 2
    for handler in handlers:
        filter_names = {type(item).__name__ for item in handler.event_filters}
        assert "CommandFilter" in filter_names
        assert "PermissionTypeFilter" not in filter_names


def test_compensation_claim_commands_do_not_require_admin_permission():
    handlers = [
        handler
        for handler in star_handlers_registry
        if handler.handler_module_path == f"{ROOT.name}.main"
        and handler.handler_name
        in {"compensation_claim_command", "compensation_confirm_claim_command"}
    ]
    assert len(handlers) == 2
    for handler in handlers:
        filter_names = {type(item).__name__ for item in handler.event_filters}
        assert "CommandFilter" in filter_names
        assert "PermissionTypeFilter" not in filter_names
