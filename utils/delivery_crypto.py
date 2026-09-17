from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import settings


class DeliveryCryptoError(ValueError):
    pass


def _decode_key(value: str) -> bytes:
    try:
        padded = value + "=" * (-len(value) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise DeliveryCryptoError("delivery key is invalid") from exc
    if len(key) != 32:
        raise DeliveryCryptoError("delivery key is invalid")
    return key


def _keyring() -> tuple[str, Mapping[str, bytes]]:
    active_key_id = settings.DELIVERY_ENCRYPTION_ACTIVE_KEY_ID
    if not active_key_id or len(active_key_id) > 64:
        raise DeliveryCryptoError("delivery encryption is unavailable")
    try:
        raw_keyring = json.loads(settings.DELIVERY_ENCRYPTION_KEYS_JSON)
    except json.JSONDecodeError as exc:
        raise DeliveryCryptoError("delivery encryption is unavailable") from exc
    if not isinstance(raw_keyring, dict):
        raise DeliveryCryptoError("delivery encryption is unavailable")
    keyring: dict[str, bytes] = {}
    for key_id, encoded_key in raw_keyring.items():
        if not isinstance(key_id, str) or not isinstance(encoded_key, str):
            raise DeliveryCryptoError("delivery encryption is unavailable")
        keyring[key_id] = _decode_key(encoded_key)
    if active_key_id not in keyring:
        raise DeliveryCryptoError("delivery encryption is unavailable")
    return active_key_id, keyring


def delivery_encryption_is_available() -> bool:
    try:
        _keyring()
    except DeliveryCryptoError:
        return False
    return True


def _aad(order_id: int, method: str) -> bytes:
    return f"order-delivery:v1:{order_id}:{method}".encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise DeliveryCryptoError("delivery data is unavailable") from exc


def encrypt_destination(order_id: int, method: str, destination: Mapping[str, str]) -> str:
    active_key_id, keyring = _keyring()
    if not isinstance(order_id, int) or order_id <= 0 or not isinstance(method, str):
        raise DeliveryCryptoError("delivery encryption is unavailable")
    payload = json.dumps(
        dict(destination), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(keyring[active_key_id]).encrypt(nonce, payload, _aad(order_id, method))
    return f"gcm1.{active_key_id}.{_encode(nonce + ciphertext)}"


def decrypt_destination(order_id: int, method: str, envelope: str) -> dict[str, str]:
    if not isinstance(envelope, str):
        raise DeliveryCryptoError("delivery data is unavailable")
    try:
        version, key_id, encoded_payload = envelope.split(".", 2)
    except ValueError as exc:
        raise DeliveryCryptoError("delivery data is unavailable") from exc
    if version != "gcm1":
        raise DeliveryCryptoError("delivery data is unavailable")
    _, keyring = _keyring()
    key = keyring.get(key_id)
    payload = _decode(encoded_payload)
    if key is None or len(payload) <= 12:
        raise DeliveryCryptoError("delivery data is unavailable")
    try:
        plaintext = AESGCM(key).decrypt(payload[:12], payload[12:], _aad(order_id, method))
        data = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliveryCryptoError("delivery data is unavailable") from exc
    if not isinstance(data, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
        raise DeliveryCryptoError("delivery data is unavailable")
    return data
