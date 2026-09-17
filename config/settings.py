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
        
        # Владелец — неизменяемый bootstrap-аккаунт из окружения. Пока он не
        # настроен, ADMIN_IDS остаётся временным совместимым источником прав.
        owner_id_raw = os.getenv("OWNER_TELEGRAM_ID", "").strip()
        if owner_id_raw and (not owner_id_raw.isdigit() or int(owner_id_raw) <= 0):
            raise ValueError("OWNER_TELEGRAM_ID must be a positive Telegram ID")
        self.OWNER_TELEGRAM_ID = int(owner_id_raw) if owner_id_raw else None

        # Список legacy-администраторов для контролируемого перехода на роли.
        admin_ids_str = os.getenv("ADMIN_IDS", "")
        self.ADMIN_IDS: List[int] = [
            int(x.strip()) for x in admin_ids_str.split(",") if x.strip()
        ]
        
        # Обязательный абсолютный путь к общей SQLite-базе.
        self.DATABASE_PATH = os.getenv("DATABASE_PATH", "").strip()
        self.BOOK_MEDIA_ROOT = os.getenv("BOOK_MEDIA_ROOT", "").strip()
        
        # Настройки HTTP listener-а текущего запуска.
        self.HOST = os.getenv("HOST", "0.0.0.0").strip()
        self.PORT = int(os.getenv("PORT", "8000"))
        self.FLASK_DEBUG = os.getenv("FLASK_DEBUG", "").strip().lower() in {"1", "true", "yes"}
        self.SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS = os.getenv(
            "SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS", ""
        ).strip().lower() in {"1", "true", "yes"}
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        self.LOG_FORMAT = os.getenv("LOG_FORMAT", "text").strip().lower()
        if self.LOG_FORMAT not in {"text", "json"}:
            self.LOG_FORMAT = "text"
        self.REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Europe/Moscow").strip()
        self.BACKUP_DIR = os.getenv("BACKUP_DIR", "").strip()
        self.BACKUP_DAILY_RETENTION = int(os.getenv("BACKUP_DAILY_RETENTION", "14"))
        self.BACKUP_MONTHLY_RETENTION = int(os.getenv("BACKUP_MONTHLY_RETENTION", "3"))
        self.BACKUP_INTERVAL_SECONDS = int(
            os.getenv("BACKUP_INTERVAL_SECONDS", str(24 * 60 * 60))
        )
        self.BACKUP_LEASE_SECONDS = int(os.getenv("BACKUP_LEASE_SECONDS", "3600"))
        self.OPERATIONAL_ALERT_LEASE_SECONDS = int(
            os.getenv("OPERATIONAL_ALERT_LEASE_SECONDS", "300")
        )
        self.OPERATIONAL_ALERT_DEDUP_SECONDS = int(
            os.getenv("OPERATIONAL_ALERT_DEDUP_SECONDS", "1800")
        )
        self.NEW_ORDER_NOTIFICATION_LEASE_SECONDS = int(
            os.getenv("NEW_ORDER_NOTIFICATION_LEASE_SECONDS", "300")
        )
        self.MANUAL_DETAILS_RESEND_COOLDOWN_SECONDS = int(
            os.getenv("MANUAL_DETAILS_RESEND_COOLDOWN_SECONDS", "60")
        )
        self.DELIVERY_ENCRYPTION_ACTIVE_KEY_ID = os.getenv(
            "DELIVERY_ENCRYPTION_ACTIVE_KEY_ID", ""
        ).strip()
        self.DELIVERY_ENCRYPTION_KEYS_JSON = os.getenv(
            "DELIVERY_ENCRYPTION_KEYS_JSON", ""
        ).strip()
        self.DELIVERY_PII_RETENTION_DAYS = int(
            os.getenv("DELIVERY_PII_RETENTION_DAYS", "90")
        )

        # Кэш активных диалогов поддержки.
        self.support_pending_users = set()


# Глобальный экземпляр настроек
settings = Settings()
