# Exercise: a word counter

Finish `count_words(text)` in `app.py` so the app counts words.

- `GET /count?text=...` answers JSON such as `{"text": "hello world", "words": 2}`.
- Words are separated by any whitespace: spaces, tabs or newlines, one or many.
- Empty text, or text with only spaces, has 0 words.

Run `python3 check.py` to test your function, and `python3 app.py` to serve the page on port 8000.
