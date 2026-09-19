from __future__ import annotations

import os
from pathlib import Path


_ALLOWED_CREDENTIALS = frozenset({"platform_bot_token"})


def is_managed_console() -> bool:
    return os.getenv("BOOKAPP_MANAGED_CONSOLE", "").strip() == "1"


def is_managed_platform_bot() -> bool:
    return os.getenv("BOOKAPP_MANAGED_PLATFORM_BOT", "").strip() == "1"


def is_managed_platform_process() -> bool:
    return is_managed_console() or is_managed_platform_bot()


def read_console_credential(name: str, *, required: bool = False) -> str | None:
    if name not in _ALLOWED_CREDENTIALS:
        raise ValueError("unsupported console credential")
    directory_value = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    directory = Path(directory_value)
    if not directory_value or not directory.is_absolute() or directory.is_symlink():
        if required:
            raise ValueError("managed console credential directory is unavailable")
        return None
    credential = directory / name
    if (
        not directory.is_dir()
        or credential.is_symlink()
        or not credential.is_file()
    ):
        if required:
            raise ValueError("managed console credential is unavailable")
        return None
    try:
        value = credential.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        if required:
            raise ValueError("managed console credential is unavailable") from exc
        return None
    if not value or "\x00" in value:
        if required:
            raise ValueError("managed console credential is unavailable")
        return None
    return value
