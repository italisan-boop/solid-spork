from __future__ import annotations

from collections.abc import Callable, Iterable

from runtime.context import TenantContext
from runtime.manifest import ManagedRuntimeManifest


StartResponse = Callable[[str, list[tuple[str, str]]], object]
WsgiApp = Callable[[dict, StartResponse], Iterable[bytes]]
_OWNER_ONBOARDING_PATHS = frozenset({
    "/setup",
    "/api/tenant/session",
    "/api/tenant/onboarding/owner-claim",
    "/api/tenant/onboarding/storefront",
})


def _host(environ: dict) -> str | None:
    raw = str(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "").strip()
    if raw.startswith("[") or raw.count(":") > 1:
        return None
    if ":" in raw:
        host, port = raw.rsplit(":", 1)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            return None
        raw = host
    return raw.lower().rstrip(".")


def _not_found(start_response: StartResponse) -> list[bytes]:
    start_response("404 Not Found", [("Content-Type", "application/json")])
    return [b'{"error":"tenant host is unavailable"}']


def create_managed_tenant_wsgi_app(
    manifest: ManagedRuntimeManifest,
    *,
    context: TenantContext,
    storefront_app: WsgiApp,
    bot_token: str,
) -> WsgiApp:
    def app(environ: dict, start_response: StartResponse):
        host = _host(environ)
        path = str(environ.get("PATH_INFO") or "/")
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if host == "localhost" and path == "/health" and method == "GET":
            from runtime.server import create_tenant_setup_app

            with context.scope():
                return create_tenant_setup_app(context, bot_token).wsgi_app(
                    environ, start_response
                )
        if host not in manifest.allowed_hosts:
            return _not_found(start_response)
        if manifest.lifecycle_state == "awaiting_owner_claim":
            if path not in _OWNER_ONBOARDING_PATHS:
                return _not_found(start_response)
            from runtime.server import create_tenant_setup_app

            with context.scope():
                return create_tenant_setup_app(context, bot_token).wsgi_app(
                    environ, start_response
                )
        with context.scope():
            return storefront_app(environ, start_response)

    return app
