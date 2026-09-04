import sqlite3

conn = sqlite3.connect('semena_znaniy.db')
cursor = conn.cursor()

print("=== Таблица orders ===")
cursor.execute("SELECT * FROM orders")
orders = cursor.fetchall()
for order in orders:
    print(order)

print("\n=== Таблица order_items ===")
cursor.execute("SELECT * FROM order_items")
items = cursor.fetchall()
for item in items:
    print(item)

print(f"\nВсего заказов: {len(orders)}")
print(f"Всего товаров: {len(items)}")

conn.close()