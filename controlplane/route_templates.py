from __future__ import annotations

import re
import uuid
from pathlib import Path


_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)


class RouteTemplateError(ValueError):
    pass


def _tenant_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RouteTemplateError("invalid tenant id") from exc


def _host(value: object) -> str:
    if not isinstance(value, str):
        raise RouteTemplateError("invalid route host")
    host = value.strip().lower().rstrip(".")
    if ":" in host or not _HOST_PATTERN.fullmatch(host):
        raise RouteTemplateError("invalid route host")
    return host


def tenant_socket(runtime_root: str | Path, tenant_id: str) -> Path:
    root = Path(runtime_root)
    if not root.is_absolute():
        raise RouteTemplateError("runtime root must be absolute")
    parsed = _tenant_id(tenant_id)
    target = (root / parsed / "tenant.sock").resolve()
    if target.parent != (root / parsed).resolve():
        raise RouteTemplateError("invalid tenant id")
    return target


def render_caddy_route(
    runtime_root: str | Path,
    *,
    tenant_id: str,
    hosts: list[str],
) -> str:
    if not hosts:
        raise RouteTemplateError("tenant route requires hosts")
    parsed = _tenant_id(tenant_id)
    normalized_hosts = sorted({_host(host) for host in hosts})
    socket = tenant_socket(runtime_root, parsed)
    return "\n".join([
        f"{' '.join(normalized_hosts)} {{",
        f"    reverse_proxy unix//{socket.as_posix()}",
        "}",
        "",
    ])
