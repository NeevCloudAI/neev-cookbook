"""A fake sandbox MCP server that answers with the shapes the real one does."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeMCP:
    def __init__(self, *, status: int = 200, exec_exit_code: int = 0, sse: bool = False):
        self.status = status
        self.exec_exit_code = exec_exit_code
        self.sse = sse
        self.calls: list[tuple[str, dict, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                headers = {key.lower(): value for key, value in self.headers.items()}
                outer.calls.append((body["params"].get("name", body["method"]), body, headers))
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b"invalid credentials")
                    return
                payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": outer.result(body)})
                if outer.sse:
                    payload = f"event: message\ndata: {payload}\n\n"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload.encode())

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def result(self, body: dict) -> dict:
        if body["method"] == "tools/list":
            return {"tools": [{"name": "exec"}, {"name": "create_sandbox"}]}
        tool = body["params"]["name"]
        if tool == "exec":
            output = {
                "stdout": "hello from the sandbox\n",
                "stderr": "boom" if self.exec_exit_code else "",
                "exit_code": self.exec_exit_code,
            }
            return {"content": [{"type": "text", "text": json.dumps(output)}], "structuredContent": output}
        return {"content": [{"type": "text", "text": "{}"}]}

    def tools_called(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def close(self):
        self.server.shutdown()
