import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
RUN_MODE = os.getenv("RUN_MODE", "polling")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
# Глобальные состояния для админки (рассылка и поддержка)
broadcast_pending_users = set()
support_pending_users = set()