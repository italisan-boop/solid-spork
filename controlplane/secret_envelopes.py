from __future__ import annotations

import base64
import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_SECRET_KINDS = frozenset({
    "telegram_bot_token",
    "telegram_webhook_secret",
    "bot_proxy_url",
    "delivery_encryption_keys",
})
_MAX_VALUE_LENGTH = 16_384
_MAX_MESSAGE_LENGTH = 65_536
_ALGORITHM = "aes-256-gcm-envelope-v1"


class SecretEnvelopeError(ValueError):
    pass


@dataclass(frozen=True)
class SecretEnvelope:
    ciphertext: str
    data_nonce: str
    wrapped_key: str
    wrap_nonce: str
    key_version: str
    algorithm: str = _ALGORITHM


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decoded(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SecretEnvelopeError("invalid secret envelope")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise SecretEnvelopeError("invalid secret envelope") from exc


def _tenant_id(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SecretEnvelopeError("invalid tenant id") from exc


def _secret_kind(value: object) -> str:
    if value not in _SECRET_KINDS:
        raise SecretEnvelopeError("unsupported secret kind")
    return str(value)


def _secret_value(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_VALUE_LENGTH:
        raise SecretEnvelopeError("invalid secret value")
    if "\x00" in value:
        raise SecretEnvelopeError("invalid secret value")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SecretEnvelopeError("invalid secret value") from exc
    return value


def _aad(tenant_id: str, secret_kind: str, generation: int, key_version: str) -> bytes:
    return f"{tenant_id}\x1f{secret_kind}\x1f{generation}\x1f{key_version}".encode("utf-8")


class EnvelopeCipher:
    def __init__(self, kek: bytes, key_version: str):
        if len(kek) != 32 or not key_version:
            raise SecretEnvelopeError("invalid secret key")
        self._kek = AESGCM(kek)
        self.key_version = key_version

    @classmethod
    def from_key_file(cls, key_file: str | Path, key_version: str) -> "EnvelopeCipher":
        try:
            encoded = Path(key_file).read_text(encoding="ascii").strip()
        except OSError as exc:
            raise SecretEnvelopeError("secret key is unavailable") from exc
        return cls(_decoded(encoded), key_version)

    def seal(
        self, tenant_id: object, secret_kind: object, generation: int, value: object
    ) -> SecretEnvelope:
        tenant_id = _tenant_id(tenant_id)
        secret_kind = _secret_kind(secret_kind)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise SecretEnvelopeError("invalid secret generation")
        value = _secret_value(value)
        aad = _aad(tenant_id, secret_kind, generation, self.key_version)
        dek = os.urandom(32)
        data_nonce = os.urandom(12)
        wrap_nonce = os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(data_nonce, value.encode("utf-8"), aad)
        wrapped_key = self._kek.encrypt(wrap_nonce, dek, aad)
        return SecretEnvelope(
            ciphertext=_encoded(ciphertext),
            data_nonce=_encoded(data_nonce),
            wrapped_key=_encoded(wrapped_key),
            wrap_nonce=_encoded(wrap_nonce),
            key_version=self.key_version,
        )

    def open(
        self, tenant_id: object, secret_kind: object, generation: int, envelope: SecretEnvelope
    ) -> str:
        tenant_id = _tenant_id(tenant_id)
        secret_kind = _secret_kind(secret_kind)
        if envelope.algorithm != _ALGORITHM or envelope.key_version != self.key_version:
            raise SecretEnvelopeError("unsupported secret envelope")
        aad = _aad(tenant_id, secret_kind, generation, self.key_version)
        try:
            dek = self._kek.decrypt(_decoded(envelope.wrap_nonce), _decoded(envelope.wrapped_key), aad)
            value = AESGCM(dek).decrypt(
                _decoded(envelope.data_nonce), _decoded(envelope.ciphertext), aad
            ).decode("utf-8")
        except Exception as exc:
            raise SecretEnvelopeError("secret envelope cannot be opened") from exc
        return _secret_value(value)


def _read_frame(connection: socket.socket) -> bytes:
    buffer = bytearray()
    while len(buffer) <= _MAX_MESSAGE_LENGTH:
        chunk = connection.recv(min(16_384, _MAX_MESSAGE_LENGTH + 1 - len(buffer)))
        if not chunk:
            raise SecretEnvelopeError("incomplete secret sealer frame")
        buffer.extend(chunk)
        delimiter = buffer.find(b"\n")
        if delimiter < 0:
            continue
        if delimiter != len(buffer) - 1:
            raise SecretEnvelopeError("invalid secret sealer frame")
        return bytes(buffer[:delimiter])
    raise SecretEnvelopeError("secret sealer frame is too large")


class UnixSocketSealer:
    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path)

    def seal(
        self, tenant_id: str, secret_kind: str, generation: int, value: str
    ) -> SecretEnvelope:
        request = json.dumps(
            {
                "tenant_id": _tenant_id(tenant_id),
                "secret_kind": _secret_kind(secret_kind),
                "generation": generation,
                "value": _secret_value(value),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request) > _MAX_MESSAGE_LENGTH:
            raise SecretEnvelopeError("secret request is too large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(10)
                connection.connect(str(self.socket_path))
                connection.sendall(request + b"\n")
                response = _read_frame(connection)
        except OSError as exc:
            raise SecretEnvelopeError("secret sealer is unavailable") from exc
        if not response:
            raise SecretEnvelopeError("invalid secret sealer response")
        try:
            payload = json.loads(response)
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ValueError
            return SecretEnvelope(
                ciphertext=str(payload["ciphertext"]),
                data_nonce=str(payload["data_nonce"]),
                wrapped_key=str(payload["wrapped_key"]),
                wrap_nonce=str(payload["wrap_nonce"]),
                key_version=str(payload["key_version"]),
                algorithm=str(payload["algorithm"]),
            )
        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretEnvelopeError("invalid secret sealer response") from exc
