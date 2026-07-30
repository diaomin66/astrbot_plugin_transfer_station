from __future__ import annotations

import asyncio
import sqlite3

import pytest
from conftest import load_plugin_module

storage_module = load_plugin_module("storage")
GiftStorage = storage_module.GiftStorage


async def register_newcomer(
    storage: GiftStorage,
    group_id: str,
    user_id: str,
) -> None:
    await storage.record_group_baseline(group_id, [])
    assert await storage.register_newcomer(group_id, user_id) == "eligible"


@pytest.mark.asyncio
async def test_import_summary_and_persistence(tmp_path):
    db_path = tmp_path / "gifts.db"
    storage = GiftStorage(db_path)

    baseline = await storage.record_group_baseline("100", ["101", "102"])
    assert baseline == {"created": True, "members": 2, "inserted_users": 2}
    assert await storage.register_newcomer("100", "200") == "eligible"
    assert await storage.register_newcomer("100", "200") == "known"
    result = await storage.import_codes([" CODE-A ", "", "CODE-A", "CODE-B"])

    assert result == {"received": 3, "inserted": 2, "duplicates": 1}
    assert await storage.summary() == {
        "available_codes": 2,
        "claimed_users": 0,
        "eligible_members": 1,
        "pending_newcomers": 1,
        "known_users": 3,
        "today_newcomers": 1,
        "gift_manual_reviews": 0,
    }

    reloaded = GiftStorage(db_path)
    assert await reloaded.is_eligible("100", "200") is True
    assert await reloaded.is_group_baselined("100") is True
    assert await reloaded.register_newcomer("100", "101") == "known"
    refreshed = await reloaded.record_group_baseline("100", ["101", "102", "103"])
    assert refreshed == {"created": False, "members": 3, "inserted_users": 1}
    assert await reloaded.register_newcomer("100", "103") == "known"
    assert await reloaded.is_eligible("100", "200") is True
    assert (await reloaded.list_codes(1, 20))["total"] == 2


@pytest.mark.asyncio
async def test_successful_claim_consumes_code_and_is_global(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await register_newcomer(storage, "100", "200")
    await storage.record_group_baseline("101", [])
    assert await storage.register_newcomer("101", "200") == "known"
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
    assert await storage.is_eligible("100", "200") is False
    assert await storage.register_newcomer("100", "200") == "known"
    assert (await storage.summary())["known_users"] == 1
    assert sent == ["SECRET-CODE-A"]
    assert (await storage.list_codes(1, 20))["items"][0]["code"] == "SECRET-CODE-B"
    claims = await storage.list_claims(1, 20)
    assert claims["items"][0]["code_suffix"] == "DE-A"
    assert "SECRET-CODE-A" not in str(claims)


@pytest.mark.asyncio
async def test_failed_delivery_rolls_back_inventory_and_claim(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await register_newcomer(storage, "100", "200")
    await storage.import_codes(["ROLLBACK-CODE"])

    async def sender(_code: str) -> None:
        raise TimeoutError("NapCat timeout")

    outcome = await storage.claim_code(
        group_id="100",
        user_id="200",
        send_code=sender,
    )

    assert outcome.status == "send_ambiguous"
    assert outcome.error_type == "TimeoutError"
    assert (await storage.list_codes(1, 20))["total"] == 0
    assert (await storage.list_claims(1, 20))["total"] == 0
    reviews = await storage.list_gift_reviews(1, 20)
    assert reviews["total"] == 1
    assert await storage.review_gift_delivery(
        reviews["items"][0]["id"],
        delivered=False,
    )
    assert (await storage.list_codes(1, 20))["items"][0]["code"] == "ROLLBACK-CODE"


@pytest.mark.asyncio
async def test_clear_delivery_failure_does_not_claim_false_rollback_after_recovery(
    tmp_path,
):
    class ActionFailed(RuntimeError):
        retcode = 1200

    storage = GiftStorage(tmp_path / "gifts.db")
    await register_newcomer(storage, "100", "200")
    await storage.import_codes(["FROZEN-CODE"])

    async def sender(_code: str) -> None:
        await storage.recover_reserved(stale_before="9999-12-31T23:59:59+00:00")
        raise ActionFailed("rejected")

    outcome = await storage.claim_code(
        group_id="100",
        user_id="200",
        send_code=sender,
    )

    assert outcome.status == "send_ambiguous"
    assert (await storage.summary())["gift_manual_reviews"] == 1
    assert (await storage.summary())["available_codes"] == 0


@pytest.mark.asyncio
async def test_ineligible_and_empty_inventory_results(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await storage.record_group_baseline("100", ["200"])

    async def sender(_code: str) -> None:
        raise AssertionError("must not send")

    assert (
        await storage.claim_code(
            group_id="100",
            user_id="200",
            send_code=sender,
        )
    ).status == "not_eligible"

    assert await storage.register_newcomer("100", "201") == "eligible"
    assert (
        await storage.claim_code(
            group_id="100",
            user_id="201",
            send_code=sender,
        )
    ).status == "no_codes"


@pytest.mark.asyncio
async def test_concurrent_claims_cannot_share_last_code(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await storage.record_group_baseline("100", [])
    assert await storage.register_newcomer("100", "201") == "eligible"
    assert await storage.register_newcomer("100", "202") == "eligible"
    await storage.import_codes(["LAST-CODE"])
    sent: list[tuple[str, str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def claim(user_id: str):
        async def sender(code: str) -> None:
            entered.set()
            await release.wait()
            sent.append((user_id, code))

        return await storage.claim_code(
            group_id="100",
            user_id=user_id,
            send_code=sender,
        )

    first_task = asyncio.create_task(claim("201"))
    await entered.wait()
    second_outcome = await claim("202")
    release.set()
    outcomes = [await first_task, second_outcome]

    assert sorted(outcome.status for outcome in outcomes) == ["no_codes", "success"]
    assert len(sent) == 1
    assert (await storage.summary())["available_codes"] == 0


@pytest.mark.asyncio
async def test_two_storage_instances_cannot_share_last_code(tmp_path):
    db_path = tmp_path / "gifts.db"
    first = GiftStorage(db_path)
    second = GiftStorage(db_path)
    await first.record_group_baseline("100", [])
    assert await first.register_newcomer("100", "201") == "eligible"
    assert await first.register_newcomer("100", "202") == "eligible"
    await first.import_codes(["LAST-CROSS-INSTANCE"])
    sent: list[tuple[str, str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def claim(storage: GiftStorage, user_id: str):
        async def sender(code: str) -> None:
            entered.set()
            await release.wait()
            sent.append((user_id, code))

        return await storage.claim_code(
            group_id="100",
            user_id=user_id,
            send_code=sender,
        )

    first_task = asyncio.create_task(claim(first, "201"))
    await entered.wait()
    second_outcome = await claim(second, "202")
    release.set()
    outcomes = [await first_task, second_outcome]
    assert sorted(outcome.status for outcome in outcomes) == ["no_codes", "success"]
    assert len(sent) == 1
    assert sent[0][1] == "LAST-CROSS-INSTANCE"


@pytest.mark.asyncio
async def test_baseline_and_known_users_cannot_gain_new_eligibility(tmp_path):
    storage = GiftStorage(tmp_path / "gifts.db")
    await storage.record_group_baseline("100", ["200"])

    assert await storage.register_newcomer("100", "200") == "known"
    assert await storage.register_newcomer("101", "200") == "baseline_pending"
    await storage.record_group_baseline("101", [])
    assert await storage.register_newcomer("101", "200") == "known"
    assert await storage.is_eligible("100", "200") is False
    assert await storage.is_eligible("101", "200") is False


@pytest.mark.asyncio
async def test_schema_v1_migrates_legacy_ids_as_permanently_known(tmp_path):
    db_path = tmp_path / "gifts.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE eligible_members (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id)
            );
            CREATE TABLE gift_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE claims (
                user_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                code_suffix TEXT NOT NULL,
                code_digest TEXT NOT NULL,
                claimed_at TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1');
            INSERT INTO eligible_members(group_id, user_id, joined_at)
            VALUES ('100', '200', '2026-07-28T00:00:00+00:00');
            INSERT INTO claims(user_id, group_id, code_suffix, code_digest, claimed_at)
            VALUES ('201', '100', 'CODE', 'digest', '2026-07-28T01:00:00+00:00');
            PRAGMA user_version = 1;
            """
        )

    storage = GiftStorage(db_path)
    await storage.initialize()
    await storage.record_group_baseline("100", [])

    assert (await storage.summary())["known_users"] == 2
    assert await storage.register_newcomer("100", "200") == "known"
    assert await storage.register_newcomer("100", "201") == "known"
    assert await storage.is_eligible("100", "200") is False


@pytest.mark.asyncio
async def test_schema_v2_migrates_reserved_delivery_to_manual_review(tmp_path):
    db_path = tmp_path / "gifts.db"
    storage = GiftStorage(db_path)
    await register_newcomer(storage, "100", "200")
    await storage.import_codes(["CRASH-CODE"])
    reservation = await storage._reserve_claim("100", "200")
    assert isinstance(reservation, dict)

    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 2")
        db.execute(
            """
            INSERT INTO schema_meta(key, value)
            VALUES ('schema_version', '2')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """
        )

    reloaded = GiftStorage(db_path)
    await reloaded.initialize()
    await reloaded.recover_reserved(stale_before="9999-12-31T23:59:59+00:00")
    reviews = await reloaded.list_gift_reviews(1, 20)
    assert reviews["total"] == 1
    assert reviews["items"][0]["error_type"] == "ProcessRestarted"

    async def must_not_send(_code: str) -> None:
        raise AssertionError("reserved delivery must remain frozen")

    outcome = await reloaded.claim_code(
        group_id="100",
        user_id="200",
        send_code=must_not_send,
    )
    assert outcome.status == "send_ambiguous"


@pytest.mark.asyncio
async def test_future_database_version_is_rejected(tmp_path):
    db_path = tmp_path / "future.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="版本高于"):
        await GiftStorage(db_path).initialize()
