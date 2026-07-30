from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite


@asynccontextmanager
async def open_sqlite(
    db_path: str | Path,
) -> AsyncIterator[aiosqlite.Connection]:
    """Open and close aiosqlite without leaking its worker on cancellation."""
    connection = aiosqlite.connect(db_path)

    async def connect() -> aiosqlite.Connection:
        return await connection

    connect_task = asyncio.create_task(connect())
    try:
        db = await asyncio.shield(connect_task)
    except asyncio.CancelledError:
        connected = (await asyncio.gather(connect_task, return_exceptions=True))[0]
        if isinstance(connected, aiosqlite.Connection):
            close_task = asyncio.create_task(connected.close())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await close_task
        raise

    try:
        yield db
    finally:
        close_task = asyncio.create_task(db.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError):
                await close_task
            raise
