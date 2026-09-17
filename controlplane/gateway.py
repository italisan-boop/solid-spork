from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Iterable

from controlplane.tenants import normalize_host
from runtime.factory import (
    TenantResolutionError,
    tenant_context_for_host,
    tenant_context_for_owner_onboarding_host,
)


StartResponse = Callable[[str, list[tuple[str, str]]], object]
WsgiApp = Callable[[dict, StartResponse], Iterable[bytes]]
TenantDispatcher = Callable[[object, dict, StartResponse], Iterable[bytes]]
_OWNER_ONBOARDING_PATHS = frozenset({
    "/setup",
    "/api/tenant/session",
    "/api/tenant/onboarding/owner-claim",
    "/api/tenant/onboarding/storefront",
})


def host_from_wsgi_environ(environ: dict) -> str:
    raw = str(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "").strip()
    if raw.startswith("[") or raw.count(":") > 1:
        raise ValueError("invalid host")
    if ":" in raw:
        host, port = raw.rsplit(":", 1)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("invalid host")
        raw = host
    return normalize_host(raw)


class TenantHostGateway:
    def __init__(
        self,
        control_database_path: str | Path,
        dispatch: TenantDispatcher,
    ) -> None:
        self.control_database_path = control_database_path
        self.dispatch = dispatch

    def __call__(self, environ: dict, start_response: StartResponse):
        try:
            host = host_from_wsgi_environ(environ)
            try:
                context = tenant_context_for_host(self.control_database_path, host)
            except TenantResolutionError:
                if environ.get("PATH_INFO") not in _OWNER_ONBOARDING_PATHS:
                    raise
                context = tenant_context_for_owner_onboarding_host(
                    self.control_database_path, host
                )
        except (TenantResolutionError, ValueError):
            start_response("404 Not Found", [("Content-Type", "application/json")])
            return [b'{"error":"tenant host is unavailable"}']
        with context.scope():
            return self.dispatch(context, environ, start_response)
