from __future__ import annotations

import pytest
from conftest import load_plugin_module
from quart import Quart

page_module = load_plugin_module("page_api")
storage_module = load_plugin_module("storage")
GiftPageApi = page_module.GiftPageApi
GiftStorage = storage_module.GiftStorage


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler, methods, description))


@pytest.mark.asyncio
async def test_page_routes_and_inventory_operations(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    context = FakeContext()
    api = GiftPageApi(context, storage)
    api.register_routes()
    app = Quart(__name__)

    route_methods = {(route, tuple(methods)) for route, _, methods, _ in context.routes}
    assert (
        "/astrbot_plugin_transfer_station/codes/<int:code_id>",
        ("DELETE",),
    ) in route_methods
    assert (
        "/astrbot_plugin_transfer_station/codes/delete",
        ("POST",),
    ) in route_methods

    async with app.test_request_context(
        "/codes/import",
        method="POST",
        json={"content": "A\nB\nA\n"},
    ):
        imported = await api.import_codes()
    assert imported["status"] == "ok"
    assert imported["data"]["inserted"] == 2
    assert imported["data"]["duplicates"] == 1

    async with app.test_request_context("/codes?page=1&page_size=20"):
        codes = await api.get_codes()
    assert codes["data"]["total"] == 2
    code_id = codes["data"]["items"][0]["id"]

    async with app.test_request_context(
        "/codes/delete",
        method="POST",
        json={"id": code_id},
    ):
        deleted = await api.delete_code_bridge()
    assert deleted["status"] == "ok"

    async with app.test_request_context("/codes/2", method="DELETE"):
        deleted_direct = await api.delete_code(2)
    assert deleted_direct["status"] == "ok"

    async with app.test_request_context("/summary"):
        summary = await api.get_summary()
    assert summary["data"]["available_codes"] == 0


@pytest.mark.asyncio
async def test_page_rejects_invalid_payload_and_pagination(tmp_path):
    api = GiftPageApi(FakeContext(), GiftStorage(tmp_path / "gifts.db"))
    app = Quart(__name__)

    async with app.test_request_context(
        "/codes/import",
        method="POST",
        json={"content": ""},
    ):
        empty = await api.import_codes()
    assert empty["status"] == "error"

    async with app.test_request_context("/codes?page=0&page_size=1000"):
        invalid_page = await api.get_codes()
    assert invalid_page["status"] == "error"
