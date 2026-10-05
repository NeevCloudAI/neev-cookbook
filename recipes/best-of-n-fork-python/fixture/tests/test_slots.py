import unittest

from scheduler import free_slots


class TestFreeSlots(unittest.TestCase):
    def test_empty_day_is_one_slot(self):
        self.assertEqual(free_slots([]), [("09:00", "17:00")])

    def test_gaps_between_meetings(self):
        busy = [("10:00", "11:00"), ("13:00", "14:30")]
        self.assertEqual(free_slots(busy), [("09:00", "10:00"), ("11:00", "13:00"), ("14:30", "17:00")])

    def test_short_gaps_are_skipped(self):
        busy = [("09:00", "10:00"), ("10:15", "12:00")]
        self.assertEqual(free_slots(busy), [("12:00", "17:00")])

    def test_meetings_outside_the_day_are_clipped(self):
        busy = [("08:00", "09:30"), ("16:30", "18:00")]
        self.assertEqual(free_slots(busy), [("09:30", "16:30")])

    def test_a_meeting_inside_a_longer_one_frees_nothing(self):
        busy = [("09:00", "12:00"), ("10:00", "10:30")]
        self.assertEqual(free_slots(busy), [("12:00", "17:00")])


if __name__ == "__main__":
    unittest.main()
