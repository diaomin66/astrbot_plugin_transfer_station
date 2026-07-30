from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import load_plugin_module

module = load_plugin_module("newapi_client")
NewApiClient = module.NewApiClient
NewApiError = module.NewApiError
QuotaSnapshot = module.QuotaSnapshot


def config(**overrides):
    result = {
        "newapi_base_url": "https://newapi.example",
        "newapi_access_token": "admin-token",
        "newapi_timeout_seconds": 10,
        "newapi_verify_ssl": True,
        "newapi_allow_insecure_http": False,
    }
    result.update(overrides)
    return result


def async_client(handler):
    return httpx.AsyncClient(
        base_url="https://newapi.example",
        transport=httpx.MockTransport(handler),
    )


def response(data=None, *, success=True, status=200, message=""):
    return httpx.Response(
        status,
        json={
            "success": success,
            "message": message,
            "data": {} if data is None else data,
        },
    )


def test_quota_conversion_for_all_display_types():
    base = {
        "quota_per_unit": 500000,
        "usd_exchange_rate": 7.2,
        "custom_currency_exchange_rate": 2.5,
    }
    assert (
        QuotaSnapshot.from_status(
            {**base, "quota_display_type": "USD"}
        ).amount_to_quota("2.5")
        == 1250000
    )
    assert (
        QuotaSnapshot.from_status(
            {**base, "quota_display_type": "CNY"}
        ).amount_to_quota("7.2")
        == 500000
    )
    assert (
        QuotaSnapshot.from_status(
            {**base, "quota_display_type": "CUSTOM"}
        ).amount_to_quota("2.5")
        == 500000
    )
    assert (
        QuotaSnapshot.from_status(
            {**base, "quota_display_type": "TOKENS"}
        ).amount_to_quota("12.5")
        == 13
    )


@pytest.mark.asyncio
async def test_token_auth_user_lookup_and_atomic_add_quota():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer admin-token"
        if request.method == "GET":
            return response({"id": 123, "username": "alice", "status": 1})
        return response()

    client = NewApiClient(config(), client=async_client(handler))
    user = await client.get_user(123)
    await client.add_quota(123, 500000)

    assert user.username == "alice"
    assert requests[1].url.path == "/api/user/manage"
    assert requests[1].read().decode() == (
        '{"id":123,"action":"add_quota","mode":"add","value":500000}'
    )
    await client.close()


@pytest.mark.asyncio
async def test_password_login_and_2fa_rejection():
    def handler(request: httpx.Request):
        assert request.url.path == "/api/user/login"
        return response({"require_2fa": True, "flow_token": "secret"})

    client = NewApiClient(
        config(
            newapi_access_token="",
            newapi_username="root",
            newapi_password="password",
        ),
        client=async_client(handler),
    )
    with pytest.raises(NewApiError, match="2FA") as exc:
        await client.get_user(1)
    assert exc.value.kind == "2fa"
    await client.close()


@pytest.mark.asyncio
async def test_password_login_uses_returned_access_token():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/api/user/login":
            assert "authorization" not in request.headers
            return response({"access_token": "session-token"})
        assert request.headers["authorization"] == "Bearer session-token"
        return response({"id": 9, "username": "bob", "status": 1})

    client = NewApiClient(
        config(
            newapi_access_token="",
            newapi_username="root",
            newapi_password="password",
        ),
        client=async_client(handler),
    )
    user = await client.get_user(9)
    assert user.username == "bob"
    assert [request.url.path for request in requests] == [
        "/api/user/login",
        "/api/user/9",
    ]
    await client.close()


@pytest.mark.asyncio
async def test_test_connection_checks_status_identity_and_admin_route():
    def handler(request: httpx.Request):
        if request.url.path == "/api/status":
            return response(
                {
                    "version": "0.6.0",
                    "quota_display_type": "USD",
                    "quota_per_unit": 500000,
                    "usd_exchange_rate": 7.2,
                    "custom_currency_exchange_rate": 1,
                }
            )
        if request.url.path == "/api/user/self":
            return response({"username": "root", "role": 100})
        if request.url.path == "/api/user/":
            return response({"items": []})
        raise AssertionError(request.url)

    client = NewApiClient(config(), client=async_client(handler))
    result = await client.test_connection()
    assert result.username == "root"
    assert result.display_type == "USD"
    await client.close()


@pytest.mark.asyncio
async def test_test_connection_rejects_insufficient_permission():
    def handler(request: httpx.Request):
        if request.url.path == "/api/status":
            return response(
                {
                    "quota_display_type": "USD",
                    "quota_per_unit": 500000,
                    "usd_exchange_rate": 7.2,
                    "custom_currency_exchange_rate": 1,
                }
            )
        if request.url.path == "/api/user/self":
            return response({"username": "member", "role": 1})
        return response(
            status=403,
            success=False,
            message="insufficient privilege",
        )

    client = NewApiClient(config(), client=async_client(handler))
    with pytest.raises(NewApiError, match="insufficient privilege"):
        await client.test_connection()
    await client.close()


def test_url_validation_and_insecure_http_gate():
    with pytest.raises(NewApiError, match="HTTPS"):
        NewApiClient(config(newapi_base_url="http://newapi.example"))
    client = NewApiClient(
        config(
            newapi_base_url="http://127.0.0.1:3000",
            newapi_allow_insecure_http=True,
        ),
        client=httpx.AsyncClient(base_url="http://127.0.0.1:3000"),
    )
    assert client.base_url == "http://127.0.0.1:3000"


@pytest.mark.asyncio
async def test_write_timeout_and_server_failure_are_ambiguous():
    def timeout(_request: httpx.Request):
        raise httpx.ReadTimeout("timeout")

    client = NewApiClient(config(), client=async_client(timeout))
    with pytest.raises(NewApiError) as exc:
        await client.add_quota(1, 1)
    assert exc.value.ambiguous is True
    await client.close()

    client = NewApiClient(
        config(),
        client=async_client(lambda _request: response(status=503)),
    )
    with pytest.raises(NewApiError) as exc:
        await client.add_quota(1, 1)
    assert exc.value.ambiguous is True
    await client.close()


def test_round_half_up_is_used():
    snapshot = QuotaSnapshot(
        display_type="USD",
        quota_per_unit=Decimal(1),
        usd_exchange_rate=Decimal(1),
        custom_currency_exchange_rate=Decimal(1),
    )
    assert snapshot.amount_to_quota("1.5") == 2
