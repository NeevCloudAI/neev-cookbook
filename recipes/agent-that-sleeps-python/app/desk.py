"""The agent's desk: a long-running process that keeps its notes in memory only, never on disk.

python3 desk.py              runs the desk (started once, stays up for the agent's whole life)
python3 desk.py add "<note>" adds a note
python3 desk.py show         prints the desk's state as JSON
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADDRESS = ("127.0.0.1", 7000)  # loopback: only commands run inside this sandbox can reach the desk
BOOT_ID = secrets.token_hex(4)  # new for every process start, so a restarted desk cannot pass for this one
notes = []
ticks = 0  # one per second while the process runs; frozen while the sandbox sleeps


def heartbeat():
    """Counts the seconds this process has actually been running."""
    global ticks
    while True:
        time.sleep(1)
        ticks += 1


def state():
    """The desk as JSON: who it is (PID, boot ID), how many seconds it has run, and its notes."""
    return {"pid": os.getpid(), "boot_id": BOOT_ID, "ticks": ticks, "notes": notes}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Answers with the desk's state."""
        self._send(state())

    def do_POST(self):
        """Adds one note."""
        note = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode().strip()
        if note:
            notes.append(note)
        self._send({"notes": len(notes)})

    def _send(self, body):
        """Writes one JSON response."""
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        """Keeps the process log quiet."""


def client(note=None):
    """Adds a note (or, with none, reads the state) on the running desk and prints its answer."""
    url = f"http://{ADDRESS[0]}:{ADDRESS[1]}/"
    request = urllib.request.Request(url, data=note.encode() if note is not None else None)
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.read().decode())


if __name__ == "__main__":
    if len(sys.argv) == 1:
        threading.Thread(target=heartbeat, daemon=True).start()
        ThreadingHTTPServer(ADDRESS, Handler).serve_forever()
    elif sys.argv[1:2] == ["add"] and len(sys.argv) >= 3:
        client(" ".join(sys.argv[2:]))
    elif sys.argv[1:] == ["show"]:
        client()
    else:
        sys.exit(__doc__)
