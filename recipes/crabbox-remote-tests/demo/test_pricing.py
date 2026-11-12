import unittest

from pricing import total


class TotalTest(unittest.TestCase):
    def test_standard_rate(self):
        self.assertEqual(total([40, 60]), 118.0)

    def test_reduced_rate(self):
        self.assertEqual(total([100], kind="reduced"), 105.0)

    def test_empty_order(self):
        self.assertEqual(total([]), 0.0)


if __name__ == "__main__":
    unittest.main()
