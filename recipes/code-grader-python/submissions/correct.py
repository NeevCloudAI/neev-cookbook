import re
from collections import Counter


def top_words(text, k):
    counts = Counter(re.findall(r"[a-z0-9]+", text.lower()))
    return sorted(counts, key=lambda word: (-counts[word], word))[:k]
