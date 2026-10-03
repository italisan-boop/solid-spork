from __future__ import annotations

from functools import wraps

from pathlib import Path

from flask import Flask, g, jsonify, make_response, request, send_from_directory

from controlplane.plan_policy import FEATURE_BRANDING, FEATURE_STORE_SETTINGS
from db.schema import connect
from db.storefront import get_storefront_settings_sync, save_storefront_settings_sync
from runtime.context import TenantContext
from runtime.features import require_feature
from telegram_auth import TelegramInitDataError, validate_telegram_init_data


def create_tenant_setup_app(context: TenantContext, bot_token: str) -> Flask:
    app = Flask(__name__)

    @app.before_request
    def enter_tenant_scope():
        scope = context.scope()
        scope.__enter__()
        g.tenant_scope = scope

    @app.teardown_request
    def exit_tenant_scope(error):
        scope = g.pop("tenant_scope", None)
        if scope is not None:
            scope.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)

    def require_tenant_user(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            try:
                g.tenant_user = validate_telegram_init_data(
                    request.headers.get("X-Telegram-Init-Data", ""), bot_token
                )
            except TelegramInitDataError:
                return jsonify({"error": "Tenant authentication failed"}), 401
            return handler(*args, **kwargs)

        return wrapped

    def require_tenant_owner(handler):
        @require_tenant_user
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if g.tenant_user.id != context.owner_telegram_id:
                return jsonify({"error": "Forbidden"}), 403
            response = make_response(handler(*args, **kwargs))
            response.headers["Cache-Control"] = "private, no-store"
            return response

        return wrapped

    def require_storefront_owner(handler):
        @require_tenant_owner
        @wraps(handler)
        def wrapped(*args, **kwargs):
            try:
                require_feature(FEATURE_BRANDING, context)
                require_feature(FEATURE_STORE_SETTINGS, context)
            except PermissionError:
                return jsonify({"error": "feature_not_available"}), 403
            return handler(*args, **kwargs)

        return wrapped

    @app.get("/setup")
    def setup_page():
        return send_from_directory(Path(__file__).parent, "tenant_onboarding.html")

    @app.get("/health")
    def health():
        return jsonify({"tenant_id": context.tenant_id, "generation": context.runtime_generation})

    @app.get("/api/storefront/config")
    def public_storefront_config():
        payload = get_storefront_settings_sync()
        payload["features"] = sorted(context.entitlements.features)
        return jsonify(payload)

    @app.get("/api/tenant/session")
    @require_tenant_user
    def tenant_session():
        return jsonify({
            "tenant_id": context.tenant_id,
            "is_owner": g.tenant_user.id == context.owner_telegram_id,
            "features": sorted(context.entitlements.features),
            "limits": dict(context.entitlements.limits),
        })

    @app.post("/api/tenant/onboarding/owner-claim")
    @require_tenant_owner
    def claim_owner():
        if request.get_data(cache=False):
            return jsonify({"error": "request body is not supported"}), 400
        database = connect()
        try:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                "SELECT telegram_user_id FROM tenant_owner_claims WHERE id = 1"
            ).fetchone()
            if existing and existing[0] != context.owner_telegram_id:
                return jsonify({"error": "owner claim conflict"}), 409
            database.execute(
                """
                INSERT INTO tenant_owner_claims (id, telegram_user_id)
                VALUES (1, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (context.owner_telegram_id,),
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()
        return jsonify({"claimed": True})

    @app.route("/api/tenant/onboarding/storefront", methods=["GET", "PUT"])
    @require_storefront_owner
    def tenant_storefront():
        if request.method == "GET":
            return jsonify(get_storefront_settings_sync())
        try:
            storefront = save_storefront_settings_sync(request.get_json(silent=True))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(storefront)

    return app
