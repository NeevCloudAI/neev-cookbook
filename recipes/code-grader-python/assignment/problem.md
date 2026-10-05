# Assignment: top words

Write `top_words(text, k)` in `solution.py`. It returns the `k` most frequent words in `text`, most frequent first.

- A word is a run of letters and digits; everything else separates words.
- Case does not matter: `Go` and `go` are the same word, returned in lowercase.
- Words with the same count are returned in alphabetical order.
- If there are fewer than `k` distinct words, return all of them.

```python
>>> top_words("the cat and the hat", 2)
['the', 'and']
```
