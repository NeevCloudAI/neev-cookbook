"""Word counter: GET /count?text=... answers {"text": ..., "words": N}; / serves index.html."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8000


def count_words(text: str) -> int:
    """Returns how many words are in text."""
    raise NotImplementedError


class Handler(BaseHTTPRequestHandler):
    """Serves the page and the /count API."""

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/count":
            text = parse_qs(url.query, keep_blank_values=True).get("text", [""])[0]
            self._send(200, "application/json", json.dumps({"text": text, "words": count_words(text)}))
        elif url.path == "/":
            self._send(200, "text/html", Path(__file__).with_name("index.html").read_text())
        else:
            self._send(404, "text/plain", "not found")

    def _send(self, status, content_type, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    # 0.0.0.0, not 127.0.0.1: the preview URL can only reach a server listening on all interfaces.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
