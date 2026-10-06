"""In-memory stand-ins for the NeevAI SDK, the pipeline service, the sandbox's MCP session and a model client."""
import copy
import json
from types import SimpleNamespace

from neevai import SnapshotStatus

# Everything the real server lists, so tests can check the model only ever sees the investigation tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "process_kill", "delete_sandbox",
                "rollback_sandbox", "create_snapshot")
ERROR = "TypeError: unsupported operand type(s) for +: 'float' and 'str'"


class Pipeline:
    """Plays app/pipeline.py: each /state read moves it one stage on, until stage 3 fails on the bad record."""

    def __init__(self, stdin, pid=13, outcome="error"):
        self.batch = json.loads(stdin)
        self.bad = next(i for i, r in enumerate(self.batch) if isinstance(r["amount"], str))
        self.outcome = outcome  # "error", "done" or "stuck" (never finishes)
        self.state = {"pid": pid, "status": "running", "stage": None, "position": None, "error": None, "reads": 0}
        self.polls = 0
        self.process_state = "running"  # what the sandbox reports for the pipeline's process

    def get(self, path):
        """Answers one GET like the real service, or None while it is not listening yet."""
        if path == "/state":
            self.polls += 1
            if self.polls == 1:
                return None  # the server is not listening yet
            stages = ("parse", "enrich", "aggregate")
            if self.state["status"] == "running" and self.outcome != "stuck":
                if self.polls - 2 < len(stages):
                    self.state["stage"] = stages[self.polls - 2]
                elif self.outcome == "done":
                    self.state["status"] = "done"
                else:
                    self.state.update(status="error", position=self.bad, error=ERROR)
            self.state["reads"] += 1
            return self.state
        if path.startswith("/records/"):
            return self.batch[int(path[9:])]
        return {"endpoints": ["/state", "/records/<index>"], "records": len(self.batch)}


class FakeSandbox:
    """One sandbox: workspace files, the pipeline process (with its memory) and snapshots that capture both."""

    def __init__(self, name, client):
        self.name, self.id, self.client = name, f"id-{name}", client
        self.workspace, self.pipeline, self.deleted = {}, None, False
        self.files = SimpleNamespace(write=self._write)
        self.processes = SimpleNamespace(start=self._start, get=self._get)

    def wait_until_ready(self, timeout_ms=None):
        return self

    def _write(self, path, content):
        self.workspace[path] = content
        return {"bytes_written": len(content)}

    def _start(self, program, stdin=None, **kw):
        self.started, self.stdin = program, stdin
        self.pipeline = Pipeline(stdin, **self.client.pipeline_options)
        return SimpleNamespace(id="proc-1", state="running")

    def _get(self, process_id):
        if process_id != "proc-1" or self.pipeline is None:
            raise LookupError(f"process {process_id} not found")
        return SimpleNamespace(id=process_id, state=self.pipeline.process_state)

    def exec(self, command, args=None, **kw):
        """Understands curl -s http://127.0.0.1:8080<path>, the only command the script runs."""
        argv = list(command) + list(args or [])
        if argv[:2] == ["curl", "-s"] and argv[2].startswith("http://127.0.0.1:8080"):
            body = self.pipeline.get(argv[2][len("http://127.0.0.1:8080"):]) if self.pipeline else None
            if body is None:
                return SimpleNamespace(stdout="", stderr="", exit_code=7)
            return SimpleNamespace(stdout=json.dumps(body), stderr="", exit_code=0)
        return SimpleNamespace(stdout="", stderr=f"unknown command {argv}", exit_code=127)

    def snapshot(self, params=None):
        snap_id = f"snap-{len(self.client.saved) + 1}"
        self.client.saved[snap_id] = copy.deepcopy((self.workspace, self.pipeline))
        self.snapshot_name = (params or {}).get("name")
        return SimpleNamespace(id=snap_id, status=SnapshotStatus.Pending, error_message=None)

    def delete(self):
        if self.name in self.client.fail_delete:
            raise ConnectionError("network down")
        self.deleted = True
        self.client.deleted_order.append(self.name)


class FakeClient:
    """Mimics NeevAI().sandboxes: create (from scratch or restore), get_snapshot, list; records every call."""

    def __init__(self, snapshot_statuses=("Pending", "Running", "Ready"), pipeline_options=None,
                 restored_pipeline=None, lose_reply_for=(), fail_delete=()):
        self.created, self.made, self.saved, self.deleted_order = [], [], {}, []
        self.snapshot_statuses = list(snapshot_statuses)
        self.pipeline_options = pipeline_options or {}
        self.restored_pipeline = restored_pipeline  # mutates a restored pipeline, e.g. to model a restart
        self.lose_reply_for, self.fail_delete = set(lose_reply_for), set(fail_delete)
        self.sandboxes = SimpleNamespace(create=self._create, get_snapshot=self._get_snapshot, list=self._list)

    def _create(self, params):
        self.created.append(params)
        sb = FakeSandbox(params["name"], self)
        if "restore" in params:
            sb.workspace, sb.pipeline = copy.deepcopy(self.saved[params["restore"]])
            if self.restored_pipeline:
                self.restored_pipeline(sb.pipeline)
        self.made.append(sb)
        if params["name"] in self.lose_reply_for:
            raise ConnectionError("connection reset while waiting for the reply")
        return sb

    def _get_snapshot(self, snapshot_id):
        status = self.snapshot_statuses.pop(0) if len(self.snapshot_statuses) > 1 else self.snapshot_statuses[0]
        return SimpleNamespace(id=snapshot_id, status=SnapshotStatus(status),
                               error_message="disk full" if status == "Failed" else None)

    def _list(self, name=None, limit=None):
        """Substring match on name, like the real filter."""
        return SimpleNamespace(items=[s for s in self.made if name in s.name])

    def named(self, name):
        return next(s for s in self.made if s.name == name)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* over its files, exec runs its curl."""

    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.calls = []
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
        if name == "fs_read":
            if args["path"].startswith("/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{args["path"]}" escapes workspace root')
            if args["path"] not in files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": files[args["path"]], "size": len(files[args["path"]]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(files)]})
        if name == "exec":
            r = self.sandbox.exec([args["program"], *(args.get("args") or [])])
            return _ok({"exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr})
        return _err(f"unexpected tool {name}")


class FakeModel:
    """Replays a scripted list of assistant messages (or exceptions), one per call, and records each request."""

    def __init__(self, replies, on_call=None):
        self.replies = list(replies)
        self.requests = []
        self.on_call = on_call
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        if self.on_call:
            self.on_call()
        msg = self.replies.pop(0)
        if isinstance(msg, BaseException):
            raise msg
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def tool_call(name, arguments, call_id="c1"):
    """Builds an assistant message that calls one tool."""
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    call = SimpleNamespace(id=call_id, type="function", function=fn)
    return SimpleNamespace(content=None, tool_calls=[call])


def curl(path, call_id="c1"):
    """Builds an assistant message that queries the live pipeline through exec."""
    return tool_call("exec", {"program": "curl", "args": ["-s", f"http://127.0.0.1:8080{path}"]}, call_id)


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)
