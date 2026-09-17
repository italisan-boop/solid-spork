from __future__ import annotations

from pathlib import Path

from controlplane.tenants import (
    effective_tenant_entitlements,
    resolve_active_host,
    resolve_owner_onboarding_host,
)
from runtime.context import TenantContext


class TenantResolutionError(ValueError):
    pass


def tenant_context_for_host(
    control_database_path: str | Path, host: object
) -> TenantContext:
    tenant = resolve_active_host(control_database_path, host)
    if tenant is None:
        raise TenantResolutionError("tenant host is unavailable")
    return TenantContext.from_tenant(
        tenant, effective_tenant_entitlements(control_database_path, tenant.id)
    )


def tenant_context_for_owner_onboarding_host(
    control_database_path: str | Path, host: object
) -> TenantContext:
    tenant = resolve_owner_onboarding_host(control_database_path, host)
    if tenant is None:
        raise TenantResolutionError("tenant host is unavailable")
    return TenantContext.from_tenant(
        tenant, effective_tenant_entitlements(control_database_path, tenant.id)
    )
