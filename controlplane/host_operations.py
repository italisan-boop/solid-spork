from __future__ import annotations

import base64
import json
import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

from controlplane.linux_operations import LinuxOperationsPaths, LinuxPrivilegedOperations
from controlplane.route_templates import render_caddy_route
from controlplane.unit_templates import (
    ControllerPaths,
    TenantStoragePaths,
    render_tenant_dropin,
    tenant_unit_name,
)
from controlplane.deployments import tenant_system_user
from controlplane.tenants import normalize_host


_MAX_MESSAGE_LENGTH = 262_144
_CONNECTION_TIMEOUT_SECONDS = 10
_CREDENTIAL_NAMES = frozenset({
    "telegram_bot_token",
    "telegram_webhook_secret",
    "bot_proxy_url",
})


class HostOperationsError(RuntimeError):
    pass


def _absolute_path(value: str, name: str) -> Path:
    path = Path(value.strip())
    if not value.strip() or not path.is_absolute():
        raise HostOperationsError(f"{name} is required")
    return path.resolve()


def _tenant_id(value: object) -> str:
    try:
        return tenant_unit_name(str(value)).removeprefix("bookapp-tenant@").removesuffix(
            ".service"
        )
    except (TypeError, ValueError) as exc:
        raise HostOperationsError("invalid tenant id") from exc


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HostOperationsError(f"invalid {name}")
    return value


def _text(value: object, name: str, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > maximum:
        raise HostOperationsError(f"invalid {name}")
    return value


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decoded(value: object, name: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > _MAX_MESSAGE_LENGTH:
        raise HostOperationsError(f"invalid {name}")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise HostOperationsError(f"invalid {name}") from exc
    if not decoded:
        raise HostOperationsError(f"invalid {name}")
    return decoded


def _read_frame(connection: socket.socket) -> bytes:
    buffer = bytearray()
    while len(buffer) <= _MAX_MESSAGE_LENGTH:
        chunk = connection.recv(min(65_536, _MAX_MESSAGE_LENGTH + 1 - len(buffer)))
        if not chunk:
            raise HostOperationsError("incomplete host operation frame")
        buffer.extend(chunk)
        delimiter = buffer.find(b"\n")
        if delimiter < 0:
            continue
        if delimiter != len(buffer) - 1:
            raise HostOperationsError("invalid host operation frame")
        return bytes(buffer[:delimiter])
    raise HostOperationsError("host operation frame is too large")


@dataclass(frozen=True)
class HostOperationsSettings:
    socket_path: Path
    allowed_uid: int
    allowed_gid: int
    release_root: Path
    credential_root: Path
    runtime_root: Path
    tenant_data_root: Path
    tenant_backup_root: Path
    public_key_file: Path
    unit_root: Path
    caddy_route_root: Path
    caddy_config: Path
    caddy_user: str

    @classmethod
    def from_environment(cls) -> "HostOperationsSettings":
        names = (
            "PLATFORM_HOST_OPERATIONS_SOCKET",
            "PLATFORM_HOST_OPERATIONS_RELEASE_ROOT",
            "PLATFORM_HOST_OPERATIONS_CREDENTIAL_ROOT",
            "PLATFORM_HOST_OPERATIONS_RUNTIME_ROOT",
            "PLATFORM_HOST_OPERATIONS_TENANT_DATA_ROOT",
            "PLATFORM_HOST_OPERATIONS_TENANT_BACKUP_ROOT",
            "PLATFORM_HOST_OPERATIONS_MANIFEST_PUBLIC_KEY_FILE",
            "PLATFORM_HOST_OPERATIONS_UNIT_ROOT",
            "PLATFORM_HOST_OPERATIONS_CADDY_ROUTE_ROOT",
            "PLATFORM_HOST_OPERATIONS_CADDY_CONFIG",
        )
        paths = {
            name: _absolute_path(os.getenv(name, ""), name)
            for name in names
        }
        allowed_uid_value = os.getenv("PLATFORM_HOST_OPERATIONS_ALLOWED_UID", "").strip()
        allowed_gid_value = os.getenv("PLATFORM_HOST_OPERATIONS_ALLOWED_GID", "").strip()
        if not allowed_uid_value.isdigit() or int(allowed_uid_value) <= 0:
            raise HostOperationsError("PLATFORM_HOST_OPERATIONS_ALLOWED_UID is required")
        if not allowed_gid_value.isdigit() or int(allowed_gid_value) <= 0:
            raise HostOperationsError("PLATFORM_HOST_OPERATIONS_ALLOWED_GID is required")
        caddy_user = os.getenv("PLATFORM_HOST_OPERATIONS_CADDY_USER", "caddy").strip()
        linux_paths = LinuxOperationsPaths(
            credential_root=paths["PLATFORM_HOST_OPERATIONS_CREDENTIAL_ROOT"],
            public_key_file=paths["PLATFORM_HOST_OPERATIONS_MANIFEST_PUBLIC_KEY_FILE"],
            unit_root=paths["PLATFORM_HOST_OPERATIONS_UNIT_ROOT"],
            caddy_route_root=paths["PLATFORM_HOST_OPERATIONS_CADDY_ROUTE_ROOT"],
            caddy_config=paths["PLATFORM_HOST_OPERATIONS_CADDY_CONFIG"],
            runtime_root=paths["PLATFORM_HOST_OPERATIONS_RUNTIME_ROOT"],
            caddy_user=caddy_user,
        )
        return cls(
            socket_path=paths["PLATFORM_HOST_OPERATIONS_SOCKET"],
            allowed_uid=int(allowed_uid_value),
            allowed_gid=int(allowed_gid_value),
            release_root=paths["PLATFORM_HOST_OPERATIONS_RELEASE_ROOT"],
            credential_root=linux_paths.credential_root,
            runtime_root=linux_paths.runtime_root,
            tenant_data_root=paths["PLATFORM_HOST_OPERATIONS_TENANT_DATA_ROOT"],
            tenant_backup_root=paths["PLATFORM_HOST_OPERATIONS_TENANT_BACKUP_ROOT"],
            public_key_file=linux_paths.public_key_file,
            unit_root=linux_paths.unit_root,
            caddy_route_root=linux_paths.caddy_route_root,
            caddy_config=linux_paths.caddy_config,
            caddy_user=caddy_user,
        )

    @property
    def controller_paths(self) -> ControllerPaths:
        return ControllerPaths(
            release_root=self.release_root,
            credential_root=self.credential_root,
            runtime_root=self.runtime_root,
            tenant_data_root=self.tenant_data_root,
            tenant_backup_root=self.tenant_backup_root,
            public_key_file=self.public_key_file,
        )

    @property
    def linux_paths(self) -> LinuxOperationsPaths:
        return LinuxOperationsPaths(
            credential_root=self.credential_root,
            public_key_file=self.public_key_file,
            unit_root=self.unit_root,
            caddy_route_root=self.caddy_route_root,
            caddy_config=self.caddy_config,
            runtime_root=self.runtime_root,
            caddy_user=self.caddy_user,
        )


class UnixSocketPrivilegedOperations:
    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path)

    def _request(self, operation: str, **payload: object) -> dict[str, object]:
        request = json.dumps({"operation": operation, **payload}, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(request) > _MAX_MESSAGE_LENGTH:
            raise HostOperationsError("host operation request is too large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(45)
                connection.connect(str(self.socket_path))
                connection.sendall(request + b"\n")
                response = _read_frame(connection)
        except OSError as exc:
            raise HostOperationsError("host operations helper is unavailable") from exc
        if not response:
            raise HostOperationsError("invalid host operations response")
        try:
            result = json.loads(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostOperationsError("invalid host operations response") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise HostOperationsError("host operation was rejected")
        return result

    def ensure_tenant_identity(self, tenant_id: str) -> int:
        result = self._request("ensure_identity", tenant_id=_tenant_id(tenant_id))
        return _positive_int(result.get("uid"), "tenant uid")

    def prepare_tenant_storage(
        self,
        *,
        tenant_id: str,
        uid: int,
    ) -> None:
        self._request(
            "prepare_storage",
            tenant_id=_tenant_id(tenant_id),
            uid=_positive_int(uid, "tenant uid"),
        )

    def initialize_tenant_database(
        self,
        *,
        tenant_id: str,
        uid: int,
        canonical_host: str,
        owner_telegram_id: int,
        display_name: str,
    ) -> None:
        self._request(
            "initialize_database",
            tenant_id=_tenant_id(tenant_id),
            uid=_positive_int(uid, "tenant uid"),
            canonical_host=_text(canonical_host, "canonical host", 253),
            owner_telegram_id=_positive_int(owner_telegram_id, "owner telegram id"),
            display_name=_text(display_name, "display name", 120),
        )

    def owner_claim_verdict(self, *, tenant_id: str, owner_telegram_id: int) -> str:
        result = self._request(
            "owner_claim_verdict",
            tenant_id=_tenant_id(tenant_id),
            owner_telegram_id=_positive_int(owner_telegram_id, "owner telegram id"),
        )
        verdict = result.get("verdict")
        if verdict not in {"claimed", "missing", "owner_mismatch"}:
            raise HostOperationsError("invalid owner claim verdict")
        return verdict

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
        if set(credentials) - _CREDENTIAL_NAMES:
            raise HostOperationsError("unsupported tenant credential")
        self._request(
            "write_material",
            tenant_id=_tenant_id(tenant_id),
            runtime_generation=_positive_int(runtime_generation, "runtime generation"),
            credentials={name: _encoded(value) for name, value in credentials.items()},
            manifest=_encoded(manifest),
            signature=_encoded(signature),
            public_key=_encoded(public_key),
        )

    def install_tenant_unit(self, *, tenant_id: str) -> None:
        self._request("install_unit", tenant_id=_tenant_id(tenant_id))

    def start_tenant_unit(self, tenant_id: str) -> None:
        self._request("start_unit", tenant_id=_tenant_id(tenant_id))

    def check_tenant_health(self, tenant_id: str, runtime_generation: int) -> None:
        self._request(
            "check_health",
            tenant_id=_tenant_id(tenant_id),
            runtime_generation=_positive_int(runtime_generation, "runtime generation"),
        )

    def publish_tenant_route(self, *, tenant_id: str, hosts: list[str]) -> None:
        if not isinstance(hosts, list) or not hosts:
            raise HostOperationsError("invalid tenant hosts")
        normalized_hosts = sorted({normalize_host(_text(host, "tenant host", 253)) for host in hosts})
        self._request(
            "publish_route",
            tenant_id=_tenant_id(tenant_id),
            hosts=normalized_hosts,
        )


class HostOperationsServer:
    def __init__(self, settings: HostOperationsSettings):
        if os.name != "posix":
            raise HostOperationsError("host operations require POSIX")
        self.settings = settings
        self.operations = LinuxPrivilegedOperations(settings.linux_paths)

    def _storage(self, tenant_id: str) -> TenantStoragePaths:
        tenant_id = _tenant_id(tenant_id)
        data_directory = (self.settings.tenant_data_root / tenant_id).resolve()
        backup_directory = (self.settings.tenant_backup_root / tenant_id).resolve()
        if (
            data_directory.parent != self.settings.tenant_data_root.resolve()
            or backup_directory.parent != self.settings.tenant_backup_root.resolve()
        ):
            raise HostOperationsError("invalid tenant storage")
        return TenantStoragePaths(
            database_path=data_directory / "app.sqlite",
            media_root=data_directory / "media",
            backup_root=backup_directory / "backups",
        )

    def dispatch(self, payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or not isinstance(payload.get("operation"), str):
            raise HostOperationsError("invalid host operation")
        operation = payload["operation"]
        tenant_id = _tenant_id(payload.get("tenant_id"))
        if operation == "ensure_identity":
            if set(payload) != {"operation", "tenant_id"}:
                raise HostOperationsError("invalid host operation")
            return {"uid": self.operations.ensure_tenant_identity(tenant_system_user(tenant_id))}
        if operation == "prepare_storage":
            if set(payload) != {"operation", "tenant_id", "uid"}:
                raise HostOperationsError("invalid host operation")
            uid = _positive_int(payload["uid"], "tenant uid")
            storage = self._storage(tenant_id)
            self.operations.prepare_tenant_storage(
                uid=uid,
                database_path=storage.database_path,
                media_root=storage.media_root,
                backup_root=storage.backup_root,
                runtime_directory=self.settings.controller_paths.runtime_root / tenant_id,
            )
            return {}
        if operation == "initialize_database":
            expected = {
                "operation", "tenant_id", "uid", "canonical_host", "owner_telegram_id", "display_name"
            }
            if set(payload) != expected:
                raise HostOperationsError("invalid host operation")
            storage = self._storage(tenant_id)
            self.operations.initialize_tenant_database(
                database_path=storage.database_path,
                uid=_positive_int(payload["uid"], "tenant uid"),
                tenant_id=tenant_id,
                canonical_host=normalize_host(
                    _text(payload["canonical_host"], "canonical host", 253)
                ),
                owner_telegram_id=_positive_int(payload["owner_telegram_id"], "owner telegram id"),
                display_name=_text(payload["display_name"], "display name", 120),
            )
            return {}
        if operation == "owner_claim_verdict":
            if set(payload) != {"operation", "tenant_id", "owner_telegram_id"}:
                raise HostOperationsError("invalid host operation")
            storage = self._storage(tenant_id)
            return {
                "verdict": self.operations.owner_claim_verdict(
                    database_path=storage.database_path,
                    owner_telegram_id=_positive_int(
                        payload["owner_telegram_id"], "owner telegram id"
                    ),
                )
            }
        if operation == "write_material":
            expected = {
                "operation", "tenant_id", "runtime_generation", "credentials", "manifest", "signature", "public_key"
            }
            if set(payload) != expected or not isinstance(payload["credentials"], dict):
                raise HostOperationsError("invalid host operation")
            credentials = {
                name: _decoded(value, "runtime credential")
                for name, value in payload["credentials"].items()
                if name in _CREDENTIAL_NAMES
            }
            if set(credentials) != set(payload["credentials"]):
                raise HostOperationsError("unsupported tenant credential")
            self.operations.write_runtime_material(
                tenant_id=tenant_id,
                runtime_generation=_positive_int(payload["runtime_generation"], "runtime generation"),
                credentials=credentials,
                manifest=_decoded(payload["manifest"], "runtime manifest"),
                signature=_decoded(payload["signature"], "runtime signature"),
                public_key=_decoded(payload["public_key"], "manifest public key"),
            )
            return {}
        if operation == "install_unit":
            if set(payload) != {"operation", "tenant_id"}:
                raise HostOperationsError("invalid host operation")
            storage = self._storage(tenant_id)
            self.operations.install_tenant_unit(
                unit_name=tenant_unit_name(tenant_id),
                dropin=render_tenant_dropin(self.settings.controller_paths, tenant_id, storage),
            )
            return {}
        if operation == "start_unit":
            if set(payload) != {"operation", "tenant_id"}:
                raise HostOperationsError("invalid host operation")
            self.operations.start_tenant_unit(tenant_unit_name(tenant_id))
            return {}
        if operation == "check_health":
            if set(payload) != {"operation", "tenant_id", "runtime_generation"}:
                raise HostOperationsError("invalid host operation")
            self.operations.check_tenant_health(
                tenant_id,
                _positive_int(payload["runtime_generation"], "runtime generation"),
            )
            return {}
        if operation == "publish_route":
            if set(payload) != {"operation", "tenant_id", "hosts"}:
                raise HostOperationsError("invalid host operation")
            hosts = payload["hosts"]
            if not isinstance(hosts, list) or not hosts:
                raise HostOperationsError("invalid host operation")
            normalized_hosts = sorted(
                {normalize_host(_text(host, "tenant host", 253)) for host in hosts}
            )
            if len(normalized_hosts) != len(hosts):
                raise HostOperationsError("invalid host operation")
            self.operations.publish_tenant_route(
                tenant_id=tenant_id,
                route=render_caddy_route(
                    self.settings.runtime_root,
                    tenant_id=tenant_id,
                    hosts=normalized_hosts,
                ),
            )
            return {}
        raise HostOperationsError("unsupported host operation")

    def serve(self) -> None:
        socket_path = self.settings.socket_path
        socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if socket_path.exists() or socket_path.is_symlink():
            socket_path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            os.chown(socket_path, 0, self.settings.allowed_gid)
            os.chmod(socket_path, 0o660)
            listener.listen()
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(_CONNECTION_TIMEOUT_SECONDS)
                    if not self._allowed(connection):
                        connection.sendall(b'{"ok":false}\n')
                        continue
                    try:
                        raw = _read_frame(connection)
                        response = self.dispatch(json.loads(raw))
                        connection.sendall(
                            json.dumps({"ok": True, **response}, separators=(",", ":")).encode("utf-8")
                            + b"\n"
                        )
                    except Exception:
                        try:
                            connection.sendall(b'{"ok":false}\n')
                        except OSError:
                            pass

    def _allowed(self, connection: socket.socket) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return False
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, peer_uid, _gid = struct.unpack("3i", credentials)
        return peer_uid == self.settings.allowed_uid


def main() -> int:
    HostOperationsServer(HostOperationsSettings.from_environment()).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
