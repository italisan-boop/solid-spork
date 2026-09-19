from __future__ import annotations

import base64
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from controlplane.plan_policy import Entitlements, effective_entitlements, parse_plan


_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)
_MANAGED_LIFECYCLE_STATES = frozenset({"awaiting_owner_claim", "active"})


class RuntimeManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedRuntimeManifest:
    tenant_id: str
    canonical_host: str
    allowed_hosts: frozenset[str]
    database_path: Path
    media_root: Path
    backup_root: Path
    socket_path: Path
    owner_telegram_id: int
    lifecycle_state: str
    runtime_generation: int
    entitlements: Entitlements


def _required_absolute_path(payload: dict[str, object], name: str) -> Path:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeManifestError("invalid runtime manifest")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeManifestError("invalid runtime manifest")
    return path.resolve()


def _host(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeManifestError("invalid runtime manifest")
    host = value.strip().lower().rstrip(".")
    if ":" in host or not _HOST_PATTERN.fullmatch(host):
        raise RuntimeManifestError("invalid runtime manifest")
    return host


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeManifestError("invalid runtime manifest")
    return value


def _decoded(value: bytes) -> bytes:
    try:
        return base64.urlsafe_b64decode(value.strip() + b"=" * (-len(value.strip()) % 4))
    except (ValueError, TypeError) as exc:
        raise RuntimeManifestError("invalid managed runtime signature") from exc


def _credential_directory() -> Path:
    value = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    if not value:
        raise RuntimeManifestError("managed runtime credentials are unavailable")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RuntimeManifestError("managed runtime credentials are unavailable")
    return path.resolve()


def _public_key_path() -> Path:
    value = os.getenv("BOOKAPP_MANIFEST_PUBLIC_KEY_FILE", "").strip()
    if not value:
        raise RuntimeManifestError("managed runtime verification key is unavailable")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeManifestError("managed runtime verification key is unavailable")
    return path.resolve()


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RuntimeManifestError("managed runtime credentials are unavailable") from exc


def _parse(payload: dict[str, object]) -> ManagedRuntimeManifest:
    if set(payload) != {
        "tenant_id",
        "canonical_host",
        "allowed_hosts",
        "database_path",
        "media_root",
        "backup_root",
        "socket_path",
        "owner_telegram_id",
        "lifecycle_state",
        "runtime_generation",
        "plan",
        "feature_overrides",
        "limit_overrides",
    }:
        raise RuntimeManifestError("invalid runtime manifest")
    try:
        tenant_id = str(uuid.UUID(str(payload["tenant_id"])))
        entitlements = effective_entitlements(
            parse_plan(payload["plan"]),
            feature_overrides=payload["feature_overrides"],
            limit_overrides=payload["limit_overrides"],
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeManifestError("invalid runtime manifest") from exc
    raw_hosts = payload["allowed_hosts"]
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise RuntimeManifestError("invalid runtime manifest")
    allowed_hosts = frozenset(_host(host) for host in raw_hosts)
    canonical_host = _host(payload["canonical_host"])
    if canonical_host not in allowed_hosts:
        raise RuntimeManifestError("invalid runtime manifest")
    lifecycle_state = payload["lifecycle_state"]
    if lifecycle_state not in _MANAGED_LIFECYCLE_STATES:
        raise RuntimeManifestError("invalid runtime manifest")
    return ManagedRuntimeManifest(
        tenant_id=tenant_id,
        canonical_host=canonical_host,
        allowed_hosts=allowed_hosts,
        database_path=_required_absolute_path(payload, "database_path"),
        media_root=_required_absolute_path(payload, "media_root"),
        backup_root=_required_absolute_path(payload, "backup_root"),
        socket_path=_required_absolute_path(payload, "socket_path"),
        owner_telegram_id=_positive_int(payload["owner_telegram_id"]),
        lifecycle_state=lifecycle_state,
        runtime_generation=_positive_int(payload["runtime_generation"]),
        entitlements=entitlements,
    )


def load_managed_runtime_manifest() -> ManagedRuntimeManifest:
    credentials = _credential_directory()
    manifest_path = credentials / "runtime.json"
    signature_path = credentials / "runtime.sig"
    if manifest_path.is_symlink() or signature_path.is_symlink():
        raise RuntimeManifestError("managed runtime credentials are unavailable")
    raw_manifest = _read(manifest_path)
    signature = _decoded(_read(signature_path))
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_decoded(_read(_public_key_path())))
        public_key.verify(signature, raw_manifest)
        payload = json.loads(raw_manifest)
    except (InvalidSignature, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeManifestError("invalid managed runtime manifest") from exc
    if not isinstance(payload, dict):
        raise RuntimeManifestError("invalid runtime manifest")
    return _parse(payload)
