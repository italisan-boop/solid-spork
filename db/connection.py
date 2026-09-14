from __future__ import annotations

from contextlib import asynccontextmanager

import aiosqlite

from db.schema import DB_PATH


@asynccontextmanager
async def connection():
    database = await aiosqlite.connect(str(DB_PATH), timeout=10)
    try:
        await database.execute("PRAGMA foreign_keys = ON")
        await database.execute("PRAGMA busy_timeout = 10000")
        yield database
    finally:
        await database.close()
