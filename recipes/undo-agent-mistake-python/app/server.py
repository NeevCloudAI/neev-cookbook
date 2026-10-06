"""A tiny shop API: GET /stats reports customers and orders read from data/, plus an in-memory request count."""
import csv
import http.server
import json
import os
import sqlite3

served = 0  # lives only in this process's memory, so it shows whether memory survived


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        """Answers /stats from the data files; any other path is a plain health check that is not counted."""
        global served
        if self.path != "/stats":
            self._send(200, {"ok": True})
            return
        served += 1
        try:
            with open("data/customers.csv", newline="") as f:
                customers = sum(1 for _ in csv.DictReader(f))
            with sqlite3.connect("file:data/shop.db?mode=ro", uri=True) as db:
                orders = db.execute("SELECT count(*) FROM orders").fetchone()[0]
            self._send(200, {"pid": os.getpid(), "served": served, "customers": customers, "orders": orders})
        except Exception as e:  # missing files or table: report it instead of dropping the connection
            self._send(500, {"pid": os.getpid(), "served": served, "error": str(e)})

    def _send(self, code, body):
        """Writes one JSON response."""
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        """Keeps the process log quiet."""


# 0.0.0.0, not 127.0.0.1: the preview URL can only reach a server listening on all interfaces.
http.server.ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
