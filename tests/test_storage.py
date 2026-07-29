from __future__ import annotations

import asyncio

import pytest
from conftest import load_plugin_module

storage_module = load_plugin_module("storage")
GiftStorage = storage_module.GiftStorage


@pytest.mark.asyncio
async def test_import_summary_and_persistence(tmp_path):
    db_path = tmp_path / "gifts.db"
    storage = GiftStorage(db_path)

    assert await storage.add_eligible("100", "200") is True
    assert await storage.add_eligible("100", "200") is False
    result = await storage.import_codes([" CODE-A ", "", "CODE-A", "CODE-B"])

    assert result == {"received": 3, "inserted": 2, "duplicates": 1}
    assert await storage.summary() == {
        "available_codes": 2,
        "claimed_users": 0,
        "eligible_members": 1,
        "pending_newcomers": 1,
    }

    reloaded = GiftStorage(db_path)
    assert await reloaded.is_eligible("100", "200") is True
    assert (await reloaded.list_codes(1, 20))["total"] == 2


@pytest.mark.asyncio
async def test_successful_claim_consumes_code_and_is_global(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await storage.add_eligible("100", "200")
    await storage.add_eligible("101", "200")
    await storage.import_codes(["SECRET-CODE-A", "SECRET-CODE-B"])
    sent: list[str] = []

    async def sender(code: str) -> None:
        sent.append(code)

    outcome = await storage.claim_code(
        group_id="100",
        user_id="200",
        send_code=sender,
    )
    second = await storage.claim_code(
        group_id="101",
        user_id="200",
        send_code=sender,
    )

    assert outcome.status == "success"
    assert second.status == "already_claimed"
    assert sent == ["SECRET-CODE-A"]
    assert (await storage.list_codes(1, 20))["items"][0]["code"] == "SECRET-CODE-B"
    claims = await storage.list_claims(1, 20)
    assert claims["items"][0]["code_suffix"] == "DE-A"
    assert "SECRET-CODE-A" not in str(claims)


@pytest.mark.asyncio
async def test_failed_delivery_rolls_back_inventory_and_claim(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await storage.add_eligible("100", "200")
    await storage.import_codes(["ROLLBACK-CODE"])

    async def sender(_code: str) -> None:
        raise TimeoutError("NapCat timeout")

    outcome = await storage.claim_code(
        group_id="100",
        user_id="200",
        send_code=sender,
    )

    assert outcome.status == "send_failed"
    assert outcome.error_type == "TimeoutError"
    assert (await storage.list_codes(1, 20))["items"][0]["code"] == "ROLLBACK-CODE"
    assert (await storage.list_claims(1, 20))["total"] == 0


@pytest.mark.asyncio
async def test_ineligible_and_empty_inventory_results(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")

    async def sender(_code: str) -> None:
        raise AssertionError("must not send")

    assert (
        await storage.claim_code(
            group_id="100",
            user_id="200",
            send_code=sender,
        )
    ).status == "not_eligible"

    await storage.add_eligible("100", "200")
    assert (
        await storage.claim_code(
            group_id="100",
            user_id="200",
            send_code=sender,
        )
    ).status == "no_codes"


@pytest.mark.asyncio
async def test_concurrent_claims_cannot_share_last_code(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await storage.add_eligible("100", "201")
    await storage.add_eligible("100", "202")
    await storage.import_codes(["LAST-CODE"])
    sent: list[tuple[str, str]] = []

    async def claim(user_id: str):
        async def sender(code: str) -> None:
            await asyncio.sleep(0.01)
            sent.append((user_id, code))

        return await storage.claim_code(
            group_id="100",
            user_id=user_id,
            send_code=sender,
        )

    outcomes = await asyncio.gather(claim("201"), claim("202"))

    assert sorted(outcome.status for outcome in outcomes) == ["no_codes", "success"]
    assert len(sent) == 1
    assert (await storage.summary())["available_codes"] == 0
