"""Checks the shop's data: every customer and order is still there and every order has its customer."""
import sqlite3
import unittest


class ShopDataTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect("shop.db")
        self.addCleanup(self.db.close)

    def count(self, sql):
        return self.db.execute(sql).fetchone()[0]

    def test_every_customer_is_kept(self):
        self.assertEqual(self.count("SELECT COUNT(*) FROM customers"), 50)

    def test_every_order_is_kept(self):
        self.assertEqual(self.count("SELECT COUNT(*) FROM orders"), 120)

    def test_every_order_has_its_customer(self):
        self.assertEqual(self.count(
            "SELECT COUNT(*) FROM orders WHERE customer_id NOT IN (SELECT id FROM customers)"), 0)


if __name__ == "__main__":
    unittest.main()
