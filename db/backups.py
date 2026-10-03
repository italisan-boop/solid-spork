from __future__ import annotations

import argparse
import os
import socket
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from config import settings
from db import schema
from db.operational_events import OperationalEvent, record_event_sync
from runtime.context import current_tenant_context


_BACKUP_PREFIX = "solid-spork-backup-"
_BACKUP_SUFFIX = ".sqlite"
_BACKUP_LEASE_KEY = "verified_sqlite_backup"
_REQUIRED_UPLOAD_TABLES = {
    "books",
    "orders",
    "payment_settings",
    "promo_codes",
    "message_templates",
    "tenant_runtime_metadata",
}
_SQLITE_HEADER = b"SQLite format 3\x00"


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def claim_backup_lease(lease_seconds: int | None = None) -> bool:
    """Allow one runtime process to create a SQLite backup at a time."""
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
            (_BACKUP_LEASE_KEY, _lease_owner(), f"-{duration} seconds"),
        )
        database.commit()
        return cursor.rowcount == 1
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def release_backup_lease() -> None:
    database = schema.connect()
    try:
        database.execute(
            "DELETE FROM maintenance_leases WHERE lease_key = ? AND owner_id = ?",
            (_BACKUP_LEASE_KEY, _lease_owner()),
        )
        database.commit()
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


def _verify_database(path: Path, *, expected_context: bool) -> int:
    with path.open("rb") as source:
        if source.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
            raise ValueError("uploaded file is not a SQLite database")
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        database.execute("PRAGMA trusted_schema = OFF")
        quick_check = database.execute("PRAGMA quick_check").fetchone()[0]
        integrity_check = database.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_issues = database.execute("PRAGMA foreign_key_check").fetchall()
        schema_version = database.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if (
            quick_check != "ok"
            or integrity_check != "ok"
            or foreign_key_issues
            or schema_version != schema.SCHEMA_VERSION
            or not _REQUIRED_UPLOAD_TABLES.issubset(tables)
        ):
            raise ValueError("backup verification failed")
        if expected_context:
            context = current_tenant_context()
            metadata = database.execute(
                """
                SELECT tenant_id, canonical_host, owner_telegram_id
                FROM tenant_runtime_metadata WHERE id = 1
                """
            ).fetchone()
            if metadata != (
                context.tenant_id,
                context.canonical_host,
                context.owner_telegram_id,
            ):
                raise ValueError("backup belongs to another tenant")
        return int(schema_version)
    finally:
        database.close()


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
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S%fZ")
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
        _verify_database(temporary, expected_context=False)
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


def _tenant_root() -> Path:
    context = current_tenant_context()
    root = Path(context.backup_root).expanduser().resolve()
    source = Path(context.database_path).expanduser().resolve()
    if root == source.parent or source.parent in root.parents:
        raise ValueError("tenant backup root is invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _artifact_directory(kind: str) -> Path:
    if kind not in {"generated", "uploaded"}:
        raise ValueError("invalid backup artifact")
    directory = _tenant_root() / ("owner-generated" if kind == "generated" else "owner-staged")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory.resolve()


def _artifact_payload(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": row[0],
        "source": row[1],
        "size": row[2],
        "schema_version": row[3],
        "created_at": row[4],
        "expires_at": row[5],
        "validation": "verified",
    }


def _artifact_path(source_kind: str, storage_name: str) -> Path:
    valid_name = (
        re_full_artifact_name(storage_name)
        if source_kind == "uploaded"
        else storage_name.startswith(_BACKUP_PREFIX)
        and storage_name.endswith(_BACKUP_SUFFIX)
        and Path(storage_name).name == storage_name
    )
    if not valid_name:
        raise ValueError("invalid backup artifact")
    directory = _artifact_directory(source_kind)
    path = (directory / storage_name).resolve()
    if path.parent != directory:
        raise ValueError("invalid backup artifact")
    return path


def re_full_artifact_name(value: str) -> bool:
    return len(value) == 39 and value.endswith(".sqlite") and all(
        character in "0123456789abcdef" for character in value[:-7]
    )


def cleanup_expired_artifacts_sync() -> None:
    database = schema.connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT artifact_id, source_kind, storage_name
            FROM database_backup_artifacts
            WHERE expires_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP
            """
        ).fetchall()
        for row in rows:
            try:
                _artifact_path(row["source_kind"], row["storage_name"]).unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        if rows:
            database.executemany(
                "DELETE FROM database_backup_artifacts WHERE artifact_id = ?",
                [(row["artifact_id"],) for row in rows],
            )
            database.commit()
    finally:
        database.close()


def list_backup_artifacts_sync() -> list[dict]:
    cleanup_expired_artifacts_sync()
    database = schema.connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT artifact_id, source_kind, byte_size, schema_version, created_at, expires_at,
                   storage_name
            FROM database_backup_artifacts
            ORDER BY created_at DESC, artifact_id DESC
            """
        ).fetchall()
        result = []
        missing: list[str] = []
        for row in rows:
            try:
                exists = _artifact_path(row["source_kind"], row["storage_name"]).is_file()
            except ValueError:
                exists = False
            if not exists:
                missing.append(row["artifact_id"])
                continue
            result.append(_artifact_payload(row))
        if missing:
            database.executemany(
                "DELETE FROM database_backup_artifacts WHERE artifact_id = ?",
                [(artifact_id,) for artifact_id in missing],
            )
            database.commit()
        return result
    finally:
        database.close()


def _register_artifact(
    *, source_kind: str, storage_name: str, byte_size: int, schema_version: int, expires_at: str | None
) -> dict:
    artifact_id = uuid.uuid4().hex
    database = schema.connect()
    try:
        database.execute(
            """
            INSERT INTO database_backup_artifacts (
                artifact_id, source_kind, storage_name, byte_size, schema_version,
                validation_state, expires_at
            ) VALUES (?, ?, ?, ?, ?, 'verified', ?)
            """,
            (artifact_id, source_kind, storage_name, byte_size, schema_version, expires_at),
        )
        database.commit()
        row = database.execute(
            """
            SELECT artifact_id, source_kind, byte_size, schema_version, created_at, expires_at
            FROM database_backup_artifacts WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        return _artifact_payload(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def create_owner_backup_sync() -> dict:
    context = current_tenant_context()
    if not claim_backup_lease():
        raise RuntimeError("backup is already running")
    try:
        target = backup_database(
            source_path=context.database_path,
            backup_directory=_artifact_directory("generated"),
        )
        schema_version = _verify_database(target, expected_context=True)
        return _register_artifact(
            source_kind="generated",
            storage_name=target.name,
            byte_size=target.stat().st_size,
            schema_version=schema_version,
            expires_at=None,
        )
    finally:
        release_backup_lease()


def stage_uploaded_backup_sync(source: BinaryIO) -> dict:
    context = current_tenant_context()
    directory = _artifact_directory("uploaded")
    artifact_name = f"{uuid.uuid4().hex}.sqlite"
    temporary = directory / f".{artifact_name}.tmp"
    target = directory / artifact_name
    written = 0
    limit = max(1, settings.BACKUP_UPLOAD_MAX_BYTES)
    try:
        with temporary.open("xb") as destination:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValueError("backup file is too large")
                destination.write(chunk)
        if written == 0:
            raise ValueError("backup file is empty")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        schema_version = _verify_database(temporary, expected_context=True)
        os.replace(temporary, target)
        expires_at = (
            datetime.now(UTC) + timedelta(days=max(1, settings.BACKUP_UPLOAD_STAGING_RETENTION_DAYS))
        ).strftime("%Y-%m-%d %H:%M:%S")
        return _register_artifact(
            source_kind="uploaded",
            storage_name=artifact_name,
            byte_size=written,
            schema_version=schema_version,
            expires_at=expires_at,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise


def resolve_backup_artifact_sync(artifact_id: str) -> tuple[Path, dict] | None:
    if len(artifact_id) != 32 or any(character not in "0123456789abcdef" for character in artifact_id):
        return None
    cleanup_expired_artifacts_sync()
    database = schema.connect()
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT artifact_id, source_kind, byte_size, schema_version, created_at, expires_at,
                   storage_name
            FROM database_backup_artifacts WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            path = _artifact_path(row["source_kind"], row["storage_name"])
        except ValueError:
            return None
        if not path.is_file():
            return None
        database.execute(
            "UPDATE database_backup_artifacts SET downloaded_at = CURRENT_TIMESTAMP WHERE artifact_id = ?",
            (artifact_id,),
        )
        database.commit()
        return path, _artifact_payload(row)
    finally:
        database.close()


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
