"""
Конфигурация приложения.
Загружает переменные окружения и предоставляет доступ к настройкам.
"""
import os
from dotenv import load_dotenv
from typing import List

# Загружаем переменные окружения
load_dotenv()


class Settings:
    """Класс настроек приложения."""
    
    def __init__(self):
        # Токен бота
        self.BOT_TOKEN = os.getenv("BOT_TOKEN")
        
        # URL веб-приложения
        self.WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
        
        # Режим запуска (polling или webhook)
        self.RUN_MODE = os.getenv("RUN_MODE", "polling")
        
        # Список ID администраторов
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        self.ADMIN_IDS: List[int] = [
            int(x.strip()) for x in admin_ids_str.split(",") if x.strip()
        ]
        
        # Обязательный абсолютный путь к общей SQLite-базе.
        self.DATABASE_PATH = os.getenv("DATABASE_PATH", "").strip()
        
        # Настройки сервера
        self.HOST = os.getenv("HOST", "0.0.0.0")
        self.PORT = int(os.getenv("PORT", "8000"))
        
        # Кэш активных диалогов поддержки.
        self.support_pending_users = set()


# Глобальный экземпляр настроек
settings = Settings()
