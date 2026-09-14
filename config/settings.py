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
        
        # Транспорт доставки Telegram updates: polling или webhook.
        self.RUN_MODE = os.getenv("RUN_MODE", "polling").strip().lower()
        self.WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
        self.WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
        self.YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
        self.YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
        self.YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()
        
        # Список ID администраторов
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        self.ADMIN_IDS: List[int] = [
            int(x.strip()) for x in admin_ids_str.split(",") if x.strip()
        ]
        
        # Обязательный абсолютный путь к общей SQLite-базе.
        self.DATABASE_PATH = os.getenv("DATABASE_PATH", "").strip()
        
        # Настройки HTTP listener-а текущего запуска.
        self.HOST = os.getenv("HOST", "0.0.0.0").strip()
        self.PORT = int(os.getenv("PORT", "8000"))
        self.FLASK_DEBUG = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes"}
        
        # Кэш активных диалогов поддержки.
        self.support_pending_users = set()


# Глобальный экземпляр настроек
settings = Settings()
