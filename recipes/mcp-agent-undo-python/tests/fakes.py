"""In-memory stand-ins for the sandbox (SDK and MCP views of it), its shop database, and a model client."""
import asyncio
import copy
import json
from types import SimpleNamespace

from neevai import SnapshotStatus

# Everything the real server lists, so tests can check the model only ever sees its allowlist.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox",
                "create_snapshot", "list_snapshots", "rollback_sandbox", "pause_sandbox")
MIGRATE = "python3 migrate.py migrations/002_customer_email.sql"
TESTS = "python3 -m unittest -v test_shop"


class FakeSandbox:
    """One sandbox: workspace files, the row counts in shop.db, and snapshots that capture both."""

    def __init__(self, name="mcp-undo-1", polls_to_ready=1, rollback_restores=True):
        self.name = name
        self.workspace, self.db = {}, None
        self.snaps = {}  # id -> {"state", "polls_left", "name"}
        self.deleted = False
        self.rollbacks = []
        self.refreshed = 0
        self.polls_to_ready = polls_to_ready
        self.descriptor_size = 10  # the real server adds about 1 KB of build details per snapshot
        self.rollback_restores = rollback_restores
        self.files = SimpleNamespace(write=self._write)

    def wait_until_ready(self, timeout_ms=None):
        return self

    def refresh(self):
        self.refreshed += 1
        return self

    def _write(self, path, content):
        self.workspace[path] = content
        return {"bytes_written": len(content)}

    def exec(self, command, args=None, **kw):
        """The SDK's exec: understands the seed, the row count and the test run."""
        argv = list(command) + list(args or [])
        if argv == ["python3", "seed.py"]:
            self.db = {"customers": 50, "orders": 120}
            return _result(0)
        if argv[:2] == ["python3", "-c"]:
            return _result(0, json.dumps(self.db))
        if argv[:3] == ["python3", "-m", "unittest"]:
            return self.run_tests()
        return _result(127, stderr=f"unknown command {argv}")

    def run_tests(self):
        """What test_shop.py reports for the current rows."""
        if self.db == {"customers": 50, "orders": 120}:
            return _result(0, stderr="Ran 3 tests\n\nOK")
        return _result(1, stderr="AssertionError: 40 != 50\n\nFAILED (failures=1)")

    def shell(self, script):
        """Runs the two commands the agent needs: the migration and the tests; anything else is a no-op."""
        if MIGRATE in script:
            self.db = {**self.db, "customers": 40}
            return _result(0, "applied migrations/002_customer_email.sql to /workspace/shop.db")
        if "unittest" in script:
            return self.run_tests()
        return _result(0)

    def take_snapshot(self, name=None):
        sid = f"0000000{len(self.snaps) + 1}-0000-7000-8000-000000000000"
        self.snaps[sid] = {"state": copy.deepcopy((self.workspace, self.db)), "polls_left": self.polls_to_ready, "name": name}
        return sid

    def snapshot_status(self, sid):
        return "Ready" if self.snaps[sid]["polls_left"] <= 0 else "Pending"

    def snapshots(self):
        return [SimpleNamespace(id=sid, name=s["name"], status=SnapshotStatus(self.snapshot_status(sid)))
                for sid, s in self.snaps.items()]

    def rollback_to(self, sid):
        self.rollbacks.append(sid)
        if self.rollback_restores:
            self.workspace, self.db = copy.deepcopy(self.snaps[sid]["state"])

    def delete(self):
        self.deleted = True


def _result(code, stdout="", stderr=""):
    return SimpleNamespace(exit_code=code, stdout=stdout, stderr=stderr)


class FakeClient:
    """Mimics NeevAI().sandboxes.create, recording the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def _text(data, is_error=False):
    """A tool result as the server sends these tools: JSON in a text block, no structured content."""
    body = data if isinstance(data, str) else json.dumps(data)
    return SimpleNamespace(is_error=is_error, structured_content=None, content=[SimpleNamespace(text=body)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox; the tools act on the given FakeSandbox."""

    def __init__(self, sandbox=None):
        self.sandbox = sandbox or FakeSandbox()
        self.calls = []
        self.raise_on = {}
        self.hang_on = set()

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server", input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    async def call_tool(self, name, arguments=None):
        args = arguments or {}
        self.calls.append((name, args))
        if name in self.raise_on:
            raise self.raise_on[name]
        if name in self.hang_on:
            await asyncio.sleep(30)
        sb = self.sandbox
        if name == "fs_read":
            if args["path"] not in sb.workspace:
                return _text(f"the sandbox refused this call: not_found: {args['path']}", True)
            return _ok_structured({"content": sb.workspace[args["path"]], "eof": True})
        if name == "fs_list":
            return _ok_structured({"entries": [{"name": n, "type": "file"} for n in sorted(sb.workspace)]})
        if name == "exec":
            argv = [args.get("program", "")] + list(args.get("args") or [])
            r = sb.shell(argv[2]) if argv[:2] == ["sh", "-c"] else _result(0)
            return _ok_structured({"exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr})
        if name == "create_snapshot":
            sid = sb.take_snapshot(args.get("name"))
            return _text({"id": sid, "status": "Pending", "details": {"cpu": "1"}})
        if name == "list_snapshots":
            items = []
            for sid, s in sb.snaps.items():
                items.append({"id": sid, "status": sb.snapshot_status(sid), "details": {"pad": "x" * sb.descriptor_size}})
                s["polls_left"] -= 1
            return _text({"items": items, "page": 1, "total": len(items)})
        if name == "rollback_sandbox":
            sid = args.get("snapshot_id")
            if sid not in sb.snaps or sb.snapshot_status(sid) != "Ready":
                return _text(f"the sandbox refused this call: snapshot {sid} is not a finished snapshot of this sandbox", True)
            sb.rollback_to(sid)
            return _text({"name": sb.name, "phase": "Pending"})
        return _text(f"unexpected tool {name}", True)


def _ok_structured(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


class FakeModel:
    """Replays a scripted list of assistant messages, one per call, and records each request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        msg = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def tool_call(name, arguments, call_id="c1"):
    """Builds an assistant message that calls one tool."""
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    call = SimpleNamespace(id=call_id, type="function", function=fn)
    return SimpleNamespace(content=None, tool_calls=[call])


def shell(script, call_id="c1"):
    """Builds an assistant message that runs one shell command through exec."""
    return tool_call("exec", {"program": "sh", "args": ["-c", script]}, call_id)


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)


SNAP_1 = "00000001-0000-7000-8000-000000000000"


def careful_model():
    """An agent that follows the protocol: snapshot, wait for Ready, migrate, test, roll back, retest, finish."""
    return FakeModel([
        tool_call("create_snapshot", {"name": "before-migration"}, "c1"),
        tool_call("list_snapshots", {}, "c2"),
        tool_call("list_snapshots", {}, "c3"),
        shell(MIGRATE, "c4"),
        shell(TESTS, "c5"),
        tool_call("rollback_sandbox", {"snapshot_id": SNAP_1}, "c6"),
        shell(TESTS, "c7"),
        tool_call("finish", {"verdict": "unsafe", "summary": "the migration drops the 10 customers with no orders"}, "c8"),
    ])


def reckless_model():
    """An agent that migrates straight away and never snapshots."""
    return FakeModel([
        shell(MIGRATE, "c1"),
        shell(TESTS, "c2"),
        tool_call("finish", {"verdict": "unsafe", "summary": "tests fail after the migration"}, "c3"),
    ])
