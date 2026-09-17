import os
from dotenv import load_dotenv

load_dotenv()

raw_value = os.getenv("ADMIN_IDS")
print("=" * 50)
print(f"ADMIN_IDS configured: {'yes' if raw_value else 'no'}")

if raw_value:
    try:
        admin_ids = [int(value.strip()) for value in raw_value.split(",") if value.strip()]
    except ValueError:
        print("❌ ADMIN_IDS contains a non-numeric value.")
    else:
        print(f"Configured administrator count: {len(admin_ids)}")
        print("Check that your Telegram ID is included in the configured list.")
else:
    print("❌ ADMIN_IDS is not configured in .env.")
    print("Check the file name, location, and variable spelling.")

print("=" * 50)
