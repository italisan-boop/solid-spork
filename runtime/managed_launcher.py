from __future__ import annotations

from runtime.credentials import read_runtime_credential
from runtime.managed_wsgi import create_managed_tenant_wsgi_app
from runtime.manifest import load_managed_runtime_manifest


class ManagedLauncherError(ValueError):
    pass


def configure_managed_runtime():
    manifest = load_managed_runtime_manifest()
    bot_token = read_runtime_credential("telegram_bot_token", required=True)
    from runtime.context import TenantContext

    return manifest, TenantContext.from_managed_manifest(manifest), bot_token


def main() -> int:
    manifest, context, bot_token = configure_managed_runtime()
    from main import main as tenant_main
    from server import app as storefront_app

    wsgi_app = create_managed_tenant_wsgi_app(
        manifest,
        context=context,
        storefront_app=storefront_app,
        bot_token=bot_token,
    )
    with context.scope():
        return tenant_main(wsgi_app=wsgi_app)


if __name__ == "__main__":
    raise SystemExit(main())
