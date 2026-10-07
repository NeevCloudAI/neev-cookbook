import unittest

from scheduler import merge, to_hhmm, to_minutes


class TestConversions(unittest.TestCase):
    def test_round_trip(self):
        for hhmm in ("00:00", "09:05", "13:30", "23:59"):
            self.assertEqual(to_hhmm(to_minutes(hhmm)), hhmm)


class TestMerge(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(merge([]), [])

    def test_disjoint_intervals_are_sorted(self):
        self.assertEqual(merge([(300, 360), (60, 120)]), [(60, 120), (300, 360)])

    def test_overlapping_intervals_merge(self):
        self.assertEqual(merge([(60, 120), (90, 150)]), [(60, 150)])

    def test_touching_intervals_merge(self):
        self.assertEqual(merge([(60, 120), (120, 180)]), [(60, 180)])

    def test_contained_interval_does_not_shrink_the_outer_one(self):
        self.assertEqual(merge([(60, 300), (120, 180)]), [(60, 300)])

    def test_chain_with_a_contained_interval(self):
        self.assertEqual(merge([(0, 100), (10, 20), (90, 120)]), [(0, 120)])


if __name__ == "__main__":
    unittest.main()
