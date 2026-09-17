from __future__ import annotations

import argparse
import os
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from config import settings
from db import schema
from db.operational_events import OperationalEvent, record_event_sync


_BACKUP_PREFIX = "solid-spork-backup-"
_BACKUP_SUFFIX = ".sqlite"
_BACKUP_LEASE_KEY = "verified_sqlite_backup"


def claim_backup_lease(lease_seconds: int | None = None) -> bool:
    """Allow one runtime process to create a scheduled backup at a time."""
    duration = max(60, lease_seconds or settings.BACKUP_LEASE_SECONDS)
    database = schema.connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            INSERT INTO maintenance_leases (lease_key, claimed_at, owner_id)
            VALUES (?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(lease_key) DO UPDATE SET
                claimed_at = excluded.claimed_at, owner_id = excluded.owner_id
            WHERE maintenance_leases.claimed_at < datetime('now', ?)
            """,
            (_BACKUP_LEASE_KEY, f"{socket.gethostname()}:{os.getpid()}", f"-{duration} seconds"),
        )
        database.commit()
        return cursor.rowcount == 1
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def _backup_directory(
    value: str | Path | None = None,
    source_path: str | Path | None = None,
) -> Path:
    raw_path = Path(value or settings.BACKUP_DIR).expanduser()
    if not raw_path.is_absolute():
        raise ValueError("BACKUP_DIR must be an absolute path")
    backup_dir = raw_path.resolve()
    source = Path(
        source_path if source_path is not None else schema.current_database_path()
    ).expanduser().resolve()
    database_dir = source.parent
    if backup_dir == database_dir or database_dir in backup_dir.parents:
        raise ValueError("BACKUP_DIR must be outside the database directory")
    return backup_dir


def _backup_files(backup_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in backup_dir.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}")
            if path.is_file()
        ),
        key=lambda path: path.name,
        reverse=True,
    )


def _retire_backups(backup_dir: Path, daily_retention: int, monthly_retention: int) -> None:
    files = _backup_files(backup_dir)
    keep = set(files[:daily_retention])
    monthly_kept = 0
    months: set[str] = set()
    for path in files[daily_retention:]:
        month = path.name[len(_BACKUP_PREFIX):len(_BACKUP_PREFIX) + 6]
        if month not in months and monthly_kept < monthly_retention:
            months.add(month)
            keep.add(path)
            monthly_kept += 1
    for path in files:
        if path not in keep:
            path.unlink()


def backup_database(
    source_path: str | Path | None = None,
    backup_directory: str | Path | None = None,
) -> Path:
    source = Path(
        source_path if source_path is not None else schema.current_database_path()
    ).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    backup_dir = _backup_directory(backup_directory, source)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
    target = backup_dir / f"{_BACKUP_PREFIX}{timestamp}{_BACKUP_SUFFIX}"
    temporary = backup_dir / f".{target.name}.{os.getpid()}.tmp"
    if target.exists() or temporary.exists():
        raise FileExistsError(target)

    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()

    try:
        verified_connection = sqlite3.connect(f"file:{temporary.as_posix()}?mode=ro", uri=True)
        try:
            quick_check = verified_connection.execute("PRAGMA quick_check").fetchone()[0]
            integrity_check = verified_connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_issues = verified_connection.execute("PRAGMA foreign_key_check").fetchall()
            schema_version = verified_connection.execute("PRAGMA user_version").fetchone()[0]
            verified_connection.execute("SELECT COUNT(*) FROM books").fetchone()
        finally:
            verified_connection.close()
        if quick_check != "ok" or integrity_check != "ok" or foreign_key_issues or schema_version != schema.SCHEMA_VERSION:
            raise sqlite3.DatabaseError("backup integrity check failed")
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        _retire_backups(
            backup_dir,
            settings.BACKUP_DAILY_RETENTION,
            settings.BACKUP_MONTHLY_RETENTION,
        )
        try:
            record_event_sync(
                OperationalEvent(
                    severity="info",
                    component="backup",
                    event="sqlite_backup",
                    outcome="succeeded",
                )
            )
        except Exception:
            pass
        return target
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        try:
            record_event_sync(
                OperationalEvent(
                    severity="critical",
                    component="backup",
                    event="sqlite_backup",
                    outcome="failed",
                    reason="backup_or_verification_failed",
                    error_type=type(exc).__name__,
                )
            )
        except Exception:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verified SQLite backup")
    parser.add_argument("--backup-dir", help="Absolute destination directory")
    args = parser.parse_args()
    schema.initialize_database()
    target = backup_database(backup_directory=args.backup_dir)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
