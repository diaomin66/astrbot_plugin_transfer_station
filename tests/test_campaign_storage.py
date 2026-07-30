from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from conftest import load_plugin_module

lottery = load_plugin_module("lottery")
compensation = load_plugin_module("compensation")
newapi = load_plugin_module("newapi_client")
utils = load_plugin_module("campaign_utils")

QuotaSnapshot = newapi.QuotaSnapshot


def snap():
    return QuotaSnapshot(
        "USD",
        Decimal(500000),
        Decimal("7.2"),
        Decimal(1),
    )


@pytest.mark.asyncio
async def test_lottery_publish_draw_and_persistent_winner(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "Test", "1", now=now)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    assert await storage.register("100", "200", "参与抽奖", now=now) == "joined"
    assert await storage.register("100", "201", "参与抽奖", now=now) == "joined"
    winners = await storage.draw(int(activity["id"]), ["200"], now=now)
    assert len(winners) == 1
    assert winners[0]["user_id"] == "200"
    reloaded = lottery.LotteryStorage(tmp_path / "lottery.db")
    assert (await reloaded.winners(int(activity["id"])))[0]["user_id"] == "200"


@pytest.mark.asyncio
async def test_lottery_confirmation_success_and_timeout_review(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "Test", "1", now=now)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    await storage.register("100", "200", "参与抽奖", now=now)
    await storage.draw(int(activity["id"]), ["200"], now=now)
    user = newapi.NewApiUser(3, "alice", 1)
    payout = await storage.create_pending_payout("100", "200", user, now=now)
    reserved = await storage.reserve_confirmation("100", "200", now=now)
    assert reserved["serial"] == payout["serial"]
    await storage.finish_payout(payout["serial"], "manual_review", now=now)
    assert (await storage.review("100", payout["serial"], False, now=now))[
        "status"
    ] == "failed"
    assert (await storage.winners(int(activity["id"])))[0]["payout_state"] is None


@pytest.mark.asyncio
async def test_compensation_deduplicates_and_budget_is_atomic(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    activity = await storage.open_activity(
        "100",
        "服务补偿",
        "1",
        "10",
        None,
        "10",
        snap(),
        now=now,
    )
    user = newapi.NewApiUser(3, "alice", 1)
    claim = await storage.create_pending("100", "200", user, now=now)
    with pytest.raises(ValueError, match="duplicate"):
        await storage.create_pending("100", "201", user, now=now)
    await storage.reserve("100", "200", now=now)
    await storage.finish(claim["serial"], "paid", now=now)
    with pytest.raises(ValueError, match="no_active"):
        await storage.create_pending("100", "200", user, now=now)
    assert (await storage.get(int(activity["id"])))["status"] == "completed"


@pytest.mark.asyncio
async def test_compensation_concurrent_last_budget(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    await storage.open_activity(
        "100",
        "服务补偿",
        "1",
        "10",
        None,
        "10",
        snap(),
        now=now,
    )
    user1 = newapi.NewApiUser(3, "a", 1)
    user2 = newapi.NewApiUser(4, "b", 1)
    c1 = await storage.create_pending("100", "200", user1, now=now)
    c2 = await storage.create_pending("100", "201", user2, now=now)

    async def reserve(qq):
        try:
            return await storage.reserve("100", qq, now=now)
        except ValueError as exc:
            return str(exc)

    results = await asyncio.gather(reserve("200"), reserve("201"))
    assert sorted(
        "budget_reserved" if x == "budget_reserved" else "ok" for x in results
    ) == ["budget_reserved", "ok"]
    ok = next(x for x in results if isinstance(x, dict))
    await storage.finish(ok["serial"], "paid", now=now)
    assert c1["serial"] != c2["serial"]


@pytest.mark.asyncio
async def test_clear_failure_releases_last_budget_for_waiting_claim(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    await storage.open_activity(
        "100",
        "服务补偿",
        "1",
        "10",
        None,
        "10",
        snap(),
        now=now,
    )
    first = await storage.create_pending(
        "100",
        "200",
        newapi.NewApiUser(3, "a", 1),
        now=now,
    )
    await storage.create_pending(
        "100",
        "201",
        newapi.NewApiUser(4, "b", 1),
        now=now,
    )
    await storage.reserve("100", "200", now=now)
    with pytest.raises(ValueError, match="budget_reserved"):
        await storage.reserve("100", "201", now=now)

    await storage.finish(first["serial"], "failed", now=now)
    second = await storage.reserve("100", "201", now=now)

    assert second["api_user_id"] == 4
    assert await storage.get_active("100") is not None


@pytest.mark.asyncio
async def test_restart_moves_processing_payouts_to_manual_review(tmp_path):
    lottery_storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await lottery_storage.create_draft("100", "Test", "1", now=now)
    await lottery_storage.add_prize("100", "一等奖", 1, "10")
    await lottery_storage.publish("100", snap(), now=now)
    await lottery_storage.register("100", "200", "参与抽奖", now=now)
    await lottery_storage.draw(int(activity["id"]), ["200"], now=now)
    payout = await lottery_storage.create_pending_payout(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )
    await lottery_storage.reserve_confirmation("100", "200", now=now)

    recovered = await lottery.LotteryStorage(
        tmp_path / "lottery.db"
    ).recover_processing(now=now)

    assert recovered == 1
    reviewed = await lottery_storage.review(
        "100",
        payout["serial"],
        False,
        now=now,
    )
    assert reviewed["status"] == "failed"
