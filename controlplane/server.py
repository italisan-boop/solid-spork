from __future__ import annotations

from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, make_response, request, send_from_directory

from controlplane.deployments import deployment_status, request_deployment
from controlplane.plan_policy import effective_entitlements, plan_defaults
from controlplane.provisioning import ProvisioningError, provision_tenant
from controlplane.settings import ControlPlaneSettings
from controlplane.schema import initialize
from controlplane.tenants import (
    activate_after_owner_claim,
    configured_secret_kinds,
    create_tenant,
    effective_tenant_entitlements,
    entitlement_overrides,
    get_tenant,
    list_tenants,
    request_custom_domain,
    request_managed_deletion,
    sealed_secret_kinds,
    set_custom_domain_verification,
    set_lifecycle_state,
    set_secret_envelopes,
    set_secret_reference,
    set_secret_references,
    soft_delete_tenant,
    tenant_domains,
    update_entitlements,
    update_plan,
    update_tenant_configuration,
)
from telegram_auth import TelegramInitDataError, validate_telegram_init_data


def _tenant_payload(control_database_path: str | Path, tenant, entitlements=None) -> dict:
    result = {
        "id": tenant.id,
        "slug": tenant.slug,
        "display_name": tenant.display_name,
        "owner_telegram_id": tenant.owner_telegram_id,
        "tenant_kind": tenant.tenant_kind,
        "plan": tenant.plan.value,
        "lifecycle_state": tenant.lifecycle_state,
        "canonical_host": tenant.canonical_host,
        "runtime_generation": tenant.runtime_generation,
        "entitlement_version": tenant.entitlement_version,
        "domains": tenant_domains(control_database_path, tenant.id),
        "configured_secret_kinds": configured_secret_kinds(control_database_path, tenant.id),
        "sealed_secret_kinds": sealed_secret_kinds(control_database_path, tenant.id),
        "entitlement_overrides": entitlement_overrides(control_database_path, tenant.id),
        "deployment": deployment_status(control_database_path, tenant.id),
    }
    if entitlements is not None:
        result["entitlements"] = {
            "policy_version": entitlements.policy_version,
            "features": sorted(entitlements.features),
            "limits": dict(entitlements.limits),
        }
    return result


def create_controlplane_app(
    settings: ControlPlaneSettings,
    *,
    secret_sealer=None,
) -> Flask:
    initialize(settings.database_path)
    app = Flask(__name__)

    def managed_legacy_mutation_error():
        if settings.managed_mode:
            return jsonify({"error": "managed Console requires sealed secrets and deployment jobs"}), 409
        return None

    def legacy_managed_mutation_error():
        if not settings.managed_mode:
            return jsonify({"error": "legacy Console requires secret references and direct operations"}), 409
        return None

    def require_platform_admin(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            try:
                user = validate_telegram_init_data(
                    request.headers.get("X-Telegram-Init-Data", ""), settings.bot_token
                )
            except TelegramInitDataError as error:
                status = 503 if error.kind == "unavailable" else 401
                return jsonify({"error": "Platform authentication failed"}), status
            if user.id not in settings.admin_telegram_ids:
                return jsonify({"error": "Forbidden"}), 403
            g.platform_admin_id = user.id
            response = make_response(handler(*args, **kwargs))
            response.headers["Cache-Control"] = "private, no-store"
            return response

        return wrapped

    @app.get("/")
    def index():
        return send_from_directory(Path(__file__).parent, "platform_index.html")

    @app.get("/api/platform/session")
    @require_platform_admin
    def session():
        return jsonify({"platform_admin_id": g.platform_admin_id})

    @app.route("/api/platform/tenants", methods=["GET", "POST"])
    @require_platform_admin
    def tenants():
        if request.method == "GET":
            return jsonify({
                "managed_mode": settings.managed_mode,
                "plan_defaults": plan_defaults(),
                "tenants": [
                    _tenant_payload(
                        settings.database_path,
                        tenant,
                        effective_tenant_entitlements(settings.database_path, tenant.id),
                    )
                    for tenant in list_tenants(settings.database_path)
                ]
            })
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {
            "display_name", "slug", "owner_telegram_id", "plan"
        }:
            return jsonify({"error": "display_name, slug, owner_telegram_id and plan are required"}), 400
        try:
            tenant = create_tenant(
                settings.database_path,
                display_name=payload["display_name"],
                slug=payload["slug"],
                owner_telegram_id=payload["owner_telegram_id"],
                plan=payload["plan"],
                tenant_data_root=settings.tenant_data_root,
                tenant_backup_root=settings.tenant_backup_root,
                tenant_base_domain=settings.tenant_base_domain,
                actor_telegram_id=g.platform_admin_id,
                tenant_kind="managed" if settings.managed_mode else "legacy",
            )
        except (ValueError, TypeError) as error:
            return jsonify({"error": str(error)}), 400
        return jsonify(_tenant_payload(
            settings.database_path,
            tenant, effective_tenant_entitlements(settings.database_path, tenant.id)
        )), 201

    @app.get("/api/platform/tenants/<tenant_id>")
    @require_platform_admin
    def tenant(tenant_id: str):
        selected = get_tenant(settings.database_path, tenant_id)
        if selected is None:
            return jsonify({"error": "tenant not found"}), 404
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.post("/api/platform/tenants/<tenant_id>/domains")
    @require_platform_admin
    def request_domain(tenant_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"host"}:
            return jsonify({"error": "host is required"}), 400
        try:
            host = request_custom_domain(
                settings.database_path,
                tenant_id=tenant_id,
                host=payload["host"],
                actor_telegram_id=g.platform_admin_id,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify({"host": host, "verification_state": "pending"}), 201

    @app.put("/api/platform/tenants/<tenant_id>/domains/<path:host>/verification")
    @require_platform_admin
    def verify_domain(tenant_id: str, host: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"state"}:
            return jsonify({"error": "state is required"}), 400
        try:
            result = set_custom_domain_verification(
                settings.database_path,
                tenant_id=tenant_id,
                host=host,
                verification_state=payload["state"],
                actor_telegram_id=g.platform_admin_id,
                managed_reconciliation=settings.managed_mode,
            )
        except ValueError as error:
            status = 404 if str(error) == "custom domain not found" else 400
            return jsonify({"error": str(error)}), status
        response = {
            "host": result.host,
            "verification_state": result.verification_state,
            "changed": result.changed,
            "reconciliation_queued": result.reconciliation_queued,
        }
        if result.reconciliation_queued:
            response["deployment"] = deployment_status(settings.database_path, tenant_id)
            return jsonify(response), 202
        return jsonify(response)

    @app.put("/api/platform/tenants/<tenant_id>/plan")
    @require_platform_admin
    def tenant_plan(tenant_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"plan"}:
            return jsonify({"error": "plan is required"}), 400
        try:
            selected = update_plan(
                settings.database_path,
                tenant_id=tenant_id,
                plan=payload["plan"],
                actor_telegram_id=g.platform_admin_id,
                managed_reconciliation=settings.managed_mode,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.put("/api/platform/tenants/<tenant_id>/configuration")
    @require_platform_admin
    def tenant_configuration(tenant_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {
            "plan", "feature_overrides", "limit_overrides"
        }:
            return jsonify({
                "error": "plan, feature_overrides and limit_overrides are required"
            }), 400
        try:
            selected = update_tenant_configuration(
                settings.database_path,
                tenant_id=tenant_id,
                plan=payload["plan"],
                feature_overrides=payload["feature_overrides"],
                limit_overrides=payload["limit_overrides"],
                actor_telegram_id=g.platform_admin_id,
                managed_reconciliation=settings.managed_mode,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.put("/api/platform/tenants/<tenant_id>/entitlements")
    @require_platform_admin
    def tenant_entitlements(tenant_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"feature_overrides", "limit_overrides"}:
            return jsonify({"error": "feature_overrides and limit_overrides are required"}), 400
        if not isinstance(payload["feature_overrides"], dict) or not isinstance(payload["limit_overrides"], dict):
            return jsonify({"error": "entitlement overrides must be objects"}), 400
        try:
            entitlements = update_entitlements(
                settings.database_path,
                tenant_id=tenant_id,
                feature_overrides=payload["feature_overrides"],
                limit_overrides=payload["limit_overrides"],
                actor_telegram_id=g.platform_admin_id,
                managed_reconciliation=settings.managed_mode,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify({
            "policy_version": entitlements.policy_version,
            "features": sorted(entitlements.features),
            "limits": dict(entitlements.limits),
        })

    @app.put("/api/platform/tenants/<tenant_id>/secrets")
    @require_platform_admin
    def tenant_secrets(tenant_id: str):
        legacy_error = legacy_managed_mutation_error()
        if legacy_error is not None:
            return legacy_error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) not in (
            {"secrets"},
            {"secrets", "clear_bot_proxy"},
        ):
            return jsonify({"error": "secrets are required"}), 400
        if not isinstance(payload["secrets"], dict) or not isinstance(
            payload.get("clear_bot_proxy", False), bool
        ):
            return jsonify({"error": "invalid tenant secret request"}), 400
        try:
            selected = set_secret_envelopes(
                settings.database_path,
                tenant_id=tenant_id,
                values=payload["secrets"],
                actor_telegram_id=g.platform_admin_id,
                sealer=secret_sealer,
                clear_bot_proxy=payload.get("clear_bot_proxy", False),
                managed_reconciliation=True,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.put("/api/platform/tenants/<tenant_id>/secret-references")
    @require_platform_admin
    def secret_references_batch(tenant_id: str):
        managed_error = managed_legacy_mutation_error()
        if managed_error is not None:
            return managed_error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"references"}:
            return jsonify({"error": "references is required"}), 400
        if not isinstance(payload["references"], dict):
            return jsonify({"error": "references must be an object"}), 400
        try:
            selected = set_secret_references(
                settings.database_path,
                tenant_id=tenant_id,
                references=payload["references"],
                actor_telegram_id=g.platform_admin_id,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.put("/api/platform/tenants/<tenant_id>/secret-references/<secret_kind>")
    @require_platform_admin
    def secret_reference(tenant_id: str, secret_kind: str):
        managed_error = managed_legacy_mutation_error()
        if managed_error is not None:
            return managed_error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"reference", "version"}:
            return jsonify({"error": "reference and version are required"}), 400
        try:
            set_secret_reference(
                settings.database_path,
                tenant_id=tenant_id,
                secret_kind=secret_kind,
                reference=payload["reference"],
                version=payload["version"],
                actor_telegram_id=g.platform_admin_id,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify({"configured": True, "secret_kind": secret_kind})

    @app.post("/api/platform/tenants/<tenant_id>/deployments")
    @require_platform_admin
    def request_managed_deployment(tenant_id: str):
        legacy_error = legacy_managed_mutation_error()
        if legacy_error is not None:
            return legacy_error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"operation"}:
            return jsonify({"error": "operation is required"}), 400
        try:
            job = request_deployment(
                settings.database_path,
                tenant_id=tenant_id,
                operation=payload["operation"],
                actor_telegram_id=g.platform_admin_id,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 409
            return jsonify({"error": str(error)}), status
        return jsonify({
            "job": {
                "id": job.id,
                "operation": job.operation,
                "desired_generation": job.desired_generation,
                "state": "pending",
            },
            "deployment": deployment_status(settings.database_path, tenant_id),
        }), 202

    @app.post("/api/platform/tenants/<tenant_id>/provision")
    @require_platform_admin
    def provision(tenant_id: str):
        managed_error = managed_legacy_mutation_error()
        if managed_error is not None:
            return managed_error
        if request.get_data(cache=False):
            return jsonify({"error": "request body is not supported"}), 400
        try:
            selected = provision_tenant(
                settings.database_path,
                tenant_id=tenant_id,
                actor_telegram_id=g.platform_admin_id,
            )
        except ProvisioningError as error:
            return jsonify({"error": str(error)}), 409
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.post("/api/platform/tenants/<tenant_id>/activate")
    @require_platform_admin
    def activate_tenant(tenant_id: str):
        managed_error = managed_legacy_mutation_error()
        if managed_error is not None:
            return managed_error
        if request.get_data(cache=False):
            return jsonify({"error": "request body is not supported"}), 400
        try:
            selected = activate_after_owner_claim(
                settings.database_path,
                tenant_id=tenant_id,
                actor_telegram_id=g.platform_admin_id,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 409
            return jsonify({"error": str(error)}), status
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.put("/api/platform/tenants/<tenant_id>/lifecycle")
    @require_platform_admin
    def lifecycle(tenant_id: str):
        if settings.managed_mode:
            return jsonify({"error": "managed lifecycle changes are unavailable"}), 409
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"state"}:
            return jsonify({"error": "state is required"}), 400
        try:
            selected = set_lifecycle_state(
                settings.database_path,
                tenant_id=tenant_id,
                lifecycle_state=payload["state"],
                actor_telegram_id=g.platform_admin_id,
            )
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify(_tenant_payload(
            settings.database_path,
            selected, effective_tenant_entitlements(settings.database_path, selected.id)
        ))

    @app.delete("/api/platform/tenants/<tenant_id>")
    @require_platform_admin
    def delete_tenant(tenant_id: str):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"confirm_slug"}:
            return jsonify({"error": "confirm_slug is required"}), 400
        try:
            if settings.managed_mode:
                job = request_managed_deletion(
                    settings.database_path,
                    tenant_id=tenant_id,
                    confirm_slug=payload["confirm_slug"],
                    actor_telegram_id=g.platform_admin_id,
                )
                selected = get_tenant(settings.database_path, tenant_id)
                if job is None:
                    return jsonify({"deleted": True, "lifecycle_state": "deleted"})
                return jsonify({
                    "deleted": False,
                    "job": {
                        "id": job.id,
                        "operation": job.operation,
                        "desired_generation": job.desired_generation,
                        "state": "pending",
                    },
                    "tenant": _tenant_payload(
                        settings.database_path,
                        selected,
                        effective_tenant_entitlements(settings.database_path, tenant_id),
                    ),
                }), 202
            deleted = soft_delete_tenant(
                settings.database_path,
                tenant_id=tenant_id,
                confirm_slug=payload["confirm_slug"],
                actor_telegram_id=g.platform_admin_id,
            )
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 409
        except ValueError as error:
            status = 404 if str(error) == "tenant not found" else 400
            return jsonify({"error": str(error)}), status
        return jsonify({"deleted": deleted})

    return app
