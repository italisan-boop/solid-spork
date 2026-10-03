from __future__ import annotations

from collections.abc import Iterable

from config import settings
from controlplane.plan_policy import (
    FEATURE_ANALYTICS,
    FEATURE_BOOK_IMPORT,
    FEATURE_BRANDING,
    FEATURE_BROADCAST,
    FEATURE_CAMPAIGNS,
    FEATURE_CATALOG,
    FEATURE_INVENTORY,
    FEATURE_STAFF,
)
from db.staff import active_staff_ids_for_roles_sync, get_staff_roles_sync
from runtime.context import maybe_current_tenant_context


OWNER = "owner"
ADMINISTRATOR = "administrator"
MANAGER = "manager"
WAREHOUSE = "warehouse"
EDITOR = "editor"
_ROLE_PRIORITY = (ADMINISTRATOR, MANAGER, WAREHOUSE, EDITOR)

_ROLE_PERMISSIONS = {
    OWNER: {"*"},
    ADMINISTRATOR: {
        "admin.access",
        "orders.read",
        "orders.transition",
        "payment.reconcile",
        "payment.configure",
        "delivery.manage",
        "support.respond",
        "catalog.manage",
        "inventory.read",
        "inventory.adjust",
        "fulfillment.manage",
        "fulfillment.override",
        "book.import",
        "branding.manage",
        "reports.view",
        "audit.read",
        "campaign.manage",
        "broadcast.send",
        "staff.manage",
    },
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
    EDITOR: {
        "admin.access",
        "catalog.manage",
    },
}
_EVENT_AUDIENCES = {
    "payment": {ADMINISTRATOR, MANAGER},
    "delivery": {ADMINISTRATOR, MANAGER},
    "support": {ADMINISTRATOR, MANAGER},
    "stock": {ADMINISTRATOR, WAREHOUSE},
    "packing": {ADMINISTRATOR, WAREHOUSE},
}
_PERMISSION_FEATURES = {
    "catalog.manage": FEATURE_CATALOG,
    "branding.manage": FEATURE_BRANDING,
    "reports.view": FEATURE_ANALYTICS,
    "staff.manage": FEATURE_STAFF,
    "inventory.read": FEATURE_INVENTORY,
    "inventory.adjust": FEATURE_INVENTORY,
    "fulfillment.manage": FEATURE_INVENTORY,
    "fulfillment.override": FEATURE_INVENTORY,
    "book.import": FEATURE_BOOK_IMPORT,
    "campaign.manage": FEATURE_CAMPAIGNS,
    "broadcast.send": FEATURE_BROADCAST,
}


def _legacy_owner_ids(legacy_admin_ids: Iterable[int] | None = None) -> set[int]:
    return set(settings.ADMIN_IDS if legacy_admin_ids is None else legacy_admin_ids)


def is_owner_sync(telegram_user_id: int, *, legacy_admin_ids: Iterable[int] | None = None) -> bool:
    if not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
        return False
    context = maybe_current_tenant_context()
    if context is not None and telegram_user_id == context.owner_telegram_id:
        return True
    if settings.OWNER_TELEGRAM_ID is not None:
        return telegram_user_id == settings.OWNER_TELEGRAM_ID
    return telegram_user_id in _legacy_owner_ids(legacy_admin_ids)


def actor_roles_sync(
    telegram_user_id: int, *, legacy_admin_ids: Iterable[int] | None = None
) -> tuple[str, ...]:
    if not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
        return ()
    if is_owner_sync(telegram_user_id, legacy_admin_ids=legacy_admin_ids):
        return (OWNER,)
    try:
        return get_staff_roles_sync(telegram_user_id)
    except Exception:
        return ()


def actor_role_sync(
    telegram_user_id: int, *, legacy_admin_ids: Iterable[int] | None = None
) -> str | None:
    roles = actor_roles_sync(telegram_user_id, legacy_admin_ids=legacy_admin_ids)
    if OWNER in roles:
        return OWNER
    return next((role for role in _ROLE_PRIORITY if role in roles), None)


def _entitled_permissions(permissions: set[str]) -> set[str]:
    context = maybe_current_tenant_context()
    if context is None:
        return permissions
    return {
        permission
        for permission in permissions
        if (feature := _PERMISSION_FEATURES.get(permission)) is None
        or feature in context.entitlements.features
    }


def has_permission_sync(
    telegram_user_id: int,
    permission: str,
    *,
    legacy_admin_ids: Iterable[int] | None = None,
) -> bool:
    roles = actor_roles_sync(telegram_user_id, legacy_admin_ids=legacy_admin_ids)
    if not roles:
        return False
    permissions = set().union(*(_ROLE_PERMISSIONS.get(role, set()) for role in roles))
    context = maybe_current_tenant_context()
    feature = _PERMISSION_FEATURES.get(permission)
    if context is not None and feature is not None and feature not in context.entitlements.features:
        return False
    return "*" in permissions or permission in permissions


def capabilities_for_roles(roles: Iterable[str]) -> list[str]:
    selected = set(roles)
    if OWNER in selected:
        context = maybe_current_tenant_context()
        if context is None:
            return ["*"]
        permissions = set().union(*_ROLE_PERMISSIONS.values()) | {
            "admin.maintenance",
        }
    else:
        permissions = set().union(*(_ROLE_PERMISSIONS.get(role, set()) for role in selected))
    return sorted(_entitled_permissions(permissions))


def capabilities_for_role(role: str | None) -> list[str]:
    return capabilities_for_roles([role] if role else [])


def recipient_ids_for_event_sync(
    event_family: str, *, legacy_admin_ids: Iterable[int] | None = None
) -> list[int]:
    """Resolve owner and active role members allowed to act on an incident."""
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
