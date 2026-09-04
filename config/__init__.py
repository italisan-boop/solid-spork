"""
Конфигурация приложения.
Загружает переменные окружения и предоставляет доступ к настройкам.
"""
from .settings import settings, Settings

__all__ = ["settings", "Settings"]
