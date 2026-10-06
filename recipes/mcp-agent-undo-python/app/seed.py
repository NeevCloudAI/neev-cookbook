"""Creates shop.db: 50 customers (the last 10 are new sign-ups with no orders yet) and 120 orders."""
import sqlite3

CITIES = ["Bengaluru", "Chennai", "Delhi", "Hyderabad", "Mumbai", "Pune"]

with sqlite3.connect("shop.db") as db:
    db.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL)")
    db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), "
               "contact_email TEXT NOT NULL, total REAL NOT NULL)")
    db.executemany("INSERT INTO customers (id, name, city) VALUES (?, ?, ?)",
                   [(i, f"Customer {i}", CITIES[i % len(CITIES)]) for i in range(1, 51)])
    db.executemany("INSERT INTO orders (customer_id, contact_email, total) VALUES (?, ?, ?)",
                   [(i % 40 + 1, f"customer{i % 40 + 1}@example.com", round(199 + i * 37.5, 2)) for i in range(120)])
