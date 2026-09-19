from __future__ import annotations

import os
import json
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from controlplane.deployments import tenant_system_user
from controlplane.unit_templates import tenant_unit_name
from db.schema import initialize_database


_USER_PATTERN = re.compile(r"tenant-[0-9a-f]{16}\Z")
_CADDY_USER_PATTERN = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_UNIT_PATTERN = re.compile(r"bookapp-tenant@[0-9a-f-]{36}\.service\Z")
_REQUIRED_RUNTIME_CREDENTIALS = frozenset({
    "telegram_bot_token",
    "telegram_webhook_secret",
})
_OPTIONAL_RUNTIME_CREDENTIALS = frozenset({"bot_proxy_url"})
_RUNTIME_CREDENTIALS = _REQUIRED_RUNTIME_CREDENTIALS | _OPTIONAL_RUNTIME_CREDENTIALS


class LinuxOperationsError(RuntimeError):
    pass


@dataclass(frozen=True)
class LinuxOperationsPaths:
    credential_root: Path
    public_key_file: Path
    unit_root: Path
    caddy_route_root: Path
    caddy_config: Path
    runtime_root: Path
    caddy_user: str = "caddy"

    def __post_init__(self):
        for path in (
            self.credential_root,
            self.public_key_file,
            self.unit_root,
            self.caddy_route_root,
            self.caddy_config,
            self.runtime_root,
        ):
            if not path.is_absolute():
                raise LinuxOperationsError("linux controller paths must be absolute")
        if not _CADDY_USER_PATTERN.fullmatch(self.caddy_user):
            raise LinuxOperationsError("invalid Caddy user")


def _tenant_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LinuxOperationsError("invalid tenant id") from exc


def _run(arguments: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=capture_output,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LinuxOperationsError("privileged deployment operation failed") from exc


def _lookup_tenant_uid(system_user: str) -> int | None:
    try:
        result = subprocess.run(
            ["id", "-u", system_user],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise LinuxOperationsError("tenant identity lookup failed") from exc
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise LinuxOperationsError("tenant identity lookup failed")
    value = result.stdout.strip()
    if not value.isdigit() or int(value) <= 0:
        raise LinuxOperationsError("tenant system user is unavailable")
    return int(value)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class LinuxPrivilegedOperations:
    def __init__(self, paths: LinuxOperationsPaths):
        if os.name != "posix":
            raise LinuxOperationsError("linux deployment operations require POSIX")
        self.paths = paths

    def ensure_tenant_identity(self, system_user: str) -> int:
        if not _USER_PATTERN.fullmatch(system_user):
            raise LinuxOperationsError("invalid tenant system user")
        uid = _lookup_tenant_uid(system_user)
        if uid is None:
            _run([
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                system_user,
            ])
            uid = _lookup_tenant_uid(system_user)
        if uid is None:
            raise LinuxOperationsError("tenant system user is unavailable")
        return uid

    def prepare_tenant_storage(
        self,
        *,
        uid: int,
        database_path: Path,
        media_root: Path,
        backup_root: Path,
        runtime_directory: Path,
    ) -> None:
        if uid <= 0:
            raise LinuxOperationsError("invalid tenant uid")
        tenant_storage_roots = {
            database_path.parent.parent.resolve(),
            backup_root.parent.parent.resolve(),
        }
        for root in sorted(tenant_storage_roots):
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root.chmod(0o700)
            self._grant_tenant_storage_access(root, uid)
        runtime_root = runtime_directory.parent
        runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_root.chmod(0o700)
        self._grant_tenant_storage_access(runtime_root, uid)
        self._grant_caddy_runtime_access(runtime_root, runtime_directory)
        for path in (database_path.parent, media_root, backup_root, runtime_directory):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(path, uid, uid)
            path.chmod(0o700)
        self._grant_caddy_socket_access(runtime_directory)

    @staticmethod
    def _grant_tenant_storage_access(root: Path, uid: int) -> None:
        _run([
            "setfacl",
            "-m",
            f"u:{uid}:--x,m::--x",
            str(root),
        ])

    def _grant_caddy_runtime_access(
        self, runtime_root: Path, runtime_directory: Path
    ) -> None:
        if runtime_directory.parent.resolve() != runtime_root.resolve():
            raise LinuxOperationsError("invalid tenant runtime directory")
        _run([
            "setfacl",
            "-m",
            f"u:{self.paths.caddy_user}:--x,m::--x",
            str(runtime_root),
        ])

    def _grant_caddy_socket_access(self, runtime_directory: Path) -> None:
        _run([
            "setfacl",
            "-m",
            f"u:{self.paths.caddy_user}:--x,m::--x",
            str(runtime_directory),
        ])
        _run([
            "setfacl",
            "-m",
            f"d:u:{self.paths.caddy_user}:rw-,d:m::rw-",
            str(runtime_directory),
        ])

    def initialize_tenant_database(
        self,
        *,
        database_path: Path,
        uid: int,
        tenant_id: str,
        canonical_host: str,
        owner_telegram_id: int,
        display_name: str,
    ) -> None:
        _tenant_id(tenant_id)
        if uid <= 0 or owner_telegram_id <= 0 or not display_name.strip():
            raise LinuxOperationsError("invalid tenant runtime metadata")
        initialize_database(database_path, seed_catalog=False)
        database = sqlite3.connect(database_path)
        try:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                INSERT INTO tenant_runtime_metadata (
                    id, tenant_id, canonical_host, owner_telegram_id
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    canonical_host = excluded.canonical_host,
                    owner_telegram_id = excluded.owner_telegram_id
                """,
                (tenant_id, canonical_host, owner_telegram_id),
            )
            database.execute(
                """
                INSERT INTO storefront_settings (id, store_name)
                VALUES (1, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (display_name.strip(),),
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()
        os.chown(database_path, uid, uid)
        database_path.chmod(0o600)

    def owner_claim_verdict(
        self, *, database_path: Path, owner_telegram_id: int
    ) -> str:
        if owner_telegram_id <= 0:
            raise LinuxOperationsError("invalid owner telegram id")
        try:
            database = sqlite3.connect(database_path)
            try:
                row = database.execute(
                    "SELECT telegram_user_id FROM tenant_owner_claims WHERE id = 1"
                ).fetchone()
            finally:
                database.close()
        except sqlite3.Error as exc:
            raise LinuxOperationsError("tenant owner claim is unavailable") from exc
        if row is None:
            return "missing"
        return "claimed" if row[0] == owner_telegram_id else "owner_mismatch"

    def write_runtime_material(
        self,
        *,
        tenant_id: str,
        runtime_generation: int,
        credentials: dict[str, bytes],
        manifest: bytes,
        signature: bytes,
        public_key: bytes,
    ) -> None:
        tenant_id = _tenant_id(tenant_id)
        if runtime_generation <= 0:
            raise LinuxOperationsError("invalid runtime generation")
        target = (self.paths.credential_root / tenant_id).resolve()
        if target.parent != self.paths.credential_root.resolve():
            raise LinuxOperationsError("invalid tenant credential target")
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _REQUIRED_RUNTIME_CREDENTIALS.issubset(credentials):
            raise LinuxOperationsError("required tenant credential is unavailable")
        for name, value in credentials.items():
            if name not in _RUNTIME_CREDENTIALS or not value:
                raise LinuxOperationsError("unsupported tenant credential")
            _atomic_write(target / name, value, 0o400)
        for name in _OPTIONAL_RUNTIME_CREDENTIALS - set(credentials):
            _atomic_write(target / name, b"", 0o400)
        _atomic_write(target / "runtime.json", manifest, 0o400)
        _atomic_write(target / "runtime.sig", signature, 0o400)
        _atomic_write(self.paths.public_key_file, public_key, 0o444)

    def install_tenant_unit(self, *, unit_name: str, dropin: str) -> None:
        if not _UNIT_PATTERN.fullmatch(unit_name):
            raise LinuxOperationsError("invalid tenant unit")
        unit_directory = self.paths.unit_root / f"{unit_name}.d"
        _atomic_write(unit_directory / "runtime.conf", dropin.encode("utf-8"), 0o644)
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "enable", unit_name])

    def start_tenant_unit(self, unit_name: str) -> None:
        if not _UNIT_PATTERN.fullmatch(unit_name):
            raise LinuxOperationsError("invalid tenant unit")
        _run(["systemctl", "restart", unit_name])

    def check_tenant_health(self, tenant_id: str, runtime_generation: int) -> None:
        _tenant_id(tenant_id)
        if runtime_generation <= 0:
            raise LinuxOperationsError("invalid runtime generation")
        socket_path = (self.paths.runtime_root / tenant_id / "tenant.sock").resolve()
        for attempt in range(6):
            try:
                _run(["systemctl", "is-active", "--quiet", tenant_unit_name(tenant_id)])
                result = _run(
                    [
                        "curl",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--connect-timeout",
                        "2",
                        "--max-time",
                        "4",
                        "--unix-socket",
                        str(socket_path),
                        "http://localhost/health",
                    ],
                    capture_output=True,
                )
            except LinuxOperationsError:
                if attempt < 5:
                    time.sleep(1)
                    continue
                raise LinuxOperationsError("tenant health response is invalid") from None
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise LinuxOperationsError("tenant health response is invalid") from exc
            if payload != {"tenant_id": tenant_id, "generation": runtime_generation}:
                raise LinuxOperationsError("tenant health response is invalid")
            return

    def publish_tenant_route(self, *, tenant_id: str, route: str) -> None:
        tenant_id = _tenant_id(tenant_id)
        target = (self.paths.caddy_route_root / f"{tenant_id}.caddy").resolve()
        if target.parent != self.paths.caddy_route_root.resolve() or target.is_symlink():
            raise LinuxOperationsError("invalid tenant route target")
        candidate = route.encode("utf-8")
        if target.is_file() and target.read_bytes() == candidate:
            return
        previous = target.read_bytes() if target.is_file() else None
        try:
            _atomic_write(target, candidate, 0o644)
            _run(["caddy", "validate", "--config", str(self.paths.caddy_config)])
            _run(["systemctl", "reload", "caddy"])
        except Exception:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, previous, 0o644)
            raise
