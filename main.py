from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from .page_api import GiftPageApi
from .storage import ClaimOutcome, GiftStorage

PLUGIN_NAME = "astrbot_plugin_transfer_station"
PLUGIN_VERSION = "1.2.0"
REPOSITORY = "https://github.com/diaomin66/astrbot_plugin_transfer_station"
BASELINE_ACTION_TIMEOUT_SECONDS = 20
BASELINE_RETRY_SECONDS = 60


class TransferStationPlugin(Star):
    """Newcomer welcome and one-time gift-code plugin for QQ OneBot."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.storage = GiftStorage(StarTools.get_data_dir(PLUGIN_NAME) / "gifts.db")
        self.page_api = GiftPageApi(context, self.storage)
        self.page_api.register_routes()
        self._baseline_lock = asyncio.Lock()
        self._baseline_task: asyncio.Task[None] | None = None
        self._ready_group_ids: set[str] = set()

    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _group_enabled(self, group_id: str) -> bool:
        enabled_groups = self._enabled_group_ids()
        return not enabled_groups or str(group_id) in enabled_groups

    def _enabled_group_ids(self) -> set[str]:
        configured = self.config.get("enabled_group_ids", [])
        if not isinstance(configured, list):
            return set()
        return {str(item).strip() for item in configured if str(item).strip()}

    def _claim_phrase(self) -> str:
        phrase = str(self.config.get("claim_phrase", "领取新人礼")).strip()
        return phrase or "领取新人礼"

    def _configured_content(
        self,
        key: str,
        default: str,
        **placeholders: str,
    ) -> str:
        content = str(self.config.get(key, default)).strip() or default
        for name, value in placeholders.items():
            content = content.replace(f"{{{name}}}", value)
        return content

    def _welcome_content(self) -> str:
        default = "欢迎加入本群！@机器人并发送“{claim_phrase}”即可领取新人礼。"
        return self._configured_content(
            "welcome_content",
            default,
            claim_phrase=self._claim_phrase(),
        )

    def _gift_message_content(self, code: str) -> str:
        default = "欢迎领取新人礼！你的兑换码是：{code}"
        template = (
            str(self.config.get("gift_message_content", default)).strip() or default
        )
        if "{code}" not in template:
            template = f"{template}\n{{code}}"
        return template.replace("{code}", code).replace(
            "{claim_phrase}",
            self._claim_phrase(),
        )

    def _outcome_content(self, status: str) -> str:
        replies = {
            "success": (
                "claim_success_content",
                "新人礼已通过群临时会话发送，请查收。",
            ),
            "already_claimed": (
                "already_claimed_content",
                "你已经领取过新人礼，每人只能领取一次。",
            ),
            "not_eligible": (
                "not_eligible_content",
                "只有永久用户库中从未出现过的新入群成员才能领取新人礼。",
            ),
            "no_codes": (
                "no_codes_content",
                "新人礼兑换码库存不足，请联系管理员。",
            ),
            "send_failed": (
                "temporary_chat_failed_content",
                (
                    "机器人暂时无法主动发起群临时会话。请先主动私聊机器人发送任意消息"
                    "建立会话，然后回到本群重新 @机器人并发送“{claim_phrase}”。"
                    "本次兑换码已退回库存。"
                ),
            ),
            "baseline_pending": (
                "baseline_pending_content",
                "本群成员基线尚未同步完成，暂时无法领取新人礼，请稍后重试。",
            ),
        }
        key, default = replies.get(
            status,
            ("claim_failed_content", "新人礼领取失败，请稍后重试。"),
        )
        return self._configured_content(
            key,
            default,
            claim_phrase=self._claim_phrase(),
        )

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

    @staticmethod
    def _extract_action_list(result: Any) -> list[Mapping[str, Any]]:
        if isinstance(result, list):
            return [item for item in result if isinstance(item, Mapping)]
        if isinstance(result, Mapping):
            data = result.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, Mapping)]
        raise ValueError("OneBot action did not return a list")

    def _iter_onebot_clients(self) -> list[Any]:
        manager = getattr(self.context, "platform_manager", None)
        clients: list[Any] = []
        seen: set[int] = set()
        for platform in getattr(manager, "platform_insts", ()):
            try:
                if getattr(platform.meta(), "name", "") != "aiocqhttp":
                    continue
                getter = getattr(platform, "get_client", None)
                client = (
                    getter() if callable(getter) else getattr(platform, "bot", None)
                )
            except Exception as exc:  # noqa: BLE001 - third-party adapter boundary
                logger.debug(
                    "Skipping unavailable aiocqhttp platform error_type=%s",
                    type(exc).__name__,
                )
                continue
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            clients.append(client)
        return clients

    async def _call_action_list(
        self,
        client: Any,
        action: str,
        **params: Any,
    ) -> list[Mapping[str, Any]]:
        result = await asyncio.wait_for(
            client.call_action(action, **params),
            timeout=BASELINE_ACTION_TIMEOUT_SECONDS,
        )
        return self._extract_action_list(result)

    async def _sync_group_baseline(self, client: Any, group_id: str) -> bool:
        group_id = str(group_id).strip()
        if not group_id or not self._group_enabled(group_id):
            return False
        if group_id in self._ready_group_ids:
            return True

        async with self._baseline_lock:
            if group_id in self._ready_group_ids:
                return True
            try:
                members = await self._call_action_list(
                    client,
                    "get_group_member_list",
                    group_id=int(group_id),
                )
            except Exception as exc:  # noqa: BLE001 - OneBot boundary
                logger.warning(
                    "Group baseline sync failed group=%s error_type=%s",
                    group_id,
                    type(exc).__name__,
                )
                return False

            user_ids = {
                str(member.get("user_id", "")).strip()
                for member in members
                if str(member.get("user_id", "")).strip()
            }
            if not user_ids:
                logger.warning(
                    "Group baseline sync returned no users group=%s", group_id
                )
                return False

            result = await self.storage.record_group_baseline(
                group_id,
                list(user_ids),
            )
            self._ready_group_ids.add(group_id)
            logger.info(
                "Group baseline stored group=%s members=%s inserted_users=%s",
                group_id,
                result["members"],
                result["inserted_users"],
            )
            return True

    async def _sync_configured_groups(self) -> bool:
        clients = self._iter_onebot_clients()
        if not clients:
            logger.warning("Group baseline sync waiting for aiocqhttp client")
            return False

        configured_groups = self._enabled_group_ids()
        if configured_groups:
            complete = True
            for group_id in sorted(configured_groups):
                synced = False
                for client in clients:
                    if await self._sync_group_baseline(client, group_id):
                        synced = True
                        break
                complete = complete and synced
            return complete

        listed_all_clients = True
        for client in clients:
            try:
                groups = await self._call_action_list(client, "get_group_list")
            except Exception as exc:  # noqa: BLE001 - OneBot boundary
                listed_all_clients = False
                logger.warning(
                    "Group list sync failed error_type=%s",
                    type(exc).__name__,
                )
                continue
            for group in groups:
                group_id = str(group.get("group_id", "")).strip()
                if group_id and not await self._sync_group_baseline(client, group_id):
                    listed_all_clients = False
        return listed_all_clients

    async def _baseline_sync_loop(self) -> None:
        while self._enabled():
            try:
                if await self._sync_configured_groups():
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background task boundary
                logger.exception(
                    "Group baseline background sync failed error_type=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(BASELINE_RETRY_SECONDS)

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

        if not await self._sync_group_baseline(event.bot, group_id):
            return
        registration = await self.storage.register_newcomer(group_id, user_id)

        if registration != "eligible":
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
        if not await self._sync_group_baseline(event.bot, group_id):
            await self._reply_for_outcome(
                event,
                ClaimOutcome("baseline_pending"),
                user_id,
                group_id,
            )
            return

        async def send_code(code: str) -> None:
            await event.bot.call_action(
                "send_private_msg",
                user_id=int(user_id),
                group_id=int(group_id),
                message=self._gift_message_content(code),
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
        if outcome.status == "send_failed":
            logger.warning(
                "Temporary chat delivery failed user=%s group=%s error_type=%s",
                user_id,
                group_id,
                outcome.error_type,
            )
        await self._send_group_text(
            event,
            self._outcome_content(outcome.status),
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

    async def initialize(self) -> None:
        """Initialize storage and start the one-time group baseline sync."""
        await self.storage.initialize()
        if self._enabled() and (
            self._baseline_task is None or self._baseline_task.done()
        ):
            self._baseline_task = asyncio.create_task(
                self._baseline_sync_loop(),
                name=f"{PLUGIN_NAME}:baseline-sync",
            )

    async def terminate(self) -> None:
        """Release plugin resources."""
        if self._baseline_task is not None and not self._baseline_task.done():
            self._baseline_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._baseline_task
        self._baseline_task = None
