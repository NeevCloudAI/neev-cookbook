import re
from collections import Counter


def top_words(text, k):
    # Returns (word, count) pairs instead of just the words.
    counts = Counter(re.findall(r"[a-z0-9]+", text.lower()))
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:k]
