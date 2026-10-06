"""A three-stage order pipeline: it reads its batch from stdin, keeps it and its running totals in memory, and
serves its progress on 127.0.0.1:8080. A stage that fails leaves the service up, holding the state it failed in.
"""
import http.server
import json
import os
import sys
import threading
import time
import traceback

PORT = 8080
TEAMS = {"north": "delhi", "south": "chennai", "east": "kolkata", "west": "mumbai"}
STAGES = ("parse", "enrich", "aggregate")
PAUSE_S = 0.02  # per record and stage, so a run takes several seconds like a real job

lock = threading.Lock()
batch: list[dict] = []
state = {"pid": os.getpid(), "status": "running", "stage": None, "done": {s: 0 for s in STAGES},
         "totals": {}, "error": None, "traceback": None, "position": None, "reads": 0}


def parse(record: dict) -> dict:
    """Stage 1: keeps the fields the pipeline uses, failing on a missing one, and normalises the region."""
    return {"id": record["id"], "region": record["region"].strip().lower(), "amount": record["amount"]}


def enrich(record: dict) -> dict:
    """Stage 2: assigns each order to the sales team for its region."""
    return {**record, "team": TEAMS[record["region"]]}


def aggregate(record: dict) -> None:
    """Stage 3: adds the order to its region's running total."""
    totals = state["totals"]
    totals[record["region"]] = totals.get(record["region"], 0.0) + record["amount"]


def work() -> None:
    """Runs the stages in order over the batch held in memory; a failure is kept in state, not raised."""
    for stage, step in zip(STAGES, (parse, enrich, aggregate)):
        with lock:
            state["stage"] = stage
        for i, record in enumerate(batch):
            time.sleep(PAUSE_S)
            try:
                with lock:
                    result = step(record)
                    batch[i] = result if result is not None else record
                    state["done"][stage] += 1
            except Exception as e:
                with lock:
                    state.update(status="error", position=i, error=f"{type(e).__name__}: {e}",
                                 traceback=traceback.format_exc())
                print(f"stage {stage} failed at record {i}: {type(e).__name__}: {e}", flush=True)
                return
        print(f"stage {stage}: {len(batch)} records", flush=True)
    with lock:
        state["status"] = "done"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        """Serves / (the endpoints), /state (progress and any error) and /records/<index> (one record in memory)."""
        with lock:
            if self.path == "/state":
                state["reads"] += 1  # lives only in this process, so it shows whether memory survived
                self._send(200, state)
            elif self.path.startswith("/records/") and self.path[9:].isdigit() and int(self.path[9:]) < len(batch):
                self._send(200, batch[int(self.path[9:])])
            elif self.path == "/":
                self._send(200, {"endpoints": ["/state", "/records/<index>"], "records": len(batch)})
            else:
                self._send(404, {"error": "not found"})

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


if __name__ == "__main__":
    batch = json.load(sys.stdin)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=work, daemon=True).start()
    server.serve_forever()
