from collections import Counter


def top_words(text, k):
    words, i = [], 0
    while i < len(text):
        if text[i].isalnum():
            start = i
            while i < len(text) and text[i].isalnum():
                i += 1
            words.append(text[start:i].lower())
        elif text[i] == " ":
            i += 1
        # Bug: never steps past punctuation, so any "," or "!" loops forever.
    counts = Counter(words)
    return sorted(counts, key=lambda word: (-counts[word], word))[:k]
