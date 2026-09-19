from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from controlplane.plan_policy import Entitlements
from db.schema import database_context

if TYPE_CHECKING:
    from controlplane.tenants import Tenant


_CURRENT_TENANT_CONTEXT: ContextVar["TenantContext | None"] = ContextVar(
    "current_tenant_context", default=None
)


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    canonical_host: str
    database_path: Path
    media_root: Path
    backup_root: Path
    owner_telegram_id: int
    entitlements: Entitlements
    runtime_generation: int

    @classmethod
    def from_tenant(cls, tenant: Tenant, entitlements: Entitlements) -> "TenantContext":
        return cls(
            tenant_id=tenant.id,
            canonical_host=tenant.canonical_host,
            database_path=tenant.database_path,
            media_root=tenant.media_root,
            backup_root=tenant.backup_root,
            owner_telegram_id=tenant.owner_telegram_id,
            entitlements=entitlements,
            runtime_generation=tenant.runtime_generation,
        )

    @classmethod
    def from_managed_manifest(cls, manifest) -> "TenantContext":
        return cls(
            tenant_id=manifest.tenant_id,
            canonical_host=manifest.canonical_host,
            database_path=manifest.database_path,
            media_root=manifest.media_root,
            backup_root=manifest.backup_root,
            owner_telegram_id=manifest.owner_telegram_id,
            entitlements=manifest.entitlements,
            runtime_generation=manifest.runtime_generation,
        )

    @contextmanager
    def database_scope(self):
        with database_context(self.database_path):
            yield self

    @contextmanager
    def scope(self):
        token = _CURRENT_TENANT_CONTEXT.set(self)
        try:
            with self.database_scope():
                yield self
        finally:
            _CURRENT_TENANT_CONTEXT.reset(token)


def maybe_current_tenant_context() -> TenantContext | None:
    return _CURRENT_TENANT_CONTEXT.get()


def current_tenant_context() -> TenantContext:
    context = maybe_current_tenant_context()
    if context is None:
        raise RuntimeError("tenant context is required")
    return context
