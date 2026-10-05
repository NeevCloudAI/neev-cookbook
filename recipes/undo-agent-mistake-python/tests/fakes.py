"""In-memory stand-ins for the sandbox (SDK and MCP views of it), its app server, and a model client."""
import copy
import json
import shlex
from types import SimpleNamespace

from neevai import SnapshotStatus

# Everything the real server lists, so tests can check the model only ever sees the workspace tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "process_kill",
                "delete_sandbox", "rollback_sandbox", "create_snapshot")


class FakeSandbox:
    """One sandbox: workspace files, the app server's process memory, and snapshots that capture both."""

    def __init__(self, name="undo-mistake-1", snapshot_statuses=("Pending", "Running", "Ready"), rollback_restores=True):
        self.name, self.id = name, "sb-1"
        self.workspace = {}
        self.server = None  # {"pid", "served"} while the app server runs
        self.deleted = False
        self.saved = {}
        self.rollbacks = []
        self.execs = []
        self.snapshot_statuses = list(snapshot_statuses)
        self.rollback_restores = rollback_restores
        self.files = SimpleNamespace(write=self._write, exists=lambda p: p in self.workspace, read_text=self._read)
        self.processes = SimpleNamespace(start=self._start, get=self._get)

    def wait_until_ready(self, timeout_ms=None):
        return self

    def _write(self, path, content):
        self.workspace[path] = content
        return {"bytes_written": len(content)}

    def _read(self, path):
        if path not in self.workspace:
            raise FileNotFoundError(path)
        return self.workspace[path]

    def exec(self, command, args=None, **kw):
        argv = list(command) + list(args or [])
        self.execs.append(argv)
        if argv == ["python3", "seed.py"]:
            self.workspace["data/customers.csv"] = "id,name,city\n" + "".join(f"{i},C{i},Pune\n" for i in range(1, 51))
            self.workspace["data/shop.db"] = "sqlite"
            return SimpleNamespace(stdout="", stderr="", exit_code=0)
        if argv[:2] == ["sh", "-c"]:
            return self.shell(argv[2])
        return SimpleNamespace(stdout="", stderr=f"unknown command {argv}", exit_code=127)

    def shell(self, script):
        """Understands the few commands the tests use, joined by &&: rm -rf <dir>, pkill; anything else is a no-op."""
        for command in script.split("&&"):
            words = shlex.split(command)
            if words[:2] == ["rm", "-rf"]:
                for target in words[2:]:
                    for path in [p for p in self.workspace if p == target or p.startswith(target.rstrip("/") + "/")]:
                        del self.workspace[path]
            elif words[:1] == ["pkill"]:
                self.server = None
        return SimpleNamespace(stdout="", stderr="", exit_code=0)

    def _start(self, program, **kw):
        self.started = program
        self.server = {"pid": 12, "served": 0}
        return SimpleNamespace(id="proc-1", state="running")

    def _get(self, process_id):
        return SimpleNamespace(process_id=process_id, state="running" if self.server else "exited")

    def get_url(self, port, **kw):
        return f"https://{port}-preview.example"

    def snapshot(self, params=None):
        self.saved["snap-1"] = copy.deepcopy((self.workspace, self.server))
        self.snapshot_name = (params or {}).get("name")
        return SimpleNamespace(id="snap-1", status=SnapshotStatus.Pending, error_message=None)

    def rollback(self, snapshot_id):
        self.rollbacks.append(snapshot_id)
        if self.rollback_restores:
            self.workspace, self.server = copy.deepcopy(self.saved[snapshot_id])
        return self

    def delete(self):
        self.deleted = True

    def stats(self, url):
        """What GET /stats on the preview URL answers, mirroring app/server.py."""
        if self.server is None:
            return None, {"error": "unreachable (ConnectError)"}
        self.server["served"] += 1
        base = {"pid": self.server["pid"], "served": self.server["served"]}
        if "data/customers.csv" not in self.workspace or "data/shop.db" not in self.workspace:
            return 500, {**base, "error": "[Errno 2] No such file or directory: 'data/customers.csv'"}
        return 200, {**base, "customers": 50, "orders": 120}


class FakeClient:
    """Mimics NeevAI().sandboxes: create and get_snapshot, recording the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        def get_snapshot(snapshot_id):
            status = sandbox.snapshot_statuses.pop(0) if len(sandbox.snapshot_statuses) > 1 else sandbox.snapshot_statuses[0]
            return SimpleNamespace(id=snapshot_id, status=SnapshotStatus(status),
                                   error_message="disk full" if status == "Failed" else None)

        self.sandboxes = SimpleNamespace(create=create, get_snapshot=get_snapshot)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox; fs_* and exec act on the given FakeSandbox."""

    def __init__(self, sandbox=None, exec_output=""):
        self.sandbox = sandbox or FakeSandbox()
        self.calls = []
        self.exec_output = exec_output
        self.raise_on = {}

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server", input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    async def call_tool(self, name, arguments=None):
        args = arguments or {}
        self.calls.append((name, args))
        if name in self.raise_on:
            raise self.raise_on[name]
        files = self.sandbox.workspace
        if name == "fs_write":
            if args["path"].startswith("/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{args["path"]}" escapes workspace root')
            files[args["path"]] = args["content"]
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": files[args["path"]], "size": len(files[args["path"]]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(files)]})
        if name == "exec":
            if args.get("program") == "sh" and (args.get("args") or [])[:1] == ["-c"]:
                self.sandbox.shell(args["args"][1])
            return _ok({"exit_code": 0, "stdout": self.exec_output, "stderr": ""})
        return _err(f"unexpected tool {name}")


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
