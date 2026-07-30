from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from .campaign_utils import decimal_text, parse_positive_decimal

MAX_RAW_QUOTA = 2**63 - 1
MAX_NEWAPI_USER_ID = 10**18 - 1
MAX_RAW_QUOTA_DECIMAL = Decimal(MAX_RAW_QUOTA)


class NewApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "clear",
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code

    @property
    def ambiguous(self) -> bool:
        return self.kind == "ambiguous"


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    display_type: str
    quota_per_unit: Decimal
    usd_exchange_rate: Decimal
    custom_currency_exchange_rate: Decimal
    custom_currency_symbol: str = ""

    @classmethod
    def from_status(cls, data: dict[str, Any]) -> QuotaSnapshot:
        raw_type = data.get("quota_display_type")
        if not raw_type:
            raw_type = "USD" if data.get("display_in_currency") else "TOKENS"
        display_type = str(raw_type).strip().upper()
        aliases = {
            "TOKEN": "TOKENS",
            "QUOTA": "TOKENS",
            "RMB": "CNY",
        }
        display_type = aliases.get(display_type, display_type)
        if display_type not in {"USD", "CNY", "CUSTOM", "TOKENS"}:
            raise NewApiError("New API 返回了不支持的额度显示类型")

        try:
            quota_per_unit = Decimal(str(data.get("quota_per_unit", 0)))
            usd_rate = Decimal(str(data.get("usd_exchange_rate", 0)))
            custom_rate = Decimal(str(data.get("custom_currency_exchange_rate", 0)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise NewApiError("New API 额度换算配置无效") from exc
        if not all(
            value.is_finite() for value in (quota_per_unit, usd_rate, custom_rate)
        ):
            raise NewApiError("New API 额度换算配置无效")
        if quota_per_unit <= 0:
            raise NewApiError("New API quota_per_unit 必须大于 0")
        if display_type == "CNY" and usd_rate <= 0:
            raise NewApiError("New API usd_exchange_rate 必须大于 0")
        if display_type == "CUSTOM" and custom_rate <= 0:
            raise NewApiError("New API custom_currency_exchange_rate 必须大于 0")
        return cls(
            display_type=display_type,
            quota_per_unit=quota_per_unit,
            usd_exchange_rate=usd_rate,
            custom_currency_exchange_rate=custom_rate,
            custom_currency_symbol=str(data.get("custom_currency_symbol", "")).strip(),
        )

    def amount_to_quota(self, value: Decimal | str | int) -> int:
        amount = parse_positive_decimal(str(value))
        try:
            if self.display_type == "TOKENS":
                raw = amount
            elif self.display_type == "USD":
                raw = amount * self.quota_per_unit
            elif self.display_type == "CNY":
                raw = amount / self.usd_exchange_rate * self.quota_per_unit
            else:
                raw = amount / self.custom_currency_exchange_rate * self.quota_per_unit
        except (DecimalException, ZeroDivisionError) as exc:
            raise NewApiError("额度换算结果无效") from exc
        if not raw.is_finite() or raw > MAX_RAW_QUOTA_DECIMAL + Decimal("0.5"):
            raise NewApiError("换算后的原始 quota 超出安全范围")
        try:
            quota = int(raw.to_integral_value(rounding=ROUND_HALF_UP))
        except (DecimalException, OverflowError, ValueError) as exc:
            raise NewApiError("额度换算结果无效") from exc
        if quota <= 0:
            raise NewApiError("换算后的原始 quota 必须大于 0")
        if quota > MAX_RAW_QUOTA:
            raise NewApiError("换算后的原始 quota 超出安全范围")
        return quota

    def display_amount(self, value: Decimal | str | int) -> str:
        amount = decimal_text(value)
        if self.display_type == "USD":
            return f"${amount}"
        if self.display_type == "CNY":
            return f"¥{amount}"
        if self.display_type == "CUSTOM":
            return f"{self.custom_currency_symbol}{amount}"
        return f"{amount} quota"


@dataclass(frozen=True, slots=True)
class NewApiUser:
    user_id: int
    username: str
    status: int | str


@dataclass(frozen=True, slots=True)
class NewApiTestResult:
    version: str
    username: str
    role: str
    display_type: str


class NewApiClient:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.config = dict(config)
        self.configuration_fingerprint = self.config_fingerprint(self.config)
        self.base_url = self._validated_base_url()
        self._timeout_seconds = self._timeout()
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._timeout_seconds,
            verify=bool(self.config.get("newapi_verify_ssl", True)),
            follow_redirects=False,
        )
        self._configured_token = str(self.config.get("newapi_access_token", "")).strip()
        self._session_token = ""

    @staticmethod
    def config_fingerprint(config: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(config.get("newapi_base_url", "")).strip().rstrip("/"),
            str(config.get("newapi_access_token", "")).strip(),
            str(config.get("newapi_username", "")).strip(),
            str(config.get("newapi_password", "")),
            config.get("newapi_timeout_seconds", 10),
            bool(config.get("newapi_verify_ssl", True)),
            bool(config.get("newapi_allow_insecure_http", False)),
        )

    def _timeout(self) -> float:
        try:
            value = float(self.config.get("newapi_timeout_seconds", 10))
        except (TypeError, ValueError):
            value = 10
        return min(120.0, max(1.0, value))

    def _validated_base_url(self) -> str:
        raw = str(self.config.get("newapi_base_url", "")).strip().rstrip("/")
        if not raw:
            raise NewApiError("尚未配置 New API 地址", kind="config")
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise NewApiError("New API 地址格式无效", kind="config")
        if parsed.scheme == "http" and not bool(
            self.config.get("newapi_allow_insecure_http", False)
        ):
            raise NewApiError(
                "New API 地址必须使用 HTTPS；如确需 HTTP，请显式开启不安全 HTTP",
                kind="config",
            )
        return raw

    async def close(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    async def _login(self) -> None:
        username = str(self.config.get("newapi_username", "")).strip()
        password = str(self.config.get("newapi_password", ""))
        if not username or not password:
            raise NewApiError(
                "请配置 New API 访问令牌，或同时配置用户名和密码",
                kind="config",
            )
        payload = await self._request_json(
            "POST",
            "/api/user/login",
            json={"username": username, "password": password},
            authenticated=False,
            write=False,
        )
        data = payload.get("data")
        if isinstance(data, dict) and data.get("require_2fa"):
            raise NewApiError(
                "New API 账号已启用 2FA，请改用管理员访问令牌",
                kind="2fa",
            )
        if isinstance(data, dict):
            self._session_token = str(data.get("access_token", "")).strip()

    async def _authorization_headers(self) -> dict[str, str]:
        token = self._configured_token or self._session_token
        if not token and not self._configured_token:
            await self._login()
            token = self._session_token
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _response_message(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            message = str(payload.get("message", "")).strip()
            if message:
                return message[:300]
        return fallback

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
        write: bool = False,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                headers = (
                    await self._authorization_headers()
                    if authenticated
                    else {"Accept": "application/json"}
                )
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    headers=headers,
                )
        except (TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
            kind = "ambiguous" if write else "clear"
            raise NewApiError("New API 网络请求失败", kind=kind) from exc
        except httpx.HTTPError as exc:
            kind = "ambiguous" if write else "clear"
            raise NewApiError("New API HTTP 请求失败", kind=kind) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            kind = (
                "clear"
                if write and 400 <= response.status_code < 500
                else "ambiguous"
                if write
                else "clear"
            )
            raise NewApiError(
                "New API 返回了无法解析的响应",
                kind=kind,
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            kind = (
                "clear"
                if write and 400 <= response.status_code < 500
                else "ambiguous"
                if write
                else "clear"
            )
            raise NewApiError(
                "New API 返回结构无效",
                kind=kind,
                status_code=response.status_code,
            )

        if response.status_code >= 500:
            raise NewApiError(
                self._response_message(payload, "New API 服务暂时不可用"),
                kind="ambiguous" if write else "clear",
                status_code=response.status_code,
            )
        if 300 <= response.status_code < 400:
            raise NewApiError(
                "New API 返回了未处理的重定向",
                kind="ambiguous" if write else "clear",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise NewApiError(
                self._response_message(payload, "New API 请求被拒绝"),
                status_code=response.status_code,
            )
        if payload.get("success") is False:
            raise NewApiError(
                self._response_message(payload, "New API 操作失败"),
                status_code=response.status_code,
            )
        if write and payload.get("success") is not True:
            raise NewApiError(
                "New API 写操作响应缺少明确成功标记",
                kind="ambiguous",
                status_code=response.status_code,
            )
        return payload

    async def status_snapshot(self) -> QuotaSnapshot:
        payload = await self._request_json(
            "GET",
            "/api/status",
            authenticated=False,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NewApiError("New API 状态响应缺少 data")
        return QuotaSnapshot.from_status(data)

    async def get_user(self, user_id: int | str) -> NewApiUser:
        try:
            normalized_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise NewApiError("New API 用户 ID 必须是正整数") from exc
        if normalized_id <= 0:
            raise NewApiError("New API 用户 ID 必须是正整数")
        if normalized_id > MAX_NEWAPI_USER_ID:
            raise NewApiError("New API 用户 ID 超出安全范围")
        payload = await self._request_json("GET", f"/api/user/{normalized_id}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NewApiError("New API 用户响应缺少 data")
        status = data.get("status", "")
        if status not in (1, "1", "enabled", "active"):
            raise NewApiError("该 New API 用户不存在或未启用")
        username = str(data.get("username", "")).strip()
        if not username:
            raise NewApiError("New API 用户响应缺少用户名")
        return NewApiUser(normalized_id, username, status)

    async def add_quota(self, user_id: int, raw_quota: int) -> None:
        if (
            int(user_id) <= 0
            or int(user_id) > MAX_NEWAPI_USER_ID
            or int(raw_quota) <= 0
            or int(raw_quota) > MAX_RAW_QUOTA
        ):
            raise NewApiError("用户 ID 和原始 quota 必须大于 0")
        await self._request_json(
            "POST",
            "/api/user/manage",
            json={
                "id": int(user_id),
                "action": "add_quota",
                "mode": "add",
                "value": int(raw_quota),
            },
            write=True,
        )

    async def test_connection(self) -> NewApiTestResult:
        status_payload = await self._request_json(
            "GET",
            "/api/status",
            authenticated=False,
        )
        status_data = status_payload.get("data")
        if not isinstance(status_data, dict):
            raise NewApiError("New API 状态响应缺少 data")
        snapshot = QuotaSnapshot.from_status(status_data)
        self_payload = await self._request_json("GET", "/api/user/self")
        self_data = self_payload.get("data")
        if not isinstance(self_data, dict):
            raise NewApiError("New API 身份响应缺少 data")
        await self._request_json("GET", "/api/user/?p=1&page_size=1")
        return NewApiTestResult(
            version=str(status_data.get("version", "")).strip() or "unknown",
            username=str(self_data.get("username", "")).strip() or "unknown",
            role=str(self_data.get("role", "")).strip() or "unknown",
            display_type=snapshot.display_type,
        )
