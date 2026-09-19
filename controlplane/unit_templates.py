from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from controlplane.deployments import tenant_system_user


class UnitTemplateError(ValueError):
    pass


@dataclass(frozen=True)
class ControllerPaths:
    release_root: Path
    credential_root: Path
    runtime_root: Path
    tenant_data_root: Path
    tenant_backup_root: Path
    public_key_file: Path

    def __post_init__(self):
        for path in (
            self.release_root,
            self.credential_root,
            self.runtime_root,
            self.tenant_data_root,
            self.tenant_backup_root,
            self.public_key_file,
        ):
            if not path.is_absolute():
                raise UnitTemplateError("controller paths must be absolute")

@dataclass(frozen=True)
class TenantStoragePaths:
    database_path: Path
    media_root: Path
    backup_root: Path

    def writable_paths(self) -> tuple[Path, Path, Path]:
        paths = (self.database_path.parent, self.media_root, self.backup_root)
        if any(not path.is_absolute() for path in paths):
            raise UnitTemplateError("tenant storage paths must be absolute")
        return tuple(path.resolve() for path in paths)


def _tenant_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise UnitTemplateError("invalid tenant id") from exc


def _child(root: Path, tenant_id: str) -> Path:
    target = (root / tenant_id).resolve()
    if target.parent != root.resolve():
        raise UnitTemplateError("invalid tenant id")
    return target


def tenant_credential_directory(paths: ControllerPaths, tenant_id: str) -> Path:
    return _child(paths.credential_root, _tenant_id(tenant_id))


def tenant_runtime_directory(paths: ControllerPaths, tenant_id: str) -> Path:
    return _child(paths.runtime_root, _tenant_id(tenant_id))


def tenant_unit_name(tenant_id: str) -> str:
    return f"bookapp-tenant@{_tenant_id(tenant_id)}.service"


def render_tenant_dropin(
    paths: ControllerPaths, tenant_id: str, storage: TenantStoragePaths
) -> str:
    tenant_id = _tenant_id(tenant_id)
    user = tenant_system_user(tenant_id)
    credentials = tenant_credential_directory(paths, tenant_id)
    runtime = tenant_runtime_directory(paths, tenant_id)
    writable_paths = storage.writable_paths()
    python = paths.release_root / ".venv" / "bin" / "python"
    lines = [
        "[Service]",
        f"User={user}",
        f"Group={user}",
        "Environment=BOOKAPP_MANAGED_RUNTIME=1",
        f"Environment=BOOKAPP_MANIFEST_PUBLIC_KEY_FILE={paths.public_key_file}",
        f"Environment=BOOKAPP_RUNTIME_DIRECTORY={runtime}",
        f"WorkingDirectory={paths.release_root}",
        "ExecStart=",
        f"ExecStart={python} -m runtime.managed_launcher",
        "LoadCredential=runtime.json:" + str(credentials / "runtime.json"),
        "LoadCredential=runtime.sig:" + str(credentials / "runtime.sig"),
        "LoadCredential=telegram_bot_token:" + str(credentials / "telegram_bot_token"),
        "LoadCredential=telegram_webhook_secret:" + str(
            credentials / "telegram_webhook_secret"
        ),
        "LoadCredential=bot_proxy_url:" + str(credentials / "bot_proxy_url"),
        "UMask=0077",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ProtectControlGroups=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "RestrictNamespaces=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "ReadWritePaths=" + " ".join(str(path) for path in (*writable_paths, runtime)),
        "Restart=on-failure",
        "RestartSec=5",
        "",
    ]
    return "\n".join(lines)
