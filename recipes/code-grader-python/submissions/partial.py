from collections import Counter


def top_words(text, k):
    # Splits on spaces only, and most_common keeps ties in first-seen order.
    return [word for word, _ in Counter(text.lower().split()).most_common(k)]
