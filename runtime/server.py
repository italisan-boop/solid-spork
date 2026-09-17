from __future__ import annotations

import re
from functools import wraps

from pathlib import Path

from flask import Flask, g, jsonify, make_response, request, send_from_directory

from controlplane.plan_policy import FEATURE_BRANDING, FEATURE_STORE_SETTINGS
from db.schema import connect
from runtime.context import TenantContext
from runtime.features import require_feature
from telegram_auth import TelegramInitDataError, validate_telegram_init_data


_COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}\Z")


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

    def storefront() -> dict:
        database = connect()
        try:
            row = database.execute(
                """
                SELECT store_name, primary_color, accent_color, logo_asset_id, support_contact
                FROM storefront_settings WHERE id = 1
                """
            ).fetchone()
            if row is None:
                return {
                    "store_name": "",
                    "primary_color": "#2f7d4a",
                    "accent_color": "#f2b84b",
                    "logo_asset_id": None,
                    "support_contact": "",
                }
            return {
                "store_name": row[0],
                "primary_color": row[1],
                "accent_color": row[2],
                "logo_asset_id": row[3],
                "support_contact": row[4],
            }
        finally:
            database.close()

    @app.get("/setup")
    def setup_page():
        return send_from_directory(Path(__file__).parent, "tenant_onboarding.html")

    @app.get("/health")
    def health():
        return jsonify({"tenant_id": context.tenant_id, "generation": context.runtime_generation})

    @app.get("/api/storefront/config")
    def public_storefront_config():
        payload = storefront()
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
            return jsonify(storefront())
        payload = request.get_json(silent=True)
        fields = {"store_name", "primary_color", "accent_color", "support_contact"}
        if not isinstance(payload, dict) or set(payload) != fields:
            return jsonify({"error": "invalid storefront configuration"}), 400
        name = payload["store_name"]
        primary = payload["primary_color"]
        accent = payload["accent_color"]
        contact = payload["support_contact"]
        if (
            not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
            or not isinstance(primary, str) or not _COLOR_PATTERN.fullmatch(primary)
            or not isinstance(accent, str) or not _COLOR_PATTERN.fullmatch(accent)
            or not isinstance(contact, str) or len(contact.strip()) > 160
        ):
            return jsonify({"error": "invalid storefront configuration"}), 400
        database = connect()
        try:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                INSERT INTO storefront_settings (
                    id, store_name, primary_color, accent_color, support_contact, updated_at
                ) VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    store_name = excluded.store_name,
                    primary_color = excluded.primary_color,
                    accent_color = excluded.accent_color,
                    support_contact = excluded.support_contact,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (name.strip(), primary.lower(), accent.lower(), contact.strip()),
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()
        return jsonify(storefront())

    return app
