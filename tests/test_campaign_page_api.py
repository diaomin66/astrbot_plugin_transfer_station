from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from astrbot.api.web import PluginRequest, bind_request_context
from conftest import load_plugin_module
from starlette.requests import Request

api_module = load_plugin_module("campaign_page_api")
compensation_module = load_plugin_module("compensation")
lottery_module = load_plugin_module("lottery")
newapi_module = load_plugin_module("newapi_client")
utils = load_plugin_module("campaign_utils")

CampaignPageApi = api_module.CampaignPageApi
CompensationService = compensation_module.CompensationService
CompensationStorage = compensation_module.CompensationStorage
LotteryService = lottery_module.LotteryService
LotteryStorage = lottery_module.LotteryStorage


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler, methods, description))


class SavingConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class AsyncSavingConfig(SavingConfig):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.async_save_count = 0

    async def save_config_async(self):
        self.async_save_count += 1

    def save_config(self):
        raise AssertionError("sync save should not be used")


class RefusingConfig(AsyncSavingConfig):
    async def save_config_async(self):
        self.async_save_count += 1
        return False


class BlockingAsyncConfig(AsyncSavingConfig):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_entered = asyncio.Event()
        self.release_save = asyncio.Event()

    async def save_config_async(self):
        self.async_save_count += 1
        self.save_entered.set()
        await self.release_save.wait()


class FakeNewApi:
    async def status_snapshot(self):
        return newapi_module.QuotaSnapshot(
            "USD",
            Decimal(500000),
            Decimal("7.2"),
            Decimal(1),
        )

    async def test_connection(self):
        return newapi_module.NewApiTestResult(
            "0.9.9",
            "root",
            "admin",
            "USD",
        )


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

    raw_request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": (
                [(b"content-type", b"application/json")] if payload is not None else []
            ),
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


def make_api(tmp_path, config=None, context=None):
    lottery_storage = LotteryStorage(tmp_path / "lottery.db")
    compensation_storage = CompensationStorage(tmp_path / "compensation.db")
    fake_newapi = FakeNewApi()
    changed = []
    flushed = []

    async def settings_changed():
        changed.append(True)

    async def flush():
        flushed.append(True)

    async def draw(activity_id):
        activity = await lottery_storage.get_activity(activity_id)
        return await LotteryService(lottery_storage, None).draw(activity, ["200"])

    actual_config = config or SavingConfig(
        {
            "newapi_base_url": "https://newapi.example.com",
            "newapi_access_token": "secret-token",
            "newapi_user_id": "7",
            "newapi_username": "root",
            "newapi_password": "secret-password",
            "newapi_timeout_seconds": 10,
            "newapi_verify_ssl": True,
            "newapi_allow_insecure_http": False,
            "lottery_enabled": True,
            "lottery_enabled_group_ids": ["100"],
            "compensation_enabled": True,
            "compensation_enabled_group_ids": ["100"],
        }
    )
    actual_context = context or FakeContext()
    api = CampaignPageApi(
        actual_context,
        actual_config,
        lottery_storage,
        compensation_storage,
        lottery_service=lambda required: LotteryService(
            lottery_storage,
            fake_newapi if required else None,
        ),
        compensation_service=lambda required: CompensationService(
            compensation_storage,
            fake_newapi if required else None,
        ),
        newapi_client=lambda: fake_newapi,
        render_action=lambda result: result.key,
        settings_changed=settings_changed,
        draw_lottery=draw,
        flush_notifications=flush,
    )
    api.register_routes()
    return (
        api,
        actual_context,
        actual_config,
        lottery_storage,
        compensation_storage,
        changed,
        flushed,
    )


def editable_settings(base_url: str) -> dict:
    return {
        "newapi_base_url": base_url,
        "newapi_user_id": "7",
        "newapi_timeout_seconds": 10,
        "newapi_verify_ssl": True,
        "newapi_allow_insecure_http": False,
        "lottery_enabled": True,
        "lottery_enabled_group_ids": ["100"],
        "compensation_enabled": True,
        "compensation_enabled_group_ids": ["100"],
    }


@pytest.mark.asyncio
async def test_campaign_page_routes_settings_and_secret_redaction(tmp_path):
    api, context, config, *_rest = make_api(tmp_path)
    route_methods = {(route, tuple(methods)) for route, _, methods, _ in context.routes}
    assert route_methods == {
        (
            f"/astrbot_plugin_transfer_station/{endpoint}",
            (method,),
        )
        for endpoint, method in {
            "campaigns/summary": "GET",
            "campaigns/settings": "GET",
            "campaigns/settings/save": "POST",
            "campaigns/newapi/test": "POST",
            "campaigns/lotteries": "GET",
            "campaigns/lotteries/detail": "GET",
            "campaigns/lotteries/create": "POST",
            "campaigns/lotteries/update": "POST",
            "campaigns/lotteries/prizes/add": "POST",
            "campaigns/lotteries/prizes/delete": "POST",
            "campaigns/lotteries/publish": "POST",
            "campaigns/lotteries/draw": "POST",
            "campaigns/lotteries/cancel": "POST",
            "campaigns/lotteries/review": "POST",
            "campaigns/compensations": "GET",
            "campaigns/compensations/detail": "GET",
            "campaigns/compensations/open": "POST",
            "campaigns/compensations/close": "POST",
            "campaigns/compensations/review": "POST",
        }.items()
    }
    assert (
        "/astrbot_plugin_transfer_station/campaigns/lotteries/create",
        ("POST",),
    ) in route_methods
    assert (
        "/astrbot_plugin_transfer_station/campaigns/compensations/review",
        ("POST",),
    ) in route_methods

    settings = response_json(await api.get_settings())
    assert settings["newapi_access_token_configured"] is True
    assert settings["newapi_password_configured"] is True
    assert settings["newapi_user_id"] == "7"
    assert "newapi_access_token" not in settings
    assert "newapi_password" not in settings
    revision = settings["revision"]
    config["newapi_access_token"] = "rotated-secret-token"
    config["newapi_password"] = "rotated-secret-password"
    assert response_json(await api.get_settings())["revision"] == revision

    with request_context(
        "POST",
        "/campaigns/settings/save",
        payload={
            "revision": revision,
            "settings": {
                "newapi_base_url": "https://new.example.com",
                "newapi_user_id": "8",
                "newapi_timeout_seconds": 15,
                "newapi_verify_ssl": True,
                "newapi_allow_insecure_http": False,
                "lottery_enabled": True,
                "lottery_enabled_group_ids": ["100", "100"],
                "compensation_enabled": False,
                "compensation_enabled_group_ids": [],
            },
        },
    ):
        saved = response_json(await api.save_settings())
    assert saved["settings"]["newapi_base_url"] == "https://new.example.com"
    assert saved["settings"]["newapi_user_id"] == "8"
    assert config["newapi_user_id"] == "8"
    assert config["lottery_enabled_group_ids"] == ["100"]
    assert config.save_count == 1
    assert _rest[-2] == [True]

    with request_context(
        "POST",
        "/campaigns/settings/save",
        payload={"revision": revision, "settings": {}},
    ):
        conflict = await api.save_settings()
    assert conflict.status_code == 409
    assert "newapi_access_token" not in conflict.body.decode("utf-8")


@pytest.mark.asyncio
async def test_latest_page_instance_rejects_stale_hot_reload_writer(tmp_path):
    context = FakeContext()
    first_config = SavingConfig(editable_settings("https://old.example.com"))
    second_config = SavingConfig(editable_settings("https://old.example.com"))
    first_api, *_ = make_api(tmp_path, first_config, context)
    second_api, *_ = make_api(tmp_path, second_config, context)
    await second_api.activate()
    revision = response_json(await second_api.get_settings())["revision"]

    async def save(api, base_url):
        with request_context(
            "POST",
            "/campaigns/settings/save",
            payload={
                "revision": revision,
                "settings": editable_settings(base_url),
            },
        ):
            return await api.save_settings()

    old_response, current_response = await asyncio.gather(
        save(first_api, "https://stale.example.com"),
        save(second_api, "https://current.example.com"),
    )

    assert old_response.status_code == 409
    assert current_response.status_code == 200
    assert first_config["newapi_base_url"] == "https://old.example.com"
    assert second_config["newapi_base_url"] == "https://current.example.com"


@pytest.mark.asyncio
async def test_shutdown_waits_for_settings_persistence_and_rejects_later_saves(
    tmp_path,
):
    config = BlockingAsyncConfig(editable_settings("https://old.example.com"))
    api, *_ = make_api(tmp_path, config)
    revision = response_json(await api.get_settings())["revision"]

    async def save(base_url):
        with request_context(
            "POST",
            "/campaigns/settings/save",
            payload={
                "revision": revision,
                "settings": editable_settings(base_url),
            },
        ):
            return await api.save_settings()

    save_task = asyncio.create_task(save("https://saved.example.com"))
    await config.save_entered.wait()
    api.begin_shutdown()
    idle_task = asyncio.create_task(api.wait_for_settings_idle())
    await asyncio.sleep(0)
    assert idle_task.done() is False

    config.release_save.set()
    saved = await save_task
    await idle_task
    assert saved.status_code == 200
    assert config["newapi_base_url"] == "https://saved.example.com"

    rejected = await save("https://rejected.example.com")
    assert rejected.status_code == 503
    assert config["newapi_base_url"] == "https://saved.example.com"


@pytest.mark.asyncio
async def test_campaign_page_lottery_crud_publish_draw_and_detail(tmp_path):
    (
        api,
        _context,
        _config,
        lottery_storage,
        _compensation_storage,
        _changed,
        flushed,
    ) = make_api(tmp_path)

    with request_context(
        "POST",
        "/campaigns/lotteries/create",
        payload={"group_id": "100", "title": "夏日抽奖"},
    ):
        created = response_json(await api.create_lottery())
    activity_id = created["placeholders"]["activity_id"]

    with request_context(
        "GET",
        "/campaigns/lotteries/detail",
        query={"activity_id": activity_id, "page": 1, "page_size": 20},
    ):
        draft = response_json(await api.get_lottery_detail())
    assert isinstance(draft["activity"]["id"], str)
    assert isinstance(draft["activity"]["revision"], str)

    with request_context(
        "POST",
        "/campaigns/lotteries/update",
        payload={
            "activity_id": activity_id,
            "revision": draft["activity"]["revision"],
            "title": "夏日抽奖",
            "description": "面向测试群",
            "keyword": "参加夏日抽奖",
            "start_time": "now",
            "draw_time": "+1h",
            "claim_duration": "24h",
        },
    ):
        updated = await api.update_lottery()
    assert updated.status_code == 200

    activity = await lottery_storage.get_activity(int(activity_id))
    with request_context(
        "POST",
        "/campaigns/lotteries/prizes/add",
        payload={
            "activity_id": activity_id,
            "revision": str(activity["revision"]),
            "name": "一等奖",
            "winner_count": 1,
            "amount": "10",
        },
    ):
        added = await api.add_lottery_prize()
    assert added.status_code == 200

    activity = await lottery_storage.get_activity(int(activity_id))
    with request_context(
        "POST",
        "/campaigns/lotteries/publish",
        payload={
            "activity_id": activity_id,
            "revision": str(activity["revision"]),
        },
    ):
        published = await api.publish_lottery()
    assert published.status_code == 200
    assert flushed

    await lottery_storage.register(
        "100",
        "200",
        "参加夏日抽奖",
        now=utils.utc_now(),
    )
    with request_context(
        "POST",
        "/campaigns/lotteries/draw",
        payload={
            "activity_id": activity_id,
            "revision": str(
                (await lottery_storage.get_activity(int(activity_id)))["revision"]
            ),
        },
    ):
        drawn = await api.draw_lottery()
    assert drawn.status_code == 200

    with request_context(
        "GET",
        "/campaigns/lotteries/detail",
        query={"activity_id": activity_id, "page": 1, "page_size": 20},
    ):
        detail = response_json(await api.get_lottery_detail())
    assert detail["activity"]["winner_count"] == 1
    assert detail["winners"][0]["user_id"] == "200"
    assert isinstance(detail["winners"][0]["raw_quota"], str)


@pytest.mark.asyncio
async def test_campaign_page_rejects_fractional_and_boolean_integers(tmp_path):
    (
        api,
        _context,
        _config,
        lottery_storage,
        _compensation_storage,
        _changed,
        _flushed,
    ) = make_api(tmp_path)
    with request_context(
        "POST",
        "/campaigns/lotteries/create",
        payload={"group_id": "100", "title": "严格整数"},
    ):
        created = response_json(await api.create_lottery())
    activity_id = created["placeholders"]["activity_id"]
    activity = await lottery_storage.get_activity(int(activity_id))

    with request_context(
        "POST",
        "/campaigns/lotteries/prizes/add",
        payload={
            "activity_id": activity_id,
            "revision": str(activity["revision"]),
            "name": "一等奖",
            "winner_count": 1.9,
            "amount": "10",
        },
    ):
        fractional = await api.add_lottery_prize()
    assert fractional.status_code == 400
    assert await lottery_storage.prizes(int(activity_id)) == []

    with request_context(
        "POST",
        "/campaigns/lotteries/cancel",
        payload={
            "activity_id": True,
            "revision": str(activity["revision"]),
            "reason": "非法 ID",
        },
    ):
        boolean_id = await api.cancel_lottery()
    assert boolean_id.status_code == 400
    assert (await lottery_storage.get_activity(int(activity_id)))["status"] == "draft"


@pytest.mark.asyncio
async def test_campaign_page_compensation_open_close_history_and_summary(tmp_path):
    api, *_ = make_api(tmp_path)

    with request_context(
        "POST",
        "/campaigns/compensations/open",
        payload={
            "group_id": "100",
            "per_amount": "5",
            "duration": "2h",
            "total_amount": "100",
            "title": "服务补偿",
        },
    ):
        opened = response_json(await api.open_compensation())
    activity_id = opened["placeholders"]["activity_id"]

    with request_context(
        "GET",
        "/campaigns/compensations",
        query={"scope": "active", "page": 1, "page_size": 20},
    ):
        active = response_json(await api.get_compensations())
    assert active["total"] == 1
    assert active["items"][0]["id"] == activity_id
    assert isinstance(active["items"][0]["per_raw_quota"], str)

    with request_context(
        "POST",
        "/campaigns/compensations/close",
        payload={"activity_id": activity_id, "reason": "维护结束"},
    ):
        closed = await api.close_compensation()
    assert closed.status_code == 200

    with request_context(
        "GET",
        "/campaigns/compensations",
        query={"scope": "history", "page": 1, "page_size": 20},
    ):
        history = response_json(await api.get_compensations())
    assert history["total"] == 1
    assert history["items"][0]["status"] == "completed"

    summary = response_json(await api.get_summary())
    assert summary["compensation"]["activity_count"] == 1
    assert isinstance(summary["compensation"]["used_raw_quota"], str)


@pytest.mark.asyncio
async def test_settings_save_reports_runtime_refresh_warning_after_persist(tmp_path):
    api, _context, config, *_ = make_api(tmp_path)

    async def fail_refresh():
        raise RuntimeError("scheduler unavailable")

    api._settings_changed = fail_refresh
    current = response_json(await api.get_settings())
    with request_context(
        "POST",
        "/campaigns/settings/save",
        payload={
            "revision": current["revision"],
            "settings": {
                "newapi_base_url": "https://saved.example.com",
                "newapi_timeout_seconds": 12,
                "newapi_verify_ssl": True,
                "newapi_allow_insecure_http": False,
                "lottery_enabled": True,
                "lottery_enabled_group_ids": ["100"],
                "compensation_enabled": True,
                "compensation_enabled_group_ids": ["100"],
            },
        },
    ):
        response = await api.save_settings()

    assert response.status_code == 200
    body = response_json(response)
    assert "运行时刷新失败" in body["warning"]
    assert config["newapi_base_url"] == "https://saved.example.com"
    assert config.save_count == 1


@pytest.mark.asyncio
async def test_settings_save_prefers_astrbot_async_persistence(tmp_path):
    config = AsyncSavingConfig(
        {
            "newapi_base_url": "https://newapi.example.com",
            "newapi_access_token": "secret-token",
            "newapi_username": "root",
            "newapi_password": "secret-password",
            "newapi_timeout_seconds": 10,
            "newapi_verify_ssl": True,
            "newapi_allow_insecure_http": False,
            "lottery_enabled": True,
            "lottery_enabled_group_ids": ["100"],
            "compensation_enabled": True,
            "compensation_enabled_group_ids": ["100"],
        }
    )
    api, *_ = make_api(tmp_path, config)
    current = response_json(await api.get_settings())
    with request_context(
        "POST",
        "/campaigns/settings/save",
        payload={
            "revision": current["revision"],
            "settings": {
                "newapi_base_url": "https://async.example.com",
                "newapi_timeout_seconds": 11,
                "newapi_verify_ssl": True,
                "newapi_allow_insecure_http": False,
                "lottery_enabled": True,
                "lottery_enabled_group_ids": ["100"],
                "compensation_enabled": True,
                "compensation_enabled_group_ids": ["100"],
            },
        },
    ):
        response = await api.save_settings()

    assert response.status_code == 200
    assert config.async_save_count == 1


@pytest.mark.asyncio
async def test_settings_save_does_not_report_success_when_astrbot_refuses(tmp_path):
    config = RefusingConfig(
        {
            "newapi_base_url": "https://newapi.example.com",
            "newapi_access_token": "secret-token",
            "newapi_username": "root",
            "newapi_password": "secret-password",
            "newapi_timeout_seconds": 10,
            "newapi_verify_ssl": True,
            "newapi_allow_insecure_http": False,
            "lottery_enabled": True,
            "lottery_enabled_group_ids": ["100"],
            "compensation_enabled": True,
            "compensation_enabled_group_ids": ["100"],
        }
    )
    api, *_ = make_api(tmp_path, config)
    current = response_json(await api.get_settings())
    with request_context(
        "POST",
        "/campaigns/settings/save",
        payload={
            "revision": current["revision"],
            "settings": {
                "newapi_base_url": "https://rejected.example.com",
                "newapi_timeout_seconds": 11,
                "newapi_verify_ssl": True,
                "newapi_allow_insecure_http": False,
                "lottery_enabled": True,
                "lottery_enabled_group_ids": ["100"],
                "compensation_enabled": True,
                "compensation_enabled_group_ids": ["100"],
            },
        },
    ):
        response = await api.save_settings()

    assert response.status_code == 500
    assert config["newapi_base_url"] == "https://newapi.example.com"
    assert config.async_save_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            newapi_module.NewApiError("尚未配置 New API 地址", kind="config"),
            "尚未配置 New API 地址",
        ),
        (
            newapi_module.NewApiError("2FA", kind="2fa"),
            "启用了 2FA",
        ),
        (
            newapi_module.NewApiError("secret upstream", status_code=401),
            "认证失败",
        ),
        (
            newapi_module.NewApiError("secret upstream", status_code=403),
            "权限不足",
        ),
        (
            newapi_module.NewApiError("secret upstream", status_code=404),
            "接口不存在",
        ),
    ],
)
async def test_newapi_page_test_returns_safe_error_categories(tmp_path, error, message):
    api, *_ = make_api(tmp_path)

    class FailingNewApi:
        async def test_connection(self):
            raise error

    api._newapi_client = lambda: FailingNewApi()
    response = await api.test_newapi()
    body = response.body.decode("utf-8")

    assert response.status_code == 400
    assert message in body
    assert "secret upstream" not in body


@pytest.mark.asyncio
async def test_settings_reject_invalid_legacy_newapi_user_id(tmp_path):
    api, _context, config, *_ = make_api(tmp_path)
    current = response_json(await api.get_settings())
    values = editable_settings("https://newapi.example.com")
    values["newapi_user_id"] = "7.5"

    with request_context(
        "POST",
        "/campaigns/settings/save",
        payload={
            "revision": current["revision"],
            "settings": values,
        },
    ):
        response = await api.save_settings()

    assert response.status_code == 400
    assert config["newapi_user_id"] == "7"


@pytest.mark.asyncio
async def test_lottery_cancel_requires_current_revision(tmp_path):
    (
        api,
        _context,
        _config,
        lottery_storage,
        _compensation,
        _changed,
        _flushed,
    ) = make_api(tmp_path)
    with request_context(
        "POST",
        "/campaigns/lotteries/create",
        payload={"group_id": "100", "title": "取消版本测试"},
    ):
        created = response_json(await api.create_lottery())
    activity_id = created["placeholders"]["activity_id"]
    activity = await lottery_storage.get_activity(int(activity_id))

    with request_context(
        "POST",
        "/campaigns/lotteries/cancel",
        payload={
            "activity_id": activity_id,
            "revision": str(int(activity["revision"]) + 1),
            "reason": "旧页面",
        },
    ):
        conflict = await api.cancel_lottery()
    assert conflict.status_code == 409

    with request_context(
        "POST",
        "/campaigns/lotteries/cancel",
        payload={
            "activity_id": activity_id,
            "revision": str(activity["revision"]),
            "reason": "管理员取消",
        },
    ):
        cancelled = await api.cancel_lottery()
    assert cancelled.status_code == 200
