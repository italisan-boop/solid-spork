from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from controlplane.gateway import StartResponse, TenantHostGateway
from controlplane.tenants import get_tenant
from runtime.context import TenantContext


WsgiApp = Callable[[dict, StartResponse], Iterable[bytes]]


def create_tenant_wsgi_app(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    storefront_app: WsgiApp,
    bot_token: str,
) -> WsgiApp:
    def dispatch(context: TenantContext, environ: dict, start_response: StartResponse):
        if context.tenant_id != tenant_id:
            start_response("404 Not Found", [("Content-Type", "application/json")])
            return [b'{"error":"tenant host is unavailable"}']
        tenant = get_tenant(control_database_path, tenant_id)
        if tenant is None:
            start_response("404 Not Found", [("Content-Type", "application/json")])
            return [b'{"error":"tenant host is unavailable"}']
        if tenant.lifecycle_state == "awaiting_owner_claim":
            from runtime.server import create_tenant_setup_app

            return create_tenant_setup_app(context, bot_token).wsgi_app(environ, start_response)
        return storefront_app(environ, start_response)

    return TenantHostGateway(control_database_path, dispatch)
