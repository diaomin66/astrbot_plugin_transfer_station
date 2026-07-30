from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from weakref import WeakKeyDictionary

import aiosqlite

SCHEMA_VERSION = 3
MAX_IMPORT_CODES = 10000
_SHARED_INIT_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Lock],
] = WeakKeyDictionary()


def _shared_init_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = str(path.resolve()).casefold()
    locks = _SHARED_INIT_LOCKS.setdefault(loop, {})
    return locks.setdefault(key, asyncio.Lock())


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def code_digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def code_suffix(code: str) -> str:
    return code[-4:] if len(code) > 4 else code


def is_clear_delivery_failure(exc: Exception) -> bool:
    if getattr(exc, "retcode", None) is not None:
        return True
    return type(exc).__name__ in {
        "ActionFailed",
        "Unauthorized",
    }


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    status: str
    error_type: str = ""


class GiftStorage:
    """Persistent eligibility, inventory and claim storage."""

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
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(
                        "gifts.db 版本高于当前插件支持版本，拒绝降级打开"
                    )
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

                    CREATE TABLE IF NOT EXISTS known_users (
                        user_id TEXT PRIMARY KEY,
                        first_group_id TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        eligible_group_id TEXT
                    );

                    CREATE TABLE IF NOT EXISTS group_baselines (
                        group_id TEXT PRIMARY KEY,
                        synced_at TEXT NOT NULL,
                        member_count INTEGER NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_known_users_source_seen
                    ON known_users(source, first_seen_at);

                    CREATE TABLE IF NOT EXISTS gift_reservations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code_id INTEGER NOT NULL UNIQUE
                            REFERENCES gift_codes(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL UNIQUE,
                        group_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reserved_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        error_type TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_gift_reservation_status
                    ON gift_reservations(status, updated_at);
                    """
                )
                if current_version < 2:
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO known_users(
                            user_id, first_group_id, first_seen_at,
                            source, eligible_group_id
                        )
                        SELECT user_id, group_id, claimed_at, 'claimed', NULL
                        FROM claims
                        """
                    )
                    await db.execute(
                        """
                        INSERT OR IGNORE INTO known_users(
                            user_id, first_group_id, first_seen_at,
                            source, eligible_group_id
                        )
                        SELECT user_id, group_id, joined_at, 'legacy', NULL
                        FROM eligible_members
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

    async def is_group_baselined(self, group_id: str) -> bool:
        group_id = str(group_id).strip()
        if not group_id:
            return False
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT 1 FROM group_baselines
                WHERE group_id = ?
                LIMIT 1
                """,
                (group_id,),
            )
            return await cursor.fetchone() is not None

    async def record_group_baseline(
        self,
        group_id: str,
        user_ids: list[str],
    ) -> dict[str, int | bool]:
        group_id = str(group_id).strip()
        normalized = sorted(
            {str(user_id).strip() for user_id in user_ids if str(user_id).strip()}
        )
        if not group_id:
            return {"created": False, "members": 0, "inserted_users": 0}

        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = await db.execute(
                "SELECT 1 FROM group_baselines WHERE group_id = ? LIMIT 1",
                (group_id,),
            )
            created = await existing.fetchone() is None

            now = utc_now()
            inserted_users = 0
            for user_id in normalized:
                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO known_users(
                        user_id, first_group_id, first_seen_at, source, eligible_group_id
                    )
                    VALUES (?, ?, ?, 'baseline', NULL)
                    """,
                    (user_id, group_id, now),
                )
                inserted_users += cursor.rowcount

            await db.execute(
                """
                INSERT INTO group_baselines(group_id, synced_at, member_count)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    synced_at = excluded.synced_at,
                    member_count = excluded.member_count
                """,
                (group_id, now, len(normalized)),
            )
            await db.commit()
            return {
                "created": created,
                "members": len(normalized),
                "inserted_users": inserted_users,
            }

    async def register_newcomer(self, group_id: str, user_id: str) -> str:
        """Permanently register a never-seen QQ ID as a one-time newcomer."""
        group_id = str(group_id).strip()
        user_id = str(user_id).strip()
        if not group_id or not user_id:
            return "baseline_pending"

        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            baseline_cursor = await db.execute(
                """
                SELECT 1 FROM group_baselines
                WHERE group_id = ?
                LIMIT 1
                """,
                (group_id,),
            )
            if await baseline_cursor.fetchone() is None:
                await db.rollback()
                return "baseline_pending"

            known_cursor = await db.execute(
                "SELECT 1 FROM known_users WHERE user_id = ? LIMIT 1",
                (user_id,),
            )
            if await known_cursor.fetchone() is not None:
                await db.rollback()
                return "known"

            now = utc_now()
            await db.execute(
                """
                INSERT INTO known_users(
                    user_id, first_group_id, first_seen_at, source, eligible_group_id
                )
                VALUES (?, ?, ?, 'newcomer', ?)
                """,
                (user_id, group_id, now, group_id),
            )
            await db.execute(
                """
                INSERT OR IGNORE INTO eligible_members(group_id, user_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (group_id, user_id, now),
            )
            await db.commit()
            return "eligible"

    async def is_eligible(self, group_id: str, user_id: str) -> bool:
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM known_users
                JOIN eligible_members
                  ON eligible_members.user_id = known_users.user_id
                 AND eligible_members.group_id = known_users.eligible_group_id
                LEFT JOIN claims ON claims.user_id = known_users.user_id
                WHERE known_users.user_id = ?
                  AND known_users.eligible_group_id = ?
                  AND known_users.source = 'newcomer'
                  AND claims.user_id IS NULL
                LIMIT 1
                """,
                (str(user_id), str(group_id)),
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
        if len(normalized) > MAX_IMPORT_CODES:
            raise ValueError(f"单次最多导入 {MAX_IMPORT_CODES} 个兑换码")
        async with self._write_lock, self._connection() as db:
            before = db.total_changes
            await db.executemany(
                """
                INSERT OR IGNORE INTO gift_codes(code, created_at)
                VALUES (?, ?)
                """,
                [(code, utc_now()) for code in normalized],
            )
            inserted = db.total_changes - before
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
            total_cursor = await db.execute(
                """
                SELECT COUNT(*) AS total
                FROM gift_codes AS gc
                LEFT JOIN gift_reservations AS gr ON gr.code_id = gc.id
                WHERE gr.id IS NULL
                """
            )
            total_row = await total_cursor.fetchone()
            total = int(total_row["total"]) if total_row else 0
            cursor = await db.execute(
                """
                SELECT gc.id, gc.code, gc.created_at
                FROM gift_codes AS gc
                LEFT JOIN gift_reservations AS gr ON gr.code_id = gc.id
                WHERE gr.id IS NULL
                ORDER BY gc.id ASC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            )
            rows = await cursor.fetchall()
        return {
            "items": [{**dict(row), "id": str(row["id"])} for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": offset + page_size < total,
        }

    async def delete_code(self, code_id: int) -> bool:
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                """
                DELETE FROM gift_codes
                WHERE id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM gift_reservations
                    WHERE gift_reservations.code_id = gift_codes.id
                  )
                """,
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
                "available_codes": """
                    SELECT COUNT(*) AS value
                    FROM gift_codes AS gc
                    LEFT JOIN gift_reservations AS gr ON gr.code_id = gc.id
                    WHERE gr.id IS NULL
                """,
                "gift_manual_reviews": """
                    SELECT COUNT(*) AS value
                    FROM gift_reservations
                    WHERE status = 'manual_review'
                """,
                "claimed_users": "SELECT COUNT(*) AS value FROM claims",
                "eligible_members": """
                    SELECT COUNT(*) AS value
                    FROM known_users
                    WHERE source = 'newcomer'
                """,
                "pending_newcomers": """
                    SELECT COUNT(*) AS value
                    FROM known_users
                    LEFT JOIN claims ON claims.user_id = known_users.user_id
                    WHERE known_users.source = 'newcomer'
                      AND claims.user_id IS NULL
                """,
                "known_users": """
                    SELECT COUNT(*) AS value
                    FROM known_users
                """,
                "today_newcomers": """
                    SELECT COUNT(*) AS value
                    FROM known_users
                    WHERE source = 'newcomer'
                      AND date(first_seen_at, 'localtime') = date('now', 'localtime')
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
        """Reserve, send, and consume one code without holding a network transaction."""
        group_id = str(group_id).strip()
        user_id = str(user_id).strip()
        reservation = await self._reserve_claim(group_id, user_id)
        if isinstance(reservation, ClaimOutcome):
            return reservation
        reservation_id = int(reservation["reservation_id"])
        code = str(reservation["code"])
        try:
            await send_code(code)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._mark_reservation_review(
                    reservation_id,
                    "CancelledError",
                )
            )
            raise
        except TimeoutError as exc:
            await self._mark_reservation_review(
                reservation_id,
                type(exc).__name__,
            )
            return ClaimOutcome("send_ambiguous", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - delivery adapter boundary
            if is_clear_delivery_failure(exc):
                released = await self._release_reservation(
                    reservation_id,
                    expected_status="reserved",
                )
                if released:
                    return ClaimOutcome("send_failed", type(exc).__name__)
                return ClaimOutcome("send_ambiguous", "ReservationStateChanged")
            await self._mark_reservation_review(
                reservation_id,
                type(exc).__name__,
            )
            return ClaimOutcome("send_ambiguous", type(exc).__name__)
        try:
            completed = await self._complete_reservation(
                reservation_id,
                expected_status="reserved",
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._mark_reservation_review(
                    reservation_id,
                    "CancelledError",
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 - durable finalize boundary
            await self._mark_reservation_review(
                reservation_id,
                type(exc).__name__,
            )
            return ClaimOutcome("send_ambiguous", type(exc).__name__)
        if not completed:
            return ClaimOutcome("send_ambiguous", "ReservationStateChanged")
        return ClaimOutcome("success")

    async def _reserve_claim(
        self,
        group_id: str,
        user_id: str,
    ) -> dict[str, str | int] | ClaimOutcome:
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            claim_cursor = await db.execute(
                "SELECT 1 FROM claims WHERE user_id = ? LIMIT 1",
                (user_id,),
            )
            if await claim_cursor.fetchone() is not None:
                await db.rollback()
                return ClaimOutcome("already_claimed")
            reservation_cursor = await db.execute(
                """
                SELECT status FROM gift_reservations
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            )
            if await reservation_cursor.fetchone() is not None:
                await db.rollback()
                return ClaimOutcome("send_ambiguous")

            eligible_cursor = await db.execute(
                """
                SELECT 1
                FROM known_users
                JOIN eligible_members
                  ON eligible_members.user_id = known_users.user_id
                 AND eligible_members.group_id = known_users.eligible_group_id
                WHERE known_users.user_id = ?
                  AND known_users.eligible_group_id = ?
                  AND known_users.source = 'newcomer'
                LIMIT 1
                """,
                (user_id, group_id),
            )
            if await eligible_cursor.fetchone() is None:
                await db.rollback()
                return ClaimOutcome("not_eligible")

            code_cursor = await db.execute(
                """
                SELECT gc.id, gc.code
                FROM gift_codes AS gc
                LEFT JOIN gift_reservations AS gr ON gr.code_id = gc.id
                WHERE gr.id IS NULL
                ORDER BY gc.id ASC
                LIMIT 1
                """
            )
            code_row = await code_cursor.fetchone()
            if code_row is None:
                await db.rollback()
                return ClaimOutcome("no_codes")

            code_id = int(code_row["id"])
            code = str(code_row["code"])
            now = utc_now()
            cursor = await db.execute(
                """
                INSERT INTO gift_reservations(
                    code_id, user_id, group_id, status,
                    reserved_at, updated_at
                )
                VALUES (?, ?, ?, 'reserved', ?, ?)
                """,
                (code_id, user_id, group_id, now, now),
            )
            reservation_id = int(cursor.lastrowid)
            await db.commit()
            return {
                "reservation_id": reservation_id,
                "code": code,
            }

    async def _complete_reservation(
        self,
        reservation_id: int,
        *,
        expected_status: str,
    ) -> bool:
        if expected_status not in {"reserved", "manual_review"}:
            raise ValueError("invalid_reservation_status")
        async with self._write_lock, self._connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT gr.id, gr.code_id, gr.user_id, gr.group_id,
                       gr.status, gc.code
                FROM gift_reservations AS gr
                JOIN gift_codes AS gc ON gc.id = gr.code_id
                WHERE gr.id = ? AND gr.status = ?
                """,
                (int(reservation_id), expected_status),
            )
            reservation = await cursor.fetchone()
            if not reservation:
                await db.rollback()
                return False
            code = str(reservation["code"])
            await db.execute(
                """
                INSERT INTO claims(
                    user_id, group_id, code_suffix, code_digest, claimed_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(reservation["user_id"]),
                    str(reservation["group_id"]),
                    code_suffix(code),
                    code_digest(code),
                    utc_now(),
                ),
            )
            await db.execute(
                """
                UPDATE known_users
                SET eligible_group_id = NULL
                WHERE user_id = ?
                """,
                (str(reservation["user_id"]),),
            )
            await db.execute(
                "DELETE FROM eligible_members WHERE user_id = ?",
                (str(reservation["user_id"]),),
            )
            await db.execute(
                "DELETE FROM gift_codes WHERE id = ?",
                (int(reservation["code_id"]),),
            )
            await db.commit()
            return True

    async def _release_reservation(
        self,
        reservation_id: int,
        *,
        expected_status: str,
    ) -> bool:
        if expected_status not in {"reserved", "manual_review"}:
            raise ValueError("invalid_reservation_status")
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                """
                DELETE FROM gift_reservations
                WHERE id = ? AND status = ?
                """,
                (int(reservation_id), expected_status),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def _mark_reservation_review(
        self,
        reservation_id: int,
        error_type: str,
    ) -> bool:
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                """
                UPDATE gift_reservations
                SET status = 'manual_review', updated_at = ?, error_type = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (utc_now(), str(error_type)[:100], int(reservation_id)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def list_gift_reviews(self, page: int, page_size: int) -> dict:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        offset = (page - 1) * page_size
        async with self._connection() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS total
                FROM gift_reservations
                WHERE status = 'manual_review'
                """
            )
            total_row = await cursor.fetchone()
            total = int(total_row["total"] or 0)
            cursor = await db.execute(
                """
                SELECT gr.id, gr.user_id, gr.group_id, gr.reserved_at,
                       gr.updated_at, gr.error_type,
                       substr(gc.code, -4) AS code_suffix
                FROM gift_reservations AS gr
                JOIN gift_codes AS gc ON gc.id = gr.code_id
                WHERE gr.status = 'manual_review'
                ORDER BY gr.updated_at DESC, gr.id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            )
            items = [
                {**dict(row), "id": str(row["id"])} for row in await cursor.fetchall()
            ]
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": offset + len(items) < total,
        }

    async def recover_reserved(self, *, stale_before: str) -> int:
        async with self._write_lock, self._connection() as db:
            cursor = await db.execute(
                """
                UPDATE gift_reservations
                SET status = 'manual_review',
                    updated_at = ?,
                    error_type = CASE
                        WHEN error_type = '' THEN 'ProcessRestarted'
                        ELSE error_type
                    END
                WHERE status = 'reserved' AND updated_at <= ?
                """,
                (utc_now(), str(stale_before)),
            )
            await db.commit()
            return cursor.rowcount

    async def review_gift_delivery(
        self,
        reservation_id: int,
        *,
        delivered: bool,
    ) -> bool:
        if delivered:
            return await self._complete_reservation(
                reservation_id,
                expected_status="manual_review",
            )
        return await self._release_reservation(
            reservation_id,
            expected_status="manual_review",
        )
