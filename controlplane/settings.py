from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


load_dotenv()


def _platform_admin_ids(value: str) -> frozenset[int]:
    try:
        admin_ids = frozenset(int(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise ValueError("PLATFORM_ADMIN_TELEGRAM_IDS must contain Telegram IDs") from exc
    if not admin_ids or any(item <= 0 for item in admin_ids):
        raise ValueError("PLATFORM_ADMIN_TELEGRAM_IDS must contain positive IDs")
    return admin_ids


def _platform_console_url(value: str) -> str:
    if not value:
        raise ValueError("PLATFORM_CONSOLE_URL is required")
    if any(character.isspace() for character in value):
        raise ValueError("PLATFORM_CONSOLE_URL must be an HTTPS origin")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("PLATFORM_CONSOLE_URL must be an HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError("PLATFORM_CONSOLE_URL must be an HTTPS origin")
    return value.rstrip("/")


def _platform_bot_proxy_url(value: str) -> str | None:
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise ValueError("PLATFORM_BOT_PROXY_URL must be an HTTP proxy URL")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("PLATFORM_BOT_PROXY_URL must be an HTTP proxy URL") from exc
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
        raise ValueError("PLATFORM_BOT_PROXY_URL must be an HTTP proxy URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class PlatformBotSettings:
    bot_token: str
    admin_telegram_ids: frozenset[int]
    console_url: str
    proxy_url: str | None

    @classmethod
    def from_environment(cls) -> "PlatformBotSettings":
        bot_token = os.getenv("PLATFORM_BOT_TOKEN", "").strip()
        admins_value = os.getenv("PLATFORM_ADMIN_TELEGRAM_IDS", "").strip()
        if not bot_token:
            raise ValueError("PLATFORM_BOT_TOKEN is required")
        if not admins_value:
            raise ValueError("PLATFORM_ADMIN_TELEGRAM_IDS is required")
        return cls(
            bot_token=bot_token,
            admin_telegram_ids=_platform_admin_ids(admins_value),
            console_url=_platform_console_url(
                os.getenv("PLATFORM_CONSOLE_URL", "").strip()
            ),
            proxy_url=_platform_bot_proxy_url(
                os.getenv("PLATFORM_BOT_PROXY_URL", "").strip()
            ),
        )


@dataclass(frozen=True)
class ControlPlaneSettings:
    database_path: Path
    tenant_data_root: Path
    tenant_backup_root: Path
    tenant_base_domain: str
    bot_token: str
    admin_telegram_ids: frozenset[int]
    host: str
    port: int

    @classmethod
    def from_environment(cls) -> "ControlPlaneSettings":
        database_value = os.getenv("PLATFORM_DATABASE_PATH", "").strip()
        data_root_value = os.getenv("PLATFORM_TENANT_DATA_ROOT", "").strip()
        backup_root_value = os.getenv("PLATFORM_TENANT_BACKUP_ROOT", "").strip()
        base_domain = os.getenv("PLATFORM_TENANT_BASE_DOMAIN", "").strip()
        bot_token = os.getenv("PLATFORM_BOT_TOKEN", "").strip()
        admins_value = os.getenv("PLATFORM_ADMIN_TELEGRAM_IDS", "").strip()
        values = {
            "PLATFORM_DATABASE_PATH": database_value,
            "PLATFORM_TENANT_DATA_ROOT": data_root_value,
            "PLATFORM_TENANT_BACKUP_ROOT": backup_root_value,
            "PLATFORM_TENANT_BASE_DOMAIN": base_domain,
            "PLATFORM_BOT_TOKEN": bot_token,
            "PLATFORM_ADMIN_TELEGRAM_IDS": admins_value,
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"missing platform settings: {', '.join(missing)}")
        admin_ids = _platform_admin_ids(admins_value)
        paths = [Path(value).expanduser() for value in (database_value, data_root_value, backup_root_value)]
        if any(not path.is_absolute() for path in paths):
            raise ValueError("platform storage paths must be absolute")
        host = os.getenv("PLATFORM_HOST", "127.0.0.1").strip()
        try:
            port = int(os.getenv("PLATFORM_PORT", "8100"))
        except ValueError as exc:
            raise ValueError("PLATFORM_PORT must be an integer") from exc
        if not host or not 1 <= port <= 65535:
            raise ValueError("invalid platform listener configuration")
        return cls(
            database_path=paths[0].resolve(),
            tenant_data_root=paths[1].resolve(),
            tenant_backup_root=paths[2].resolve(),
            tenant_base_domain=base_domain,
            bot_token=bot_token,
            admin_telegram_ids=admin_ids,
            host=host,
            port=port,
        )
