from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.dashboard.asgi_runtime import _convert_rule
from conftest import load_plugin_module
from starlette.requests import Request
from starlette.routing import compile_path

page_module = load_plugin_module("page_api")
storage_module = load_plugin_module("storage")
GiftPageApi = page_module.GiftPageApi
GiftStorage = storage_module.GiftStorage


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler, methods, description))


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def request_context(
    method: str,
    path: str,
    *,
    query: dict | None = None,
    payload: dict | None = None,
):
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
    query_string = urlencode(query or {}).encode("ascii")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = []
    if payload is not None:
        headers.append((b"content-type", b"application/json"))
    raw_request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )
    return bind_request_context(
        PluginRequest(
            raw_request,
            plugin_name="astrbot_plugin_transfer_station",
            username="admin",
        )
    )


@pytest.mark.asyncio
async def test_page_routes_and_inventory_operations(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    context = FakeContext()
    api = GiftPageApi(context, storage)
    api.register_routes()
    await storage.record_group_baseline("100", ["200"])
    assert await storage.register_newcomer("100", "201") == "eligible"

    route_methods = {(route, tuple(methods)) for route, _, methods, _ in context.routes}
    assert (
        "/astrbot_plugin_transfer_station/codes/<code_id>",
        ("DELETE",),
    ) in route_methods
    converted_route = _convert_rule("/astrbot_plugin_transfer_station/codes/<code_id>")
    path_regex, _, _ = compile_path(converted_route)
    assert path_regex.match("/astrbot_plugin_transfer_station/codes/123")
    assert (
        "/astrbot_plugin_transfer_station/codes/delete",
        ("POST",),
    ) in route_methods
    assert (
        "/astrbot_plugin_transfer_station/gift-reviews/resolve",
        ("POST",),
    ) in route_methods

    with request_context(
        "POST",
        "/codes/import",
        payload={"content": "A\nB\nA\n"},
    ):
        imported = response_json(await api.import_codes())
    assert imported["inserted"] == 2
    assert imported["duplicates"] == 1

    with request_context(
        "GET",
        "/codes",
        query={"page": 1, "page_size": 20},
    ):
        codes = response_json(await api.get_codes())
    assert codes["total"] == 2
    code_id = codes["items"][0]["id"]

    with request_context(
        "POST",
        "/codes/delete",
        payload={"id": code_id},
    ):
        deleted = await api.delete_code_bridge()
    assert deleted.status_code == 200

    with request_context("DELETE", "/codes/2"):
        deleted_direct = await api.delete_code("2")
    assert deleted_direct.status_code == 200

    with request_context("GET", "/summary"):
        summary = response_json(await api.get_summary())
    assert summary["available_codes"] == 0
    assert summary["known_users"] == 2
    assert summary["today_newcomers"] == 1
    assert summary["gift_manual_reviews"] == 0


@pytest.mark.asyncio
async def test_page_rejects_invalid_payload_and_pagination(tmp_path):
    api = GiftPageApi(FakeContext(), GiftStorage(tmp_path / "gifts.db"))

    with request_context("POST", "/codes/import", payload={"content": ""}):
        empty = await api.import_codes()
    assert empty.status_code == 400

    with request_context(
        "GET",
        "/codes",
        query={"page": 0, "page_size": 1000},
    ):
        invalid_page = await api.get_codes()
    assert invalid_page.status_code == 400

    with request_context(
        "POST",
        "/codes/import",
        payload={"content": "X" * (1024 * 1024 + 1)},
    ):
        too_large = await api.import_codes()
    assert too_large.status_code == 413


@pytest.mark.asyncio
async def test_page_resolves_ambiguous_gift_delivery(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    api = GiftPageApi(FakeContext(), storage)
    await storage.record_group_baseline("100", [])
    assert await storage.register_newcomer("100", "200") == "eligible"
    await storage.import_codes(["REVIEW-CODE"])

    async def timeout_sender(_code):
        raise TimeoutError("timeout")

    outcome = await storage.claim_code(
        group_id="100",
        user_id="200",
        send_code=timeout_sender,
    )
    assert outcome.status == "send_ambiguous"

    with request_context(
        "GET",
        "/gift-reviews",
        query={"page": 1, "page_size": 20},
    ):
        reviews = response_json(await api.get_gift_reviews())
    assert reviews["total"] == 1

    with request_context(
        "POST",
        "/gift-reviews/resolve",
        payload={"id": reviews["items"][0]["id"], "delivered": False},
    ):
        resolved = response_json(await api.resolve_gift_review())
    assert resolved["delivered"] is False
    assert (await storage.summary())["available_codes"] == 1
