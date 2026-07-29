from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_metadata_and_config_contract():
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert metadata["name"] == "astrbot_plugin_transfer_station"
    assert metadata["version"] == "v1.1.0"
    assert metadata["support_platforms"] == ["aiocqhttp"]
    assert metadata["astrbot_version"] == ">=4.16,<5"
    assert metadata["pages"][0]["name"] == "gift_codes"
    assert set(schema) == {
        "enabled",
        "enabled_group_ids",
        "welcome_content",
        "gift_message_content",
        "claim_success_content",
        "already_claimed_content",
        "not_eligible_content",
        "no_codes_content",
        "temporary_chat_failed_content",
        "claim_failed_content",
        "claim_phrase",
        "mention_new_member",
    }


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
    assert ".textContent" in script
    assert ".innerHTML" not in script
    assert ".code-cell {\n  display: flex" not in style
    assert "table-layout: fixed" in style
