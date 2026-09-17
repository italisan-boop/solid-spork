from __future__ import annotations

import os
import re
from typing import Protocol


_ENV_REFERENCE = re.compile(r"env:([A-Z][A-Z0-9_]{1,127})\Z")


class SecretResolutionError(ValueError):
    pass


class SecretStore(Protocol):
    def resolve(self, reference: str) -> str:
        ...


class EnvironmentSecretStore:
    def resolve(self, reference: str) -> str:
        match = _ENV_REFERENCE.fullmatch(reference)
        if not match:
            raise SecretResolutionError("unsupported secret reference provider")
        value = os.getenv(match.group(1), "")
        if not value:
            raise SecretResolutionError("secret is unavailable")
        return value
