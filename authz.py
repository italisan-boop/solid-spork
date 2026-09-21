from __future__ import annotations

from collections.abc import Iterable

from config import settings
from controlplane.plan_policy import (
    FEATURE_ANALYTICS,
    FEATURE_BOOK_IMPORT,
    FEATURE_BRANDING,
    FEATURE_BROADCAST,
    FEATURE_CAMPAIGNS,
    FEATURE_INVENTORY,
    FEATURE_STAFF,
)
from db.staff import active_staff_ids_for_roles_sync, get_staff_role_sync
from runtime.context import maybe_current_tenant_context


OWNER = "owner"
MANAGER = "manager"
WAREHOUSE = "warehouse"

_ROLE_PERMISSIONS = {
    OWNER: {"*"},
    MANAGER: {
        "admin.access",
        "orders.read",
        "orders.transition",
        "payment.reconcile",
        "delivery.manage",
        "support.respond",
    },
    WAREHOUSE: {
        "admin.access",
        "inventory.read",
        "inventory.adjust",
        "fulfillment.manage",
    },
}
_EVENT_AUDIENCES = {
    "payment": {MANAGER},
    "delivery": {MANAGER},
    "support": {MANAGER},
    "stock": {WAREHOUSE},
    "packing": {WAREHOUSE},
}
_PERMISSION_FEATURES = {
    "branding.manage": FEATURE_BRANDING,
    "reports.view": FEATURE_ANALYTICS,
    "staff.manage": FEATURE_STAFF,
    "inventory.read": FEATURE_INVENTORY,
    "inventory.adjust": FEATURE_INVENTORY,
    "fulfillment.manage": FEATURE_INVENTORY,
    "book.import": FEATURE_BOOK_IMPORT,
    "campaign.manage": FEATURE_CAMPAIGNS,
    "broadcast.send": FEATURE_BROADCAST,
}


def _legacy_owner_ids(legacy_admin_ids: Iterable[int] | None = None) -> set[int]:
    return set(settings.ADMIN_IDS if legacy_admin_ids is None else legacy_admin_ids)


def actor_role_sync(
    telegram_user_id: int, *, legacy_admin_ids: Iterable[int] | None = None
) -> str | None:
    if not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
        return None
    context = maybe_current_tenant_context()
    if context is not None and telegram_user_id == context.owner_telegram_id:
        return OWNER
    if settings.OWNER_TELEGRAM_ID is not None:
        if telegram_user_id == settings.OWNER_TELEGRAM_ID:
            return OWNER
    elif telegram_user_id in _legacy_owner_ids(legacy_admin_ids):
        return OWNER
    try:
        return get_staff_role_sync(telegram_user_id)
    except Exception:
        return None


def has_permission_sync(
    telegram_user_id: int,
    permission: str,
    *,
    legacy_admin_ids: Iterable[int] | None = None,
) -> bool:
    role = actor_role_sync(telegram_user_id, legacy_admin_ids=legacy_admin_ids)
    if not role or ("*" not in _ROLE_PERMISSIONS[role] and permission not in _ROLE_PERMISSIONS[role]):
        return False
    context = maybe_current_tenant_context()
    feature = _PERMISSION_FEATURES.get(permission)
    return not context or not feature or feature in context.entitlements.features


def capabilities_for_role(role: str | None) -> list[str]:
    context = maybe_current_tenant_context()
    if role == OWNER:
        if context is None:
            return ["*"]
        permissions = set().union(*_ROLE_PERMISSIONS.values()) | {
            "catalog.manage",
            "branding.manage",
            "broadcast.send",
            "admin.maintenance",
            "payment.configure",
            "campaign.manage",
            "reports.view",
            "staff.manage",
            "book.import",
        }
    else:
        permissions = set(_ROLE_PERMISSIONS.get(role, set()))
    if context is not None:
        permissions = {
            permission for permission in permissions
            if (feature := _PERMISSION_FEATURES.get(permission)) is None
            or feature in context.entitlements.features
        }
    return sorted(permissions)


def is_owner_sync(telegram_user_id: int, *, legacy_admin_ids: Iterable[int] | None = None) -> bool:
    return actor_role_sync(telegram_user_id, legacy_admin_ids=legacy_admin_ids) == OWNER


def recipient_ids_for_event_sync(
    event_family: str, *, legacy_admin_ids: Iterable[int] | None = None
) -> list[int]:
    """Resolve owner plus only the role allowed to act on an incident."""
    role_ids = active_staff_ids_for_roles_sync(_EVENT_AUDIENCES.get(event_family, set()))
    context = maybe_current_tenant_context()
    if context is not None:
        return sorted({context.owner_telegram_id} | set(role_ids))
    owner_ids = (
        {settings.OWNER_TELEGRAM_ID}
        if settings.OWNER_TELEGRAM_ID is not None
        else _legacy_owner_ids(legacy_admin_ids)
    )
    return sorted(owner_ids | set(role_ids))
