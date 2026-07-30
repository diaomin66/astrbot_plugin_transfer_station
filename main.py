from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import timedelta
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from .campaign_messages import CAMPAIGN_TEXT_DEFAULTS
from .campaign_page_api import CampaignPageApi
from .campaign_utils import (
    ActionResult,
    is_reserved_lottery_keyword,
    parse_duration,
    parse_positive_decimal,
    parse_time_spec,
    to_iso,
    utc_now,
)
from .compensation import CompensationService, CompensationStorage
from .lottery import LotteryService, LotteryStorage
from .newapi_client import NewApiClient, NewApiError
from .page_api import GiftPageApi
from .storage import ClaimOutcome, GiftStorage

PLUGIN_NAME = "astrbot_plugin_transfer_station"
PLUGIN_VERSION = "1.4.0"
REPOSITORY = "https://github.com/diaomin66/astrbot_plugin_transfer_station"
BASELINE_ACTION_TIMEOUT_SECONDS = 20
BASELINE_RETRY_SECONDS = 60
CAMPAIGN_SCHEDULER_SECONDS = 5
USER_LOOKUP_COOLDOWN_SECONDS = 3
USER_LOOKUP_CONCURRENCY = 4
USER_LOOKUP_QUEUE_TIMEOUT_SECONDS = 0.1
CAMPAIGN_WRITE_CONCURRENCY = 4
CAMPAIGN_WRITE_QUEUE_TIMEOUT_SECONDS = 0.1
CAMPAIGN_NOTIFICATION_TIMEOUT_SECONDS = 20
GIFT_SEND_TIMEOUT_SECONDS = 20
MAX_NEWAPI_TIMEOUT_SECONDS = 120
LOTTERY_NOTIFICATION_KEYS = {
    "lottery_published",
    "lottery_opened",
    "lottery_drawn",
    "lottery_no_winner",
    "lottery_claim_closed",
    "lottery_cancelled",
}
COMPENSATION_NOTIFICATION_KEYS = {
    "comp_opened",
    "comp_closed",
    "comp_auto_closed",
    "comp_budget_closed",
}


class TransferStationPlugin(Star):
    """Newcomer welcome and one-time gift-code plugin for QQ OneBot."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.storage = GiftStorage(StarTools.get_data_dir(PLUGIN_NAME) / "gifts.db")
        self.lottery_storage = LotteryStorage(
            StarTools.get_data_dir(PLUGIN_NAME) / "lottery.db"
        )
        self.compensation_storage = CompensationStorage(
            StarTools.get_data_dir(PLUGIN_NAME) / "compensation.db"
        )
        self._baseline_lock = asyncio.Lock()
        self._baseline_task: asyncio.Task[None] | None = None
        self._campaign_task: asyncio.Task[None] | None = None
        self._processing_recovery_task: asyncio.Task[None] | None = None
        self._gift_recovery_task: asyncio.Task[None] | None = None
        self._campaign_lifecycle_lock = asyncio.Lock()
        self._campaign_announcement_lock = asyncio.Lock()
        self._newapi_client: NewApiClient | None = None
        self._newapi_close_tasks: set[asyncio.Task[None]] = set()
        self._ready_group_ids: set[str] = set()
        self._pending_group_increases: dict[str, set[str]] = {}
        self._baseline_probe_futures: dict[str, asyncio.Future[bool]] = {}
        self._group_increase_locks: dict[str, asyncio.Lock] = {}
        self._user_lookup_lock = asyncio.Lock()
        self._user_lookup_last: dict[tuple[str, str, str], float] = {}
        self._user_lookup_semaphore = asyncio.Semaphore(USER_LOOKUP_CONCURRENCY)
        self._campaign_write_semaphore = asyncio.Semaphore(CAMPAIGN_WRITE_CONCURRENCY)
        self._plugin_initialized = False
        self._terminating = False
        self.page_api = GiftPageApi(context, self.storage)
        self.page_api.register_routes()
        self.campaign_page_api = CampaignPageApi(
            context,
            self.config,
            self.lottery_storage,
            self.compensation_storage,
            lottery_service=lambda require_newapi: self._lottery_service(
                require_newapi=require_newapi
            ),
            compensation_service=lambda require_newapi: self._compensation_service(
                require_newapi=require_newapi
            ),
            newapi_client=self._newapi,
            render_action=self._campaign_content,
            settings_changed=self._reconcile_campaign_scheduler,
            draw_lottery=self._draw_lottery_from_page,
            flush_notifications=self._flush_campaign_notifications,
        )
        self.campaign_page_api.register_routes()

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

    def _feature_enabled(self, feature: str) -> bool:
        return bool(self.config.get(f"{feature}_enabled", False))

    def _feature_group_enabled(self, feature: str, group_id: str) -> bool:
        configured = self.config.get(f"{feature}_enabled_group_ids", [])
        enabled_groups = (
            {str(item).strip() for item in configured if str(item).strip()}
            if isinstance(configured, list)
            else set()
        )
        return self._feature_enabled(feature) and (
            not enabled_groups or str(group_id) in enabled_groups
        )

    def _campaign_content(self, result: ActionResult) -> str:
        default = CAMPAIGN_TEXT_DEFAULTS.get(
            result.key,
            CAMPAIGN_TEXT_DEFAULTS["campaign_invalid_argument"],
        )
        content = (
            str(self.config.get(f"{result.key}_content", default)).strip() or default
        )
        for name, value in result.placeholders.items():
            content = content.replace(f"{{{name}}}", str(value))
        return content

    def _newapi(self) -> NewApiClient:
        if self._terminating:
            raise NewApiError("插件正在停止，暂不接受新的 New API 请求", kind="config")
        fingerprint = NewApiClient.config_fingerprint(self.config)
        if (
            self._newapi_client is not None
            and hasattr(self._newapi_client, "configuration_fingerprint")
            and self._newapi_client.configuration_fingerprint != fingerprint
        ):
            old_client = self._newapi_client
            self._newapi_client = None
            task = asyncio.create_task(
                self._close_newapi_client(
                    old_client,
                    delay_seconds=float(
                        getattr(
                            old_client,
                            "request_timeout_seconds",
                            self._newapi_timeout_seconds(),
                        )
                    )
                    + CAMPAIGN_SCHEDULER_SECONDS,
                ),
                name=f"{PLUGIN_NAME}:close-retired-newapi",
            )
            self._newapi_close_tasks.add(task)
            task.add_done_callback(self._newapi_close_tasks.discard)
        if self._newapi_client is None:
            self._newapi_client = NewApiClient(self.config)
        return self._newapi_client

    @staticmethod
    async def _close_newapi_client(
        client: NewApiClient,
        *,
        delay_seconds: float = 0,
    ) -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
        finally:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - HTTP client lifecycle boundary
                logger.warning(
                    "Closing retired New API client failed error_type=%s",
                    type(exc).__name__,
                )

    def _newapi_timeout_seconds(self) -> float:
        try:
            value = float(self.config.get("newapi_timeout_seconds", 10))
        except (TypeError, ValueError):
            value = 10
        return min(120.0, max(1.0, value))

    def _lottery_service(self, *, require_newapi: bool = False) -> LotteryService:
        client: NewApiClient | None = None
        if require_newapi:
            client = self._newapi()
        return LotteryService(
            self.lottery_storage,
            client,
            reserved_keyword=self._reserved_lottery_keyword,
        )

    def _compensation_service(
        self,
        *,
        require_newapi: bool = False,
    ) -> CompensationService:
        client: NewApiClient | None = None
        if require_newapi:
            client = self._newapi()
        return CompensationService(self.compensation_storage, client)

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
            "send_ambiguous": (
                "gift_manual_review_content",
                (
                    "新人礼发送结果暂时无法确认，兑换码已冻结且不会重复发放。"
                    "请联系管理员在新人礼管理页面核查。"
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
        return self._mentioned_plain_text(event) == self._claim_phrase()

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text).strip()).lstrip("/")

    def _command_tail(
        self,
        event: AstrMessageEvent,
        group: str,
        subcommand: str,
    ) -> str:
        text = self._normalize_command_text(event.get_message_str())
        prefix = f"{group} {subcommand}"
        if text == prefix:
            return ""
        if text.startswith(f"{prefix} "):
            return text[len(prefix) + 1 :].strip()
        return ""

    def _mentioned_plain_text(self, event: AstrMessageEvent) -> str | None:
        self_id = str(event.get_self_id()).strip()
        if not self_id:
            return None

        mentioned_self = False
        text_parts: list[str] = []
        for component in event.get_messages():
            if isinstance(component, Comp.Plain):
                text_parts.append(component.text)
                continue
            if isinstance(component, Comp.At):
                if str(component.qq) != self_id:
                    return None
                mentioned_self = True
                continue
            return None

        return "".join(text_parts).strip() if mentioned_self else None

    def _reserved_lottery_keyword(self, keyword: str) -> bool:
        return is_reserved_lottery_keyword(keyword, self._claim_phrase())

    async def _send_action(
        self,
        event: AstrMessageEvent,
        result: ActionResult,
    ) -> None:
        if result.stop:
            event.stop_event()
        if result.should_announce and self._notification_storage(result.key):
            await self._send_persisted_notification(
                result,
                lambda persisted: self._send_group_text(
                    event,
                    self._campaign_content(persisted),
                ),
            )
            return
        await self._send_group_text(event, self._campaign_content(result))

    def _notification_storage(
        self,
        event_key: str,
    ) -> LotteryStorage | CompensationStorage | None:
        if event_key in LOTTERY_NOTIFICATION_KEYS:
            return self.lottery_storage
        if event_key in COMPENSATION_NOTIFICATION_KEYS:
            return self.compensation_storage
        return None

    async def _send_persisted_notification(
        self,
        result: ActionResult,
        sender: Callable[[ActionResult], Awaitable[None]],
    ) -> bool:
        storage = self._notification_storage(result.key)
        activity_id = result.placeholders.get("activity_id")
        if storage is None or not str(activity_id or "").isdigit():
            await sender(result)
            return True
        async with self._campaign_announcement_lock:
            notification = await storage.claim_notification(
                int(str(activity_id)),
                result.key,
            )
            if not notification:
                return False
            persisted = ActionResult(
                str(notification["event_key"]),
                {
                    str(key): str(value)
                    for key, value in notification["placeholders"].items()
                },
            )
            try:
                await asyncio.wait_for(
                    sender(persisted),
                    timeout=CAMPAIGN_NOTIFICATION_TIMEOUT_SECONDS,
                )
            except BaseException:
                await asyncio.shield(
                    storage.release_notification(
                        int(notification["id"]),
                        str(notification["lease_marker"]),
                    )
                )
                raise
            finalize_task = asyncio.create_task(
                storage.mark_notification_sent(
                    int(notification["id"]),
                    str(notification["lease_marker"]),
                ),
                name=f"{PLUGIN_NAME}:finalize-notification",
            )
            try:
                marked = await asyncio.shield(finalize_task)
            except asyncio.CancelledError:
                with suppress(asyncio.CancelledError):
                    await finalize_task
                raise
            if not marked:
                logger.warning(
                    "Campaign notification sent but lease finalization failed "
                    "event=%s activity=%s",
                    result.key,
                    activity_id,
                )
            return True

    async def _campaign_guard(
        self,
        event: AstrMessageEvent,
        feature: str,
    ) -> str | None:
        group_id = str(event.get_group_id()).strip()
        if not group_id:
            await self._send_action(
                event,
                ActionResult("campaign_group_required"),
            )
            return None
        if not self._feature_group_enabled(feature, group_id):
            await self._send_action(
                event,
                ActionResult("campaign_feature_disabled"),
            )
            return None
        return group_id

    async def _send_group_text(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(event.plain_result(text))

    async def _allow_user_lookup(
        self,
        feature: str,
        group_id: str,
        user_id: str,
    ) -> bool:
        key = (feature, str(group_id), str(user_id))
        now = time.monotonic()
        async with self._user_lookup_lock:
            previous = self._user_lookup_last.get(key)
            if previous is not None and now - previous < USER_LOOKUP_COOLDOWN_SECONDS:
                return False
            self._user_lookup_last[key] = now
            if len(self._user_lookup_last) > 5000:
                cutoff = now - 3600
                self._user_lookup_last = {
                    item_key: timestamp
                    for item_key, timestamp in self._user_lookup_last.items()
                    if timestamp >= cutoff
                }
            return True

    async def _acquire_user_lookup_slot(self) -> bool:
        try:
            await asyncio.wait_for(
                self._user_lookup_semaphore.acquire(),
                timeout=USER_LOOKUP_QUEUE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return False
        return True

    async def _acquire_campaign_write_slot(self) -> bool:
        try:
            await asyncio.wait_for(
                self._campaign_write_semaphore.acquire(),
                timeout=CAMPAIGN_WRITE_QUEUE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return False
        return True

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

    async def _sync_group_baseline(
        self,
        client: Any,
        group_id: str,
        *,
        exclude_user_id: str = "",
    ) -> bool:
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

            listed_user_ids = {
                str(member.get("user_id", "")).strip()
                for member in members
                if str(member.get("user_id", "")).strip()
            }
            if not listed_user_ids:
                logger.warning(
                    "Group baseline sync returned no users group=%s", group_id
                )
                return False
            user_ids = set(listed_user_ids)
            user_ids.difference_update(self._pending_group_increases.get(group_id, ()))
            if exclude_user_id:
                user_ids.discard(str(exclude_user_id))

            result = await self.storage.record_group_baseline(group_id, list(user_ids))
            self._ready_group_ids.add(group_id)
            self._baseline_probe_futures.pop(group_id, None)
            logger.info(
                "Group baseline stored group=%s members=%s inserted_users=%s",
                group_id,
                result["members"],
                result["inserted_users"],
            )
            return True

    async def _baseline_ready_at_arrival(self, group_id: str) -> bool:
        if group_id in self._ready_group_ids:
            return True
        future = self._baseline_probe_futures.get(group_id)
        owner = future is None
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._baseline_probe_futures[group_id] = future
        if owner:
            try:
                ready = await self.storage.is_group_baselined(group_id)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                self._baseline_probe_futures.pop(group_id, None)
                raise
            except Exception as exc:  # noqa: BLE001 - storage boundary
                if not future.done():
                    future.set_exception(exc)
                self._baseline_probe_futures.pop(group_id, None)
                return await asyncio.shield(future)
            if not future.done():
                future.set_result(ready)
        return await asyncio.shield(future)

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

        pending = self._pending_group_increases.setdefault(group_id, set())
        pending.add(user_id)
        try:
            baseline_was_ready = await self._baseline_ready_at_arrival(group_id)
            group_lock = self._group_increase_locks.setdefault(
                group_id,
                asyncio.Lock(),
            )
            async with group_lock:
                if not await self._sync_group_baseline(
                    event.bot,
                    group_id,
                    exclude_user_id=user_id,
                ):
                    return
                if not baseline_was_ready:
                    await self.storage.record_group_baseline(group_id, [user_id])
                    logger.info(
                        "Newcomer event stored in initial baseline group=%s user=%s",
                        group_id,
                        user_id,
                    )
                    return
                registration = await self.storage.register_newcomer(group_id, user_id)
        finally:
            pending.discard(user_id)
            if not pending:
                self._pending_group_increases.pop(group_id, None)

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
            await asyncio.wait_for(
                event.bot.call_action(
                    "send_private_msg",
                    user_id=int(user_id),
                    group_id=int(group_id),
                    message=self._gift_message_content(code),
                ),
                timeout=GIFT_SEND_TIMEOUT_SECONDS,
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
        if outcome.status in {"send_failed", "send_ambiguous"}:
            logger.warning(
                "Temporary chat delivery unresolved status=%s user=%s group=%s "
                "error_type=%s",
                outcome.status,
                user_id,
                group_id,
                outcome.error_type,
            )
        await self._send_group_text(
            event,
            self._outcome_content(outcome.status),
        )

    @filter.command_group("newapi")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def newapi_commands(self):
        """New API 管理指令。"""

    @newapi_commands.command("测试")
    async def newapi_test(self, event: AstrMessageEvent) -> None:
        event.stop_event()
        try:
            result = await self._newapi().test_connection()
            action = ActionResult(
                "newapi_test_success",
                {
                    "version": result.version,
                    "username": result.username,
                    "role": result.role,
                    "display_type": result.display_type,
                },
            )
        except NewApiError:
            action = ActionResult("newapi_error")
        await event.send(event.plain_result(self._campaign_content(action)))

    @filter.command_group("抽奖")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def lottery_commands(self):
        """抽奖管理指令。"""

    @lottery_commands.command("帮助")
    async def lottery_help(self, event: AstrMessageEvent) -> None:
        if await self._campaign_guard(event, "lottery"):
            await self._send_action(event, ActionResult("lottery_help"))

    @lottery_commands.command("创建")
    async def lottery_create(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        title = self._command_tail(event, "抽奖", "创建")
        result = await self._lottery_service().create(
            group_id,
            title,
            str(event.get_sender_id()),
        )
        await self._send_action(event, result)

    @lottery_commands.command("时间")
    async def lottery_time(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        parts = self._command_tail(event, "抽奖", "时间").split()
        if len(parts) != 2:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        try:
            now = utc_now()
            start_at = parse_time_spec(parts[0], now=now)
            draw_at = parse_time_spec(parts[1], now=now)
            if draw_at <= start_at:
                raise ValueError("draw before start")
        except ValueError:
            await self._send_action(event, ActionResult("lottery_invalid_time"))
            return
        result = await self._lottery_service().update_draft(
            group_id,
            start_at=to_iso(start_at),
            draw_at=to_iso(draw_at),
        )
        await self._send_action(event, result)

    @lottery_commands.command("口令")
    async def lottery_keyword(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        keyword = self._command_tail(event, "抽奖", "口令").strip()
        if not keyword:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        if self._reserved_lottery_keyword(keyword):
            await self._send_action(event, ActionResult("lottery_keyword_reserved"))
            return
        await self._send_action(
            event,
            await self._lottery_service().update_draft(
                group_id,
                keyword=keyword,
            ),
        )

    @lottery_commands.command("描述")
    async def lottery_description(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        description = self._command_tail(event, "抽奖", "描述").strip()
        if not description:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        await self._send_action(
            event,
            await self._lottery_service().update_draft(
                group_id,
                description=description,
            ),
        )

    @lottery_commands.command("奖项添加")
    async def lottery_prize_add(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        parts = self._command_tail(event, "抽奖", "奖项添加").split()
        if len(parts) < 3:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        try:
            winner_count = int(parts[-2])
            amount = parse_positive_decimal(parts[-1])
        except (ValueError, TypeError):
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        result = await self._lottery_service().add_prize(
            group_id,
            " ".join(parts[:-2]),
            winner_count,
            amount,
        )
        await self._send_action(event, result)

    @lottery_commands.command("奖项删除")
    async def lottery_prize_delete(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        try:
            position = int(self._command_tail(event, "抽奖", "奖项删除"))
        except ValueError:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        await self._send_action(
            event,
            await self._lottery_service().delete_prize(group_id, position),
        )

    @lottery_commands.command("领奖时限")
    async def lottery_claim_duration(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        try:
            duration = parse_duration(self._command_tail(event, "抽奖", "领奖时限"))
        except ValueError:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        await self._send_action(
            event,
            await self._lottery_service().update_draft(
                group_id,
                claim_duration_seconds=int(duration.total_seconds()),
            ),
        )

    @lottery_commands.command("发布")
    async def lottery_publish(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        activity = await self.lottery_storage.get_active(group_id)
        if (
            activity
            and activity["status"] == "draft"
            and self._reserved_lottery_keyword(str(activity["keyword"]))
        ):
            await self._send_action(
                event,
                ActionResult("lottery_keyword_reserved"),
            )
            return
        try:
            service = self._lottery_service(require_newapi=True)
        except NewApiError:
            await self._send_action(
                event,
                ActionResult("newapi_error"),
            )
            return
        if not activity or activity["status"] != "draft":
            await self._send_action(event, ActionResult("lottery_no_draft"))
            return
        await self._send_action(
            event,
            await service.publish(
                group_id,
                expected_activity_id=int(activity["id"]),
                expected_revision=int(activity["revision"]),
            ),
        )

    @lottery_commands.command("状态")
    async def lottery_status(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if group_id:
            await self._send_action(
                event,
                await self._lottery_service().status(group_id),
            )

    @lottery_commands.command("参与者")
    async def lottery_participants(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        raw_page = self._command_tail(event, "抽奖", "参与者")
        try:
            page = int(raw_page) if raw_page else 1
            if page <= 0:
                raise ValueError
        except ValueError:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        await self._send_action(
            event,
            await self._lottery_service().participants(group_id, page),
        )

    @lottery_commands.command("提前开奖")
    async def lottery_draw_early(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        activity = await self.lottery_storage.get_active(group_id)
        if not activity or activity["status"] not in {"scheduled", "open"}:
            await self._send_action(event, ActionResult("lottery_not_open"))
            return
        try:
            members = await self._call_action_list(
                event.bot,
                "get_group_member_list",
                group_id=int(group_id),
            )
        except Exception as exc:  # noqa: BLE001 - OneBot boundary
            logger.warning(
                "Lottery member list failed group=%s error_type=%s",
                group_id,
                type(exc).__name__,
            )
            await self._send_action(
                event,
                ActionResult("lottery_member_list_failed"),
            )
            return
        member_ids = [
            str(member.get("user_id", "")).strip()
            for member in members
            if str(member.get("user_id", "")).strip()
        ]
        if not member_ids:
            await self._send_action(
                event,
                ActionResult("lottery_member_list_failed"),
            )
            return
        result = await self._lottery_service().draw(activity, member_ids)
        await self._send_action(event, result)

    @lottery_commands.command("取消")
    async def lottery_cancel(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if group_id:
            await self._send_action(
                event,
                await self._lottery_service().cancel_activity(
                    group_id,
                    self._command_tail(event, "抽奖", "取消"),
                ),
            )

    @lottery_commands.command("核查")
    async def lottery_review(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "lottery")
        if not group_id:
            return
        parts = self._command_tail(event, "抽奖", "核查").split()
        if len(parts) != 2 or parts[1] not in {"成功", "失败"}:
            await self._send_action(event, ActionResult("lottery_invalid_argument"))
            return
        await self._send_action(
            event,
            await self._lottery_service().review(
                group_id,
                parts[0],
                parts[1] == "成功",
            ),
        )

    @filter.command_group("补偿")
    @filter.permission_type(filter.PermissionType.ADMIN)
    def compensation_commands(self):
        """补偿管理指令。"""

    @compensation_commands.command("帮助")
    async def compensation_help(self, event: AstrMessageEvent) -> None:
        if await self._campaign_guard(event, "compensation"):
            await self._send_action(event, ActionResult("comp_help"))

    @compensation_commands.command("开启")
    async def compensation_open(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "compensation")
        if not group_id:
            return
        parts = self._command_tail(event, "补偿", "开启").split()
        if len(parts) < 3:
            await self._send_action(event, ActionResult("comp_invalid_argument"))
            return
        try:
            per_amount = str(parse_positive_decimal(parts[0]))
            duration = None if parts[1] == "-" else parse_duration(parts[1])
            total_amount = (
                None if parts[2] == "-" else str(parse_positive_decimal(parts[2]))
            )
            service = self._compensation_service(require_newapi=True)
        except ValueError:
            await self._send_action(event, ActionResult("comp_invalid_argument"))
            return
        except NewApiError:
            await self._send_action(
                event,
                ActionResult("newapi_error"),
            )
            return
        result = await service.open(
            group_id,
            per_amount,
            duration,
            total_amount,
            " ".join(parts[3:]),
            str(event.get_sender_id()),
        )
        await self._send_action(event, result)

    @compensation_commands.command("状态")
    async def compensation_status(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "compensation")
        if group_id:
            await self._send_action(
                event,
                await self._compensation_service().status(group_id),
            )

    @compensation_commands.command("记录")
    async def compensation_records(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "compensation")
        if not group_id:
            return
        raw_page = self._command_tail(event, "补偿", "记录")
        try:
            page = int(raw_page) if raw_page else 1
            if page <= 0:
                raise ValueError
            service = self._compensation_service()
        except ValueError:
            await self._send_action(event, ActionResult("comp_invalid_argument"))
            return
        await self._send_action(event, await service.records(group_id, page))

    @compensation_commands.command("关闭")
    async def compensation_close(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "compensation")
        if not group_id:
            return
        await self._send_action(
            event,
            await self._compensation_service().close(
                group_id,
                self._command_tail(event, "补偿", "关闭"),
            ),
        )

    @compensation_commands.command("核查")
    async def compensation_review(self, event: AstrMessageEvent) -> None:
        group_id = await self._campaign_guard(event, "compensation")
        if not group_id:
            return
        parts = self._command_tail(event, "补偿", "核查").split()
        if len(parts) != 2 or parts[1] not in {"成功", "失败"}:
            await self._send_action(event, ActionResult("comp_invalid_argument"))
            return
        await self._send_action(
            event,
            await self._compensation_service().review(
                group_id,
                parts[0],
                parts[1] == "成功",
            ),
        )

    async def _handle_campaign_message(self, event: AstrMessageEvent) -> bool:
        group_id = str(event.get_group_id()).strip()
        user_id = str(event.get_sender_id()).strip()
        text = self._mentioned_plain_text(event)
        if not group_id or not user_id or text is None:
            return False

        if self._feature_group_enabled("lottery", group_id):
            service = self._lottery_service()
            target_match = re.fullmatch(r"抽奖\s+(\d+)", text)
            if target_match:
                if not await self._allow_user_lookup(
                    "lottery",
                    group_id,
                    user_id,
                ):
                    await self._send_action(
                        event,
                        ActionResult("campaign_rate_limited"),
                    )
                    return True
                try:
                    if not await self._acquire_user_lookup_slot():
                        result = ActionResult("campaign_rate_limited")
                    else:
                        try:
                            service = self._lottery_service(require_newapi=True)
                            result = await service.submit_target(
                                group_id,
                                user_id,
                                target_match.group(1),
                            )
                        finally:
                            self._user_lookup_semaphore.release()
                except NewApiError:
                    result = ActionResult("newapi_error")
                await self._send_action(event, result)
                return True
            if text == "确认 抽奖":
                if not await self._acquire_campaign_write_slot():
                    result = ActionResult("campaign_write_busy")
                else:
                    try:
                        service = self._lottery_service(require_newapi=True)
                        result = await service.confirm(group_id, user_id)
                    except NewApiError:
                        result = ActionResult("newapi_error")
                    finally:
                        self._campaign_write_semaphore.release()
                await self._send_action(event, result)
                return True
            if text == "取消 抽奖":
                await self._send_action(
                    event,
                    await service.cancel_confirmation(group_id, user_id),
                )
                return True
            registration = await service.register(group_id, user_id, text)
            if registration is not None:
                await self._send_action(event, registration)
                return True

        if self._feature_group_enabled("compensation", group_id):
            service = self._compensation_service()
            target_match = re.fullmatch(r"补偿\s+(\d+)", text)
            if target_match:
                if not await self._allow_user_lookup(
                    "compensation",
                    group_id,
                    user_id,
                ):
                    await self._send_action(
                        event,
                        ActionResult("campaign_rate_limited"),
                    )
                    return True
                try:
                    if not await self._acquire_user_lookup_slot():
                        result = ActionResult("campaign_rate_limited")
                    else:
                        try:
                            service = self._compensation_service(require_newapi=True)
                            result = await service.submit(
                                group_id,
                                user_id,
                                target_match.group(1),
                            )
                        finally:
                            self._user_lookup_semaphore.release()
                except NewApiError:
                    result = ActionResult("newapi_error")
                await self._send_action(event, result)
                return True
            if text == "确认 补偿":
                if not await self._acquire_campaign_write_slot():
                    result = ActionResult("campaign_write_busy")
                else:
                    try:
                        service = self._compensation_service(require_newapi=True)
                        result = await service.confirm(group_id, user_id)
                    except NewApiError:
                        result = ActionResult("newapi_error")
                    finally:
                        self._campaign_write_semaphore.release()
                await self._send_action(event, result)
                return True
            if text == "取消 补偿":
                await self._send_action(
                    event,
                    await service.cancel(group_id, user_id),
                )
                return True
        return False

    async def _send_group_action(
        self,
        client: Any,
        group_id: str,
        result: ActionResult,
    ) -> bool:
        async def sender(persisted: ActionResult) -> None:
            await client.call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=[
                    {
                        "type": "text",
                        "data": {"text": self._campaign_content(persisted)},
                    }
                ],
            )

        try:
            if result.should_announce and self._notification_storage(result.key):
                return await self._send_persisted_notification(result, sender)
            await sender(result)
            return True
        except Exception as exc:  # noqa: BLE001 - OneBot boundary
            logger.warning(
                "Campaign group notification failed group=%s error_type=%s",
                group_id,
                type(exc).__name__,
            )
            return False

    async def _announce_campaign(
        self,
        group_id: str,
        result: ActionResult,
    ) -> bool:
        for client in self._iter_onebot_clients():
            if await self._send_group_action(client, group_id, result):
                return True
        return False

    async def _flush_campaign_notifications(self) -> None:
        pending = [
            *await self.lottery_storage.list_pending_notifications(),
            *await self.compensation_storage.list_pending_notifications(),
        ]
        for notification in pending:
            if self._terminating:
                return
            event_key = str(notification["event_key"])
            feature = (
                "lottery"
                if event_key in LOTTERY_NOTIFICATION_KEYS
                else "compensation"
                if event_key in COMPENSATION_NOTIFICATION_KEYS
                else ""
            )
            group_id = str(notification["group_id"])
            if not feature or not self._feature_group_enabled(feature, group_id):
                continue
            result = ActionResult(
                event_key,
                {
                    str(key): str(value)
                    for key, value in notification["placeholders"].items()
                },
            )
            for client in self._iter_onebot_clients():
                if self._terminating:
                    return
                if await self._send_group_action(client, group_id, result):
                    break

    async def _reconcile_campaign_scheduler(self) -> None:
        async with self._campaign_lifecycle_lock:
            await self._reconcile_campaign_scheduler_locked()

    async def _reconcile_campaign_scheduler_locked(self) -> None:
        if self._terminating or not self._plugin_initialized:
            return
        should_run = self._feature_enabled("lottery") or self._feature_enabled(
            "compensation"
        )
        current_task = self._campaign_task
        if should_run:
            if current_task is None or current_task.done():
                self._campaign_task = asyncio.create_task(
                    self._campaign_scheduler_loop(),
                    name=f"{PLUGIN_NAME}:campaign-scheduler",
                )
            return
        if current_task is not None and not current_task.done():
            current_task.cancel()
            with suppress(asyncio.CancelledError):
                await current_task
        if self._campaign_task is current_task:
            self._campaign_task = None

    async def _recover_stale_processing(self) -> tuple[int, int]:
        now = utc_now()
        stale_before = now - timedelta(
            seconds=MAX_NEWAPI_TIMEOUT_SECONDS + CAMPAIGN_SCHEDULER_SECONDS * 2
        )
        recovered = await asyncio.gather(
            self.lottery_storage.recover_processing(
                now=now,
                stale_before=stale_before,
            ),
            self.compensation_storage.recover_processing(
                now=now,
                stale_before=stale_before,
            ),
        )
        return int(recovered[0]), int(recovered[1])

    async def _processing_recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(CAMPAIGN_SCHEDULER_SECONDS)
            try:
                (
                    recovered_lottery,
                    recovered_compensation,
                ) = await self._recover_stale_processing()
                if recovered_lottery or recovered_compensation:
                    logger.warning(
                        "Recovered stale payouts lottery=%s compensation=%s",
                        recovered_lottery,
                        recovered_compensation,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background task boundary
                logger.exception(
                    "Processing recovery failed error_type=%s",
                    type(exc).__name__,
                )

    async def _gift_recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(CAMPAIGN_SCHEDULER_SECONDS)
            try:
                stale_before = utc_now() - timedelta(
                    seconds=GIFT_SEND_TIMEOUT_SECONDS + CAMPAIGN_SCHEDULER_SECONDS
                )
                recovered = await self.storage.recover_reserved(
                    stale_before=to_iso(stale_before),
                )
                if recovered:
                    logger.warning(
                        "Recovered stale gift reservations count=%s",
                        recovered,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background task boundary
                logger.exception(
                    "Gift recovery failed error_type=%s",
                    type(exc).__name__,
                )

    async def _draw_due_lottery(
        self,
        activity: dict[str, Any],
    ) -> None:
        group_id = str(activity["group_id"])
        if not self._feature_group_enabled("lottery", group_id):
            return
        for client in self._iter_onebot_clients():
            try:
                members = await self._call_action_list(
                    client,
                    "get_group_member_list",
                    group_id=int(group_id),
                )
            except Exception as exc:  # noqa: BLE001 - OneBot boundary
                logger.warning(
                    "Scheduled lottery member list failed group=%s error_type=%s",
                    group_id,
                    type(exc).__name__,
                )
                continue
            member_ids = [
                str(member.get("user_id", "")).strip()
                for member in members
                if str(member.get("user_id", "")).strip()
            ]
            if not member_ids:
                continue
            current = await self.lottery_storage.get_activity(int(activity["id"]))
            if not current or current["status"] != "open":
                return
            result = await self._lottery_service().draw(current, member_ids)
            if result.should_announce:
                await self._send_group_action(client, group_id, result)
            return

    async def _draw_lottery_from_page(self, activity_id: int) -> ActionResult:
        activity = await self.lottery_storage.get_activity(activity_id)
        if not activity or activity["status"] not in {"scheduled", "open"}:
            return ActionResult("lottery_not_open")
        group_id = str(activity["group_id"])
        for client in self._iter_onebot_clients():
            try:
                members = await self._call_action_list(
                    client,
                    "get_group_member_list",
                    group_id=int(group_id),
                )
            except Exception as exc:  # noqa: BLE001 - OneBot boundary
                logger.warning(
                    "Page lottery member list failed group=%s error_type=%s",
                    group_id,
                    type(exc).__name__,
                )
                continue
            member_ids = [
                str(member.get("user_id", "")).strip()
                for member in members
                if str(member.get("user_id", "")).strip()
            ]
            if not member_ids:
                return ActionResult("lottery_member_list_failed")
            try:
                result = await self._lottery_service().draw(
                    activity,
                    member_ids,
                )
            except ValueError:
                return ActionResult("lottery_not_open")
            if result.should_announce:
                await self._send_group_action(client, group_id, result)
            return result
        return ActionResult("lottery_member_list_failed")

    async def _campaign_scheduler_once(self) -> None:
        now = utc_now()
        if self._feature_enabled("lottery"):
            opened = await self.lottery_storage.mark_due_open(now=now)
            for activity in opened:
                group_id = str(activity["group_id"])
                current = await self.lottery_storage.get_activity(int(activity["id"]))
                if (
                    current
                    and current["status"] == "open"
                    and self._feature_group_enabled("lottery", group_id)
                ):
                    await self._announce_campaign(
                        group_id,
                        ActionResult(
                            "lottery_opened",
                            {
                                "activity_id": str(activity["id"]),
                                "title": activity["title"],
                                "keyword": activity["keyword"],
                            },
                        ),
                    )
            for activity in await self.lottery_storage.due_draws(now=now):
                await self._draw_due_lottery(activity)
            closed = await self.lottery_storage.expire(now=now)
            for activity in closed:
                group_id = str(activity["group_id"])
                if self._feature_group_enabled("lottery", group_id):
                    await self._announce_campaign(
                        group_id,
                        ActionResult(
                            "lottery_claim_closed",
                            {
                                "activity_id": str(activity["id"]),
                                "title": activity["title"],
                            },
                        ),
                    )
        if self._feature_enabled("compensation"):
            closed = await self.compensation_storage.tick(now=now)
            for activity in closed:
                group_id = str(activity["group_id"])
                if self._feature_group_enabled("compensation", group_id):
                    await self._announce_campaign(
                        group_id,
                        ActionResult(
                            "comp_auto_closed",
                            {
                                "activity_id": str(activity["id"]),
                                "title": activity["title"],
                            },
                        ),
                    )
        await self._flush_campaign_notifications()

    async def _campaign_scheduler_loop(self) -> None:
        while True:
            try:
                await self._campaign_scheduler_once()
            except Exception as exc:  # noqa: BLE001 - background task boundary
                logger.exception(
                    "Campaign scheduler failed error_type=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(CAMPAIGN_SCHEDULER_SECONDS)

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_aiocqhttp_event(self, event: AstrMessageEvent) -> None:
        if not (
            self._enabled()
            or self._feature_enabled("lottery")
            or self._feature_enabled("compensation")
        ):
            return

        raw = self._raw_event(event)
        if raw and raw.get("post_type") == "notice":
            if self._enabled() and raw.get("notice_type") == "group_increase":
                await self._handle_group_increase(event, raw)
            return

        if event.get_group_id():
            if self._enabled() and self._is_claim_message(event):
                await self._handle_claim(event)
                return
            if await self._handle_campaign_message(event):
                return
            if self._enabled():
                await self._handle_claim(event)

    async def initialize(self) -> None:
        """Initialize storage and start background synchronization tasks."""
        async with self._campaign_lifecycle_lock:
            await self.campaign_page_api.activate()
            await asyncio.gather(
                self.storage.initialize(),
                self.lottery_storage.initialize(),
                self.compensation_storage.initialize(),
            )
            self._terminating = False
            self._plugin_initialized = True
            if self._enabled() and (
                self._baseline_task is None or self._baseline_task.done()
            ):
                self._baseline_task = asyncio.create_task(
                    self._baseline_sync_loop(),
                    name=f"{PLUGIN_NAME}:baseline-sync",
                )
            await self._reconcile_campaign_scheduler_locked()
            if (
                self._processing_recovery_task is None
                or self._processing_recovery_task.done()
            ):
                self._processing_recovery_task = asyncio.create_task(
                    self._processing_recovery_loop(),
                    name=f"{PLUGIN_NAME}:processing-recovery",
                )
            if self._gift_recovery_task is None or self._gift_recovery_task.done():
                self._gift_recovery_task = asyncio.create_task(
                    self._gift_recovery_loop(),
                    name=f"{PLUGIN_NAME}:gift-recovery",
                )

    async def terminate(self) -> None:
        """Release plugin resources."""
        async with self._campaign_lifecycle_lock:
            self._terminating = True
            self._plugin_initialized = False
            self.campaign_page_api.begin_shutdown()
            await self.campaign_page_api.wait_for_settings_idle()
            campaign_task = self._campaign_task
            if campaign_task is not None and not campaign_task.done():
                campaign_task.cancel()
                with suppress(asyncio.CancelledError):
                    await campaign_task
            if self._campaign_task is campaign_task:
                self._campaign_task = None
            if self._baseline_task is not None and not self._baseline_task.done():
                self._baseline_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._baseline_task
            self._baseline_task = None
            if (
                self._processing_recovery_task is not None
                and not self._processing_recovery_task.done()
            ):
                self._processing_recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._processing_recovery_task
            self._processing_recovery_task = None
            if (
                self._gift_recovery_task is not None
                and not self._gift_recovery_task.done()
            ):
                self._gift_recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._gift_recovery_task
            self._gift_recovery_task = None
            close_tasks = tuple(self._newapi_close_tasks)
            if close_tasks:
                for task in close_tasks:
                    task.cancel()
                await asyncio.gather(*close_tasks, return_exceptions=True)
            self._newapi_close_tasks.clear()
            if self._newapi_client is not None:
                await self._close_newapi_client(self._newapi_client)
            self._newapi_client = None
