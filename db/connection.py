from __future__ import annotations

from contextlib import asynccontextmanager

import aiosqlite

from db.schema import DB_PATH as INITIAL_DB_PATH
from db.schema import current_database_path


DB_PATH = INITIAL_DB_PATH


@asynccontextmanager
async def connection():
    path = DB_PATH if DB_PATH != INITIAL_DB_PATH else current_database_path()
    database = await aiosqlite.connect(str(path), timeout=10)
    try:
        await database.execute("PRAGMA foreign_keys = ON")
        await database.execute("PRAGMA busy_timeout = 10000")
        yield database
    finally:
        await database.close()
