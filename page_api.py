from __future__ import annotations

from typing import Any

from astrbot.api import logger
from quart import request

from .storage import GiftStorage

PLUGIN_NAME = "astrbot_plugin_transfer_station"


class GiftPageApi:
    """AstrBot Plugin Page API for gift-code administration."""

    def __init__(self, context: Any, storage: GiftStorage):
        self.context = context
        self.storage = storage

    def register_routes(self) -> None:
        routes = (
            ("summary", self.get_summary, ["GET"], "Gift inventory summary"),
            ("codes", self.get_codes, ["GET"], "Gift code inventory"),
            ("codes/import", self.import_codes, ["POST"], "Import gift codes"),
            (
                "codes/<int:code_id>",
                self.delete_code,
                ["DELETE"],
                "Delete an unused gift code",
            ),
            (
                "codes/delete",
                self.delete_code_bridge,
                ["POST"],
                "Delete an unused gift code from Plugin Page bridge",
            ),
            ("claims", self.get_claims, ["GET"], "Gift claim history"),
        )
        for endpoint, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}",
                handler,
                methods,
                description,
            )

    @staticmethod
    def _ok(data: Any = None, message: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "ok",
            "data": data if data is not None else {},
        }
        if message:
            payload["message"] = message
        return payload

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"status": "error", "message": message}

    @staticmethod
    def _pagination() -> tuple[int, int]:
        try:
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError("分页参数无效") from exc
        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("page 必须大于 0，page_size 必须在 1 到 100 之间")
        return page, page_size

    async def get_summary(self) -> dict[str, Any]:
        try:
            return self._ok(await self.storage.summary())
        except Exception:  # noqa: BLE001 - Web API boundary
            logger.exception("Transfer station Page summary failed")
            return self._error("读取统计信息失败")

    async def get_codes(self) -> dict[str, Any]:
        try:
            page, page_size = self._pagination()
            return self._ok(await self.storage.list_codes(page, page_size))
        except ValueError as exc:
            return self._error(str(exc))
        except Exception:  # noqa: BLE001 - Web API boundary
            logger.exception("Transfer station Page code list failed")
            return self._error("读取兑换码库存失败")

    async def import_codes(self) -> dict[str, Any]:
        try:
            payload = await request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return self._error("请求体必须是 JSON 对象")
            content = payload.get("content")
            if not isinstance(content, str):
                return self._error("content 必须是字符串")
            codes = content.splitlines()
            if not any(code.strip() for code in codes):
                return self._error("请至少输入一个兑换码")
            result = await self.storage.import_codes(codes)
            return self._ok(result, "兑换码导入完成")
        except ValueError as exc:
            return self._error(str(exc))
        except Exception:  # noqa: BLE001 - Web API boundary
            logger.exception("Transfer station Page code import failed")
            return self._error("导入兑换码失败")

    async def delete_code(self, code_id: int) -> dict[str, Any]:
        return await self._delete_code(code_id)

    async def delete_code_bridge(self) -> dict[str, Any]:
        try:
            payload = await request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return self._error("请求体必须是 JSON 对象")
            code_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            return self._error("兑换码 ID 无效")
        return await self._delete_code(code_id)

    async def _delete_code(self, code_id: int) -> dict[str, Any]:
        if code_id <= 0:
            return self._error("兑换码 ID 无效")
        try:
            deleted = await self.storage.delete_code(code_id)
            if not deleted:
                return self._error("兑换码不存在或已被领取")
            return self._ok({"deleted_id": code_id}, "兑换码已删除")
        except Exception:  # noqa: BLE001 - Web API boundary
            logger.exception("Transfer station Page code delete failed id=%s", code_id)
            return self._error("删除兑换码失败")

    async def get_claims(self) -> dict[str, Any]:
        try:
            page, page_size = self._pagination()
            return self._ok(await self.storage.list_claims(page, page_size))
        except ValueError as exc:
            return self._error(str(exc))
        except Exception:  # noqa: BLE001 - Web API boundary
            logger.exception("Transfer station Page claim list failed")
            return self._error("读取领取记录失败")
