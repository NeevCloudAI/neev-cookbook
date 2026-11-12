"""In-memory stand-ins for the NeevAI client and its sandboxes."""
import copy
from types import SimpleNamespace


def tap(cart_js: str) -> tuple[int, str]:
    """Plays node --test over the bundled tests: the discount test fails while the planted bug is in cart.js."""
    buggy = "subtotal - discountPercent" in cart_js
    lines = ["TAP version 13", "ok 1 - adds up price times quantity", "  ---", "  duration_ms: 0.8", "  ...",
             "ok 2 - an empty cart costs nothing"]
    if buggy:
        lines += ["not ok 3 - takes 10% off", "  ---", "  expected: 1800", "  actual: 1990", "  ..."]
    else:
        lines += ["ok 3 - takes 10% off"]
    return (1 if buggy else 0), "\n".join(lines + ["1..3"]) + "\n"


class FakeSandbox:
    """Mimics a sandbox handle: files in a dict, exec answers node --version and node --test."""

    def __init__(self, client, name, node="v22.23.3", files=None):
        self.client, self.name, self.node = client, name, node
        self.store = files or {}
        self.execs, self.snapshots, self.deleted = [], [], False
        self.files = SimpleNamespace(write=self._write, read_text=lambda path: self.store[path])

    def _write(self, path, content):
        self.store[path] = content
        return {"bytes_written": len(content)}

    def wait_until_ready(self, timeout_ms=None):
        return self

    def exec(self, command, cwd=None, timeout_ms=None):
        self.execs.append((command, cwd))
        if command == ["node", "--version"]:
            return SimpleNamespace(exit_code=127 if self.node is None else 0, stdout=self.node or "", stderr="")
        if command[:2] == ["node", "--test"]:
            code, out = tap(self.store.get(f"{cwd}/cart.js", ""))
            return SimpleNamespace(exit_code=code, stdout=out, stderr="")
        raise AssertionError(f"unexpected command {command}")

    def snapshot(self, params, wait_until_ready=False):
        self.snapshots.append((params, wait_until_ready))
        return SimpleNamespace(id="snap-1", status="Ready")

    def fork(self, name):
        forked = FakeSandbox(self.client, name, self.node, copy.deepcopy(self.store))
        if self.client.lost_reply:
            self.client.made.append(forked)
        if self.client.fork_error:
            raise self.client.fork_error
        self.client.made.append(forked)
        return forked

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create and list, and records every sandbox the server made.

    lost_reply makes the server create the sandbox or fork even though the call then raises.
    """

    def __init__(self, node="v22.23.3", create_error=None, fork_error=None, lost_reply=False):
        self.made, self.created = [], []
        self.fork_error, self.lost_reply = fork_error, lost_reply

        def create(params):
            self.created.append(params)
            sandbox = FakeSandbox(self, params["name"], node)
            if lost_reply:
                self.made.append(sandbox)
            if create_error:
                raise create_error
            if not lost_reply:
                self.made.append(sandbox)
            return sandbox

        def list_(name, limit):
            return SimpleNamespace(items=[sb for sb in self.made if sb.name.startswith(name)])

        self.sandboxes = SimpleNamespace(create=create, list=list_)
