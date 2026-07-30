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
    assert metadata["version"] == "v1.3.0"
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["astrbot_version"] == ">=4.26,<5"
    assert [page["name"] for page in metadata["pages"]] == ["gift_codes"]
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
