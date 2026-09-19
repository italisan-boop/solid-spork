from __future__ import annotations

import json
import os
import socket
import struct
from pathlib import Path

from controlplane.secret_envelopes import (
    EnvelopeCipher,
    SecretEnvelopeError,
    _read_frame,
)


_MAX_MESSAGE_LENGTH = 65_536
_CONNECTION_TIMEOUT_SECONDS = 10


def _required_path(name: str) -> Path:
    value = os.getenv(name, "").strip()
    path = Path(value)
    if not value or not path.is_absolute():
        raise ValueError(f"{name} is required")
    return path


def _allowed_uid() -> int:
    value = os.getenv("PLATFORM_SEALER_ALLOWED_UID", "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("PLATFORM_SEALER_ALLOWED_UID is required")
    return int(value)


def _allowed_gid() -> int:
    value = os.getenv("PLATFORM_SEALER_ALLOWED_GID", "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("PLATFORM_SEALER_ALLOWED_GID is required")
    return int(value)


def _response(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def _allowed(connection: socket.socket, uid: int) -> bool:
    if not hasattr(socket, "SO_PEERCRED"):
        return False
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, peer_uid, _gid = struct.unpack("3i", credentials)
    return peer_uid == uid


def serve() -> None:
    socket_path = _required_path("PLATFORM_SEALER_SOCKET")
    cipher = EnvelopeCipher.from_key_file(
        _required_path("PLATFORM_SEALER_KEK_FILE"),
        os.getenv("PLATFORM_SEALER_KEY_VERSION", "v1"),
    )
    allowed_uid = _allowed_uid()
    allowed_gid = _allowed_gid()
    socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        socket_path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
        os.chown(socket_path, 0, allowed_gid)
        socket_path.chmod(0o660)
        listener.listen()
        while True:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(_CONNECTION_TIMEOUT_SECONDS)
                if not _allowed(connection, allowed_uid):
                    connection.sendall(_response({"ok": False}))
                    continue
                try:
                    request = _read_frame(connection)
                    payload = json.loads(request)
                    if not isinstance(payload, dict) or set(payload) != {
                        "tenant_id", "secret_kind", "generation", "value"
                    }:
                        raise SecretEnvelopeError("invalid sealer request")
                    envelope = cipher.seal(
                        payload["tenant_id"],
                        payload["secret_kind"],
                        payload["generation"],
                        payload["value"],
                    )
                    connection.sendall(_response({
                        "ok": True,
                        "algorithm": envelope.algorithm,
                        "ciphertext": envelope.ciphertext,
                        "data_nonce": envelope.data_nonce,
                        "wrapped_key": envelope.wrapped_key,
                        "wrap_nonce": envelope.wrap_nonce,
                        "key_version": envelope.key_version,
                    }))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, SecretEnvelopeError):
                    try:
                        connection.sendall(_response({"ok": False}))
                    except OSError:
                        pass


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
