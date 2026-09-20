from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from controlplane.plan_policy import Entitlements
from controlplane.schema import connect
from controlplane.secret_envelopes import EnvelopeCipher, SecretEnvelope, SecretEnvelopeError
from controlplane.tenants import Tenant, entitlement_overrides, tenant_domains


_REQUIRED_KINDS = ("telegram_bot_token", "telegram_webhook_secret")
_OPTIONAL_KINDS = ("bot_proxy_url", "delivery_encryption_keys")


class MaterializationError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeMaterial:
    credentials: dict[str, bytes]
    manifest: bytes
    signature: bytes
    allowed_hosts: list[str]


def _decoded(value: bytes) -> bytes:
    try:
        return base64.urlsafe_b64decode(value.strip() + b"=" * (-len(value.strip()) % 4))
    except (ValueError, TypeError) as exc:
        raise MaterializationError("invalid controller signing key") from exc


class ManifestSigner:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._private_key = private_key

    @classmethod
    def from_key_file(cls, key_file: str | Path) -> "ManifestSigner":
        try:
            encoded = Path(key_file).read_bytes()
            private_key = Ed25519PrivateKey.from_private_bytes(_decoded(encoded))
        except (OSError, ValueError) as exc:
            raise MaterializationError("controller signing key is unavailable") from exc
        return cls(private_key)

    def public_key(self) -> bytes:
        return base64.urlsafe_b64encode(
            self._private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).rstrip(b"=")

    def sign(self, value: bytes) -> bytes:
        return base64.urlsafe_b64encode(self._private_key.sign(value)).rstrip(b"=")


def _envelopes(
    control_database_path: str | Path, tenant_id: str
) -> dict[str, tuple[int, SecretEnvelope]]:
    database = connect(control_database_path)
    try:
        rows = database.execute(
            """
            SELECT secret_kind, generation, algorithm, ciphertext, data_nonce,
                   wrapped_key, wrap_nonce, key_version
            FROM tenant_secret_envelopes
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchall()
    finally:
        database.close()
    return {
        row[0]: (
            row[1],
            SecretEnvelope(
                algorithm=row[2],
                ciphertext=row[3],
                data_nonce=row[4],
                wrapped_key=row[5],
                wrap_nonce=row[6],
                key_version=row[7],
            ),
        )
        for row in rows
    }


def allowed_hosts(control_database_path: str | Path, tenant: Tenant) -> list[str]:
    if tenant.lifecycle_state == "awaiting_owner_claim":
        return [tenant.canonical_host]
    if tenant.lifecycle_state != "active":
        raise MaterializationError("tenant is not routable")
    return sorted(
        {
            domain["host"]
            for domain in tenant_domains(control_database_path, tenant.id)
            if domain["verification_state"] == "verified"
        }
    )


def materialize_runtime(
    control_database_path: str | Path,
    *,
    tenant: Tenant,
    entitlements: Entitlements,
    cipher: EnvelopeCipher,
    signer: ManifestSigner,
    socket_path: Path,
) -> RuntimeMaterial:
    envelopes = _envelopes(control_database_path, tenant.id)
    credentials: dict[str, bytes] = {}
    for secret_kind in (*_REQUIRED_KINDS, *_OPTIONAL_KINDS):
        configured = envelopes.get(secret_kind)
        if configured is None:
            if secret_kind in _REQUIRED_KINDS:
                raise MaterializationError("required tenant secret is unavailable")
            continue
        generation, envelope = configured
        if generation > tenant.runtime_generation:
            raise MaterializationError("tenant secret generation is invalid")
        try:
            credentials[secret_kind] = cipher.open(
                tenant.id, secret_kind, generation, envelope
            ).encode("utf-8")
        except SecretEnvelopeError as exc:
            raise MaterializationError("tenant secret cannot be materialized") from exc
    overrides = entitlement_overrides(control_database_path, tenant.id)
    allowed_runtime_hosts = allowed_hosts(control_database_path, tenant)
    payload = {
        "tenant_id": tenant.id,
        "canonical_host": tenant.canonical_host,
        "allowed_hosts": allowed_runtime_hosts,
        "database_path": str(tenant.database_path),
        "media_root": str(tenant.media_root),
        "backup_root": str(tenant.backup_root),
        "socket_path": str(socket_path.resolve()),
        "owner_telegram_id": tenant.owner_telegram_id,
        "lifecycle_state": tenant.lifecycle_state,
        "runtime_generation": tenant.runtime_generation,
        "plan": tenant.plan.value,
        "feature_overrides": overrides["feature_overrides"],
        "limit_overrides": overrides["limit_overrides"],
    }
    manifest = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return RuntimeMaterial(
        credentials=credentials,
        manifest=manifest,
        signature=signer.sign(manifest),
        allowed_hosts=allowed_runtime_hosts,
    )
