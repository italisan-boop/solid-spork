from __future__ import annotations

from urllib.parse import urlparse


def normalize_authenticated_http_proxy(value: str, *, setting_name: str) -> str | None:
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise ValueError(f"{setting_name} must be an HTTP proxy URL")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be an HTTP proxy URL") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or not parsed.username
        or not parsed.password
        or port is None
        or not 1 <= port <= 65535
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{setting_name} must be an HTTP proxy URL")
    return value.rstrip("/")
