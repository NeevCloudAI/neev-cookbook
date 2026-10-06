"""An inventory service that keeps stock in memory and serves a small JSON API on port 8000 (see SERVICE.md)."""
import http.server
import json
import os
import secrets

BOOT_ID = secrets.token_hex(8)  # new on every start, so an answer shows which start of the process it came from
with open("data/inventory.json") as f:
    stock = {item["sku"]: item for item in json.load(f)}  # loaded once; from here on it lives only in memory
writes = 0  # successful PUTs since this process started


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status, body):
        """Writes one JSON response."""
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """GET /health, GET /items and GET /items/<sku>."""
        if self.path == "/health":
            return self._send(200, {"boot_id": BOOT_ID, "writes": writes})
        if self.path == "/items":
            return self._send(200, {"items": sorted(stock.values(), key=lambda i: i["sku"])})
        sku = self.path.removeprefix("/items/")
        if self.path.startswith("/items/") and sku in stock:
            return self._send(200, stock[sku])
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        """PUT /items/<sku> with {"stock": <non-negative integer>} sets that item's stock."""
        global writes
        sku = self.path.removeprefix("/items/")
        if not self.path.startswith("/items/") or sku not in stock:
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            value = body["stock"]
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"error": 'send a JSON body like {"stock": 20}'})
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return self._send(400, {"error": "stock must be a non-negative integer"})
        stock[sku]["stock"] = value
        writes += 1
        self._send(200, stock[sku])

    def log_message(self, *args):
        """Keeps the process log quiet."""


http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()
