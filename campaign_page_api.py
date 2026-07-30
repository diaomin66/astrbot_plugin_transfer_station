from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from weakref import WeakKeyDictionary

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .campaign_utils import (
    ActionResult,
    parse_duration,
    parse_positive_decimal,
    parse_time_spec,
    to_iso,
    utc_now,
)
from .compensation import CompensationService, CompensationStorage
from .lottery import LotteryService, LotteryStorage
from .newapi_client import (
    MAX_NEWAPI_USER_ID,
    NewApiClient,
    NewApiError,
    public_newapi_error,
)
from .page_validation import positive_int

PLUGIN_NAME = "astrbot_plugin_transfer_station"
MAX_PAGE_SIZE = 100
MAX_GROUP_FILTERS = 500
MAX_GROUP_ID_LENGTH = 20
PAGE_EDITABLE_SETTINGS = (
    "newapi_base_url",
    "newapi_user_id",
    "newapi_timeout_seconds",
    "newapi_verify_ssl",
    "newapi_allow_insecure_http",
    "lottery_enabled",
    "lottery_enabled_group_ids",
    "compensation_enabled",
    "compensation_enabled_group_ids",
)


@dataclass
class _SettingsState:
    lock: asyncio.Lock
    revision: str
    owner_token: object


_SHARED_SETTINGS_STATES: WeakKeyDictionary[Any, _SettingsState] = WeakKeyDictionary()


class CampaignPageApi:
    """Plugin Page API for lottery and compensation operations."""

    def __init__(
        self,
        context: Any,
        config: dict[str, Any],
        lottery_storage: LotteryStorage,
        compensation_storage: CompensationStorage,
        *,
        lottery_service: Callable[[bool], LotteryService],
        compensation_service: Callable[[bool], CompensationService],
        newapi_client: Callable[[], NewApiClient],
        render_action: Callable[[ActionResult], str],
        settings_changed: Callable[[], Awaitable[None]],
        draw_lottery: Callable[[int], Awaitable[ActionResult]],
        flush_notifications: Callable[[], Awaitable[None]],
    ):
        self.context = context
        self.config = config
        self.lottery_storage = lottery_storage
        self.compensation_storage = compensation_storage
        self._lottery_service = lottery_service
        self._compensation_service = compensation_service
        self._newapi_client = newapi_client
        self._render_action = render_action
        self._settings_changed = settings_changed
        self._draw_lottery = draw_lottery
        self._flush_notifications = flush_notifications
        self._settings_owner_token = object()
        current_revision = self._settings_revision()
        settings_state = _SHARED_SETTINGS_STATES.get(context)
        if settings_state is None:
            settings_state = _SettingsState(
                lock=asyncio.Lock(),
                revision=current_revision,
                owner_token=self._settings_owner_token,
            )
            _SHARED_SETTINGS_STATES[context] = settings_state
            self._accept_settings_saves = True
        else:
            self._accept_settings_saves = False
        self._settings_state = settings_state

    async def activate(self) -> None:
        async with self._settings_state.lock:
            self._settings_state.revision = self._settings_revision()
            self._settings_state.owner_token = self._settings_owner_token
            self._accept_settings_saves = True

    def begin_shutdown(self) -> None:
        self._accept_settings_saves = False

    async def wait_for_settings_idle(self) -> None:
        async with self._settings_state.lock:
            return

    def register_routes(self) -> None:
        routes = (
            ("campaigns/summary", self.get_summary, ["GET"], "Campaign summary"),
            ("campaigns/settings", self.get_settings, ["GET"], "Campaign settings"),
            (
                "campaigns/settings/save",
                self.save_settings,
                ["POST"],
                "Save campaign settings",
            ),
            (
                "campaigns/newapi/test",
                self.test_newapi,
                ["POST"],
                "Test New API connection",
            ),
            (
                "campaigns/lotteries",
                self.get_lotteries,
                ["GET"],
                "Lottery activities",
            ),
            (
                "campaigns/lotteries/detail",
                self.get_lottery_detail,
                ["GET"],
                "Lottery activity detail",
            ),
            (
                "campaigns/lotteries/create",
                self.create_lottery,
                ["POST"],
                "Create lottery draft",
            ),
            (
                "campaigns/lotteries/update",
                self.update_lottery,
                ["POST"],
                "Update lottery draft",
            ),
            (
                "campaigns/lotteries/prizes/add",
                self.add_lottery_prize,
                ["POST"],
                "Add lottery prize",
            ),
            (
                "campaigns/lotteries/prizes/delete",
                self.delete_lottery_prize,
                ["POST"],
                "Delete lottery prize",
            ),
            (
                "campaigns/lotteries/publish",
                self.publish_lottery,
                ["POST"],
                "Publish lottery",
            ),
            (
                "campaigns/lotteries/draw",
                self.draw_lottery,
                ["POST"],
                "Draw lottery",
            ),
            (
                "campaigns/lotteries/cancel",
                self.cancel_lottery,
                ["POST"],
                "Cancel lottery",
            ),
            (
                "campaigns/lotteries/review",
                self.review_lottery,
                ["POST"],
                "Review lottery payout",
            ),
            (
                "campaigns/compensations",
                self.get_compensations,
                ["GET"],
                "Compensation activities",
            ),
            (
                "campaigns/compensations/detail",
                self.get_compensation_detail,
                ["GET"],
                "Compensation activity detail",
            ),
            (
                "campaigns/compensations/open",
                self.open_compensation,
                ["POST"],
                "Open compensation",
            ),
            (
                "campaigns/compensations/close",
                self.close_compensation,
                ["POST"],
                "Close compensation",
            ),
            (
                "campaigns/compensations/review",
                self.review_compensation,
                ["POST"],
                "Review compensation payout",
            ),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                description,
            )

    @staticmethod
    async def _payload() -> dict[str, Any]:
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise TypeError("请求体必须是 JSON 对象")
        return payload

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        return positive_int(value, label)

    @classmethod
    def _pagination(cls) -> tuple[int, int]:
        page = cls._positive_int(request.query.get("page", "1"), "页码")
        page_size = cls._positive_int(
            request.query.get("page_size", "20"),
            "每页数量",
        )
        if page_size > MAX_PAGE_SIZE:
            raise ValueError(f"每页数量不能超过 {MAX_PAGE_SIZE}")
        return page, page_size

    @classmethod
    def _activity_query(cls) -> tuple[int, int, int]:
        activity_id = cls._positive_int(
            request.query.get("activity_id"),
            "活动 ID",
        )
        page, page_size = cls._pagination()
        return activity_id, page, page_size

    @staticmethod
    def _group_id(value: Any) -> str:
        normalized = str(value).strip()
        if (
            not normalized
            or len(normalized) > MAX_GROUP_ID_LENGTH
            or not normalized.isdigit()
            or int(normalized) <= 0
        ):
            raise ValueError("群号必须是有效的正整数")
        return normalized

    @staticmethod
    def _optional_newapi_user_id(value: Any) -> str:
        normalized = str(value).strip()
        if not normalized:
            return ""
        parsed = positive_int(normalized, "New API 用户数字 ID")
        if parsed > MAX_NEWAPI_USER_ID:
            raise ValueError("New API 用户数字 ID 超出安全范围")
        return str(parsed)

    @classmethod
    def _group_ids(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise TypeError("群白名单必须是数组")
        if len(value) > MAX_GROUP_FILTERS:
            raise ValueError(f"群白名单最多支持 {MAX_GROUP_FILTERS} 项")
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            group_id = cls._group_id(item)
            if group_id not in seen:
                seen.add(group_id)
                result.append(group_id)
        return result

    def _settings_revision(self) -> str:
        snapshot = {key: self.config.get(key) for key in PAGE_EDITABLE_SETTINGS}
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]

    def _public_settings(self) -> dict[str, Any]:
        return {
            "revision": self._settings_revision(),
            "newapi_base_url": str(self.config.get("newapi_base_url", "")).strip(),
            "newapi_user_id": str(self.config.get("newapi_user_id", "")).strip(),
            "newapi_timeout_seconds": self.config.get(
                "newapi_timeout_seconds",
                10,
            ),
            "newapi_verify_ssl": bool(self.config.get("newapi_verify_ssl", True)),
            "newapi_allow_insecure_http": bool(
                self.config.get("newapi_allow_insecure_http", False)
            ),
            "newapi_username": str(self.config.get("newapi_username", "")).strip(),
            "newapi_access_token_configured": bool(
                str(self.config.get("newapi_access_token", "")).strip()
            ),
            "newapi_password_configured": bool(
                str(self.config.get("newapi_password", ""))
            ),
            "lottery_enabled": bool(self.config.get("lottery_enabled", False)),
            "lottery_enabled_group_ids": list(
                self.config.get("lottery_enabled_group_ids", [])
                if isinstance(
                    self.config.get("lottery_enabled_group_ids", []),
                    list,
                )
                else []
            ),
            "compensation_enabled": bool(
                self.config.get("compensation_enabled", False)
            ),
            "compensation_enabled_group_ids": list(
                self.config.get("compensation_enabled_group_ids", [])
                if isinstance(
                    self.config.get("compensation_enabled_group_ids", []),
                    list,
                )
                else []
            ),
        }

    def _feature_group_enabled(self, feature: str, group_id: str) -> bool:
        if not bool(self.config.get(f"{feature}_enabled", False)):
            return False
        configured = self.config.get(f"{feature}_enabled_group_ids", [])
        enabled_groups = (
            {str(item).strip() for item in configured if str(item).strip()}
            if isinstance(configured, list)
            else set()
        )
        return not enabled_groups or group_id in enabled_groups

    def _action_response(
        self,
        result: ActionResult,
        *,
        success_keys: set[str],
        conflict_keys: set[str] | None = None,
    ):
        message = self._render_action(result)
        body = {
            "key": result.key,
            "message": message,
            "placeholders": dict(result.placeholders),
        }
        if result.key in success_keys:
            return json_response(body)
        status_code = 409 if result.key in (conflict_keys or set()) else 400
        return error_response(message, status_code=status_code, data=body)

    async def _flush_after_action(self) -> None:
        try:
            await self._flush_notifications()
        except Exception as exc:  # noqa: BLE001 - OneBot notification boundary
            logger.warning(
                "Campaign Page notification flush failed error_type=%s",
                type(exc).__name__,
            )

    @staticmethod
    def _newapi_error(exc: NewApiError):
        status_code = exc.status_code
        if exc.kind == "config":
            error_kind = "config"
        elif exc.kind == "2fa":
            error_kind = "2fa"
        elif status_code == 401:
            error_kind = "unauthorized"
        elif status_code == 403:
            error_kind = "forbidden"
        elif status_code == 404:
            error_kind = "not_found"
        else:
            error_kind = "connection"
        message = f"New API 测试失败：{public_newapi_error(exc)}"
        return error_response(
            message,
            data={
                "kind": error_kind,
                "status_code": status_code,
            },
        )

    async def get_summary(self):
        try:
            lottery, compensation = await asyncio.gather(
                self.lottery_storage.dashboard_summary(),
                self.compensation_storage.dashboard_summary(),
            )
            return json_response(
                {
                    "lottery": lottery,
                    "compensation": compensation,
                    "settings_revision": self._settings_revision(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page summary failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取活动统计失败", status_code=500)

    async def get_settings(self):
        return json_response(self._public_settings())

    async def save_settings(self):
        try:
            payload = await self._payload()
            revision = str(payload.get("revision", "")).strip()
            if not revision:
                raise ValueError("缺少设置版本，请刷新页面后重试")
            values = payload.get("settings")
            if not isinstance(values, dict):
                raise TypeError("settings 必须是 JSON 对象")
            unknown = set(values) - set(PAGE_EDITABLE_SETTINGS)
            if unknown:
                raise ValueError("包含不允许通过活动页面修改的设置")

            async with self._settings_state.lock:
                current_revision = self._settings_revision()
                if not self._accept_settings_saves:
                    return error_response(
                        "插件正在重载，请稍后重试",
                        status_code=503,
                    )
                if (
                    self._settings_state.owner_token is not self._settings_owner_token
                    or revision != current_revision
                ):
                    return error_response(
                        "设置已被其他页面或 AstrBot 配置页更新，请刷新后重试",
                        status_code=409,
                        data={"revision": current_revision},
                    )
                if self._settings_state.revision != current_revision:
                    self._settings_state.revision = current_revision
                normalized = {
                    "newapi_base_url": str(values.get("newapi_base_url", "")).strip(),
                    "newapi_user_id": self._optional_newapi_user_id(
                        values.get("newapi_user_id", "")
                    ),
                    "newapi_timeout_seconds": self._positive_int(
                        values.get("newapi_timeout_seconds", 10),
                        "New API 超时秒数",
                    ),
                    "newapi_verify_ssl": values.get("newapi_verify_ssl"),
                    "newapi_allow_insecure_http": values.get(
                        "newapi_allow_insecure_http"
                    ),
                    "lottery_enabled": values.get("lottery_enabled"),
                    "lottery_enabled_group_ids": self._group_ids(
                        values.get("lottery_enabled_group_ids", [])
                    ),
                    "compensation_enabled": values.get("compensation_enabled"),
                    "compensation_enabled_group_ids": self._group_ids(
                        values.get("compensation_enabled_group_ids", [])
                    ),
                }
                for key in (
                    "newapi_verify_ssl",
                    "newapi_allow_insecure_http",
                    "lottery_enabled",
                    "compensation_enabled",
                ):
                    if not isinstance(normalized[key], bool):
                        raise TypeError(f"{key} 必须是布尔值")
                if normalized["newapi_timeout_seconds"] > 120:
                    raise ValueError("New API 超时秒数必须在 1 到 120 之间")
                previous = {key: self.config.get(key) for key in PAGE_EDITABLE_SETTINGS}
                try:
                    self.config.update(normalized)
                    save_config_async = getattr(
                        self.config,
                        "save_config_async",
                        None,
                    )
                    if callable(save_config_async):
                        pending = save_config_async()
                        saved = (
                            await pending if inspect.isawaitable(pending) else pending
                        )
                        if saved is False:
                            raise RuntimeError("AstrBot 拒绝保存插件配置")
                    else:
                        save_config = getattr(self.config, "save_config", None)
                        if callable(save_config):
                            saved = save_config()
                            if saved is False:
                                raise RuntimeError("AstrBot 拒绝保存插件配置")
                except Exception:
                    self.config.update(previous)
                    raise
                self._settings_state.revision = self._settings_revision()
                public = self._public_settings()
            warning = ""
            try:
                await self._settings_changed()
            except Exception as exc:  # noqa: BLE001 - runtime refresh boundary
                warning = "设置已保存，但运行时刷新失败；请重载插件后生效。"
                logger.warning(
                    "Campaign Page runtime settings refresh failed error_type=%s",
                    type(exc).__name__,
                )
            return json_response(
                {
                    "message": "活动系统设置已保存",
                    "settings": public,
                    "warning": warning,
                }
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - config persistence boundary
            logger.exception(
                "Campaign Page settings save failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("保存活动系统设置失败", status_code=500)

    async def test_newapi(self):
        try:
            result = await self._newapi_client().test_connection()
            return json_response(
                {
                    "message": "New API 连接与管理权限验证成功",
                    "version": result.version,
                    "username": result.username,
                    "role": result.role,
                    "display_type": result.display_type,
                }
            )
        except NewApiError as exc:
            return self._newapi_error(exc)
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.warning(
                "Campaign Page New API test failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("New API 连接或管理权限验证失败")

    async def get_lotteries(self):
        try:
            page, page_size = self._pagination()
            scope = str(request.query.get("scope", "all")).strip()
            raw_group = str(request.query.get("group_id", "")).strip()
            group_id = self._group_id(raw_group) if raw_group else ""
            return json_response(
                await self.lottery_storage.list_activities(
                    page,
                    page_size,
                    scope=scope,
                    group_id=group_id,
                )
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page lottery list failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取抽奖活动失败", status_code=500)

    async def get_lottery_detail(self):
        try:
            activity_id, page, page_size = self._activity_query()
            detail = await self.lottery_storage.dashboard_activity(
                activity_id,
                page,
                page_size,
            )
            if detail is None:
                return error_response("抽奖活动不存在", status_code=404)
            return json_response(detail)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page lottery detail failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取抽奖详情失败", status_code=500)

    async def create_lottery(self):
        try:
            payload = await self._payload()
            group_id = self._group_id(payload.get("group_id"))
            if not self._feature_group_enabled("lottery", group_id):
                return error_response("该群未启用抽奖系统")
            result = await self._lottery_service(False).create(
                group_id,
                str(payload.get("title", "")),
                f"page:{request.username or 'admin'}",
            )
            return self._action_response(
                result,
                success_keys={"lottery_created"},
                conflict_keys={"lottery_active_exists"},
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page lottery create failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("创建抽奖草稿失败", status_code=500)

    async def update_lottery(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            revision = self._positive_int(payload.get("revision"), "活动版本")
            activity = await self.lottery_storage.get_activity(activity_id)
            if not activity:
                return error_response("抽奖活动不存在", status_code=404)
            group_id = str(activity["group_id"])
            if not self._feature_group_enabled("lottery", group_id):
                return error_response("该群未启用抽奖系统")
            values: dict[str, Any] = {
                "title": str(payload.get("title", "")),
                "description": str(payload.get("description", "")),
                "keyword": str(payload.get("keyword", "")),
            }
            now = utc_now()
            start_at = parse_time_spec(str(payload.get("start_time", "")), now=now)
            draw_at = parse_time_spec(str(payload.get("draw_time", "")), now=now)
            if draw_at <= start_at:
                raise ValueError("开奖时间必须晚于开始时间")
            values["start_at"] = to_iso(start_at)
            values["draw_at"] = to_iso(draw_at)
            values["claim_duration_seconds"] = int(
                parse_duration(str(payload.get("claim_duration", ""))).total_seconds()
            )
            result = await self._lottery_service(False).update_draft(
                group_id,
                expected_activity_id=activity_id,
                expected_revision=revision,
                **values,
            )
            return self._action_response(
                result,
                success_keys={"lottery_updated"},
                conflict_keys={
                    "campaign_invalid_argument",
                    "lottery_no_draft",
                },
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page lottery update failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("更新抽奖草稿失败", status_code=500)

    async def add_lottery_prize(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            revision = self._positive_int(payload.get("revision"), "活动版本")
            winner_count = self._positive_int(
                payload.get("winner_count"),
                "中奖人数",
            )
            amount = parse_positive_decimal(str(payload.get("amount", "")))
            activity = await self.lottery_storage.get_activity(activity_id)
            if not activity:
                return error_response("抽奖活动不存在", status_code=404)
            group_id = str(activity["group_id"])
            if not self._feature_group_enabled("lottery", group_id):
                return error_response("该群未启用抽奖系统")
            result = await self._lottery_service(False).add_prize(
                group_id,
                str(payload.get("name", "")),
                winner_count,
                amount,
                expected_activity_id=activity_id,
                expected_revision=revision,
            )
            return self._action_response(
                result,
                success_keys={"lottery_prize_added"},
                conflict_keys={
                    "campaign_invalid_argument",
                    "lottery_no_draft",
                },
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page prize add failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("添加抽奖奖项失败", status_code=500)

    async def delete_lottery_prize(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            revision = self._positive_int(payload.get("revision"), "活动版本")
            prize_id = self._positive_int(payload.get("prize_id"), "奖项 ID")
            activity = await self.lottery_storage.get_activity(activity_id)
            if not activity:
                return error_response("抽奖活动不存在", status_code=404)
            group_id = str(activity["group_id"])
            if not self._feature_group_enabled("lottery", group_id):
                return error_response("该群未启用抽奖系统")
            result = await self._lottery_service(False).delete_prize_by_id(
                group_id,
                prize_id,
                expected_activity_id=activity_id,
                expected_revision=revision,
            )
            return self._action_response(
                result,
                success_keys={"lottery_prize_deleted"},
                conflict_keys={
                    "campaign_invalid_argument",
                    "lottery_no_draft",
                },
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page prize delete failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("删除抽奖奖项失败", status_code=500)

    async def publish_lottery(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            revision = self._positive_int(payload.get("revision"), "活动版本")
            activity = await self.lottery_storage.get_activity(activity_id)
            if not activity:
                return error_response("抽奖活动不存在", status_code=404)
            group_id = str(activity["group_id"])
            if not self._feature_group_enabled("lottery", group_id):
                return error_response("该群未启用抽奖系统")
            result = await self._lottery_service(True).publish(
                group_id,
                expected_activity_id=activity_id,
                expected_revision=revision,
            )
            response = self._action_response(
                result,
                success_keys={"lottery_published"},
                conflict_keys={
                    "campaign_invalid_argument",
                    "lottery_no_draft",
                },
            )
            if result.key == "lottery_published":
                await self._flush_after_action()
            return response
        except NewApiError:
            return error_response("New API 连接或管理权限验证失败")
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page lottery publish failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("发布抽奖活动失败", status_code=500)

    async def draw_lottery(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            revision = self._positive_int(payload.get("revision"), "活动版本")
            activity = await self.lottery_storage.get_activity(activity_id)
            if not activity:
                return error_response("抽奖活动不存在", status_code=404)
            if int(activity["revision"]) != revision:
                return error_response(
                    "抽奖活动已被其他页面更新，请重新载入详情",
                    status_code=409,
                )
            if not self._feature_group_enabled(
                "lottery",
                str(activity["group_id"]),
            ):
                return error_response("该群未启用抽奖系统")
            result = await self._draw_lottery(activity_id)
            return self._action_response(
                result,
                success_keys={"lottery_drawn", "lottery_no_winner"},
                conflict_keys={"lottery_not_open"},
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - OneBot boundary
            logger.exception(
                "Campaign Page lottery draw failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取群成员或开奖失败", status_code=500)

    async def cancel_lottery(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            revision = self._positive_int(payload.get("revision"), "活动版本")
            activity = await self.lottery_storage.get_activity(activity_id)
            if not activity:
                return error_response("抽奖活动不存在", status_code=404)
            result = await self._lottery_service(False).cancel_activity(
                str(activity["group_id"]),
                str(payload.get("reason", "")),
                expected_activity_id=activity_id,
                expected_revision=revision,
            )
            response = self._action_response(
                result,
                success_keys={"lottery_cancelled"},
                conflict_keys={"lottery_no_active", "campaign_invalid_argument"},
            )
            if result.key == "lottery_cancelled":
                await self._flush_after_action()
            return response
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page lottery cancel failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("取消抽奖活动失败", status_code=500)

    async def review_lottery(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            success = payload.get("success")
            if not isinstance(success, bool):
                raise TypeError("核查结果必须是布尔值")
            activity = await self.lottery_storage.get_activity(activity_id)
            if not activity:
                return error_response("抽奖活动不存在", status_code=404)
            result = await self._lottery_service(False).review(
                str(activity["group_id"]),
                str(payload.get("serial", "")).strip(),
                success,
                activity_id=activity_id,
            )
            return self._action_response(
                result,
                success_keys={
                    "lottery_review_success",
                    "lottery_review_failed",
                },
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page lottery review failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("核查抽奖流水失败", status_code=500)

    async def get_compensations(self):
        try:
            page, page_size = self._pagination()
            scope = str(request.query.get("scope", "all")).strip()
            raw_group = str(request.query.get("group_id", "")).strip()
            group_id = self._group_id(raw_group) if raw_group else ""
            return json_response(
                await self.compensation_storage.list_activities(
                    page,
                    page_size,
                    scope=scope,
                    group_id=group_id,
                )
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page compensation list failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取补偿活动失败", status_code=500)

    async def get_compensation_detail(self):
        try:
            activity_id, page, page_size = self._activity_query()
            detail = await self.compensation_storage.dashboard_activity(
                activity_id,
                page,
                page_size,
            )
            if detail is None:
                return error_response("补偿活动不存在", status_code=404)
            return json_response(detail)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page compensation detail failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取补偿详情失败", status_code=500)

    async def open_compensation(self):
        try:
            payload = await self._payload()
            group_id = self._group_id(payload.get("group_id"))
            if not self._feature_group_enabled("compensation", group_id):
                return error_response("该群未启用补偿系统")
            per_amount = str(parse_positive_decimal(str(payload.get("per_amount", ""))))
            duration_value = str(payload.get("duration", "")).strip()
            total_value = str(payload.get("total_amount", "")).strip()
            duration: timedelta | None = (
                None if duration_value == "-" else parse_duration(duration_value)
            )
            total_amount: str | None = (
                None if total_value == "-" else str(parse_positive_decimal(total_value))
            )
            if duration is None and total_amount is None:
                raise ValueError("持续时间和总金额至少填写一项")
            result = await self._compensation_service(True).open(
                group_id,
                per_amount,
                duration,
                total_amount,
                str(payload.get("title", "")),
                f"page:{request.username or 'admin'}",
            )
            response = self._action_response(
                result,
                success_keys={"comp_opened"},
                conflict_keys={"comp_active_exists"},
            )
            if result.key == "comp_opened":
                await self._flush_after_action()
            return response
        except NewApiError:
            return error_response("New API 连接或管理权限验证失败")
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page compensation open failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("开启补偿活动失败", status_code=500)

    async def close_compensation(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            activity = await self.compensation_storage.get(activity_id)
            if not activity:
                return error_response("补偿活动不存在", status_code=404)
            result = await self._compensation_service(False).close(
                str(activity["group_id"]),
                str(payload.get("reason", "")),
                expected_activity_id=activity_id,
            )
            response = self._action_response(
                result,
                success_keys={"comp_closed"},
                conflict_keys={"comp_no_active"},
            )
            if result.key == "comp_closed":
                await self._flush_after_action()
            return response
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page compensation close failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("关闭补偿活动失败", status_code=500)

    async def review_compensation(self):
        try:
            payload = await self._payload()
            activity_id = self._positive_int(
                payload.get("activity_id"),
                "活动 ID",
            )
            success = payload.get("success")
            if not isinstance(success, bool):
                raise TypeError("核查结果必须是布尔值")
            activity = await self.compensation_storage.get(activity_id)
            if not activity:
                return error_response("补偿活动不存在", status_code=404)
            result = await self._compensation_service(False).review(
                str(activity["group_id"]),
                str(payload.get("serial", "")).strip(),
                success,
                activity_id=activity_id,
            )
            return self._action_response(
                result,
                success_keys={"comp_review_success", "comp_review_failed"},
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Campaign Page compensation review failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("核查补偿流水失败", status_code=500)
