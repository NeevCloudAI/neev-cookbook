"""Checks count_words against the exercise. Run: python3 check.py"""
from app import count_words

CASES = [("hello world", 2), ("one", 1), ("", 0), ("two  spaces", 2), (" padded ", 1), ("tabs\tand\nnewlines", 3)]

failed = 0
for text, want in CASES:
    try:
        got = count_words(text)
    except Exception as e:
        got = type(e).__name__
    failed += got != want
    print(f"{'PASS' if got == want else 'FAIL'}  count_words({text!r}) = {got!r}, expected {want}")
raise SystemExit(1 if failed else 0)
