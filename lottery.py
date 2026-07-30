from __future__ import annotations

import asyncio
import secrets
import sqlite3
from collections.abc import AsyncIterator, Iterable
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

LOTTERY_SCHEMA_VERSION = 1
ACTIVE_ACTIVITY_STATES = ("draft", "scheduled", "open", "claiming")


class LotteryStorage:
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
                        close_reason TEXT NOT NULL DEFAULT ''
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
                    """
                )
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
        **values: Any,
    ) -> dict[str, Any]:
        allowed = {
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
            assignments = ", ".join(f"{key} = ?" for key in updates)
            await db.execute(
                f"UPDATE lottery_activities SET {assignments} WHERE id = ?",
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
    ) -> int:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "draft":
                await db.rollback()
                raise ValueError("no_draft")
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
            await db.commit()
            return position

    async def delete_prize(self, group_id: str, position: int) -> bool:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "draft":
                await db.rollback()
                raise ValueError("no_draft")
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
    ) -> dict[str, Any]:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity or activity["status"] != "draft":
                await db.rollback()
                raise ValueError("no_draft")
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
                    custom_currency_symbol = ?, published_at = ?
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
                return await self.winners(int(activity_id))
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
            await db.commit()
        return await self.winners(int(activity_id))

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
    ) -> dict[str, Any]:
        page = max(1, int(page))
        async with self._connection() as db:
            activity = await self._active_activity(db, group_id)
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
            }

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
    ) -> None:
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
                return
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
    ) -> dict[str, Any] | None:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            activity = await self._active_activity(db, group_id)
            if not activity:
                await db.rollback()
                return None
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
            await db.commit()
            return activity

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
                SELECT lp.*, lw.claim_deadline_at
                FROM lottery_payouts AS lp
                JOIN lottery_winners AS lw ON lw.id = lp.winner_id
                JOIN lottery_activities AS la ON la.id = lw.activity_id
                WHERE lp.serial = ? AND lp.status = 'manual_review'
                  AND la.group_id = ?
                """,
                (serial, str(group_id)),
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

    async def recover_processing(self, *, now: datetime) -> int:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE lottery_payouts
                SET status = 'manual_review', updated_at = ?,
                    error_type = 'ProcessRestarted'
                WHERE status = 'processing'
                """,
                (to_iso(now),),
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
                    WHERE activity_id = ? AND payout_state IS NULL
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
            await db.commit()
        return closed


class LotteryService:
    def __init__(
        self,
        storage: LotteryStorage,
        newapi: NewApiClient | None,
    ):
        self.storage = storage
        self.newapi = newapi

    async def create(
        self,
        group_id: str,
        title: str,
        admin_id: str,
        *,
        now: datetime | None = None,
    ) -> ActionResult:
        if not title.strip():
            return ActionResult("lottery_invalid_argument")
        try:
            activity = await self.storage.create_draft(
                group_id,
                title.strip(),
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

    async def update_draft(self, group_id: str, **values: Any) -> ActionResult:
        try:
            await self.storage.update_draft(group_id, **values)
        except ValueError:
            return ActionResult("lottery_no_draft")
        return ActionResult("lottery_updated")

    async def add_prize(
        self,
        group_id: str,
        name: str,
        winner_count: int,
        amount: Decimal,
    ) -> ActionResult:
        if not name.strip() or winner_count <= 0:
            return ActionResult("lottery_invalid_argument")
        try:
            position = await self.storage.add_prize(
                group_id,
                name.strip(),
                winner_count,
                decimal_text(amount),
            )
        except ValueError:
            return ActionResult("lottery_no_draft")
        return ActionResult(
            "lottery_prize_added",
            {
                "position": str(position),
                "prize_name": name.strip(),
                "winner_count": str(winner_count),
                "amount": decimal_text(amount),
            },
        )

    async def delete_prize(
        self,
        group_id: str,
        position: int,
    ) -> ActionResult:
        try:
            deleted = await self.storage.delete_prize(group_id, position)
        except ValueError:
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
    ) -> ActionResult:
        current = now or utc_now()
        if self.newapi is None:
            return ActionResult("newapi_error", {"error": "尚未配置 New API"})
        try:
            snapshot = await self.newapi.status_snapshot()
            activity = await self.storage.publish(
                group_id,
                snapshot,
                now=current,
            )
        except NewApiError as exc:
            return ActionResult("newapi_error", {"error": str(exc)})
        except ValueError as exc:
            mapping = {
                "no_draft": "lottery_no_draft",
                "invalid_time": "lottery_invalid_time",
                "no_prizes": "lottery_no_prizes",
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
        page = await self.storage.participant_page(group_id, 1)
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
        winners = await self.storage.draw(
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
            return ActionResult("newapi_error", {"error": "尚未配置 New API"})
        try:
            user = await self.newapi.get_user(api_user_id)
            payout = await self.storage.create_pending_payout(
                group_id,
                qq_id,
                user,
                now=now or utc_now(),
            )
        except NewApiError as exc:
            return ActionResult("newapi_user_error", {"error": str(exc)})
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
        activity = await self.storage.get_active(group_id)
        assert activity is not None
        snapshot = self._snapshot(activity)
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
            return ActionResult("newapi_error", {"error": "尚未配置 New API"})
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
        except NewApiError as exc:
            status = "manual_review" if exc.ambiguous else "failed"
            await self.storage.finish_payout(
                payout["serial"],
                status,
                error_type=type(exc).__name__,
                now=utc_now(),
            )
            if exc.ambiguous:
                return ActionResult(
                    "lottery_manual_review",
                    {"serial": payout["serial"]},
                )
            return ActionResult("lottery_payout_failed", {"error": str(exc)})
        await self.storage.finish_payout(
            payout["serial"],
            "paid",
            now=utc_now(),
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
    ) -> ActionResult:
        activity = await self.storage.cancel_activity(
            group_id,
            reason,
            now=utc_now(),
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
    ) -> ActionResult:
        payout = await self.storage.review(
            group_id,
            serial,
            success,
            now=utc_now(),
        )
        if not payout:
            return ActionResult("lottery_review_not_found")
        return ActionResult(
            "lottery_review_success" if success else "lottery_review_failed",
            {"serial": serial},
        )
