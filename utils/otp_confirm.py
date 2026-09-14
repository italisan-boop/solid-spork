"""Одноразовые коды подтверждения критичных админ-действий.

Поток:
  1. Админ запускает критичное действие из меню.
  2. Бот выдаёт 6-значный одноразовый код с TTL.
  3. Админ отправляет код вторым сообщением.
  4. Только после успешной проверки действие выполняется.

Память in-process (как support_claims): при рестарте бота коды сгорают —
это правильно, подтверждение нужно получить заново. Код одноразовый:
неверная попытка «сжигает» его, чтобы нельзя было перебирать в столбик.
"""
import secrets
import time

# Время жизни одноразового кода, секунд
OTP_TTL_SECONDS = 120

# Имена критичных действий (используются и как ключи, и в логах)
ACTION_DROP_CACHE = "drop_cache"
ACTION_MASS_BROADCAST = "mass_broadcast"

# (admin_id, action) -> (code, expires_at)
_pending: dict[tuple[int, str], tuple[str, float]] = {}


def issue_otp(admin_id: int, action: str, ttl: int = OTP_TTL_SECONDS) -> str:
    """Выдать одноразовый код для действия. Старый код перетирается."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    _pending[(admin_id, action)] = (code, time.monotonic() + ttl)
    return code


def consume_otp(admin_id: int, action: str, code: str) -> bool:
    """Проверить и «сжечь» код подтверждения.

    True — только если код верный и не истёк. Код одноразовый: любая
    проверка (даже неверная) удаляет его, защищая от перебора.
    """
    record = _pending.pop((admin_id, action), None)
    if not record:
        return False
    expected, expires_at = record
    if time.monotonic() > expires_at:
        return False
    return secrets.compare_digest(expected, code)


def revoke_otp(admin_id: int, action: str | None = None) -> None:
    """Аннулировать коды админа — все или одного действия."""
    if action is not None:
        _pending.pop((admin_id, action), None)
        return
    for admin_id_action in list(_pending):
        if admin_id_action[0] == admin_id:
            del _pending[admin_id_action]