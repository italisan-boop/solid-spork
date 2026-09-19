"""
Конфигурация приложения.
Загружает переменные окружения и предоставляет доступ к настройкам.
"""
import os
from dotenv import load_dotenv
from typing import List

from proxy_url import normalize_authenticated_http_proxy
from runtime.credentials import is_managed_runtime, read_runtime_credential
from runtime.manifest import load_managed_runtime_manifest


if not is_managed_runtime():
    load_dotenv()


class Settings:
    """Класс настроек приложения."""
    
    def __init__(self):
        self.MANAGED_RUNTIME = is_managed_runtime()
        manifest = load_managed_runtime_manifest() if self.MANAGED_RUNTIME else None
        if manifest is not None:
            self.BOT_TOKEN = read_runtime_credential("telegram_bot_token", required=True)
            self.BOT_PROXY_URL = normalize_authenticated_http_proxy(
                (read_runtime_credential("bot_proxy_url") or "").strip(),
                setting_name="BOT_PROXY_URL",
            )
            self.WEBAPP_URL = (
                f"https://{manifest.canonical_host}/setup"
                if manifest.lifecycle_state == "awaiting_owner_claim"
                else f"https://{manifest.canonical_host}"
            )
            self.RUN_MODE = "webhook"
            self.WEBHOOK_URL = f"https://{manifest.canonical_host}/webhook"
            self.WEBHOOK_SECRET = read_runtime_credential(
                "telegram_webhook_secret", required=True
            )
            self.YOOKASSA_SHOP_ID = ""
            self.YOOKASSA_SECRET_KEY = ""
            self.YOOKASSA_RETURN_URL = ""
            self.OWNER_TELEGRAM_ID = manifest.owner_telegram_id
            self.ADMIN_IDS: List[int] = []
            self.DATABASE_PATH = str(manifest.database_path)
            self.BOOK_MEDIA_ROOT = str(manifest.media_root)
            self.BACKUP_DIR = str(manifest.backup_root)
            self.UNIX_SOCKET_PATH = str(manifest.socket_path)
        else:
            self.BOT_TOKEN = os.getenv("BOT_TOKEN")
            self.BOT_PROXY_URL = normalize_authenticated_http_proxy(
                os.getenv("BOT_PROXY_URL", "").strip(), setting_name="BOT_PROXY_URL"
            )
            self.WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
            self.RUN_MODE = os.getenv("RUN_MODE", "polling").strip().lower()
            self.WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
            self.WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
            self.YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
            self.YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
            self.YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()
            owner_id_raw = os.getenv("OWNER_TELEGRAM_ID", "").strip()
            if owner_id_raw and (not owner_id_raw.isdigit() or int(owner_id_raw) <= 0):
                raise ValueError("OWNER_TELEGRAM_ID must be a positive Telegram ID")
            self.OWNER_TELEGRAM_ID = int(owner_id_raw) if owner_id_raw else None
            admin_ids_str = os.getenv("ADMIN_IDS", "")
            self.ADMIN_IDS = [
                int(x.strip()) for x in admin_ids_str.split(",") if x.strip()
            ]
            self.DATABASE_PATH = os.getenv("DATABASE_PATH", "").strip()
            self.BOOK_MEDIA_ROOT = os.getenv("BOOK_MEDIA_ROOT", "").strip()
            self.BACKUP_DIR = os.getenv("BACKUP_DIR", "").strip()
            self.UNIX_SOCKET_PATH = os.getenv("UNIX_SOCKET_PATH", "").strip()

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
