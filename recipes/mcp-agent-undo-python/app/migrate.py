"""Applies one SQL migration file to shop.db in a single transaction: python3 migrate.py <file.sql>."""
import os
import sqlite3
import sys

path = sys.argv[1]
with open(path) as f:
    script = f.read()
db = sqlite3.connect("shop.db")
db.executescript(f"BEGIN;\n{script}\nCOMMIT;")
db.close()
print(f"applied {path} to {os.path.abspath('shop.db')}")
