"""FSM-хранилище на SQLite.

Вместо MemoryStorage (теряет состояния при рестарте) используем таблицу
в той же SQLite-базе — состояния и данные FSM переживают рестарт бота
и доступны с нескольких воркеров.
"""
import json

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import (
    BaseStorage,
    DefaultKeyBuilder,
    StateType,
    StorageKey,
)

from db.schema import current_database_path


class SQLiteStorage(BaseStorage):
    """FSM Storage поверх aiosqlite: ключ -> state + JSON-данные."""

    TABLE = "fsm_records"

    def __init__(
        self,
        db_path: str | None = None,
        key_builder: "DefaultKeyBuilder | None" = None,
    ) -> None:
        self.db_path = db_path or str(current_database_path())
        self._db: aiosqlite.Connection | None = None
        self.key_builder = key_builder or DefaultKeyBuilder(
            with_bot_id=True, with_destiny=True
        )

    async def _connect(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path, timeout=10)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA foreign_keys = ON")
            await self._db.execute("PRAGMA busy_timeout = 10000")
        return self._db

    def _key(self, key: StorageKey) -> str:
        return self.key_builder.build(key)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_str = state.state if isinstance(state, State) else state
        db = await self._connect()
        fsm_key = self._key(key)
        await db.execute(
            f"""
            INSERT INTO {self.TABLE} (fsm_key, state, data)
            VALUES (?, ?, ?)
            ON CONFLICT(fsm_key) DO UPDATE SET state = excluded.state
            """,
            (fsm_key, state_str, "{}"),
        )
        await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        db = await self._connect()
        fsm_key = self._key(key)
        cursor = await db.execute(
            f"SELECT state FROM {self.TABLE} WHERE fsm_key = ?", (fsm_key,)
        )
        row = await cursor.fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: dict) -> None:
        db = await self._connect()
        fsm_key = self._key(key)
        payload = json.dumps(data)
        await db.execute(
            f"""
            INSERT INTO {self.TABLE} (fsm_key, state, data)
            VALUES (?, NULL, ?)
            ON CONFLICT(fsm_key) DO UPDATE SET data = excluded.data
            """,
            (fsm_key, payload),
        )
        await db.commit()

    async def get_data(self, key: StorageKey) -> dict:
        db = await self._connect()
        fsm_key = self._key(key)
        cursor = await db.execute(
            f"SELECT data FROM {self.TABLE} WHERE fsm_key = ?", (fsm_key,)
        )
        row = await cursor.fetchone()
        if not row or row["data"] is None:
            return {}
        try:
            return json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            return {}

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None