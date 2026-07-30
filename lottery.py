from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

import aiosqlite

from .campaign_utils import (
    MAX_DESCRIPTION_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_PRIZE_COUNT,
    MAX_PRIZE_NAME_LENGTH,
    MAX_REASON_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_TOTAL_WINNERS,
    MAX_WINNER_COUNT,
    ActionResult,
    decimal_text,
    format_shanghai,
    from_iso,
    new_serial,
    to_iso,
    utc_now,
    validate_text,
)
from .newapi_client import NewApiClient, NewApiError, NewApiUser, QuotaSnapshot

LOTTERY_SCHEMA_VERSION = 4
MAX_LOOKUP_ATTEMPTS_PER_USER = 5
NOTIFICATION_LEASE_SECONDS = 60
ACTIVE_ACTIVITY_STATES = ("draft", "scheduled", "open", "claiming")
_SHARED_INIT_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Lock],
] = WeakKeyDictionary()


def _shared_init_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = str(path.resolve()).casefold()
    locks = _SHARED_INIT_LOCKS.setdefault(loop, {})
    return locks.setdefault(key, asyncio.Lock())


class LotteryStorage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with _shared_init_lock(self.db_path):
            if self._initialized:
                return
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout=5000")
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA foreign_keys=ON")
                version_cursor = await db.execute("PRAGMA user_version")
                version_row = await version_cursor.fetchone()
                current_version = int(version_row[0]) if version_row else 0
                if current_version > LOTTERY_SCHEMA_VERSION:
                    raise RuntimeError(
                        "lottery.db 版本高于当前插件支持版本，拒绝降级打开"
                    )
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS lottery_activities (
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
                        close_reason TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE INDEX IF NOT EXISTS idx_lottery_group_status
                    ON lottery_activities(group_id, status);

                    CREATE TABLE IF NOT EXISTS lottery_prizes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        activity_id INTEGER NOT NULL
                            REFERENCES lottery_activities(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        winner_count INTEGER NOT NULL,
                        display_amount TEXT NOT NULL,
                        raw_quota INTEGER,
                        UNIQUE(activity_id, position)
                    );

                    CREATE TABLE IF NOT EXISTS lottery_participants (
                        activity_id INTEGER NOT NULL
                            REFERENCES lottery_activities(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        joined_at TEXT NOT NULL,
                        eligible_at_draw INTEGER,
                        PRIMARY KEY(activity_id, user_id)
                    );

                    CREATE TABLE IF NOT EXISTS lottery_winners (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        activity_id INTEGER NOT NULL
                            REFERENCES lottery_activities(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        prize_id INTEGER NOT NULL
                            REFERENCES lottery_prizes(id),
                        payout_state TEXT,
                        claim_deadline_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(activity_id, user_id)
                    );

                    CREATE TABLE IF NOT EXISTS lottery_payouts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        serial TEXT NOT NULL UNIQUE,
                        winner_id INTEGER NOT NULL
                            REFERENCES lottery_winners(id),
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

                    CREATE INDEX IF NOT EXISTS idx_lottery_payout_status
                    ON lottery_payouts(status, confirmation_expires_at);

                    CREATE TABLE IF NOT EXISTS lottery_lookup_attempts (
                        activity_id INTEGER NOT NULL
                            REFERENCES lottery_activities(id) ON DELETE CASCADE,
                        qq_id TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(activity_id, qq_id)
                    );

                    CREATE TABLE IF NOT EXISTS lottery_notifications (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        activity_id INTEGER NOT NULL
                            REFERENCES lottery_activities(id) ON DELETE CASCADE,
                        group_id TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        placeholders_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(activity_id, event_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_lottery_notification_status
                    ON lottery_notifications(status, id);
                    """
                )
                columns_cursor = await db.execute(
                    "PRAGMA table_info(lottery_activities)"
                )
                columns = {str(row[1]) for row in await columns_cursor.fetchall()}
                if "revision" not in columns:
                    try:
                        await db.execute(
                            """
                            ALTER TABLE lottery_activities
                            ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
                            """
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
                await db.execute(
                    """
                    INSERT INTO schema_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(LOTTERY_SCHEMA_VERSION),),
                )
                await db.execute(f"PRAGMA user_version = {LOTTERY_SCHEMA_VERSION}")
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

    @staticmethod
    async def _enqueue_notification(
        db: aiosqlite.Connection,
        activity_id: int,
        group_id: str,
        event_key: str,
        placeholders: dict[str, Any],
        *,
        now: datetime,
        supersede_pending: bool = False,
    ) -> None:
        if supersede_pending:
            await db.execute(
                """
                UPDATE lottery_notifications
                SET status = 'superseded', updated_at = ?
                WHERE activity_id = ? AND status = 'pending'
                """,
                (to_iso(now), int(activity_id)),
            )
        await db.execute(
            """
            INSERT OR IGNORE INTO lottery_notifications(
                activity_id, group_id, event_key, placeholders_json,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                int(activity_id),
                str(group_id),
                event_key,
                json.dumps(placeholders, ensure_ascii=False, separators=(",", ":")),
                to_iso(now),
                to_iso(now),
            ),
        )

    async def claim_notification(
        self,
        activity_id: int,
        event_key: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        marker = to_iso(now)
        stale_before = to_iso(now - timedelta(seconds=NOTIFICATION_LEASE_SECONDS))
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE lottery_notifications
                SET status = 'pending', updated_at = ?
                WHERE status = 'sending' AND updated_at <= ?
                """,
                (marker, stale_before),
            )
            cursor = await db.execute(
                """
                SELECT id, activity_id, group_id, event_key, placeholders_json
                FROM lottery_notifications AS current
                WHERE current.activity_id = ?
                  AND current.event_key = ?
                  AND current.status = 'pending'
                  AND current.id = (
                    SELECT MIN(queued.id)
                    FROM lottery_notifications AS queued
                    WHERE queued.activity_id = current.activity_id
                      AND queued.status = 'pending'
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM lottery_notifications AS active
                    WHERE active.activity_id = current.activity_id
                      AND active.status = 'sending'
                  )
                LIMIT 1
                """,
                (int(activity_id), str(event_key)),
            )
            row = await cursor.fetchone()
            if not row:
                await db.commit()
                return None
            cursor = await db.execute(
                """
                UPDATE lottery_notifications
                SET status = 'sending', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (marker, int(row["id"])),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            await db.commit()
        result = dict(row)
        result["placeholders"] = json.loads(result.pop("placeholders_json"))
        result["lease_marker"] = marker
        return result

    async def list_pending_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        now = utc_now()
        marker = to_iso(now)
        stale_before = to_iso(now - timedelta(seconds=NOTIFICATION_LEASE_SECONDS))
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                UPDATE lottery_notifications
                SET status = 'pending', updated_at = ?
                WHERE status = 'sending' AND updated_at <= ?
                """,
                (marker, stale_before),
            )
            cursor = await db.execute(
                """
                SELECT id, activity_id, group_id, event_key, placeholders_json
                FROM lottery_notifications
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT ?
                """,
                (min(100, max(1, int(limit))),),
            )
            rows = await cursor.fetchall()
            await db.commit()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["placeholders"] = json.loads(item.pop("placeholders_json"))
            result.append(item)
        return result

    async def mark_notification_sent(
        self,
        notification_id: int,
        lease_marker: str,
    ) -> bool:
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                """
                UPDATE lottery_notifications
                SET status = 'sent', updated_at = ?
                WHERE id = ? AND status = 'sending' AND updated_at = ?
                """,
                (to_iso(utc_now()), int(notification_id), str(lease_marker)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def release_notification(
        self,
        notification_id: int,
        lease_marker: str,
    ) -> bool:
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                """
                UPDATE lottery_notifications
                SET status = 'pending', updated_at = ?
                WHERE id = ? AND status = 'sending' AND updated_at = ?
                """,
                (to_iso(utc_now()), int(notification_id), str(lease_marker)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def _active_activity(
        self,
        db: aiosqlite.Connection,
        group_id: str,
    ) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_ACTIVITY_STATES)
        cursor = await db.execute(
            f"""
            SELECT * FROM lottery_activities
            WHERE group_id = ?
              AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(group_id), *ACTIVE_ACTIVITY_STATES),
        )
        return self._dict(await cursor.fetchone())

    async def get_active(self, group_id: str) -> dict[str, Any] | None:
        async with self._connection() as db:
            return await self._active_activity(db, str(group_id))

    async def get_activity(self, activity_id: int) -> dict[str, Any] | None:
        async with self._connection() as db:
            cursor = await db.execute(
                "SELECT * FROM lottery_activities WHERE id = ?",
                (int(activity_id),),
            )
            return self._dict(await cursor.fetchone())

    async def create_draft(
        self,
        group_id: str,
        title: str,
        created_by: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            if await self._active_activity(db, group_id):
                await db.rollback()
                raise ValueError("active_exists")
            cursor = await db.execute(
                """
                INSERT INTO lottery_activities(
                    group_id, title, status, start_at, draw_at, keyword,
                    claim_duration_seconds, created_by, created_at
                )
                VALUES (?, ?, 'draft', ?, ?, '参与抽奖', 86400, ?, ?)
                """,
                (
                    str(group_id),
                    title,
                    to_iso(now),
                    to_iso(now + timedelta(hours=1)),
                    str(created_by),
                    to_iso(now),
                ),
            )
            activity_id = int(cursor.lastrowid)
            await db.commit()
        activity = await self.get_activity(activity_id)
        assert activity is not None
        return activity

    async def update_draft(
        self,
        group_id: str,
        expected_activity_id: int | None = None,
        expected_revision: int | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        allowed = {
            "title",
            "start_at",
            "draw_at",
            "keyword",
            "description",
            "claim_duration_seconds",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            raise ValueError("invalid_update")
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "draft":
                await db.rollback()
                raise ValueError("no_draft")
            if expected_activity_id is not None and int(activity["id"]) != int(
                expected_activity_id
            ):
                await db.rollback()
                raise ValueError("stale_activity")
            if expected_revision is not None and int(activity["revision"]) != int(
                expected_revision
            ):
                await db.rollback()
                raise ValueError("stale_revision")
            assignments = ", ".join(f"{key} = ?" for key in updates)
            await db.execute(
                f"""
                UPDATE lottery_activities
                SET {assignments}, revision = revision + 1
                WHERE id = ?
                """,
                (*updates.values(), int(activity["id"])),
            )
            await db.commit()
            activity_id = int(activity["id"])
        updated = await self.get_activity(activity_id)
        assert updated is not None
        return updated

    async def add_prize(
        self,
        group_id: str,
        name: str,
        winner_count: int,
        display_amount: str,
        *,
        expected_activity_id: int | None = None,
        expected_revision: int | None = None,
    ) -> int:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "draft":
                await db.rollback()
                raise ValueError("no_draft")
            if expected_activity_id is not None and int(activity["id"]) != int(
                expected_activity_id
            ):
                await db.rollback()
                raise ValueError("stale_activity")
            if expected_revision is not None and int(activity["revision"]) != int(
                expected_revision
            ):
                await db.rollback()
                raise ValueError("stale_revision")
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS prize_count,
                       COALESCE(SUM(winner_count), 0) AS total_winners
                FROM lottery_prizes
                WHERE activity_id = ?
                """,
                (int(activity["id"]),),
            )
            totals = await cursor.fetchone()
            if int(totals["prize_count"] or 0) >= MAX_PRIZE_COUNT:
                await db.rollback()
                raise ValueError("prize_limit")
            if (
                int(totals["total_winners"] or 0) + int(winner_count)
                > MAX_TOTAL_WINNERS
            ):
                await db.rollback()
                raise ValueError("winner_limit")
            cursor = await db.execute(
                """
                SELECT COALESCE(MAX(position), 0) + 1 AS next_position
                FROM lottery_prizes
                WHERE activity_id = ?
                """,
                (int(activity["id"]),),
            )
            row = await cursor.fetchone()
            position = int(row["next_position"])
            await db.execute(
                """
                INSERT INTO lottery_prizes(
                    activity_id, position, name, winner_count, display_amount
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(activity["id"]),
                    position,
                    name,
                    int(winner_count),
                    display_amount,
                ),
            )
            await db.execute(
                """
                UPDATE lottery_activities
                SET revision = revision + 1
                WHERE id = ?
                """,
                (int(activity["id"]),),
            )
            await db.commit()
            return position

    async def delete_prize(
        self,
        group_id: str,
        position: int,
        *,
        expected_activity_id: int | None = None,
    ) -> bool:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "draft":
                await db.rollback()
                raise ValueError("no_draft")
            if expected_activity_id is not None and int(activity["id"]) != int(
                expected_activity_id
            ):
                await db.rollback()
                raise ValueError("stale_activity")
            cursor = await db.execute(
                """
                DELETE FROM lottery_prizes
                WHERE activity_id = ? AND position = ?
                """,
                (int(activity["id"]), int(position)),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            await db.execute(
                """
                UPDATE lottery_prizes
                SET position = position - 1
                WHERE activity_id = ? AND position > ?
                """,
                (int(activity["id"]), int(position)),
            )
            await db.execute(
                """
                UPDATE lottery_activities
                SET revision = revision + 1
                WHERE id = ?
                """,
                (int(activity["id"]),),
            )
            await db.commit()
            return True

    async def prizes(self, activity_id: int) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT * FROM lottery_prizes
                WHERE activity_id = ?
                ORDER BY position ASC
                """,
                (int(activity_id),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def publish(
        self,
        group_id: str,
        snapshot: QuotaSnapshot,
        *,
        now: datetime,
        expected_activity_id: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "draft":
                await db.rollback()
                raise ValueError("no_draft")
            if expected_activity_id is not None and int(activity["id"]) != int(
                expected_activity_id
            ):
                await db.rollback()
                raise ValueError("stale_activity")
            if expected_revision is not None and int(activity["revision"]) != int(
                expected_revision
            ):
                await db.rollback()
                raise ValueError("stale_revision")
            if from_iso(activity["draw_at"]) <= from_iso(activity["start_at"]):
                await db.rollback()
                raise ValueError("invalid_time")
            cursor = await db.execute(
                """
                SELECT * FROM lottery_prizes
                WHERE activity_id = ?
                ORDER BY position ASC
                """,
                (int(activity["id"]),),
            )
            prizes = [dict(row) for row in await cursor.fetchall()]
            if not prizes:
                await db.rollback()
                raise ValueError("no_prizes")
            for prize in prizes:
                raw_quota = snapshot.amount_to_quota(prize["display_amount"])
                await db.execute(
                    "UPDATE lottery_prizes SET raw_quota = ? WHERE id = ?",
                    (raw_quota, int(prize["id"])),
                )
            status = "open" if from_iso(activity["start_at"]) <= now else "scheduled"
            await db.execute(
                """
                UPDATE lottery_activities
                SET status = ?, display_type = ?, quota_per_unit = ?,
                    usd_exchange_rate = ?,
                    custom_currency_exchange_rate = ?,
                    custom_currency_symbol = ?, published_at = ?,
                    revision = revision + 1
                WHERE id = ?
                """,
                (
                    status,
                    snapshot.display_type,
                    str(snapshot.quota_per_unit),
                    str(snapshot.usd_exchange_rate),
                    str(snapshot.custom_currency_exchange_rate),
                    snapshot.custom_currency_symbol,
                    to_iso(now),
                    int(activity["id"]),
                ),
            )
            prize_lines = "\n".join(
                (
                    f"{prize['position']}. {prize['name']} ×{prize['winner_count']}"
                    f"（{snapshot.display_amount(prize['display_amount'])}）"
                )
                for prize in prizes
            )
            await self._enqueue_notification(
                db,
                int(activity["id"]),
                str(activity["group_id"]),
                "lottery_published",
                {
                    "activity_id": str(activity["id"]),
                    "title": activity["title"],
                    "description": activity["description"] or "-",
                    "start_time": format_shanghai(activity["start_at"]),
                    "draw_time": format_shanghai(activity["draw_at"]),
                    "keyword": activity["keyword"],
                    "prizes": prize_lines or "-",
                },
                now=now,
            )
            await db.commit()
            activity_id = int(activity["id"])
        published = await self.get_activity(activity_id)
        assert published is not None
        return published

    async def register(
        self,
        group_id: str,
        user_id: str,
        keyword: str,
        *,
        now: datetime,
    ) -> str:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity:
                await db.rollback()
                return "no_activity"
            if activity["keyword"] != keyword:
                await db.rollback()
                return "keyword_mismatch"
            if (
                activity["status"] not in {"scheduled", "open"}
                or now < from_iso(activity["start_at"])
                or now >= from_iso(activity["draw_at"])
            ):
                await db.rollback()
                return "not_open"
            if activity["status"] == "scheduled":
                await db.execute(
                    "UPDATE lottery_activities SET status = 'open' WHERE id = ?",
                    (int(activity["id"]),),
                )
                await self._enqueue_notification(
                    db,
                    int(activity["id"]),
                    str(activity["group_id"]),
                    "lottery_opened",
                    {
                        "activity_id": str(activity["id"]),
                        "title": activity["title"],
                        "keyword": activity["keyword"],
                    },
                    now=now,
                )
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO lottery_participants(
                    activity_id, user_id, joined_at
                )
                VALUES (?, ?, ?)
                """,
                (int(activity["id"]), str(user_id), to_iso(now)),
            )
            await db.commit()
            return "joined" if cursor.rowcount == 1 else "already_joined"

    async def mark_due_open(self, *, now: datetime) -> list[dict[str, Any]]:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT * FROM lottery_activities
                WHERE status = 'scheduled' AND start_at <= ?
                ORDER BY id ASC
                """,
                (to_iso(now),),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            if rows:
                await db.executemany(
                    "UPDATE lottery_activities SET status = 'open' WHERE id = ?",
                    [(int(row["id"]),) for row in rows],
                )
                for row in rows:
                    await self._enqueue_notification(
                        db,
                        int(row["id"]),
                        str(row["group_id"]),
                        "lottery_opened",
                        {
                            "activity_id": str(row["id"]),
                            "title": row["title"],
                            "keyword": row["keyword"],
                        },
                        now=now,
                    )
            await db.commit()
            return rows

    async def due_draws(self, *, now: datetime) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT * FROM lottery_activities
                WHERE status IN ('scheduled', 'open')
                  AND draw_at <= ?
                ORDER BY draw_at ASC, id ASC
                """,
                (to_iso(now),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def draw(
        self,
        activity_id: int,
        member_ids: Iterable[str],
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        winners, _ = await self.draw_with_status(
            activity_id,
            member_ids,
            now=now,
        )
        return winners

    async def draw_with_status(
        self,
        activity_id: int,
        member_ids: Iterable[str],
        *,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], bool]:
        active_members = {str(user_id) for user_id in member_ids}
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM lottery_activities WHERE id = ?",
                (int(activity_id),),
            )
            activity = self._dict(await cursor.fetchone())
            if not activity:
                await db.rollback()
                raise ValueError("not_found")
            if activity["status"] == "claiming":
                await db.rollback()
                return await self.winners(int(activity_id)), False
            if activity["status"] == "completed" and activity["drawn_at"]:
                await db.rollback()
                return await self.winners(int(activity_id)), False
            if activity["status"] not in {"scheduled", "open"}:
                await db.rollback()
                raise ValueError("not_drawable")

            cursor = await db.execute(
                """
                SELECT user_id FROM lottery_participants
                WHERE activity_id = ?
                ORDER BY joined_at ASC, user_id ASC
                """,
                (int(activity_id),),
            )
            participants = [
                str(row["user_id"])
                for row in await cursor.fetchall()
                if str(row["user_id"]) in active_members
            ]
            await db.execute(
                """
                UPDATE lottery_participants
                SET eligible_at_draw = 0
                WHERE activity_id = ?
                """,
                (int(activity_id),),
            )
            if participants:
                await db.executemany(
                    """
                    UPDATE lottery_participants
                    SET eligible_at_draw = 1
                    WHERE activity_id = ? AND user_id = ?
                    """,
                    [(int(activity_id), user_id) for user_id in participants],
                )
            cursor = await db.execute(
                """
                SELECT * FROM lottery_prizes
                WHERE activity_id = ?
                ORDER BY position ASC
                """,
                (int(activity_id),),
            )
            prizes = [dict(row) for row in await cursor.fetchall()]
            total_slots = sum(int(prize["winner_count"]) for prize in prizes)
            selected = secrets.SystemRandom().sample(
                participants,
                min(total_slots, len(participants)),
            )
            deadline = now + timedelta(seconds=int(activity["claim_duration_seconds"]))
            selected_index = 0
            winner_lines: list[str] = []
            snapshot = QuotaSnapshot(
                display_type=str(activity["display_type"]),
                quota_per_unit=Decimal(str(activity["quota_per_unit"])),
                usd_exchange_rate=Decimal(str(activity["usd_exchange_rate"])),
                custom_currency_exchange_rate=Decimal(
                    str(activity["custom_currency_exchange_rate"])
                ),
                custom_currency_symbol=str(activity["custom_currency_symbol"] or ""),
            )
            for prize in prizes:
                for _ in range(int(prize["winner_count"])):
                    if selected_index >= len(selected):
                        break
                    await db.execute(
                        """
                        INSERT INTO lottery_winners(
                            activity_id, user_id, prize_id, claim_deadline_at,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            int(activity_id),
                            selected[selected_index],
                            int(prize["id"]),
                            to_iso(deadline),
                            to_iso(now),
                        ),
                    )
                    winner_lines.append(
                        f"@{selected[selected_index]} - {prize['name']} - "
                        f"{snapshot.display_amount(prize['display_amount'])}"
                    )
                    selected_index += 1
            status = "claiming" if selected else "completed"
            await db.execute(
                """
                UPDATE lottery_activities
                SET status = ?, drawn_at = ?, claim_deadline_at = ?,
                    closed_at = CASE WHEN ? = 'completed' THEN ? ELSE closed_at END
                WHERE id = ?
                """,
                (
                    status,
                    to_iso(now),
                    to_iso(deadline),
                    status,
                    to_iso(now),
                    int(activity_id),
                ),
            )
            event_key = "lottery_drawn" if selected else "lottery_no_winner"
            placeholders = {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
            }
            if selected:
                placeholders.update(
                    {
                        "winners": "\n".join(winner_lines),
                        "claim_deadline": format_shanghai(deadline),
                    }
                )
            await self._enqueue_notification(
                db,
                int(activity["id"]),
                str(activity["group_id"]),
                event_key,
                placeholders,
                now=now,
                supersede_pending=True,
            )
            await db.commit()
        return await self.winners(int(activity_id)), True

    async def winners(self, activity_id: int) -> list[dict[str, Any]]:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT w.*, p.name AS prize_name, p.position,
                       p.display_amount, p.raw_quota
                FROM lottery_winners AS w
                JOIN lottery_prizes AS p ON p.id = w.prize_id
                WHERE w.activity_id = ?
                ORDER BY p.position ASC, w.id ASC
                """,
                (int(activity_id),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def participant_page(
        self,
        group_id: str,
        page: int,
        page_size: int = 20,
        *,
        activity_id: int | None = None,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        async with self._connection() as db:
            if activity_id is None:
                activity = await self._active_activity(db, group_id)
            else:
                cursor = await db.execute(
                    """
                    SELECT * FROM lottery_activities
                    WHERE id = ? AND group_id = ?
                    """,
                    (int(activity_id), str(group_id)),
                )
                activity = self._dict(await cursor.fetchone())
            if not activity:
                return {"activity": None, "items": [], "total": 0, "page": page}
            offset = (page - 1) * page_size
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS total FROM lottery_participants
                WHERE activity_id = ?
                """,
                (int(activity["id"]),),
            )
            total_row = await cursor.fetchone()
            cursor = await db.execute(
                """
                SELECT user_id, joined_at, eligible_at_draw
                FROM lottery_participants
                WHERE activity_id = ?
                ORDER BY joined_at ASC, user_id ASC
                LIMIT ? OFFSET ?
                """,
                (int(activity["id"]), page_size, offset),
            )
            return {
                "activity": activity,
                "items": [dict(row) for row in await cursor.fetchall()],
                "total": int(total_row["total"]) if total_row else 0,
                "page": page,
            }

    async def create_pending_payout(
        self,
        group_id: str,
        qq_id: str,
        user: NewApiUser,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "claiming":
                await db.rollback()
                raise ValueError("no_claiming")
            cursor = await db.execute(
                """
                SELECT w.*, p.name AS prize_name, p.display_amount, p.raw_quota
                FROM lottery_winners AS w
                JOIN lottery_prizes AS p ON p.id = w.prize_id
                WHERE w.activity_id = ? AND w.user_id = ?
                LIMIT 1
                """,
                (int(activity["id"]), str(qq_id)),
            )
            winner = self._dict(await cursor.fetchone())
            if not winner:
                await db.rollback()
                raise ValueError("not_winner")
            if now >= from_iso(winner["claim_deadline_at"]):
                await db.execute(
                    """
                    UPDATE lottery_winners SET payout_state = 'expired'
                    WHERE id = ? AND payout_state IS NULL
                    """,
                    (int(winner["id"]),),
                )
                await db.commit()
                raise ValueError("claim_expired")
            if winner["payout_state"] in {
                "pending_confirmation",
                "processing",
                "paid",
                "manual_review",
            }:
                await db.rollback()
                raise ValueError(str(winner["payout_state"]))

            serial = new_serial("L")
            confirmation_expires = now + timedelta(minutes=5)
            await db.execute(
                """
                INSERT INTO lottery_payouts(
                    serial, winner_id, qq_id, api_user_id, api_username,
                    raw_quota, display_amount, status,
                    confirmation_expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_confirmation', ?, ?, ?)
                """,
                (
                    serial,
                    int(winner["id"]),
                    str(qq_id),
                    user.user_id,
                    user.username,
                    int(winner["raw_quota"]),
                    str(winner["display_amount"]),
                    to_iso(confirmation_expires),
                    to_iso(now),
                    to_iso(now),
                ),
            )
            await db.execute(
                """
                UPDATE lottery_winners
                SET payout_state = 'pending_confirmation'
                WHERE id = ?
                """,
                (int(winner["id"]),),
            )
            await db.commit()
            return {
                **winner,
                "serial": serial,
                "api_user_id": user.user_id,
                "api_username": user.username,
                "confirmation_expires_at": to_iso(confirmation_expires),
                "activity_title": activity["title"],
                "display_type": activity["display_type"],
                "quota_per_unit": activity["quota_per_unit"],
                "usd_exchange_rate": activity["usd_exchange_rate"],
                "custom_currency_exchange_rate": activity[
                    "custom_currency_exchange_rate"
                ],
                "custom_currency_symbol": activity["custom_currency_symbol"],
            }

    async def submission_state(
        self,
        group_id: str,
        qq_id: str,
        *,
        now: datetime,
    ) -> str:
        async with self._connection() as db:
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "claiming":
                return "no_claiming"
            cursor = await db.execute(
                """
                SELECT w.payout_state, w.claim_deadline_at
                FROM lottery_winners AS w
                WHERE w.activity_id = ? AND w.user_id = ?
                LIMIT 1
                """,
                (int(activity["id"]), str(qq_id)),
            )
            winner = await cursor.fetchone()
            if not winner:
                return "not_winner"
            state = str(winner["payout_state"] or "")
            if state in {"paid", "processing", "manual_review"}:
                return state
            if now >= from_iso(winner["claim_deadline_at"]):
                return "claim_expired"
            return state or "eligible"

    async def payout_status(self, serial: str) -> str | None:
        async with self._connection() as db:
            cursor = await db.execute(
                "SELECT status FROM lottery_payouts WHERE serial = ?",
                (str(serial),),
            )
            row = await cursor.fetchone()
            return str(row["status"]) if row else None

    async def consume_lookup_attempt(
        self,
        group_id: str,
        qq_id: str,
        *,
        now: datetime,
    ) -> bool:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "claiming":
                await db.rollback()
                return False
            await db.execute(
                """
                INSERT OR IGNORE INTO lottery_lookup_attempts(
                    activity_id, qq_id, attempt_count, updated_at
                )
                VALUES (?, ?, 0, ?)
                """,
                (int(activity["id"]), str(qq_id), to_iso(now)),
            )
            cursor = await db.execute(
                """
                UPDATE lottery_lookup_attempts
                SET attempt_count = attempt_count + 1, updated_at = ?
                WHERE activity_id = ? AND qq_id = ?
                  AND attempt_count < ?
                """,
                (
                    to_iso(now),
                    int(activity["id"]),
                    str(qq_id),
                    MAX_LOOKUP_ATTEMPTS_PER_USER,
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def reserve_confirmation(
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
                SELECT lp.*, lw.claim_deadline_at, la.title AS activity_title
                       , la.display_type, la.quota_per_unit,
                       la.usd_exchange_rate,
                       la.custom_currency_exchange_rate,
                       la.custom_currency_symbol
                FROM lottery_payouts AS lp
                JOIN lottery_winners AS lw ON lw.id = lp.winner_id
                JOIN lottery_activities AS la ON la.id = lw.activity_id
                WHERE la.group_id = ? AND lp.qq_id = ?
                  AND lp.status = 'pending_confirmation'
                ORDER BY lp.id DESC
                LIMIT 1
                """,
                (str(group_id), str(qq_id)),
            )
            payout = self._dict(await cursor.fetchone())
            if not payout:
                await db.rollback()
                raise ValueError("no_confirmation")
            if now >= from_iso(payout["confirmation_expires_at"]) or now >= from_iso(
                payout["claim_deadline_at"]
            ):
                await db.execute(
                    """
                    UPDATE lottery_payouts
                    SET status = 'expired', updated_at = ?
                    WHERE id = ?
                    """,
                    (to_iso(now), int(payout["id"])),
                )
                await db.execute(
                    """
                    UPDATE lottery_winners
                    SET payout_state = CASE
                        WHEN claim_deadline_at <= ? THEN 'expired'
                        ELSE NULL
                    END
                    WHERE id = ?
                    """,
                    (to_iso(now), int(payout["winner_id"])),
                )
                await db.commit()
                raise ValueError("confirmation_expired")
            await db.execute(
                """
                UPDATE lottery_payouts
                SET status = 'processing', updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), int(payout["id"])),
            )
            await db.execute(
                """
                UPDATE lottery_winners SET payout_state = 'processing'
                WHERE id = ?
                """,
                (int(payout["winner_id"]),),
            )
            await db.commit()
            payout["status"] = "processing"
            return payout

    async def finish_payout(
        self,
        serial: str,
        status: str,
        *,
        error_type: str = "",
        now: datetime,
    ) -> bool:
        if status not in {"paid", "failed", "manual_review"}:
            raise ValueError("invalid_status")
        winner_state = status if status != "failed" else None
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT id, winner_id FROM lottery_payouts
                WHERE serial = ? AND status = 'processing'
                """,
                (serial,),
            )
            payout = await cursor.fetchone()
            if not payout:
                await db.rollback()
                return False
            await db.execute(
                """
                UPDATE lottery_payouts
                SET status = ?, error_type = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error_type, to_iso(now), int(payout["id"])),
            )
            await db.execute(
                "UPDATE lottery_winners SET payout_state = ? WHERE id = ?",
                (winner_state, int(payout["winner_id"])),
            )
            await db.commit()
            return True

    async def cancel_confirmation(
        self,
        group_id: str,
        qq_id: str,
        *,
        now: datetime,
    ) -> bool:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT lp.id, lp.winner_id
                FROM lottery_payouts AS lp
                JOIN lottery_winners AS lw ON lw.id = lp.winner_id
                JOIN lottery_activities AS la ON la.id = lw.activity_id
                WHERE la.group_id = ? AND lp.qq_id = ?
                  AND lp.status = 'pending_confirmation'
                ORDER BY lp.id DESC LIMIT 1
                """,
                (str(group_id), str(qq_id)),
            )
            payout = await cursor.fetchone()
            if not payout:
                await db.rollback()
                return False
            await db.execute(
                """
                UPDATE lottery_payouts
                SET status = 'cancelled', updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), int(payout["id"])),
            )
            await db.execute(
                "UPDATE lottery_winners SET payout_state = NULL WHERE id = ?",
                (int(payout["winner_id"]),),
            )
            await db.commit()
            return True

    async def cancel_activity(
        self,
        group_id: str,
        reason: str,
        *,
        now: datetime,
        expected_activity_id: int | None = None,
    ) -> dict[str, Any] | None:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity:
                await db.rollback()
                return None
            if expected_activity_id is not None and int(activity["id"]) != int(
                expected_activity_id
            ):
                await db.rollback()
                raise ValueError("stale_activity")
            await db.execute(
                """
                UPDATE lottery_activities
                SET status = 'cancelled', close_reason = ?, closed_at = ?
                WHERE id = ?
                """,
                (reason, to_iso(now), int(activity["id"])),
            )
            await db.execute(
                """
                UPDATE lottery_payouts
                SET status = 'cancelled', updated_at = ?
                WHERE winner_id IN (
                    SELECT id FROM lottery_winners WHERE activity_id = ?
                ) AND status = 'pending_confirmation'
                """,
                (to_iso(now), int(activity["id"])),
            )
            await db.execute(
                """
                UPDATE lottery_winners
                SET payout_state = NULL
                WHERE activity_id = ? AND payout_state = 'pending_confirmation'
                """,
                (int(activity["id"]),),
            )
            await self._enqueue_notification(
                db,
                int(activity["id"]),
                str(activity["group_id"]),
                "lottery_cancelled",
                {
                    "activity_id": str(activity["id"]),
                    "title": activity["title"],
                    "reason": reason or "-",
                },
                now=now,
                supersede_pending=True,
            )
            await db.commit()
            return activity

    async def review(
        self,
        group_id: str,
        serial: str,
        success: bool,
        *,
        now: datetime,
        activity_id: int | None = None,
    ) -> dict[str, Any] | None:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity_filter = "AND la.id = ?" if activity_id is not None else ""
            params: tuple[Any, ...] = (
                (serial, str(group_id), int(activity_id))
                if activity_id is not None
                else (serial, str(group_id))
            )
            cursor = await db.execute(
                f"""
                SELECT lp.*, lw.claim_deadline_at
                FROM lottery_payouts AS lp
                JOIN lottery_winners AS lw ON lw.id = lp.winner_id
                JOIN lottery_activities AS la ON la.id = lw.activity_id
                WHERE lp.serial = ? AND lp.status = 'manual_review'
                  AND la.group_id = ?
                  {activity_filter}
                """,
                params,
            )
            payout = self._dict(await cursor.fetchone())
            if not payout:
                await db.rollback()
                return None
            status = "paid" if success else "failed"
            if success:
                winner_state = "paid"
            elif now >= from_iso(payout["claim_deadline_at"]):
                winner_state = "expired"
            else:
                winner_state = None
            await db.execute(
                """
                UPDATE lottery_payouts
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, to_iso(now), int(payout["id"])),
            )
            await db.execute(
                "UPDATE lottery_winners SET payout_state = ? WHERE id = ?",
                (winner_state, int(payout["winner_id"])),
            )
            await db.commit()
            payout["status"] = status
            return payout

    async def recover_processing(
        self,
        *,
        now: datetime,
        stale_before: datetime | None = None,
    ) -> int:
        cutoff = to_iso(stale_before or now)
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE lottery_payouts
                SET status = 'manual_review', updated_at = ?,
                    error_type = 'ProcessRestarted'
                WHERE status = 'processing' AND updated_at <= ?
                """,
                (to_iso(now), cutoff),
            )
            await db.execute(
                """
                UPDATE lottery_winners
                SET payout_state = 'manual_review'
                WHERE id IN (
                    SELECT winner_id FROM lottery_payouts
                    WHERE status = 'manual_review'
                ) AND payout_state = 'processing'
                """
            )
            await db.commit()
            return cursor.rowcount

    async def expire(self, *, now: datetime) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            now_iso = to_iso(now)
            cursor = await db.execute(
                """
                SELECT lp.id, lp.winner_id
                FROM lottery_payouts AS lp
                WHERE lp.status = 'pending_confirmation'
                  AND lp.confirmation_expires_at <= ?
                """,
                (now_iso,),
            )
            pending = await cursor.fetchall()
            if pending:
                await db.executemany(
                    """
                    UPDATE lottery_payouts
                    SET status = 'expired', updated_at = ?
                    WHERE id = ?
                    """,
                    [(now_iso, int(row["id"])) for row in pending],
                )
                await db.executemany(
                    """
                    UPDATE lottery_winners SET payout_state = NULL
                    WHERE id = ? AND claim_deadline_at > ?
                    """,
                    [(int(row["winner_id"]), now_iso) for row in pending],
                )
            cursor = await db.execute(
                """
                SELECT * FROM lottery_activities
                WHERE status = 'claiming' AND claim_deadline_at <= ?
                """,
                (now_iso,),
            )
            closed = [dict(row) for row in await cursor.fetchall()]
            for activity in closed:
                await db.execute(
                    """
                    UPDATE lottery_payouts
                    SET status = 'expired', updated_at = ?
                    WHERE winner_id IN (
                        SELECT id FROM lottery_winners WHERE activity_id = ?
                    ) AND status = 'pending_confirmation'
                    """,
                    (now_iso, int(activity["id"])),
                )
                await db.execute(
                    """
                    UPDATE lottery_winners
                    SET payout_state = 'expired'
                    WHERE activity_id = ?
                      AND (
                        payout_state IS NULL
                        OR payout_state = 'pending_confirmation'
                      )
                    """,
                    (int(activity["id"]),),
                )
                await db.execute(
                    """
                    UPDATE lottery_activities
                    SET status = 'completed', closed_at = ?,
                        close_reason = 'claim_deadline'
                    WHERE id = ?
                    """,
                    (now_iso, int(activity["id"])),
                )
                await self._enqueue_notification(
                    db,
                    int(activity["id"]),
                    str(activity["group_id"]),
                    "lottery_claim_closed",
                    {
                        "activity_id": str(activity["id"]),
                        "title": activity["title"],
                    },
                    now=now,
                    supersede_pending=True,
                )
            await db.commit()
        return closed


class LotteryService:
    def __init__(
        self,
        storage: LotteryStorage,
        newapi: NewApiClient | None,
        reserved_keyword: Callable[[str], bool] | None = None,
    ):
        self.storage = storage
        self.newapi = newapi
        self.reserved_keyword = reserved_keyword

    async def create(
        self,
        group_id: str,
        title: str,
        admin_id: str,
        *,
        now: datetime | None = None,
    ) -> ActionResult:
        try:
            title = validate_text(
                title,
                label="抽奖标题",
                maximum=MAX_TITLE_LENGTH,
            )
        except ValueError:
            return ActionResult("lottery_invalid_argument")
        try:
            activity = await self.storage.create_draft(
                group_id,
                title,
                admin_id,
                now=now or utc_now(),
            )
        except ValueError:
            return ActionResult("lottery_active_exists")
        return ActionResult(
            "lottery_created",
            {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
                "description": activity["description"] or "-",
                "start_time": format_shanghai(activity["start_at"]),
                "draw_time": format_shanghai(activity["draw_at"]),
                "keyword": activity["keyword"],
            },
        )

    async def update_draft(
        self,
        group_id: str,
        *,
        expected_activity_id: int | None = None,
        expected_revision: int | None = None,
        **values: Any,
    ) -> ActionResult:
        try:
            if "title" in values:
                values["title"] = validate_text(
                    values["title"],
                    label="抽奖标题",
                    maximum=MAX_TITLE_LENGTH,
                )
            if "description" in values:
                values["description"] = validate_text(
                    values["description"],
                    label="抽奖描述",
                    maximum=MAX_DESCRIPTION_LENGTH,
                    allow_empty=True,
                )
            if "keyword" in values:
                values["keyword"] = validate_text(
                    values["keyword"],
                    label="报名口令",
                    maximum=MAX_KEYWORD_LENGTH,
                )
        except ValueError:
            return ActionResult("lottery_invalid_argument")
        try:
            await self.storage.update_draft(
                group_id,
                expected_activity_id=expected_activity_id,
                expected_revision=expected_revision,
                **values,
            )
        except ValueError as exc:
            if str(exc) in {"stale_activity", "stale_revision"}:
                return ActionResult("campaign_invalid_argument")
            return ActionResult("lottery_no_draft")
        return ActionResult("lottery_updated")

    async def add_prize(
        self,
        group_id: str,
        name: str,
        winner_count: int,
        amount: Decimal,
        *,
        expected_activity_id: int | None = None,
        expected_revision: int | None = None,
    ) -> ActionResult:
        try:
            name = validate_text(
                name,
                label="奖项名称",
                maximum=MAX_PRIZE_NAME_LENGTH,
            )
        except ValueError:
            return ActionResult("lottery_invalid_argument")
        if winner_count <= 0 or winner_count > MAX_WINNER_COUNT:
            return ActionResult("lottery_invalid_argument")
        try:
            position = await self.storage.add_prize(
                group_id,
                name,
                winner_count,
                decimal_text(amount),
                expected_activity_id=expected_activity_id,
                expected_revision=expected_revision,
            )
        except ValueError as exc:
            if str(exc) in {"stale_activity", "stale_revision"}:
                return ActionResult("campaign_invalid_argument")
            return ActionResult("lottery_no_draft")
        return ActionResult(
            "lottery_prize_added",
            {
                "position": str(position),
                "prize_name": name,
                "winner_count": str(winner_count),
                "amount": decimal_text(amount),
            },
        )

    async def delete_prize(
        self,
        group_id: str,
        position: int,
        *,
        expected_activity_id: int | None = None,
    ) -> ActionResult:
        try:
            deleted = await self.storage.delete_prize(
                group_id,
                position,
                expected_activity_id=expected_activity_id,
            )
        except ValueError as exc:
            if str(exc) == "stale_activity":
                return ActionResult("campaign_invalid_argument")
            return ActionResult("lottery_no_draft")
        return ActionResult(
            "lottery_prize_deleted" if deleted else "lottery_prize_not_found",
            {"position": str(position)},
        )

    async def publish(
        self,
        group_id: str,
        *,
        now: datetime | None = None,
        expected_activity_id: int | None = None,
        expected_revision: int | None = None,
    ) -> ActionResult:
        current = now or utc_now()
        if self.newapi is None:
            return ActionResult("newapi_error")
        activity = await self.storage.get_active(group_id)
        if (
            activity
            and activity["status"] == "draft"
            and self.reserved_keyword is not None
            and self.reserved_keyword(str(activity["keyword"]))
        ):
            return ActionResult("lottery_keyword_reserved")
        try:
            snapshot = await self.newapi.status_snapshot()
            activity = await self.storage.publish(
                group_id,
                snapshot,
                now=current,
                expected_activity_id=expected_activity_id,
                expected_revision=expected_revision,
            )
        except NewApiError:
            return ActionResult("newapi_error")
        except ValueError as exc:
            mapping = {
                "no_draft": "lottery_no_draft",
                "invalid_time": "lottery_invalid_time",
                "no_prizes": "lottery_no_prizes",
                "stale_activity": "campaign_invalid_argument",
                "stale_revision": "campaign_invalid_argument",
            }
            return ActionResult(mapping.get(str(exc), "lottery_invalid_argument"))
        prize_lines = self._prize_lines(
            await self.storage.prizes(int(activity["id"])),
            snapshot,
        )
        return ActionResult(
            "lottery_published",
            {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
                "description": activity["description"] or "-",
                "start_time": format_shanghai(activity["start_at"]),
                "draw_time": format_shanghai(activity["draw_at"]),
                "keyword": activity["keyword"],
                "prizes": prize_lines,
            },
        )

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

    @staticmethod
    def _prize_lines(
        prizes: list[dict[str, Any]],
        snapshot: QuotaSnapshot | None = None,
    ) -> str:
        lines = []
        for prize in prizes:
            amount = (
                snapshot.display_amount(prize["display_amount"])
                if snapshot
                else str(prize["display_amount"])
            )
            lines.append(
                f"{prize['position']}. {prize['name']} ×"
                f"{prize['winner_count']}（{amount}）"
            )
        return "\n".join(lines) or "-"

    async def status(self, group_id: str) -> ActionResult:
        activity = await self.storage.get_active(group_id)
        if not activity:
            return ActionResult("lottery_no_active")
        prizes = await self.storage.prizes(int(activity["id"]))
        snapshot = self._snapshot(activity) if activity.get("display_type") else None
        page = await self.storage.participant_page(
            group_id,
            1,
            activity_id=int(activity["id"]),
        )
        state_names = {
            "draft": "草稿",
            "scheduled": "待开始",
            "open": "报名中",
            "claiming": "领奖中",
        }
        return ActionResult(
            "lottery_status",
            {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
                "status": state_names.get(activity["status"], activity["status"]),
                "description": activity["description"] or "-",
                "start_time": format_shanghai(activity["start_at"]),
                "draw_time": format_shanghai(activity["draw_at"]),
                "claim_deadline": format_shanghai(activity.get("claim_deadline_at")),
                "keyword": activity["keyword"],
                "participant_count": str(page["total"]),
                "prizes": self._prize_lines(prizes, snapshot),
            },
        )

    async def participants(self, group_id: str, page: int) -> ActionResult:
        result = await self.storage.participant_page(group_id, page)
        if not result["activity"]:
            return ActionResult("lottery_no_active")
        lines = [
            f"{index}. {row['user_id']}  {format_shanghai(row['joined_at'])}"
            for index, row in enumerate(
                result["items"],
                start=(result["page"] - 1) * 20 + 1,
            )
        ]
        return ActionResult(
            "lottery_participants",
            {
                "page": str(result["page"]),
                "total": str(result["total"]),
                "participants": "\n".join(lines) or "-",
            },
        )

    async def register(
        self,
        group_id: str,
        user_id: str,
        keyword: str,
        *,
        now: datetime | None = None,
    ) -> ActionResult | None:
        result = await self.storage.register(
            group_id,
            user_id,
            keyword,
            now=now or utc_now(),
        )
        mapping = {
            "joined": "lottery_joined",
            "already_joined": "lottery_already_joined",
            "not_open": "lottery_not_open",
        }
        key = mapping.get(result)
        return ActionResult(key) if key else None

    async def draw(
        self,
        activity: dict[str, Any],
        member_ids: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> ActionResult:
        current = now or utc_now()
        winners, newly_drawn = await self.storage.draw_with_status(
            int(activity["id"]),
            member_ids,
            now=current,
        )
        if not winners:
            return ActionResult(
                "lottery_no_winner",
                {
                    "activity_id": str(activity["id"]),
                    "title": activity["title"],
                },
                should_announce=newly_drawn,
            )
        snapshot = self._snapshot(activity)
        lines = [
            f"@{winner['user_id']} - {winner['prize_name']} - "
            f"{snapshot.display_amount(winner['display_amount'])}"
            for winner in winners
        ]
        return ActionResult(
            "lottery_drawn",
            {
                "activity_id": str(activity["id"]),
                "title": activity["title"],
                "winners": "\n".join(lines),
                "claim_deadline": format_shanghai(winners[0]["claim_deadline_at"]),
            },
            should_announce=newly_drawn,
        )

    async def submit_target(
        self,
        group_id: str,
        qq_id: str,
        api_user_id: str,
        *,
        now: datetime | None = None,
    ) -> ActionResult:
        if self.newapi is None:
            return ActionResult("newapi_error")
        current = now or utc_now()
        local_state = await self.storage.submission_state(
            group_id,
            qq_id,
            now=current,
        )
        state_mapping = {
            "no_claiming": "lottery_not_open",
            "not_winner": "lottery_not_winner",
            "claim_expired": "lottery_claim_expired",
            "pending_confirmation": "lottery_confirmation_exists",
            "processing": "lottery_processing",
            "paid": "lottery_already_paid",
            "manual_review": "lottery_manual_review_locked",
        }
        if local_state != "eligible":
            return ActionResult(
                state_mapping.get(local_state, "lottery_invalid_argument")
            )
        if not await self.storage.consume_lookup_attempt(
            group_id,
            qq_id,
            now=current,
        ):
            return ActionResult("campaign_rate_limited")
        try:
            user = await self.newapi.get_user(api_user_id)
            payout = await self.storage.create_pending_payout(
                group_id,
                qq_id,
                user,
                now=current,
            )
        except NewApiError:
            return ActionResult("newapi_user_error")
        except ValueError as exc:
            mapping = {
                "no_claiming": "lottery_not_open",
                "not_winner": "lottery_not_winner",
                "claim_expired": "lottery_claim_expired",
                "pending_confirmation": "lottery_confirmation_exists",
                "processing": "lottery_processing",
                "paid": "lottery_already_paid",
                "manual_review": "lottery_manual_review_locked",
            }
            return ActionResult(mapping.get(str(exc), "lottery_invalid_argument"))
        if await self.storage.payout_status(payout["serial"]) != "pending_confirmation":
            return ActionResult("lottery_not_open")
        snapshot = self._snapshot(payout)
        return ActionResult(
            "lottery_confirmation",
            {
                "activity": payout["activity_title"],
                "prize": payout["prize_name"],
                "amount": snapshot.display_amount(payout["display_amount"]),
                "user_id": str(payout["api_user_id"]),
                "username": payout["api_username"],
                "expires_at": format_shanghai(payout["confirmation_expires_at"]),
                "serial": payout["serial"],
            },
        )

    async def confirm(
        self,
        group_id: str,
        qq_id: str,
        *,
        now: datetime | None = None,
    ) -> ActionResult:
        if self.newapi is None:
            return ActionResult("newapi_error")
        current = now or utc_now()
        try:
            payout = await self.storage.reserve_confirmation(
                group_id,
                qq_id,
                now=current,
            )
        except ValueError as exc:
            key = (
                "lottery_confirmation_expired"
                if str(exc) == "confirmation_expired"
                else "lottery_no_confirmation"
            )
            return ActionResult(key)
        try:
            await self.newapi.add_quota(
                int(payout["api_user_id"]),
                int(payout["raw_quota"]),
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self.storage.finish_payout(
                    payout["serial"],
                    "manual_review",
                    error_type="CancelledError",
                    now=utc_now(),
                )
            )
            raise
        except NewApiError as exc:
            status = "manual_review" if exc.ambiguous else "failed"
            finished = await self.storage.finish_payout(
                payout["serial"],
                status,
                error_type=type(exc).__name__,
                now=utc_now(),
            )
            if not finished:
                return ActionResult(
                    "lottery_manual_review",
                    {"serial": payout["serial"]},
                )
            if exc.ambiguous:
                return ActionResult(
                    "lottery_manual_review",
                    {"serial": payout["serial"]},
                )
            return ActionResult("lottery_payout_failed")
        except Exception as exc:  # noqa: BLE001 - New API client boundary
            await self.storage.finish_payout(
                payout["serial"],
                "manual_review",
                error_type=type(exc).__name__,
                now=utc_now(),
            )
            return ActionResult(
                "lottery_manual_review",
                {"serial": payout["serial"]},
            )
        finished = await self.storage.finish_payout(
            payout["serial"],
            "paid",
            now=utc_now(),
        )
        if not finished:
            return ActionResult(
                "lottery_manual_review",
                {"serial": payout["serial"]},
            )
        snapshot = self._snapshot(payout)
        return ActionResult(
            "lottery_paid",
            {
                "activity": payout["activity_title"],
                "amount": snapshot.display_amount(payout["display_amount"]),
                "user_id": str(payout["api_user_id"]),
                "username": payout["api_username"],
                "serial": payout["serial"],
            },
        )

    async def cancel_confirmation(
        self,
        group_id: str,
        qq_id: str,
    ) -> ActionResult:
        cancelled = await self.storage.cancel_confirmation(
            group_id,
            qq_id,
            now=utc_now(),
        )
        return ActionResult(
            "lottery_confirmation_cancelled" if cancelled else "lottery_no_confirmation"
        )

    async def cancel_activity(
        self,
        group_id: str,
        reason: str,
        *,
        expected_activity_id: int | None = None,
    ) -> ActionResult:
        try:
            reason = validate_text(
                reason,
                label="取消原因",
                maximum=MAX_REASON_LENGTH,
                allow_empty=True,
            )
        except ValueError:
            return ActionResult("lottery_invalid_argument")
        activity = await self.storage.cancel_activity(
            group_id,
            reason,
            now=utc_now(),
            expected_activity_id=expected_activity_id,
        )
        if not activity:
            return ActionResult("lottery_no_active")
        return ActionResult(
            "lottery_cancelled",
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
        *,
        activity_id: int | None = None,
    ) -> ActionResult:
        payout = await self.storage.review(
            group_id,
            serial,
            success,
            now=utc_now(),
            activity_id=activity_id,
        )
        if not payout:
            return ActionResult("lottery_review_not_found")
        return ActionResult(
            "lottery_review_success" if success else "lottery_review_failed",
            {"serial": serial},
        )
