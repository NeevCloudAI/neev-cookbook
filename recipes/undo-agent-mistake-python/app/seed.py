"""Creates the shop's data: data/customers.csv (50 rows) and data/shop.db with an orders table (120 rows)."""
import csv
import os
import sqlite3

CITIES = ["Bengaluru", "Chennai", "Delhi", "Hyderabad", "Mumbai", "Pune"]

os.makedirs("data", exist_ok=True)
with open("data/customers.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["id", "name", "city"])
    for i in range(1, 51):
        writer.writerow([i, f"Customer {i}", CITIES[i % len(CITIES)]])

with sqlite3.connect("data/shop.db") as db:
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL)")
    db.executemany("INSERT INTO orders (customer_id, total) VALUES (?, ?)",
                   [(i % 50 + 1, round(199 + i * 37.5, 2)) for i in range(120)])
