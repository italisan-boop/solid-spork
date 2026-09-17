from __future__ import annotations

from runtime.context import TenantContext, current_tenant_context


class FeatureUnavailableError(PermissionError):
    def __init__(self, feature: str):
        super().__init__(f"feature is unavailable: {feature}")
        self.feature = feature


class QuotaExceededError(ValueError):
    def __init__(self, limit_name: str, limit: int):
        super().__init__(f"quota exceeded: {limit_name}")
        self.limit_name = limit_name
        self.limit = limit


def has_feature(feature: str, context: TenantContext | None = None) -> bool:
    selected = context or current_tenant_context()
    return feature in selected.entitlements.features


def require_feature(feature: str, context: TenantContext | None = None) -> None:
    if not has_feature(feature, context):
        raise FeatureUnavailableError(feature)


def require_quota(
    limit_name: str,
    current_value: int,
    increment: int = 1,
    *,
    context: TenantContext | None = None,
) -> None:
    selected = context or current_tenant_context()
    if not isinstance(current_value, int) or not isinstance(increment, int) or current_value < 0 or increment < 0:
        raise ValueError("quota values must be non-negative integers")
    limit = selected.entitlements.limits.get(limit_name)
    if limit is None:
        raise FeatureUnavailableError(limit_name)
    if current_value + increment > limit:
        raise QuotaExceededError(limit_name, limit)
