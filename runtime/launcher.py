from __future__ import annotations

import json
import os
from pathlib import Path

from controlplane.secret_store import EnvironmentSecretStore, SecretResolutionError
from controlplane.settings import ControlPlaneSettings
from controlplane.tenants import (
    effective_tenant_entitlements,
    get_tenant,
    secret_references,
)


class TenantLauncherError(ValueError):
    pass


def _runtime_id() -> str:
    tenant_id = os.getenv("TENANT_RUNTIME_ID", "").strip()
    if not tenant_id:
        raise TenantLauncherError("TENANT_RUNTIME_ID is required")
    return tenant_id


def configure_tenant_environment() -> tuple[object, object]:
    control_settings = ControlPlaneSettings.from_environment()
    tenant = get_tenant(control_settings.database_path, _runtime_id())
    if tenant is None:
        raise TenantLauncherError("tenant not found")
    if tenant.lifecycle_state not in {"awaiting_owner_claim", "active"}:
        raise TenantLauncherError("tenant runtime is not active")
    references = secret_references(control_settings.database_path, tenant.id)
    try:
        bot_token = EnvironmentSecretStore().resolve(references["telegram_bot_token"])
        webhook_secret = EnvironmentSecretStore().resolve(
            references["telegram_webhook_secret"]
        )
    except KeyError as exc:
        raise TenantLauncherError("required secret reference is missing") from exc
    except SecretResolutionError as exc:
        raise TenantLauncherError("required tenant secret is unavailable") from exc

    os.environ["DATABASE_PATH"] = str(tenant.database_path)
    os.environ["BOOK_MEDIA_ROOT"] = str(tenant.media_root)
    os.environ["BACKUP_DIR"] = str(tenant.backup_root)
    os.environ["OWNER_TELEGRAM_ID"] = str(tenant.owner_telegram_id)
    os.environ["WEBAPP_URL"] = f"https://{tenant.canonical_host}"
    os.environ["WEBHOOK_URL"] = os.getenv(
        "TENANT_WEBHOOK_URL", f"https://{tenant.canonical_host}/webhook"
    )
    os.environ["BOT_TOKEN"] = bot_token
    os.environ["WEBHOOK_SECRET"] = webhook_secret

    yookassa_reference = references.get("yookassa_credentials")
    if yookassa_reference:
        try:
            credentials = json.loads(EnvironmentSecretStore().resolve(yookassa_reference))
        except (SecretResolutionError, json.JSONDecodeError) as exc:
            raise TenantLauncherError("YooKassa credentials are unavailable") from exc
        if not isinstance(credentials, dict) or not all(
            isinstance(credentials.get(key), str) and credentials[key]
            for key in ("shop_id", "secret_key")
        ):
            raise TenantLauncherError("YooKassa credentials are invalid")
        os.environ["YOOKASSA_SHOP_ID"] = credentials["shop_id"]
        os.environ["YOOKASSA_SECRET_KEY"] = credentials["secret_key"]
        os.environ["YOOKASSA_RETURN_URL"] = credentials.get(
            "return_url", f"https://{tenant.canonical_host}/payments/yookassa/return"
        )

    from runtime.context import TenantContext

    context = TenantContext.from_tenant(
        tenant, effective_tenant_entitlements(control_settings.database_path, tenant.id)
    )
    return tenant, context


def _component() -> str:
    component = os.getenv("TENANT_RUNTIME_COMPONENT", "bot").strip().lower()
    if component not in {"bot", "web"}:
        raise TenantLauncherError("TENANT_RUNTIME_COMPONENT must be bot or web")
    return component


def _tenant_wsgi(tenant, context):
    from server import app as storefront_app
    from runtime.tenant_wsgi import create_tenant_wsgi_app

    return create_tenant_wsgi_app(
        ControlPlaneSettings.from_environment().database_path,
        tenant_id=tenant.id,
        storefront_app=storefront_app,
        bot_token=os.environ["BOT_TOKEN"],
    )


def main() -> int:
    tenant, context = configure_tenant_environment()
    wsgi_app = _tenant_wsgi(tenant, context)
    if _component() == "web":
        from werkzeug.serving import run_simple

        host = os.getenv("HOST", "127.0.0.1")
        try:
            port = int(os.getenv("PORT", "8080"))
        except ValueError as exc:
            raise TenantLauncherError("PORT must be an integer") from exc
        run_simple(host, port, wsgi_app, use_reloader=False)
        return 0
    with context.scope():
        from main import main as tenant_main

        return tenant_main(wsgi_app=wsgi_app)


if __name__ == "__main__":
    raise SystemExit(main())
