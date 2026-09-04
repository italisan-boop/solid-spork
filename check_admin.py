import os
from dotenv import load_dotenv

load_dotenv()

raw_value = os.getenv("ADMIN_IDS")
print("=" * 50)
print(f"Сырое значение ADMIN_IDS: '{raw_value}'")
print(f"Тип: {type(raw_value)}")

if raw_value:
    admin_ids = [int(x.strip()) for x in raw_value.split(",") if x.strip()]
    print(f"Распарсенные ID: {admin_ids}")
    print(f"Проверьте, что ваш ID есть в списке выше")
else:
    print("❌ Переменная ADMIN_IDS НЕ найдена в .env!")
    print("Возможные причины:")
    print("  1. Файл называется .env.txt вместо .env")
    print("  2. Файл лежит в другой папке")
    print("  3. Опечатка в имени переменной")

print("=" * 50)

# Проверим все переменные в .env
print("\nВсе переменные из .env:")
load_dotenv(override=True)
for key in os.environ:
    if 'ADMIN' in key or 'BOT' in key or 'WEB' in key:
        value = os.getenv(key)
        # Скрываем токены и чувствительные данные
        if 'TOKEN' in key and value:
            value = value[:5] + "..." if len(value) > 5 else "***"
        print(f"  {key} = {value}")