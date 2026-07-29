from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from .page_api import GiftPageApi
from .storage import ClaimOutcome, GiftStorage

PLUGIN_NAME = "astrbot_plugin_transfer_station"
PLUGIN_VERSION = "1.0.1"
REPOSITORY = "https://github.com/diaomin66/astrbot_plugin_transfer_station"


class TransferStationPlugin(Star):
    """Newcomer welcome and one-time gift-code plugin for QQ OneBot."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.storage = GiftStorage(StarTools.get_data_dir(PLUGIN_NAME) / "gifts.db")
        self.page_api = GiftPageApi(context, self.storage)
        self.page_api.register_routes()

    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _group_enabled(self, group_id: str) -> bool:
        configured = self.config.get("enabled_group_ids", [])
        if not isinstance(configured, list):
            return True
        enabled_groups = {str(item).strip() for item in configured if str(item).strip()}
        return not enabled_groups or str(group_id) in enabled_groups

    def _claim_phrase(self) -> str:
        phrase = str(self.config.get("claim_phrase", "领取新人礼")).strip()
        return phrase or "领取新人礼"

    def _welcome_content(self) -> str:
        default = "欢迎加入本群！@机器人并发送“{claim_phrase}”即可领取新人礼。"
        content = str(self.config.get("welcome_content", default)).strip() or default
        return content.replace("{claim_phrase}", self._claim_phrase())

    @staticmethod
    def _raw_event(event: AstrMessageEvent) -> Mapping[str, Any] | None:
        raw = getattr(event.message_obj, "raw_message", None)
        return raw if isinstance(raw, Mapping) else None

    def _is_claim_message(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id()).strip()
        if not self_id:
            return False

        mentioned_self = False
        text_parts: list[str] = []
        for component in event.get_messages():
            if isinstance(component, Comp.Plain):
                text_parts.append(component.text)
                continue
            if isinstance(component, Comp.At):
                if str(component.qq) != self_id:
                    return False
                mentioned_self = True
                continue
            return False

        return mentioned_self and "".join(text_parts).strip() == self._claim_phrase()

    async def _send_group_text(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.plain_result(text))

    async def _handle_group_increase(
        self,
        event: AstrMessageEvent,
        raw: Mapping[str, Any],
    ) -> None:
        group_id = str(raw.get("group_id", "")).strip()
        user_id = str(raw.get("user_id", "")).strip()
        self_id = str(raw.get("self_id", "") or event.get_self_id()).strip()
        if (
            not group_id
            or not user_id
            or user_id == self_id
            or not self._group_enabled(group_id)
        ):
            return

        added = await self.storage.add_eligible(group_id, user_id)
        if not added:
            return

        chain = []
        if bool(self.config.get("mention_new_member", True)):
            chain.extend((Comp.At(qq=user_id), Comp.Plain(" ")))
        chain.append(Comp.Plain(self._welcome_content()))
        await event.send(event.chain_result(chain))

    async def _handle_claim(self, event: AstrMessageEvent) -> None:
        group_id = str(event.get_group_id()).strip()
        user_id = str(event.get_sender_id()).strip()
        if not group_id or not user_id or not self._group_enabled(group_id):
            return
        if not self._is_claim_message(event):
            return

        event.stop_event()

        async def send_code(code: str) -> None:
            message = f"欢迎领取新人礼！你的兑换码是：{code}"
            await event.bot.call_action(
                "send_private_msg",
                user_id=int(user_id),
                group_id=int(group_id),
                message=message,
            )

        outcome = await self.storage.claim_code(
            group_id=group_id,
            user_id=user_id,
            send_code=send_code,
        )
        await self._reply_for_outcome(event, outcome, user_id, group_id)

    async def _reply_for_outcome(
        self,
        event: AstrMessageEvent,
        outcome: ClaimOutcome,
        user_id: str,
        group_id: str,
    ) -> None:
        replies = {
            "success": "新人礼已通过群临时会话发送，请查收。",
            "already_claimed": "你已经领取过新人礼，每人只能领取一次。",
            "not_eligible": "只有插件记录到的新入群成员才能领取新人礼。",
            "no_codes": "新人礼兑换码库存不足，请联系管理员。",
            "send_failed": (
                "群临时会话发送失败，请检查 QQ 隐私或临时会话设置后重试。"
                "本次兑换码已退回库存。"
            ),
        }
        if outcome.status == "send_failed":
            logger.warning(
                "Temporary chat delivery failed user=%s group=%s error_type=%s",
                user_id,
                group_id,
                outcome.error_type,
            )
        await self._send_group_text(
            event,
            replies.get(outcome.status, "新人礼领取失败，请稍后重试。"),
        )

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_aiocqhttp_event(self, event: AstrMessageEvent) -> None:
        if not self._enabled():
            return

        raw = self._raw_event(event)
        if raw and raw.get("post_type") == "notice":
            if raw.get("notice_type") == "group_increase":
                await self._handle_group_increase(event, raw)
            return

        if event.get_group_id():
            await self._handle_claim(event)

    async def terminate(self) -> None:
        """Release plugin resources."""
