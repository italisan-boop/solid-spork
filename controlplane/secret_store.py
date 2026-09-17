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


def validate_secret_reference(reference: object) -> str:
    if not isinstance(reference, str) or not _ENV_REFERENCE.fullmatch(reference):
        raise SecretResolutionError("unsupported secret reference")
    return reference


class EnvironmentSecretStore:
    def resolve(self, reference: str) -> str:
        validated = validate_secret_reference(reference)
        variable_name = _ENV_REFERENCE.fullmatch(validated).group(1)
        value = os.getenv(variable_name, "")
        if not value:
            raise SecretResolutionError("secret is unavailable")
        return value
