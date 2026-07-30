from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .storage import MAX_IMPORT_CODES, GiftStorage

PLUGIN_NAME = "astrbot_plugin_transfer_station"
MAX_IMPORT_CONTENT_LENGTH = 1024 * 1024


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
                "codes/<code_id>",
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
            (
                "gift-reviews",
                self.get_gift_reviews,
                ["GET"],
                "Ambiguous gift deliveries",
            ),
            (
                "gift-reviews/resolve",
                self.resolve_gift_review,
                ["POST"],
                "Resolve an ambiguous gift delivery",
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
    def _pagination() -> tuple[int, int]:
        page = request.query.get("page", 1, type=int)
        page_size = request.query.get("page_size", 20, type=int)
        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("page 必须大于 0，page_size 必须在 1 到 100 之间")
        return page, page_size

    @staticmethod
    async def _payload() -> dict[str, Any]:
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise TypeError("请求体必须是 JSON 对象")
        return payload

    async def get_summary(self):
        try:
            return json_response(await self.storage.summary())
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Transfer station Page summary failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取统计信息失败", status_code=500)

    async def get_codes(self):
        try:
            page, page_size = self._pagination()
            return json_response(await self.storage.list_codes(page, page_size))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Transfer station Page code list failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取兑换码库存失败", status_code=500)

    async def import_codes(self):
        try:
            payload = await self._payload()
            content = payload.get("content")
            if not isinstance(content, str):
                raise TypeError("content 必须是字符串")
            if len(content.encode("utf-8")) > MAX_IMPORT_CONTENT_LENGTH:
                return error_response(
                    "单次导入内容不能超过 1 MiB",
                    status_code=413,
                )
            codes = content.splitlines()
            if len(codes) > MAX_IMPORT_CODES:
                raise ValueError(f"单次最多导入 {MAX_IMPORT_CODES} 行")
            if not any(code.strip() for code in codes):
                raise ValueError("请至少输入一个兑换码")
            result = await self.storage.import_codes(codes)
            return json_response({**result, "message": "兑换码导入完成"})
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Transfer station Page code import failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("导入兑换码失败", status_code=500)

    async def delete_code(self, code_id: str):
        try:
            parsed_id = int(code_id)
        except (TypeError, ValueError):
            return error_response("兑换码 ID 无效")
        return await self._delete_code(parsed_id)

    async def delete_code_bridge(self):
        try:
            payload = await self._payload()
            code_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            return error_response("兑换码 ID 无效")
        return await self._delete_code(code_id)

    async def _delete_code(self, code_id: int):
        if code_id <= 0:
            return error_response("兑换码 ID 无效")
        try:
            deleted = await self.storage.delete_code(code_id)
            if not deleted:
                return error_response("兑换码不存在、已领取或正在人工核查")
            return json_response(
                {
                    "deleted_id": code_id,
                    "message": "兑换码已删除",
                }
            )
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Transfer station Page code delete failed id=%s error_type=%s",
                code_id,
                type(exc).__name__,
            )
            return error_response("删除兑换码失败", status_code=500)

    async def get_claims(self):
        try:
            page, page_size = self._pagination()
            return json_response(await self.storage.list_claims(page, page_size))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Transfer station Page claim list failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取领取记录失败", status_code=500)

    async def get_gift_reviews(self):
        try:
            page, page_size = self._pagination()
            return json_response(await self.storage.list_gift_reviews(page, page_size))
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Transfer station Page gift review list failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("读取待核查发放记录失败", status_code=500)

    async def resolve_gift_review(self):
        try:
            payload = await self._payload()
            reservation_id = int(payload.get("id", 0))
            delivered = payload.get("delivered")
            if reservation_id <= 0 or not isinstance(delivered, bool):
                raise ValueError("核查记录 ID 或结果无效")
            resolved = await self.storage.review_gift_delivery(
                reservation_id,
                delivered=delivered,
            )
            if not resolved:
                return error_response("待核查记录不存在或已处理")
            return json_response(
                {
                    "resolved_id": reservation_id,
                    "delivered": delivered,
                    "message": (
                        "已确认送达并完成领取记账。"
                        if delivered
                        else "已确认未送达，兑换码已退回库存。"
                    ),
                }
            )
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))
        except Exception as exc:  # noqa: BLE001 - Web API boundary
            logger.exception(
                "Transfer station Page gift review resolve failed error_type=%s",
                type(exc).__name__,
            )
            return error_response("处理待核查发放记录失败", status_code=500)
