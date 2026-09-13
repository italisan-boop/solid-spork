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
        
        # Настройки базы данных
        self.DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_database.db")
        
        # Настройки сервера
        self.HOST = os.getenv("HOST", "0.0.0.0")
        self.PORT = int(os.getenv("PORT", "8000"))
        
        # Глобальные состояния для админки
        self.broadcast_pending_users = set()
        self.support_pending_users = set()
        # Карта закреплённых тикетов: user_id -> admin_id.
        # Если пользователь в поддержке И закреплён — его сообщения летят
        # только этому админу, чтобы двое не отвечали одновременно.
        self.support_claims: dict[int, int] = {}


# Глобальный экземпляр настроек
settings = Settings()
