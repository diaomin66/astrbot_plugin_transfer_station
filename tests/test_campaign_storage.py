from __future__ import annotations

import asyncio
import sqlite3
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
async def test_lottery_claim_deadline_expires_pending_winner_state(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "短领奖", "1", now=now)
    await storage.update_draft("100", claim_duration_seconds=60)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    await storage.register("100", "200", "参与抽奖", now=now)
    await storage.draw(int(activity["id"]), ["200"], now=now)
    await storage.create_pending_payout(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )

    await storage.expire(now=now + utils.parse_duration("2m"))

    winner = (await storage.winners(int(activity["id"])))[0]
    assert winner["payout_state"] == "expired"


@pytest.mark.asyncio
async def test_paid_lottery_state_wins_over_elapsed_claim_deadline(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "已到账", "1", now=now)
    await storage.update_draft("100", claim_duration_seconds=60)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    await storage.register("100", "200", "参与抽奖", now=now)
    await storage.draw(int(activity["id"]), ["200"], now=now)
    payout = await storage.create_pending_payout(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )
    await storage.reserve_confirmation("100", "200", now=now)
    await storage.finish_payout(payout["serial"], "paid", now=now)

    assert (
        await storage.submission_state(
            "100",
            "200",
            now=now + utils.parse_duration("2m"),
        )
        == "paid"
    )


@pytest.mark.asyncio
async def test_lottery_claim_deadline_boundary_is_exact(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "截止边界", "1", now=now)
    await storage.update_draft("100", claim_duration_seconds=60)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    await storage.register("100", "200", "参与抽奖", now=now)
    await storage.draw(int(activity["id"]), ["200"], now=now)
    deadline = utils.from_iso(
        (await storage.winners(int(activity["id"])))[0]["claim_deadline_at"]
    )

    assert (
        await storage.submission_state(
            "100",
            "200",
            now=deadline - utils.parse_duration("1s"),
        )
        == "eligible"
    )
    assert await storage.submission_state("100", "200", now=deadline) == "claim_expired"
    assert (
        await storage.submission_state(
            "100",
            "200",
            now=deadline + utils.parse_duration("1s"),
        )
        == "claim_expired"
    )


@pytest.mark.asyncio
async def test_lottery_start_and_draw_boundaries_are_exact(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    start_at = now + utils.parse_duration("10s")
    draw_at = now + utils.parse_duration("20s")
    await storage.create_draft("100", "时间边界", "1", now=now)
    await storage.update_draft(
        "100",
        start_at=utils.to_iso(start_at),
        draw_at=utils.to_iso(draw_at),
    )
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)

    assert (
        await storage.register(
            "100",
            "200",
            "参与抽奖",
            now=start_at - utils.parse_duration("1s"),
        )
        == "not_open"
    )
    assert await storage.register("100", "200", "参与抽奖", now=start_at) == "joined"
    assert (
        await storage.register(
            "100",
            "201",
            "参与抽奖",
            now=start_at + utils.parse_duration("1s"),
        )
        == "joined"
    )
    assert await storage.due_draws(now=draw_at - utils.parse_duration("1s")) == []
    assert len(await storage.due_draws(now=draw_at)) == 1
    assert len(await storage.due_draws(now=draw_at + utils.parse_duration("1s"))) == 1


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
    pending = await storage.list_pending_notifications()
    assert [item["event_key"] for item in pending] == ["comp_budget_closed"]


@pytest.mark.asyncio
async def test_compensation_end_boundary_is_exact(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    activity = await storage.open_activity(
        "100",
        "时间边界",
        "1",
        "10",
        60,
        None,
        snap(),
        now=now,
    )
    end_at = utils.from_iso(activity["end_at"])

    assert await storage.tick(now=end_at - utils.parse_duration("1s")) == []
    assert (await storage.get(int(activity["id"])))["status"] == "open"
    assert [item["id"] for item in await storage.tick(now=end_at)] == [activity["id"]]
    assert (await storage.get(int(activity["id"])))["status"] == "completed"
    assert await storage.tick(now=end_at + utils.parse_duration("1s")) == []


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
async def test_processing_claims_freeze_budget_but_do_not_close_activity(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    activity = await storage.open_activity(
        "100",
        "并行补偿",
        "1",
        "10",
        None,
        "20",
        snap(),
        now=now,
    )
    first = await storage.create_pending(
        "100",
        "200",
        newapi.NewApiUser(3, "a", 1),
        now=now,
    )
    second = await storage.create_pending(
        "100",
        "201",
        newapi.NewApiUser(4, "b", 1),
        now=now,
    )
    await storage.reserve("100", "200", now=now)
    await storage.reserve("100", "201", now=now)

    await storage.finish(first["serial"], "paid", now=now)
    await storage.finish(second["serial"], "failed", now=now)

    current = await storage.get(int(activity["id"]))
    assert current["status"] == "open"
    third = await storage.create_pending(
        "100",
        "202",
        newapi.NewApiUser(5, "c", 1),
        now=now,
    )
    await storage.reserve("100", "202", now=now)
    await storage.finish(third["serial"], "paid", now=now)
    assert (await storage.get(int(activity["id"])))["status"] == "completed"


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


@pytest.mark.asyncio
async def test_stale_processing_recovery_waits_for_cutoff(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    await storage.open_activity(
        "100",
        "服务补偿",
        "1",
        "10",
        None,
        "20",
        snap(),
        now=now,
    )
    await storage.create_pending(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )
    await storage.reserve("100", "200", now=now)

    assert (
        await storage.recover_processing(
            now=now,
            stale_before=now - utils.parse_duration("1m"),
        )
        == 0
    )
    assert (
        await storage.recover_processing(
            now=now + utils.parse_duration("2m"),
            stale_before=now + utils.parse_duration("1m"),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_compensation_double_confirm_calls_newapi_once(tmp_path):
    db_path = tmp_path / "compensation.db"
    first_storage = compensation.CompensationStorage(db_path)
    second_storage = compensation.CompensationStorage(db_path)
    now = utils.utc_now()
    await first_storage.open_activity(
        "100",
        "并发补偿",
        "1",
        "10",
        None,
        "20",
        snap(),
        now=now,
    )
    await first_storage.create_pending(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )

    class CountingNewApi:
        def __init__(self):
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def add_quota(self, _user_id, _raw_quota):
            self.calls += 1
            self.entered.set()
            await self.release.wait()

    client = CountingNewApi()
    first_task = asyncio.create_task(
        compensation.CompensationService(first_storage, client).confirm("100", "200")
    )
    await client.entered.wait()
    second_result = await compensation.CompensationService(
        second_storage,
        client,
    ).confirm("100", "200")
    client.release.set()
    results = [await first_task, second_result]
    assert client.calls == 1
    assert sorted(result.key for result in results) == [
        "comp_no_confirmation",
        "comp_paid",
    ]


@pytest.mark.asyncio
async def test_lottery_double_confirm_calls_newapi_once_across_instances(tmp_path):
    db_path = tmp_path / "lottery.db"
    first_storage = lottery.LotteryStorage(db_path)
    second_storage = lottery.LotteryStorage(db_path)
    now = utils.utc_now()
    activity = await first_storage.create_draft("100", "并发抽奖", "1", now=now)
    await first_storage.add_prize("100", "一等奖", 1, "10")
    await first_storage.publish("100", snap(), now=now)
    await first_storage.register("100", "200", "参与抽奖", now=now)
    await first_storage.draw(int(activity["id"]), ["200"], now=now)
    await first_storage.create_pending_payout(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )

    class CountingNewApi:
        def __init__(self):
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def add_quota(self, _user_id, _raw_quota):
            self.calls += 1
            self.entered.set()
            await self.release.wait()

    client = CountingNewApi()
    first_task = asyncio.create_task(
        lottery.LotteryService(first_storage, client).confirm("100", "200")
    )
    await client.entered.wait()
    second_result = await lottery.LotteryService(
        second_storage,
        client,
    ).confirm("100", "200")
    client.release.set()
    results = [await first_task, second_result]
    assert client.calls == 1
    assert sorted(result.key for result in results) == [
        "lottery_no_confirmation",
        "lottery_paid",
    ]


@pytest.mark.asyncio
async def test_lookup_attempt_limit_persists_for_activity(tmp_path):
    lottery_storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await lottery_storage.create_draft("100", "查号限制", "1", now=now)
    await lottery_storage.add_prize("100", "一等奖", 1, "10")
    await lottery_storage.publish("100", snap(), now=now)
    await lottery_storage.register("100", "200", "参与抽奖", now=now)
    await lottery_storage.draw(int(activity["id"]), ["200"], now=now)

    for _ in range(5):
        assert await lottery_storage.consume_lookup_attempt("100", "200", now=now)
    assert not await lottery.LotteryStorage(
        tmp_path / "lottery.db"
    ).consume_lookup_attempt("100", "200", now=now)
    assert await lottery_storage.consume_lookup_attempt(
        "100",
        "200",
        now=now + utils.parse_duration("10m") + utils.parse_duration("1s"),
    )


@pytest.mark.asyncio
async def test_lottery_submission_does_not_switch_to_a_new_activity(
    tmp_path, monkeypatch
):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    first = await storage.create_draft("100", "第一场", "1", now=now)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    await storage.register("100", "200", "参与抽奖", now=now)
    await storage.draw(int(first["id"]), ["200"], now=now)
    original_create = storage.create_pending_payout

    async def create_then_replace(*args, **kwargs):
        payout = await original_create(*args, **kwargs)
        await storage.cancel_activity("100", "切换活动", now=now)
        await storage.create_draft("100", "第二场", "1", now=now)
        return payout

    monkeypatch.setattr(storage, "create_pending_payout", create_then_replace)

    class LookupOnlyNewApi:
        async def get_user(self, user_id):
            return newapi.NewApiUser(int(user_id), "alice", 1)

    result = await lottery.LotteryService(storage, LookupOnlyNewApi()).submit_target(
        "100",
        "200",
        "3",
        now=now,
    )

    assert result.key == "lottery_not_open"


@pytest.mark.asyncio
async def test_campaign_notifications_are_durable_and_cancel_supersedes_old(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    await storage.create_draft("100", "可靠通知", "1", now=now)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)

    pending = await lottery.LotteryStorage(
        tmp_path / "lottery.db"
    ).list_pending_notifications()
    assert [item["event_key"] for item in pending] == ["lottery_published"]

    await storage.cancel_activity("100", "停止", now=now)
    pending = await storage.list_pending_notifications()
    assert [item["event_key"] for item in pending] == ["lottery_cancelled"]


@pytest.mark.asyncio
async def test_notification_claim_is_atomic_across_storage_instances(tmp_path):
    db_path = tmp_path / "lottery.db"
    first = lottery.LotteryStorage(db_path)
    second = lottery.LotteryStorage(db_path)
    now = utils.utc_now()
    activity = await first.create_draft("100", "单次通知", "1", now=now)
    await first.add_prize("100", "一等奖", 1, "10")
    await first.publish("100", snap(), now=now)

    claimed = await asyncio.gather(
        first.claim_notification(int(activity["id"]), "lottery_published"),
        second.claim_notification(int(activity["id"]), "lottery_published"),
    )

    assert sum(item is not None for item in claimed) == 1
    notification = next(item for item in claimed if item is not None)
    assert await first.mark_notification_sent(
        int(notification["id"]),
        str(notification["lease_marker"]),
    )
    assert (
        await second.claim_notification(
            int(activity["id"]),
            "lottery_published",
        )
        is None
    )


@pytest.mark.asyncio
async def test_cancel_waits_for_sending_notification_then_supersedes_it(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "过期通知", "1", now=now)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    claimed = await storage.claim_notification(
        int(activity["id"]),
        "lottery_published",
    )
    assert claimed is not None

    await storage.cancel_activity("100", "停止", now=now)

    assert (
        await storage.claim_notification(
            int(activity["id"]),
            "lottery_cancelled",
        )
        is None
    )
    assert (
        await storage.release_notification(
            int(claimed["id"]),
            str(claimed["lease_marker"]),
        )
        is True
    )
    pending = await storage.list_pending_notifications()
    assert [item["event_key"] for item in pending] == ["lottery_cancelled"]


@pytest.mark.asyncio
async def test_stale_sending_notification_is_superseded_by_newer_terminal_event(
    tmp_path,
):
    db_path = tmp_path / "lottery.db"
    storage = lottery.LotteryStorage(db_path)
    now = utils.utc_now()
    activity = await storage.create_draft("100", "过期通知", "1", now=now)
    await storage.add_prize("100", "一等奖", 1, "10")
    await storage.publish("100", snap(), now=now)
    claimed = await storage.claim_notification(
        int(activity["id"]),
        "lottery_published",
    )
    assert claimed is not None
    await storage.cancel_activity("100", "停止", now=now)

    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            UPDATE lottery_notifications
            SET updated_at = '2000-01-01T00:00:00+00:00'
            WHERE id = ?
            """,
            (int(claimed["id"]),),
        )

    pending = await storage.list_pending_notifications()
    assert [item["event_key"] for item in pending] == ["lottery_cancelled"]
    with sqlite3.connect(db_path) as db:
        status = db.execute(
            "SELECT status FROM lottery_notifications WHERE id = ?",
            (int(claimed["id"]),),
        ).fetchone()[0]
    assert status == "superseded"


@pytest.mark.asyncio
async def test_unexpected_newapi_exception_enters_manual_review(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    await storage.open_activity(
        "100",
        "异常补偿",
        "1",
        "10",
        None,
        "20",
        snap(),
        now=now,
    )
    await storage.create_pending(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )

    class BrokenNewApi:
        async def add_quota(self, _user_id, _raw_quota):
            raise RuntimeError("unexpected")

    result = await compensation.CompensationService(
        storage,
        BrokenNewApi(),
    ).confirm("100", "200")
    assert result.key == "comp_manual_review"
    page = await storage.page("100", 1)
    assert page["items"][0]["status"] == "manual_review"


@pytest.mark.asyncio
async def test_review_is_bound_to_activity_id(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    first = await storage.open_activity(
        "100",
        "第一场",
        "1",
        "10",
        None,
        "20",
        snap(),
        now=now,
    )
    claim = await storage.create_pending(
        "100",
        "200",
        newapi.NewApiUser(3, "alice", 1),
        now=now,
    )
    await storage.reserve("100", "200", now=now)
    await storage.finish(claim["serial"], "manual_review", now=now)
    await storage.close("100", "done", now=now)
    second = await storage.open_activity(
        "100",
        "第二场",
        "1",
        "10",
        None,
        "20",
        snap(),
        now=now,
    )

    assert (
        await storage.review(
            "100",
            claim["serial"],
            True,
            now=now,
            activity_id=int(second["id"]),
        )
        is None
    )
    assert (
        await storage.review(
            "100",
            claim["serial"],
            True,
            now=now,
            activity_id=int(first["id"]),
        )
    )["status"] == "paid"


@pytest.mark.asyncio
async def test_multi_prize_draw_is_unique_and_ordered(tmp_path, monkeypatch):
    class DeterministicRandom:
        @staticmethod
        def sample(population, count):
            return sorted(population, reverse=True)[:count]

    monkeypatch.setattr(lottery.secrets, "SystemRandom", DeterministicRandom)
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "分级抽奖", "1", now=now)
    await storage.add_prize("100", "一等奖", 2, "10")
    await storage.add_prize("100", "二等奖", 2, "5")
    await storage.publish("100", snap(), now=now)
    for user_id in ("200", "201", "202"):
        await storage.register("100", user_id, "参与抽奖", now=now)

    winners = await storage.draw(
        int(activity["id"]),
        ["200", "201", "202"],
        now=now,
    )
    assert [item["user_id"] for item in winners] == ["202", "201", "200"]
    assert [item["prize_name"] for item in winners] == [
        "一等奖",
        "一等奖",
        "二等奖",
    ]
    assert len({item["user_id"] for item in winners}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("storage_type", "filename"),
    [
        (lottery.LotteryStorage, "lottery.db"),
        (compensation.CompensationStorage, "compensation.db"),
    ],
)
async def test_future_campaign_database_version_is_rejected(
    tmp_path,
    storage_type,
    filename,
):
    db_path = tmp_path / filename
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="版本高于"):
        await storage_type(db_path).initialize()


@pytest.mark.asyncio
async def test_lottery_legacy_database_initializes_concurrently(tmp_path):
    db_path = tmp_path / "lottery.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE lottery_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                start_at TEXT NOT NULL,
                draw_at TEXT NOT NULL,
                keyword TEXT NOT NULL,
                claim_duration_seconds INTEGER NOT NULL,
                claim_deadline_at TEXT,
                display_type TEXT,
                quota_per_unit TEXT,
                usd_exchange_rate TEXT,
                custom_currency_exchange_rate TEXT,
                custom_currency_symbol TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                published_at TEXT,
                drawn_at TEXT,
                closed_at TEXT,
                close_reason TEXT NOT NULL DEFAULT ''
            );
            PRAGMA user_version = 1;
            """
        )

    await asyncio.gather(
        lottery.LotteryStorage(db_path).initialize(),
        lottery.LotteryStorage(db_path).initialize(),
    )

    with sqlite3.connect(db_path) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(lottery_activities)")
        }
        version = db.execute("PRAGMA user_version").fetchone()[0]
    assert "revision" in columns
    assert version == lottery.LOTTERY_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_lottery_dashboard_counts_cover_all_winner_pages(tmp_path):
    storage = lottery.LotteryStorage(tmp_path / "lottery.db")
    now = utils.utc_now()
    activity = await storage.create_draft("100", "分页统计", "1", now=now)
    await storage.add_prize("100", "一等奖", 3, "10")
    await storage.publish("100", snap(), now=now)
    for user_id in ("200", "201", "202"):
        await storage.register("100", user_id, "参与抽奖", now=now)
    await storage.draw(int(activity["id"]), ["200", "201", "202"], now=now)

    first = await storage.create_pending_payout(
        "100",
        "200",
        newapi.NewApiUser(10, "paid-user", 1),
        now=now,
    )
    await storage.reserve_confirmation("100", "200", now=now)
    await storage.finish_payout(first["serial"], "paid", now=now)
    second = await storage.create_pending_payout(
        "100",
        "201",
        newapi.NewApiUser(11, "review-user", 1),
        now=now,
    )
    await storage.reserve_confirmation("100", "201", now=now)
    await storage.finish_payout(second["serial"], "manual_review", now=now)

    detail = await storage.dashboard_activity(int(activity["id"]), 1, 1)

    assert detail["winner_total"] == 3
    assert len(detail["winners"]) == 1
    assert detail["activity"]["paid_winner_count"] == 1
    assert detail["activity"]["manual_review_count"] == 1
    assert isinstance(detail["activity"]["id"], str)
    assert isinstance(detail["activity"]["revision"], str)


@pytest.mark.asyncio
async def test_compensation_dashboard_counts_cover_all_record_pages(tmp_path):
    storage = compensation.CompensationStorage(tmp_path / "compensation.db")
    now = utils.utc_now()
    activity = await storage.open_activity(
        "100",
        "分页补偿",
        "1",
        "10",
        None,
        "100",
        snap(),
        now=now,
    )
    for qq_id, api_user_id, status in (
        ("200", 10, "paid"),
        ("201", 11, "manual_review"),
        ("202", 12, None),
    ):
        claim = await storage.create_pending(
            "100",
            qq_id,
            newapi.NewApiUser(api_user_id, f"user-{api_user_id}", 1),
            now=now,
        )
        if status is not None:
            await storage.reserve("100", qq_id, now=now)
            await storage.finish(claim["serial"], status, now=now)

    detail = await storage.dashboard_activity(int(activity["id"]), 1, 1)
    activities = await storage.list_activities(
        1,
        20,
        scope="active",
        group_id="100",
    )

    assert detail["record_total"] == 3
    assert len(detail["records"]) == 1
    assert detail["activity"]["paid_count"] == 1
    assert detail["activity"]["manual_review_count"] == 1
    assert isinstance(detail["activity"]["id"], str)
    assert isinstance(detail["activity"]["used_raw_quota"], str)
    assert activities["total"] == 1
    assert activities["items"][0]["claim_count"] == 3
