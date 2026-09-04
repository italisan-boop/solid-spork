"""
Утилиты приложения.
"""
import json
from datetime import datetime, timezone, timedelta

from .logger import setup_logger


def format_local_time(utc_time_str: str) -> str:
    """Конвертирует время из UTC в местное (Москва, UTC+3)"""
    if not utc_time_str:
        return "—"
    try:
        utc_dt = datetime.fromisoformat(utc_time_str).replace(tzinfo=timezone.utc)
        local_dt = utc_dt.astimezone(timezone(timedelta(hours=3)))
        return local_dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return utc_time_str[:16]


def parseBookImages(field):
    """Парсинг изображений из поля БД"""
    if not field:
        return []
    if isinstance(field, list):
        return field
    if isinstance(field, str):
        if field.startswith('['):
            try:
                parsed = json.loads(field)
                return parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                pass
        if field.startswith('http'):
            return [field]
        return [field] if field else []
    return []


__all__ = ["setup_logger", "format_local_time", "parseBookImages"]
