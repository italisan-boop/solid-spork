from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


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
        try:
            admin_ids = frozenset(int(value.strip()) for value in admins_value.split(","))
        except ValueError as exc:
            raise ValueError("PLATFORM_ADMIN_TELEGRAM_IDS must contain Telegram IDs") from exc
        if not admin_ids or any(value <= 0 for value in admin_ids):
            raise ValueError("PLATFORM_ADMIN_TELEGRAM_IDS must contain positive IDs")
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
