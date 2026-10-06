"""A churn-scoring service: trains a model on data.csv at start-up, keeps it in memory and answers GET / on port 8000."""
import http.server
import json
import os
import secrets

import pandas as pd
from sklearn.ensemble import RandomForestClassifier

BOOT_ID = secrets.token_hex(8)  # new on every start, so an answer shows which start of the process it came from
data = pd.read_csv("data.csv")
model = RandomForestClassifier(n_estimators=40, random_state=7, n_jobs=-1)
model.fit(data.drop(columns="churned"), data.churned)
accuracy = round(float(model.score(data.drop(columns="churned"), data.churned)), 4)
served = 0  # lives only in this process's memory


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        """Reports which process answered, how many requests it has served, and the model it holds."""
        global served
        served += 1
        body = json.dumps({"boot_id": BOOT_ID, "pid": os.getpid(), "served": served,
                           "rows": len(data), "accuracy": accuracy}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Keeps the process log quiet."""


http.server.ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
