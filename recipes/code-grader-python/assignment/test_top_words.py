"""Hidden tests for top_words: copied into a directory only the grader can read, never shown to submissions."""
import unittest

from harness import submission


class TestTopWords(unittest.TestCase):
    def test_single_most_common(self):
        self.assertEqual(submission.top_words("the cat and the hat", 1), ["the"])

    def test_counts_ignore_case(self):
        self.assertEqual(submission.top_words("Go go GO stop", 2), ["go", "stop"])

    def test_ties_break_alphabetically(self):
        self.assertEqual(submission.top_words("pear apple fig apple pear fig kiwi", 3), ["apple", "fig", "pear"])

    def test_punctuation_separates_words(self):
        self.assertEqual(submission.top_words("Hello, world! Hello... world? hello", 2), ["hello", "world"])

    def test_k_larger_than_vocabulary(self):
        self.assertEqual(submission.top_words("two one two", 5), ["two", "one"])

    def test_digits_count_as_words(self):
        self.assertEqual(submission.top_words("3 apples and 3 pears", 1), ["3"])
