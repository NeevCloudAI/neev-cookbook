"""In-memory stand-ins for AsyncNeevAI().sandboxes, sandboxes, their snapshots, the MCP session and a model."""
import asyncio
import copy
import json
import signal
from types import SimpleNamespace

from neevai import SnapshotStatus
from neevai.errors import NotFoundError

# Everything the real server lists, so tests can check the model only ever sees the workspace tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port")


def _result(stdout="", stderr="", exit_code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


def _not_found(what):
    return NotFoundError(404, {"code": "not_found", "message": f"{what} not found."}, None)


class FakeSandbox:
    """One sandbox: workspace files plus the inventory service's memory, which a snapshot captures together."""

    def __init__(self, client, name, state=None):
        self.client, self.name = client, name
        self.workspace, self.service = state if state else ({}, None)
        self.deleted = False
        self.execs = []
        self.files = SimpleNamespace(write=self._write)
        self.processes = SimpleNamespace(start=self._start)

    async def wait_until_ready(self, timeout_ms=None):
        await asyncio.sleep(0)
        return self

    async def _write(self, path, content):
        self.workspace[path] = content

    async def _start(self, program, **kw):
        self.started = program
        self.service = {"boot_id": f"boot-{self.name}", "writes": 0}
        return SimpleNamespace(id="proc-1")

    def fingerprint(self):
        return f"files-{hash(tuple(sorted(self.workspace.items()))) & 0xffffff:06x}"

    async def exec(self, command, args=None, timeout_ms=None, stdin=None, **kw):
        argv = list(command) + list(args or [])
        self.execs.append(argv)
        await asyncio.sleep(0)
        if argv[:2] == ["python3", "-c"] and "/health" in argv[2]:
            if self.service is None:
                return _result(stderr="<urlopen error [Errno 111] Connection refused>", exit_code=1)
            return _result(stdout=json.dumps(self.service) + "\n")
        if argv[:2] == ["sh", "-c"] and "sha256sum" in argv[2]:
            return _result(stdout=self.fingerprint() + "\n")
        if argv == ["python3", "-I", "-"]:
            return self.client.grader(self, stdin)
        if argv[:1] == ["pkill"]:  # an agent that stops the service
            self.service = None
            return _result()
        if argv[:1] == ["curl"] and "PUT" in argv:  # what an agent does to restock
            self.service["writes"] += 1
            return _result(stdout='{"stock": 20}')
        return _result(stderr=f"unknown command {argv}", exit_code=127)

    async def snapshot(self, params=None):
        snap_id = f"snap-{len(self.client.snapshots) + 1}"
        self.client.snapshots[snap_id] = {"source": self, "state": copy.deepcopy((self.workspace, self.service))}
        return SimpleNamespace(id=snap_id, status=SnapshotStatus.Pending, error_message=None)

    async def delete(self):
        """Deletes the sandbox; like the platform, its snapshots go with it."""
        for _ in range(3):  # a real delete takes a few round trips; other tasks run meanwhile
            await asyncio.sleep(0)
        if self.name in self.client.delete_fails:
            raise RuntimeError("delete refused")
        if self.name in self.client.interrupt_delete:  # Ctrl+C while this delete is in flight
            self.client.interrupt_delete.discard(self.name)
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(30)
        if self.deleted:
            raise _not_found("Sandbox")
        self.client.delete_order.append(self.name)
        self.deleted = True
        self.client.live.remove(self)
        for snap_id in [s for s, v in self.client.snapshots.items() if v["source"] is self]:
            del self.client.snapshots[snap_id]


def default_grader(sandbox, stdin):
    """Passes when the agent wrote done.txt containing "ok"; mirrors the checkers' one-line JSON verdict."""
    passed = sandbox.workspace.get("done.txt") == "ok"
    return _result(stdout=json.dumps({"passed": passed, "detail": "done.txt is ok" if passed else "done.txt missing"}) + "\n")


class FakeClient:
    """Mimics AsyncNeevAI().sandboxes: create (cold or with restore), get, get_snapshot and delete_snapshot."""

    def __init__(self, snapshot_statuses=("Pending", "Running", "Ready")):
        self.created, self.live, self.all = [], [], []
        self.snapshots = {}
        self.deleted_snapshots = []
        self.delete_order = []
        self.max_live = 0
        self.snapshot_statuses = list(snapshot_statuses)
        self.restore_shares_state = False  # True simulates a leak: every restore shares one copy of the state
        self.delete_fails = set()  # names whose delete is refused
        self.interrupt_delete = set()  # names whose delete is interrupted by Ctrl+C
        self.grader = default_grader
        self.on_create = None  # async hook run before a create returns, e.g. to interrupt mid-create
        self.sandboxes = SimpleNamespace(create=self._create, get=self._get, get_snapshot=self._get_snapshot,
                                         delete_snapshot=self._delete_snapshot)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def _create(self, params, allow_egress=None):
        self.created.append(params)
        state = None
        if "restore" in params:
            if params["restore"] not in self.snapshots:
                raise _not_found("Snapshot")
            snap = self.snapshots[params["restore"]]
            state = snap["state"] if self.restore_shares_state else copy.deepcopy(snap["state"])
        sandbox = FakeSandbox(self, params["name"], state)
        self.live.append(sandbox)
        self.all.append(sandbox)
        self.max_live = max(self.max_live, len(self.live))
        if self.on_create:
            await self.on_create(sandbox)
        return sandbox

    async def _get(self, name):
        for sandbox in self.live:
            if sandbox.name == name:
                return sandbox
        raise _not_found("Sandbox")

    async def _get_snapshot(self, snapshot_id):
        if snapshot_id not in self.snapshots:
            raise _not_found("Snapshot")
        statuses = self.snapshot_statuses
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return SimpleNamespace(id=snapshot_id, status=SnapshotStatus(status),
                               error_message="capture failed" if status == "Failed" else None)

    async def _delete_snapshot(self, snapshot_id):
        await asyncio.sleep(0)
        self.delete_order.append(snapshot_id)
        self.deleted_snapshots.append(snapshot_id)
        self.snapshots.pop(snapshot_id, None)


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* on its workspace dict, exec through its exec."""

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.calls = []
        self.timeouts = []
        self.raise_on = {}

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server", input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        args = arguments or {}
        self.calls.append((name, args))
        self.timeouts.append(read_timeout_seconds)
        if name in self.raise_on:
            raise self.raise_on[name]
        files = self.sandbox.workspace
        if name == "fs_write":
            if args["path"].startswith("/etc"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{args["path"]}" escapes workspace root')
            files[args["path"]] = args["content"]
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": files[args["path"]], "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(files)]})
        if name == "exec":
            r = await self.sandbox.exec([args["program"], *(args.get("args") or [])])
            return _ok({"exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr})
        return _err(f"unexpected tool {name}")


def reply(message, tokens=100):
    """Wraps an assistant message in a chat completion response with token usage."""
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(total_tokens=tokens))


def tool_call(name, arguments, call_id="c1"):
    """Builds an assistant message that calls one tool."""
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=call_id, type="function", function=fn)])


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)


class FakeModel:
    """Answers each request from a per-model script of messages, keyed by the conversation's turn number."""

    def __init__(self, scripts, default=None):
        self.scripts = scripts  # model -> list of messages, or a callable(messages) -> message
        self.default = default
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        await asyncio.sleep(0)
        script = self.scripts.get(kwargs["model"], self.default)
        if callable(script):
            return reply(await script(kwargs["messages"]))
        turn = sum(1 for m in kwargs["messages"] if m["role"] == "assistant")
        return reply(script[min(turn, len(script) - 1)])


SOLVE = [tool_call("fs_write", {"path": "done.txt", "content": "ok"}), tool_call("finish", {"summary": "done"}, "c2")]
WRONG = [tool_call("fs_write", {"path": "done.txt", "content": "nope"}), text("All done.")]
RESTOCK = [tool_call("exec", {"program": "curl", "args": ["-X", "PUT", "http://127.0.0.1:8000/items/MSE-WL"]}),
           tool_call("fs_write", {"path": "done.txt", "content": "ok"}, "c2"), tool_call("finish", {"summary": "restocked"}, "c3")]
