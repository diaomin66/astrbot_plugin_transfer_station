from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def code_digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def code_suffix(code: str) -> str:
    return code[-4:] if len(code) > 4 else code


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    status: str
    error_type: str = ""


class GiftStorage:
    """Persistent eligibility, inventory and claim storage."""

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

                    CREATE TABLE IF NOT EXISTS eligible_members (
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        joined_at TEXT NOT NULL,
                        PRIMARY KEY (group_id, user_id)
                    );

                    CREATE TABLE IF NOT EXISTS gift_codes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS claims (
                        user_id TEXT PRIMARY KEY,
                        group_id TEXT NOT NULL,
                        code_suffix TEXT NOT NULL,
                        code_digest TEXT NOT NULL,
                        claimed_at TEXT NOT NULL
                    );
                    """
                )
                await db.execute(
                    """
                    INSERT INTO schema_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(SCHEMA_VERSION),),
                )
                await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                await db.commit()
            self._initialized = True

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    async def add_eligible(self, group_id: str, user_id: str) -> bool:
        group_id = str(group_id).strip()
        user_id = str(user_id).strip()
        if not group_id or not user_id:
            return False
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO eligible_members(group_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (group_id, user_id, utc_now()),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def is_eligible(self, group_id: str, user_id: str) -> bool:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT 1 FROM eligible_members
                WHERE group_id = ? AND user_id = ?
                LIMIT 1
                """,
                (str(group_id), str(user_id)),
            )
            return await cursor.fetchone() is not None

    async def import_codes(self, codes: list[str]) -> dict[str, int]:
        normalized: list[str] = []
        seen: set[str] = set()
        submitted = 0
        for raw_code in codes:
            code = str(raw_code).strip()
            if not code:
                continue
            submitted += 1
            if code in seen:
                continue
            if len(code) > 512:
                raise ValueError("单个兑换码不能超过 512 个字符")
            seen.add(code)
            normalized.append(code)

        inserted = 0
        async with self._write_lock, self._connection() as db:
            for code in normalized:
                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO gift_codes(code, created_at)
                    VALUES (?, ?)
                    """,
                    (code, utc_now()),
                )
                inserted += cursor.rowcount
            await db.commit()
        return {
            "received": submitted,
            "inserted": inserted,
            "duplicates": submitted - inserted,
        }

    async def list_codes(self, page: int, page_size: int) -> dict:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        offset = (page - 1) * page_size
        async with self._connection() as db:
            total_cursor = await db.execute("SELECT COUNT(*) AS total FROM gift_codes")
            total_row = await total_cursor.fetchone()
            total = int(total_row["total"]) if total_row else 0
            cursor = await db.execute(
                """
                SELECT id, code, created_at
                FROM gift_codes
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            )
            rows = await cursor.fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": offset + page_size < total,
        }

    async def delete_code(self, code_id: int) -> bool:
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                "DELETE FROM gift_codes WHERE id = ?",
                (int(code_id),),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def list_claims(self, page: int, page_size: int) -> dict:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        offset = (page - 1) * page_size
        async with self._connection() as db:
            total_cursor = await db.execute("SELECT COUNT(*) AS total FROM claims")
            total_row = await total_cursor.fetchone()
            total = int(total_row["total"]) if total_row else 0
            cursor = await db.execute(
                """
                SELECT user_id, group_id, code_suffix, code_digest, claimed_at
                FROM claims
                ORDER BY claimed_at DESC, user_id ASC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            )
            rows = await cursor.fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": offset + page_size < total,
        }

    async def summary(self) -> dict[str, int]:
        async with self._connection() as db:
            queries = {
                "available_codes": "SELECT COUNT(*) AS value FROM gift_codes",
                "claimed_users": "SELECT COUNT(*) AS value FROM claims",
                "eligible_members": "SELECT COUNT(*) AS value FROM eligible_members",
                "pending_newcomers": """
                    SELECT COUNT(DISTINCT eligible_members.user_id) AS value
                    FROM eligible_members
                    LEFT JOIN claims ON claims.user_id = eligible_members.user_id
                    WHERE claims.user_id IS NULL
                """,
            }
            result: dict[str, int] = {}
            for key, query in queries.items():
                cursor = await db.execute(query)
                row = await cursor.fetchone()
                result[key] = int(row["value"]) if row else 0
            return result

    async def claim_code(
        self,
        *,
        group_id: str,
        user_id: str,
        send_code: Callable[[str], Awaitable[None]],
    ) -> ClaimOutcome:
        """Send and consume one code in a single serialized inventory operation."""
        group_id = str(group_id).strip()
        user_id = str(user_id).strip()
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            claim_cursor = await db.execute(
                "SELECT 1 FROM claims WHERE user_id = ? LIMIT 1",
                (user_id,),
            )
            if await claim_cursor.fetchone() is not None:
                await db.rollback()
                return ClaimOutcome("already_claimed")

            eligible_cursor = await db.execute(
                """
                SELECT 1 FROM eligible_members
                WHERE group_id = ? AND user_id = ?
                LIMIT 1
                """,
                (group_id, user_id),
            )
            if await eligible_cursor.fetchone() is None:
                await db.rollback()
                return ClaimOutcome("not_eligible")

            code_cursor = await db.execute(
                """
                SELECT id, code FROM gift_codes
                ORDER BY id ASC
                LIMIT 1
                """
            )
            code_row = await code_cursor.fetchone()
            if code_row is None:
                await db.rollback()
                return ClaimOutcome("no_codes")

            code_id = int(code_row["id"])
            code = str(code_row["code"])
            try:
                await send_code(code)
            except Exception as exc:  # noqa: BLE001 - delivery adapter boundary
                await db.rollback()
                return ClaimOutcome("send_failed", type(exc).__name__)

            await db.execute(
                """
                INSERT INTO claims(
                    user_id, group_id, code_suffix, code_digest, claimed_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    group_id,
                    code_suffix(code),
                    code_digest(code),
                    utc_now(),
                ),
            )
            await db.execute("DELETE FROM gift_codes WHERE id = ?", (code_id,))
            await db.commit()
            return ClaimOutcome("success")
