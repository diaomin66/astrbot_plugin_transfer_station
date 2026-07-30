from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from .campaign_utils import (
    ActionResult,
    decimal_text,
    format_shanghai,
    from_iso,
    new_serial,
    to_iso,
    utc_now,
)
from .newapi_client import NewApiClient, NewApiError, NewApiUser, QuotaSnapshot

COMPENSATION_SCHEMA_VERSION = 1
ACTIVE_STATES = ("open",)


class CompensationStorage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA foreign_keys=ON")
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS compensation_activities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        per_display_amount TEXT NOT NULL,
                        per_raw_quota INTEGER NOT NULL,
                        total_display_amount TEXT,
                        total_raw_quota INTEGER,
                        display_type TEXT NOT NULL,
                        quota_per_unit TEXT NOT NULL,
                        usd_exchange_rate TEXT NOT NULL,
                        custom_currency_exchange_rate TEXT NOT NULL,
                        custom_currency_symbol TEXT NOT NULL DEFAULT '',
                        start_at TEXT NOT NULL,
                        end_at TEXT,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        closed_at TEXT,
                        close_reason TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_comp_group_status
                    ON compensation_activities(group_id, status);

                    CREATE TABLE IF NOT EXISTS compensation_claims (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        serial TEXT NOT NULL UNIQUE,
                        activity_id INTEGER NOT NULL
                            REFERENCES compensation_activities(id) ON DELETE CASCADE,
                        qq_id TEXT NOT NULL,
                        api_user_id INTEGER NOT NULL,
                        api_username TEXT NOT NULL,
                        raw_quota INTEGER NOT NULL,
                        display_amount TEXT NOT NULL,
                        status TEXT NOT NULL,
                        confirmation_expires_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error_type TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_comp_claims_status
                    ON compensation_claims(activity_id, status);
                    """
                )
                await db.execute(
                    """
                    INSERT INTO schema_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(COMPENSATION_SCHEMA_VERSION),),
                )
                await db.execute(f"PRAGMA user_version = {COMPENSATION_SCHEMA_VERSION}")
                await db.commit()
            self._initialized = True

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def _active(
        self,
        db: aiosqlite.Connection,
        group_id: str,
    ) -> dict[str, Any] | None:
        cursor = await db.execute(
            """
            SELECT * FROM compensation_activities
            WHERE group_id = ? AND status = 'open'
            ORDER BY id DESC LIMIT 1
            """,
            (str(group_id),),
        )
        return self._dict(await cursor.fetchone())

    async def get_active(self, group_id: str) -> dict[str, Any] | None:
        async with self._connection() as db:
            return await self._active(db, group_id)

    async def open_activity(
        self,
        group_id: str,
        title: str,
        created_by: str,
        amount: str,
        duration_seconds: int | None,
        total_amount: str | None,
        snapshot: QuotaSnapshot,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        per_raw = snapshot.amount_to_quota(amount)
        total_raw = (
            snapshot.amount_to_quota(total_amount) if total_amount is not None else None
        )
        if total_raw is not None and total_raw < per_raw:
            raise ValueError("budget_too_small")
        end_at = (
            to_iso(now + timedelta(seconds=duration_seconds))
            if duration_seconds is not None
            else None
        )
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            if await self._active(db, group_id):
                await db.rollback()
                raise ValueError("active_exists")
            cursor = await db.execute(
                """
                INSERT INTO compensation_activities(
                    group_id, title, status, per_display_amount, per_raw_quota,
                    total_display_amount, total_raw_quota, display_type,
                    quota_per_unit, usd_exchange_rate,
                    custom_currency_exchange_rate, custom_currency_symbol,
                    start_at, end_at, created_by, created_at
                )
                VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(group_id),
                    title,
                    decimal_text(amount),
                    per_raw,
                    decimal_text(total_amount) if total_amount is not None else None,
                    total_raw,
                    snapshot.display_type,
                    str(snapshot.quota_per_unit),
                    str(snapshot.usd_exchange_rate),
                    str(snapshot.custom_currency_exchange_rate),
                    snapshot.custom_currency_symbol,
                    to_iso(now),
                    end_at,
                    str(created_by),
                    to_iso(now),
                ),
            )
            activity_id = int(cursor.lastrowid)
            await db.commit()
        activity = await self.get(activity_id)
        assert activity is not None
        return activity

    async def get(self, activity_id: int) -> dict[str, Any] | None:
        async with self._connection() as db:
            cursor = await db.execute(
                "SELECT * FROM compensation_activities WHERE id = ?",
                (int(activity_id),),
            )
            return self._dict(await cursor.fetchone())

    async def close(
        self,
        group_id: str,
        reason: str,
        *,
        now: datetime,
    ) -> dict[str, Any] | None:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active(db, group_id)
            if not activity:
                await db.rollback()
                return None
            await db.execute(
                """
                UPDATE compensation_activities
                SET status = 'completed', closed_at = ?, close_reason = ?
                WHERE id = ?
                """,
                (to_iso(now), reason, int(activity["id"])),
            )
            await db.execute(
                """
                UPDATE compensation_claims
                SET status = 'cancelled', updated_at = ?
                WHERE activity_id = ? AND status = 'pending_confirmation'
                """,
                (to_iso(now), int(activity["id"])),
            )
            await db.commit()
            return activity

    async def create_pending(
        self,
        group_id: str,
        qq_id: str,
        user: NewApiUser,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active(db, group_id)
            if not activity:
                await db.rollback()
                raise ValueError("no_active")
            if activity["end_at"] and now >= from_iso(activity["end_at"]):
                await db.rollback()
                raise ValueError("ended")
            cursor = await db.execute(
                """
                SELECT 1 FROM compensation_claims
                WHERE activity_id = ? AND status IN (
                    'pending_confirmation', 'processing', 'paid', 'manual_review'
                ) AND (qq_id = ? OR api_user_id = ?)
                LIMIT 1
                """,
                (int(activity["id"]), str(qq_id), int(user.user_id)),
            )
            if await cursor.fetchone():
                await db.rollback()
                raise ValueError("duplicate")
            serial = new_serial("C")
            expires = now + timedelta(minutes=5)
            await db.execute(
                """
                INSERT INTO compensation_claims(
                    serial, activity_id, qq_id, api_user_id, api_username,
                    raw_quota, display_amount, status,
                    confirmation_expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_confirmation', ?, ?, ?)
                """,
                (
                    serial,
                    int(activity["id"]),
                    str(qq_id),
                    int(user.user_id),
                    user.username,
                    int(activity["per_raw_quota"]),
                    str(activity["per_display_amount"]),
                    to_iso(expires),
                    to_iso(now),
                    to_iso(now),
                ),
            )
            await db.commit()
            return {
                **activity,
                "serial": serial,
                "api_user_id": int(user.user_id),
                "api_username": user.username,
                "confirmation_expires_at": to_iso(expires),
            }

    async def reserve(
        self,
        group_id: str,
        qq_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT cc.*, ca.title, ca.total_raw_quota,
                       ca.end_at, ca.status AS activity_status,
                       ca.display_type, ca.quota_per_unit,
                       ca.usd_exchange_rate,
                       ca.custom_currency_exchange_rate,
                       ca.custom_currency_symbol
                FROM compensation_claims AS cc
                JOIN compensation_activities AS ca ON ca.id = cc.activity_id
                WHERE ca.group_id = ? AND cc.qq_id = ?
                  AND cc.status = 'pending_confirmation'
                ORDER BY cc.id DESC LIMIT 1
                """,
                (str(group_id), str(qq_id)),
            )
            claim = self._dict(await cursor.fetchone())
            if not claim:
                await db.rollback()
                raise ValueError("no_confirmation")
            if (
                now >= from_iso(claim["confirmation_expires_at"])
                or claim["activity_status"] != "open"
                or (claim["end_at"] and now >= from_iso(claim["end_at"]))
            ):
                await db.execute(
                    """
                    UPDATE compensation_claims
                    SET status = 'expired', updated_at = ?
                    WHERE id = ?
                    """,
                    (to_iso(now), int(claim["id"])),
                )
                await db.commit()
                raise ValueError("expired")

            if claim["total_raw_quota"] is not None:
                cursor = await db.execute(
                    """
                    SELECT COALESCE(SUM(raw_quota), 0) AS used
                    FROM compensation_claims
                    WHERE activity_id = ?
                      AND status IN ('processing', 'paid', 'manual_review')
                    """,
                    (int(claim["activity_id"]),),
                )
                used_row = await cursor.fetchone()
                used = int(used_row["used"]) if used_row else 0
                if int(claim["total_raw_quota"]) - used < int(claim["raw_quota"]):
                    paid_cursor = await db.execute(
                        """
                        SELECT COALESCE(SUM(raw_quota), 0) AS paid
                        FROM compensation_claims
                        WHERE activity_id = ? AND status = 'paid'
                        """,
                        (int(claim["activity_id"]),),
                    )
                    paid_row = await paid_cursor.fetchone()
                    paid = int(paid_row["paid"]) if paid_row else 0
                    if int(claim["total_raw_quota"]) - paid < int(claim["raw_quota"]):
                        await db.execute(
                            """
                            UPDATE compensation_activities
                            SET status = 'completed', closed_at = ?,
                                close_reason = 'budget_exhausted'
                            WHERE id = ?
                            """,
                            (to_iso(now), int(claim["activity_id"])),
                        )
                        await db.execute(
                            """
                            UPDATE compensation_claims
                            SET status = 'cancelled', updated_at = ?
                            WHERE activity_id = ?
                              AND status = 'pending_confirmation'
                            """,
                            (to_iso(now), int(claim["activity_id"])),
                        )
                        await db.commit()
                        raise ValueError("budget_insufficient")
                    await db.rollback()
                    raise ValueError("budget_reserved")

            await db.execute(
                """
                UPDATE compensation_claims
                SET status = 'processing', updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), int(claim["id"])),
            )
            await db.commit()
            claim["status"] = "processing"
            return claim

    async def finish(
        self,
        serial: str,
        status: str,
        *,
        error_type: str = "",
        now: datetime,
    ) -> None:
        if status not in {"paid", "failed", "manual_review"}:
            raise ValueError("invalid_status")
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT activity_id FROM compensation_claims
                WHERE serial = ? AND status = 'processing'
                """,
                (serial,),
            )
            claim = await cursor.fetchone()
            if not claim:
                await db.rollback()
                return
            await db.execute(
                """
                UPDATE compensation_claims
                SET status = ?, error_type = ?, updated_at = ?
                WHERE serial = ? AND status = 'processing'
                """,
                (status, error_type, to_iso(now), serial),
            )
            if status == "paid":
                cursor = await db.execute(
                    """
                    SELECT total_raw_quota, per_raw_quota
                    FROM compensation_activities
                    WHERE id = ? AND status = 'open'
                    """,
                    (int(claim["activity_id"]),),
                )
                activity = await cursor.fetchone()
                if activity and activity["total_raw_quota"] is not None:
                    cursor = await db.execute(
                        """
                        SELECT COALESCE(SUM(raw_quota), 0) AS used
                        FROM compensation_claims
                        WHERE activity_id = ?
                          AND status IN ('processing', 'paid', 'manual_review')
                        """,
                        (int(claim["activity_id"]),),
                    )
                    used_row = await cursor.fetchone()
                    used = int(used_row["used"]) if used_row else 0
                    if int(activity["total_raw_quota"]) - used < int(
                        activity["per_raw_quota"]
                    ):
                        await db.execute(
                            """
                            UPDATE compensation_activities
                            SET status = 'completed', closed_at = ?,
                                close_reason = 'budget_exhausted'
                            WHERE id = ?
                            """,
                            (to_iso(now), int(claim["activity_id"])),
                        )
                        await db.execute(
                            """
                            UPDATE compensation_claims
                            SET status = 'cancelled', updated_at = ?
                            WHERE activity_id = ?
                              AND status = 'pending_confirmation'
                            """,
                            (to_iso(now), int(claim["activity_id"])),
                        )
            await db.commit()

    async def cancel_pending(self, group_id: str, qq_id: str) -> bool:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE compensation_claims
                SET status = 'cancelled', updated_at = ?
                WHERE id = (
                    SELECT cc.id
                    FROM compensation_claims AS cc
                    JOIN compensation_activities AS ca ON ca.id = cc.activity_id
                    WHERE ca.group_id = ? AND cc.qq_id = ?
                      AND cc.status = 'pending_confirmation'
                    ORDER BY cc.id DESC LIMIT 1
                )
                """,
                (to_iso(utc_now()), str(group_id), str(qq_id)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def review(
        self,
        group_id: str,
        serial: str,
        success: bool,
        *,
        now: datetime,
    ) -> dict[str, Any] | None:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT cc.*
                FROM compensation_claims AS cc
                JOIN compensation_activities AS ca ON ca.id = cc.activity_id
                WHERE cc.serial = ? AND cc.status = 'manual_review'
                  AND ca.group_id = ?
                """,
                (serial, str(group_id)),
            )
            claim = self._dict(await cursor.fetchone())
            if not claim:
                await db.rollback()
                return None
            status = "paid" if success else "failed"
            await db.execute(
                """
                UPDATE compensation_claims
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, to_iso(now), int(claim["id"])),
            )
            if success:
                cursor = await db.execute(
                    """
                    SELECT total_raw_quota, per_raw_quota
                    FROM compensation_activities
                    WHERE id = ? AND status = 'open'
                    """,
                    (int(claim["activity_id"]),),
                )
                activity = await cursor.fetchone()
                if activity and activity["total_raw_quota"] is not None:
                    cursor = await db.execute(
                        """
                        SELECT COALESCE(SUM(raw_quota), 0) AS used
                        FROM compensation_claims
                        WHERE activity_id = ?
                          AND status IN ('processing', 'paid', 'manual_review')
                        """,
                        (int(claim["activity_id"]),),
                    )
                    used_row = await cursor.fetchone()
                    used = int(used_row["used"]) if used_row else 0
                    if int(activity["total_raw_quota"]) - used < int(
                        activity["per_raw_quota"]
                    ):
                        await db.execute(
                            """
                            UPDATE compensation_activities
                            SET status = 'completed', closed_at = ?,
                                close_reason = 'budget_exhausted'
                            WHERE id = ?
                            """,
                            (to_iso(now), int(claim["activity_id"])),
                        )
                        await db.execute(
                            """
                            UPDATE compensation_claims
                            SET status = 'cancelled', updated_at = ?
                            WHERE activity_id = ?
                              AND status = 'pending_confirmation'
                            """,
                            (to_iso(now), int(claim["activity_id"])),
                        )
            await db.commit()
            claim["status"] = status
            return claim

    async def recover_processing(self, *, now: datetime) -> int:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE compensation_claims
                SET status = 'manual_review', updated_at = ?,
                    error_type = 'ProcessRestarted'
                WHERE status = 'processing'
                """,
                (to_iso(now),),
            )
            await db.commit()
            return cursor.rowcount

    async def tick(self, *, now: datetime) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            now_iso = to_iso(now)
            await db.execute(
                """
                UPDATE compensation_claims
                SET status = 'expired', updated_at = ?
                WHERE status = 'pending_confirmation'
                  AND confirmation_expires_at <= ?
                """,
                (now_iso, now_iso),
            )
            cursor = await db.execute(
                """
                SELECT * FROM compensation_activities
                WHERE status = 'open' AND end_at IS NOT NULL AND end_at <= ?
                """,
                (now_iso,),
            )
            closed = [dict(row) for row in await cursor.fetchall()]
            for activity in closed:
                await db.execute(
                    """
                    UPDATE compensation_activities
                    SET status = 'completed', closed_at = ?,
                        close_reason = 'time_expired'
                    WHERE id = ?
                    """,
                    (now_iso, int(activity["id"])),
                )
                await db.execute(
                    """
                    UPDATE compensation_claims
                    SET status = 'cancelled', updated_at = ?
                    WHERE activity_id = ? AND status = 'pending_confirmation'
                    """,
                    (now_iso, int(activity["id"])),
                )
            await db.commit()
        return closed

    async def page(self, group_id: str, page: int, page_size: int = 20) -> dict:
        page = max(1, int(page))
        async with self._connection() as db:
            activity = await self._active(db, group_id)
            if not activity:
                cursor = await db.execute(
                    """
                    SELECT * FROM compensation_activities
                    WHERE group_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (str(group_id),),
                )
                activity = self._dict(await cursor.fetchone())
            if not activity:
                return {"activity": None, "items": [], "total": 0, "page": page}
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS total FROM compensation_claims
                WHERE activity_id = ?
                """,
                (int(activity["id"]),),
            )
            total_row = await cursor.fetchone()
            cursor = await db.execute(
                """
                SELECT COALESCE(SUM(raw_quota), 0) AS used_raw_quota
                FROM compensation_claims
                WHERE activity_id = ?
                  AND status IN ('processing', 'paid', 'manual_review')
                """,
                (int(activity["id"]),),
            )
            used_row = await cursor.fetchone()
            offset = (page - 1) * page_size
            cursor = await db.execute(
                """
                SELECT serial, qq_id, api_user_id, api_username, raw_quota,
                       display_amount, status, created_at, updated_at
                FROM compensation_claims
                WHERE activity_id = ?
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (int(activity["id"]), page_size, offset),
            )
            return {
                "activity": activity,
                "items": [dict(row) for row in await cursor.fetchall()],
                "total": int(total_row["total"]) if total_row else 0,
                "used_raw_quota": (int(used_row["used_raw_quota"]) if used_row else 0),
                "page": page,
            }


class CompensationService:
    def __init__(
        self,
        storage: CompensationStorage,
        newapi: NewApiClient | None,
    ):
        self.storage = storage
        self.newapi = newapi

    @staticmethod
    def _snapshot(activity: dict[str, Any]) -> QuotaSnapshot:
        return QuotaSnapshot(
            display_type=str(activity["display_type"]),
            quota_per_unit=Decimal(str(activity["quota_per_unit"])),
            usd_exchange_rate=Decimal(str(activity["usd_exchange_rate"])),
            custom_currency_exchange_rate=Decimal(
                str(activity["custom_currency_exchange_rate"])
            ),
            custom_currency_symbol=str(activity.get("custom_currency_symbol") or ""),
        )

    async def open(
        self,
        group_id: str,
        per_amount: str,
        duration: timedelta | None,
        total_amount: str | None,
        title: str,
        admin_id: str,
    ) -> ActionResult:
        if duration is None and total_amount is None:
            return ActionResult("comp_invalid_argument")
        if self.newapi is None:
            return ActionResult("newapi_error", {"error": "尚未配置 New API"})
        try:
            snapshot = await self.newapi.status_snapshot()
            activity = await self.storage.open_activity(
                group_id,
                title.strip() or "服务异常补偿",
                admin_id,
                per_amount,
                int(duration.total_seconds()) if duration else None,
                total_amount,
                snapshot,
                now=utc_now(),
            )
        except NewApiError as exc:
            return ActionResult("newapi_error", {"error": str(exc)})
        except ValueError as exc:
            key = (
                "comp_active_exists"
                if str(exc) == "active_exists"
                else "comp_budget_too_small"
                if str(exc) == "budget_too_small"
                else "comp_invalid_argument"
            )
            return ActionResult(key)
        snap = self._snapshot(activity)
        return ActionResult(
            "comp_opened",
            {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
                "amount": snap.display_amount(activity["per_display_amount"]),
                "end_time": format_shanghai(activity.get("end_at")),
                "total_budget": (
                    snap.display_amount(activity["total_display_amount"])
                    if activity["total_display_amount"] is not None
                    else "不限制"
                ),
            },
        )

    async def status(self, group_id: str) -> ActionResult:
        activity = await self.storage.get_active(group_id)
        if not activity:
            return ActionResult("comp_no_active")
        snap = self._snapshot(activity)
        page = await self.storage.page(group_id, 1)
        total = activity["total_raw_quota"]
        used = int(page["used_raw_quota"])
        remaining_display = "不限制"
        if total is not None:
            issued_count = Decimal(used) / Decimal(int(activity["per_raw_quota"]))
            remaining_amount = max(
                Decimal(0),
                Decimal(str(activity["total_display_amount"]))
                - Decimal(str(activity["per_display_amount"])) * issued_count,
            )
            remaining_display = snap.display_amount(remaining_amount)
        return ActionResult(
            "comp_status",
            {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
                "amount": snap.display_amount(activity["per_display_amount"]),
                "end_time": format_shanghai(activity.get("end_at")),
                "total_budget": (
                    snap.display_amount(activity["total_display_amount"])
                    if activity["total_display_amount"] is not None
                    else "不限制"
                ),
                "records": str(page["total"]),
                "remaining_budget": remaining_display,
            },
        )

    async def records(self, group_id: str, page: int) -> ActionResult:
        result = await self.storage.page(group_id, page)
        if not result["activity"]:
            return ActionResult("comp_no_active")
        lines = [
            f"{row['serial']} | QQ {row['qq_id']} | New API {row['api_user_id']} "
            f"| {row['display_amount']} | {row['status']}"
            for row in result["items"]
        ]
        return ActionResult(
            "comp_records",
            {
                "page": str(result["page"]),
                "total": str(result["total"]),
                "records": "\n".join(lines) or "-",
            },
        )

    async def submit(
        self,
        group_id: str,
        qq_id: str,
        api_user_id: str,
    ) -> ActionResult:
        if self.newapi is None:
            return ActionResult("newapi_error", {"error": "尚未配置 New API"})
        try:
            user = await self.newapi.get_user(api_user_id)
            claim = await self.storage.create_pending(
                group_id,
                qq_id,
                user,
                now=utc_now(),
            )
        except NewApiError as exc:
            return ActionResult("newapi_user_error", {"error": str(exc)})
        except ValueError as exc:
            mapping = {
                "no_active": "comp_no_active",
                "ended": "comp_ended",
                "duplicate": "comp_duplicate",
            }
            return ActionResult(mapping.get(str(exc), "comp_invalid_argument"))
        snap = self._snapshot(claim)
        return ActionResult(
            "comp_confirmation",
            {
                "activity": claim["title"],
                "amount": snap.display_amount(claim["per_display_amount"]),
                "user_id": str(claim["api_user_id"]),
                "username": claim["api_username"],
                "expires_at": format_shanghai(claim["confirmation_expires_at"]),
                "serial": claim["serial"],
            },
        )

    async def confirm(self, group_id: str, qq_id: str) -> ActionResult:
        if self.newapi is None:
            return ActionResult("newapi_error", {"error": "尚未配置 New API"})
        try:
            claim = await self.storage.reserve(group_id, qq_id, now=utc_now())
        except ValueError as exc:
            mapping = {
                "expired": "comp_confirmation_expired",
                "budget_insufficient": "comp_budget_insufficient",
                "budget_reserved": "comp_budget_reserved",
                "no_confirmation": "comp_no_confirmation",
            }
            return ActionResult(mapping.get(str(exc), "comp_no_confirmation"))
        try:
            await self.newapi.add_quota(
                int(claim["api_user_id"]),
                int(claim["raw_quota"]),
            )
        except NewApiError as exc:
            status = "manual_review" if exc.ambiguous else "failed"
            await self.storage.finish(
                claim["serial"],
                status,
                error_type=type(exc).__name__,
                now=utc_now(),
            )
            if exc.ambiguous:
                return ActionResult(
                    "comp_manual_review",
                    {"serial": claim["serial"]},
                )
            return ActionResult("comp_payout_failed", {"error": str(exc)})
        await self.storage.finish(claim["serial"], "paid", now=utc_now())
        snapshot = self._snapshot(claim)
        return ActionResult(
            "comp_paid",
            {
                "activity": claim["title"],
                "amount": snapshot.display_amount(claim["display_amount"]),
                "user_id": str(claim["api_user_id"]),
                "username": claim["api_username"],
                "serial": claim["serial"],
            },
        )

    async def cancel(self, group_id: str, qq_id: str) -> ActionResult:
        return ActionResult(
            "comp_confirmation_cancelled"
            if await self.storage.cancel_pending(group_id, qq_id)
            else "comp_no_confirmation"
        )

    async def close(self, group_id: str, reason: str) -> ActionResult:
        activity = await self.storage.close(group_id, reason, now=utc_now())
        if not activity:
            return ActionResult("comp_no_active")
        return ActionResult(
            "comp_closed",
            {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
                "reason": reason or "-",
            },
        )

    async def review(
        self,
        group_id: str,
        serial: str,
        success: bool,
    ) -> ActionResult:
        claim = await self.storage.review(
            group_id,
            serial,
            success,
            now=utc_now(),
        )
        if not claim:
            return ActionResult("comp_review_not_found")
        return ActionResult(
            "comp_review_success" if success else "comp_review_failed",
            {"serial": serial},
        )
