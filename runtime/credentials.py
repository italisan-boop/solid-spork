from __future__ import annotations

import os
from pathlib import Path


class RuntimeCredentialError(ValueError):
    pass


_CREDENTIAL_NAMES = frozenset({
    "telegram_bot_token",
    "telegram_webhook_secret",
    "yookassa_credentials",
    "delivery_encryption_keys",
    "bot_proxy_url",
})


def is_managed_runtime() -> bool:
    return os.getenv("BOOKAPP_MANAGED_RUNTIME", "").strip() == "1"


def _directory() -> Path:
    value = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    if not value:
        raise RuntimeCredentialError("managed runtime credentials are unavailable")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RuntimeCredentialError("managed runtime credentials are unavailable")
    return path.resolve()


def read_runtime_credential(name: str, *, required: bool = False) -> str | None:
    if name not in _CREDENTIAL_NAMES:
        raise RuntimeCredentialError("unsupported runtime credential")
    path = _directory() / name
    if path.is_symlink():
        raise RuntimeCredentialError("managed runtime credentials are unavailable")
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise RuntimeCredentialError("required managed runtime credential is unavailable")
        return None
    except OSError as exc:
        raise RuntimeCredentialError("managed runtime credentials are unavailable") from exc
    value = value.rstrip("\r\n")
    if not value:
        if required:
            raise RuntimeCredentialError("required managed runtime credential is unavailable")
        return None
    if "\x00" in value:
        raise RuntimeCredentialError("managed runtime credentials are unavailable")
    return value
