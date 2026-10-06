import unittest

from shop.cart import bulk_discount_percent, gst, order_total


class BulkDiscountTest(unittest.TestCase):
    def test_no_discount_below_ten_items(self):
        self.assertEqual(bulk_discount_percent(9), 0)

    def test_five_percent_from_ten_items(self):
        self.assertEqual(bulk_discount_percent(10), 5)

    def test_ten_percent_from_fifty_items(self):
        self.assertEqual(bulk_discount_percent(50), 10)


class GstTest(unittest.TestCase):
    def test_whole_amount(self):
        self.assertEqual(gst(1000), 180)

    def test_rounds_to_the_nearest_paisa(self):
        self.assertEqual(gst(1003), 181)  # 180.54 paise

    def test_a_half_rounds_up(self):
        self.assertEqual(gst(25), 5)  # 4.5 paise


class OrderTotalTest(unittest.TestCase):
    def test_small_order(self):
        self.assertEqual(order_total(10_000, 2), 23_600)

    def test_bulk_order_gets_ten_percent_off(self):
        self.assertEqual(order_total(1_000, 60), 63_720)  # 60,000 less 10% is 54,000, plus 9,720 GST


if __name__ == "__main__":
    unittest.main()
