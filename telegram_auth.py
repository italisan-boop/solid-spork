from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


MAX_INIT_DATA_LENGTH = 8192
MAX_INIT_DATA_FIELDS = 32
MAX_AGE_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 60


class TelegramInitDataError(ValueError):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class TelegramUser:
    id: int
    name: str


def validate_telegram_init_data(
    raw_init_data: str,
    bot_token: str,
    *,
    now: int | None = None,
) -> TelegramUser:
    if not bot_token:
        raise TelegramInitDataError("unavailable")
    if not raw_init_data:
        raise TelegramInitDataError("missing")
    if len(raw_init_data) > MAX_INIT_DATA_LENGTH:
        raise TelegramInitDataError("invalid")

    try:
        pairs = parse_qsl(
            raw_init_data,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=MAX_INIT_DATA_FIELDS,
        )
    except ValueError as exc:
        raise TelegramInitDataError("invalid") from exc

    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise TelegramInitDataError("invalid")
        values[key] = value

    received_hash = values.pop("hash", "")
    if len(received_hash) != 64 or any(char not in "0123456789abcdef" for char in received_hash):
        raise TelegramInitDataError("invalid")
    if "auth_date" not in values or "user" not in values:
        raise TelegramInitDataError("invalid")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(values.items())
    )
    secret = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramInitDataError("invalid")

    try:
        auth_date = int(values["auth_date"])
    except ValueError as exc:
        raise TelegramInitDataError("invalid") from exc
    current_time = int(time.time()) if now is None else now
    if auth_date > current_time + MAX_FUTURE_SKEW_SECONDS:
        raise TelegramInitDataError("invalid")
    if current_time - auth_date > MAX_AGE_SECONDS:
        raise TelegramInitDataError("expired")

    try:
        user = json.loads(values["user"])
    except json.JSONDecodeError as exc:
        raise TelegramInitDataError("invalid") from exc
    user_id = user.get("id") if isinstance(user, dict) else None
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise TelegramInitDataError("invalid")

    name_parts = [
        part.strip()
        for part in (user.get("first_name"), user.get("last_name"))
        if isinstance(part, str) and part.strip()
    ]
    name = " ".join(name_parts) or "Неизвестно"
    return TelegramUser(id=user_id, name=name)
