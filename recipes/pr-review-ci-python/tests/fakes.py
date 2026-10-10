"""In-memory stand-ins for the SDK sandbox and client, the model client and GitHub, shaped like what review.py uses."""
import io
import json
import urllib.error
from types import SimpleNamespace

BASE = "a" * 40
HEAD = "b" * 40
DIFF = ("diff --git a/cart.js b/cart.js\n--- a/cart.js\n+++ b/cart.js\n@@ -5,2 +5,4 @@\n }\n"
        "+export function applyDiscount(total, code) {\n+  return total - Number(code.slice(4));\n }\n")
# The model's reply for DIFF: one comment on the new function's body, with a fix.
REPLY = ('{"summary": "Adds discount codes.", "comments": [{"path": "cart.js", "line": 7, '
         '"body": "This returns NaN for an unknown code.", "suggestion": "  return total;"}]}')


class FakeSandbox:
    """Mimics an SDK sandbox: git and getent execs are scripted, the test run streams scripted events."""

    def __init__(self, fetch_exit=0, dns_failures=0, test_events=None, stream_error=None, delete_error=None):
        self.name = "pr-review-test"
        self.execs = []
        self.updates = []
        self.deleted = False
        self._fetch_exit = fetch_exit
        self._dns_failures = dns_failures
        self._stream_error = stream_error
        self._delete_error = delete_error
        self.test_events = test_events if test_events is not None else [
            {"type": "stdout", "data": "✔ adds\n✔ sub"}, {"type": "stdout", "data": "tracts\n"},
            {"type": "exit", "exit_code": 0}]
        self.stream_calls = []

    def wait_until_ready(self, timeout_ms=None):
        return self

    def exec(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        args = list(args or [])
        self.execs.append((command, args, env))
        if command == "getent":
            if self._dns_failures:
                self._dns_failures -= 1
                return SimpleNamespace(exit_code=2, stdout="", stderr="")
            return SimpleNamespace(exit_code=0, stdout="140.82.112.3 github.com\n", stderr="")
        if command == "git" and "fetch" in args:
            return SimpleNamespace(exit_code=self._fetch_exit, stdout="",
                                   stderr="" if self._fetch_exit == 0 else "fatal: repository not found")
        if command == "git" and "diff" in args:
            return SimpleNamespace(exit_code=0, stdout=DIFF, stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    def exec_stream(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        self.stream_calls.append((command, list(args or []), cwd, env))
        yield from self.test_events
        if self._stream_error:
            raise self._stream_error

    def update(self, params):
        self.updates.append(params)
        return self

    def delete(self):
        if self._delete_error:
            raise self._delete_error
        self.deleted = True


class FakeClient:
    """Mimics NeevAI: sandboxes.create records its arguments and returns the given sandbox, or raises."""

    def __init__(self, sandbox=None, create_error=None):
        self.sandbox = sandbox or FakeSandbox()
        self.created = []
        self._create_error = create_error
        self.sandboxes = SimpleNamespace(create=self._create)

    def _create(self, params, allow_egress=None):
        self.created.append((params, allow_egress))
        if self._create_error:
            raise self._create_error
        return self.sandbox


class FakeModel:
    """Mimics the OpenAI client: returns a fixed reply and records the messages it was sent."""

    def __init__(self, reply=REPLY, error=None):
        self.calls = []
        self._reply = reply
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._reply))])


class FakeGitHubAPI:
    """Serves GitHub REST responses from a dict keyed by (method, path) and records every request."""

    def __init__(self, routes=None):
        self.requests = []
        self.routes = {("GET", "/repos/o/r/pulls/7"): {"title": "Add discounts", "base": {"sha": BASE},
                                                       "head": {"sha": HEAD}}}
        self.routes.update(routes or {})

    def __call__(self, request, timeout=None):
        path = request.full_url.removeprefix("https://api.github.com")
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), path, body, dict(request.header_items())))
        key = (request.get_method(), path)
        if key not in self.routes:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        return io.BytesIO(json.dumps(self.routes[key]).encode())
