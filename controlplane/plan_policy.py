from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class Plan(StrEnum):
    START = "start"
    BUSINESS = "business"
    PRO = "pro"


FEATURE_CATALOG = "catalog"
FEATURE_ORDERS = "orders"
FEATURE_BRANDING = "branding"
FEATURE_STORE_SETTINGS = "store_settings"
FEATURE_STAFF = "staff"
FEATURE_INVENTORY = "inventory"
FEATURE_BOOK_IMPORT = "book_import"
FEATURE_ANALYTICS = "analytics"
FEATURE_CAMPAIGNS = "campaigns"
FEATURE_BROADCAST = "broadcast"

ALL_FEATURES = frozenset({
    FEATURE_CATALOG,
    FEATURE_ORDERS,
    FEATURE_BRANDING,
    FEATURE_STORE_SETTINGS,
    FEATURE_STAFF,
    FEATURE_INVENTORY,
    FEATURE_BOOK_IMPORT,
    FEATURE_ANALYTICS,
    FEATURE_CAMPAIGNS,
    FEATURE_BROADCAST,
})
LIMIT_BOOKS = "books"
LIMIT_STAFF_MEMBERS = "staff_members"
LIMIT_IMPORTS_PER_DAY = "imports_per_day"
LIMIT_CAMPAIGNS = "campaigns"
LIMIT_BROADCAST_RECIPIENTS_PER_DAY = "broadcast_recipients_per_day"
ALL_LIMITS = frozenset({
    LIMIT_BOOKS,
    LIMIT_STAFF_MEMBERS,
    LIMIT_IMPORTS_PER_DAY,
    LIMIT_CAMPAIGNS,
    LIMIT_BROADCAST_RECIPIENTS_PER_DAY,
})


@dataclass(frozen=True)
class Entitlements:
    plan: Plan
    policy_version: int
    features: frozenset[str]
    limits: Mapping[str, int]


_BASE_FEATURES = frozenset({
    FEATURE_CATALOG,
    FEATURE_ORDERS,
    FEATURE_BRANDING,
    FEATURE_STORE_SETTINGS,
})

_PLAN_ENTITLEMENTS: dict[Plan, Entitlements] = {
    Plan.START: Entitlements(
        plan=Plan.START,
        policy_version=1,
        features=_BASE_FEATURES,
        limits={
            LIMIT_BOOKS: 100,
            LIMIT_STAFF_MEMBERS: 1,
            LIMIT_IMPORTS_PER_DAY: 0,
            LIMIT_CAMPAIGNS: 0,
            LIMIT_BROADCAST_RECIPIENTS_PER_DAY: 0,
        },
    ),
    Plan.BUSINESS: Entitlements(
        plan=Plan.BUSINESS,
        policy_version=1,
        features=_BASE_FEATURES | {
            FEATURE_STAFF,
            FEATURE_INVENTORY,
            FEATURE_BOOK_IMPORT,
            FEATURE_ANALYTICS,
        },
        limits={
            LIMIT_BOOKS: 1_000,
            LIMIT_STAFF_MEMBERS: 5,
            LIMIT_IMPORTS_PER_DAY: 2,
            LIMIT_CAMPAIGNS: 0,
            LIMIT_BROADCAST_RECIPIENTS_PER_DAY: 0,
        },
    ),
    Plan.PRO: Entitlements(
        plan=Plan.PRO,
        policy_version=1,
        features=ALL_FEATURES,
        limits={
            LIMIT_BOOKS: 10_000,
            LIMIT_STAFF_MEMBERS: 20,
            LIMIT_IMPORTS_PER_DAY: 2,
            LIMIT_CAMPAIGNS: 20,
            LIMIT_BROADCAST_RECIPIENTS_PER_DAY: 2_000,
        },
    ),
}


def plan_defaults() -> dict[str, dict[str, object]]:
    return {
        plan.value: {
            "policy_version": entitlements.policy_version,
            "features": sorted(entitlements.features),
            "limits": dict(entitlements.limits),
        }
        for plan, entitlements in _PLAN_ENTITLEMENTS.items()
    }


def parse_plan(value: object) -> Plan:
    try:
        return Plan(str(value))
    except ValueError as exc:
        raise ValueError("unsupported plan") from exc


def effective_entitlements(
    plan: Plan | str,
    *,
    feature_overrides: Mapping[str, object] | None = None,
    limit_overrides: Mapping[str, object] | None = None,
) -> Entitlements:
    base = _PLAN_ENTITLEMENTS[parse_plan(plan)]
    features = set(base.features)
    for feature, enabled in (feature_overrides or {}).items():
        if feature not in ALL_FEATURES or not isinstance(enabled, bool):
            raise ValueError("invalid feature override")
        if enabled:
            features.add(feature)
        else:
            features.discard(feature)
    limits = dict(base.limits)
    for name, value in (limit_overrides or {}).items():
        if name not in ALL_LIMITS or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid limit override")
        limits[name] = value
    return Entitlements(
        plan=base.plan,
        policy_version=base.policy_version,
        features=frozenset(features),
        limits=limits,
    )
