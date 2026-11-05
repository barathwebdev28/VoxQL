import os
import random
from datetime import date

import psycopg2
from dotenv import load_dotenv
from faker import Faker

load_dotenv()
fake = Faker()

print("Connecting to database...")
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
print("Connected.")

# --- Clear existing data (safe re-run) ---
cur.execute("TRUNCATE order_items, orders, products, customers, regions RESTART IDENTITY CASCADE;")
print("Truncate done.")

# --- Regions ---
print("Seeding regions...")
regions = ["North", "South", "East", "West"]
for r in regions:
    cur.execute("INSERT INTO regions (region_name) VALUES (%s)", (r,))
print("Regions inserted.")

cur.execute("SELECT region_id FROM regions")
region_ids = [row[0] for row in cur.fetchall()]
print(f"Got region_ids: {region_ids}")

# --- Customers ---
print("Seeding customers...")
for i in range(50):
    cur.execute(
        "INSERT INTO customers (customer_name, region_id, signup_date) VALUES (%s, %s, %s)",
        (fake.name(), random.choice(region_ids), fake.date_between(start_date='-2y', end_date='today'))
    )
print("All 50 customers inserted.")

cur.execute("SELECT customer_id FROM customers")
customer_ids = [row[0] for row in cur.fetchall()]
print(f"Got {len(customer_ids)} customer_ids.")

# --- Products ---
print("Seeding products...")
categories = ["Electronics", "Clothing", "Home", "Books"]
for _ in range(20):
    cur.execute(
        "INSERT INTO products (product_name, category, unit_price) VALUES (%s, %s, %s)",
        (
            fake.word().capitalize() + " " + random.choice(["Pro", "Max", "Lite"]),
            random.choice(categories),
            round(random.uniform(10, 500), 2),
        )
    )
print("All 20 products inserted.")

cur.execute("SELECT product_id, unit_price FROM products")
products = cur.fetchall()
print(f"Got {len(products)} products.")

# --- Orders + Order Items ---
print("Seeding orders and order_items...")
for i in range(300):
    cur.execute(
        "INSERT INTO orders (customer_id, order_date, status) VALUES (%s, %s, %s) RETURNING order_id",
        (
            random.choice(customer_ids),
            fake.date_between(start_date='-1y', end_date='today'),
            random.choice(["completed", "completed", "completed", "cancelled"]),
        )
    )
    order_id = cur.fetchone()[0]

    for _ in range(random.randint(1, 4)):
        pid, price = random.choice(products)
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (%s, %s, %s, %s)",
            (order_id, pid, random.randint(1, 5), price)
        )
    if i == 299:
        print("All 300 orders and their items inserted.")

conn.commit()
cur.close()
conn.close()
print("Database seeded successfully: 4 regions, 50 customers, 20 products, 300 orders.")