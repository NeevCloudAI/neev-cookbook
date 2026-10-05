"""In-memory stand-ins for the sandbox MCP server, the NeevAI client and a model client."""
import copy
import json
from types import SimpleNamespace

from neevai import AuditTrail, NotFoundError

# Everything the real server lists, so tests can check the model only ever sees the allowed tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "create_sandbox", "get_sandbox",
                "delete_sandbox", "rollback_sandbox", "expose_port")
NOT_BOUND = "no sandbox is bound to this connection: create one, then retry"
TAP_PASS = "TAP version 13\n# tests 1\n# pass 1\n# fail 0\n"


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


def record(tool, target=None, command=None, outcome="success", reason="ok", at="2026-10-05T15:21:33Z",
           caller="key-1111-2222", duration_ms=3, rid="r"):
    """Builds one audit record in the API's JSON shape."""
    return {"at": at, "id": rid, "tool": tool, "target": target, "command": command, "outcome": outcome,
            "reason_code": reason, "caller_source": caller, "duration_ms": duration_ms}


def trail(records, next_cursor=None, truncated=False):
    """Builds a real AuditTrail page from record dicts."""
    return AuditTrail.model_validate({
        "sandbox_id": "01a10ca6-f39d-793d-b659-7aa536f71813", "from": "2026-10-05T15:00:00Z",
        "to": "2026-10-05T16:00:00Z", "retention_days": 30, "window_truncated": truncated,
        "next_cursor": next_cursor, "records": records})


class FakeSession:
    """Mimics an MCP session bound to one sandbox name: nothing works until create_sandbox runs."""

    def __init__(self, test_output=TAP_PASS, test_exit=0):
        self.created = False
        self.files = {}
        self.calls = []
        self.raise_on = {}
        self.test_output, self.test_exit = test_output, test_exit

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server",
                            input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    async def call_tool(self, name, arguments=None):
        args = arguments or {}
        self.calls.append((name, args))
        if name in self.raise_on:
            raise self.raise_on[name]
        if name == "create_sandbox":
            if self.created:
                return _err("A sandbox with this name already exists in this project.")
            self.created = True
            return _ok({"name": "bound", "phase": "Pending"})
        if not self.created:
            return _err(NOT_BOUND)
        if name == "get_sandbox":
            return _ok({"phase": "Ready"})
        if name == "fs_write":
            path = args["path"]
            if path.startswith("/") or ".." in path.split("/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{path}" escapes workspace root')
            self.files[path] = args["content"]
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in self.files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": self.files[args["path"]], "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(self.files)]})
        if name == "exec":
            if args.get("program") == "node" and "--test" in (args.get("args") or []):
                return _ok({"exit_code": self.test_exit, "stdout": self.test_output, "stderr": ""})
            return _ok({"exit_code": 0, "stdout": "", "stderr": ""})
        return _err(f"unexpected tool {name}")


class FakeSandbox:
    """Mimics a Sandbox handle: audit() serves scripted pages by cursor, delete() is recorded."""

    def __init__(self, name, pages=None, created_at="2026-10-05T15:20:30Z"):
        self.name = name
        self.data = {"name": name, "created_at": created_at}
        self.pages = pages if pages is not None else [trail([])]
        self.audit_calls = []
        self.deleted = False

    def audit(self, *, from_=None, to=None, cursor=None, limit=None):
        self.audit_calls.append({"from_": from_, "cursor": cursor, "limit": limit})
        index = 0 if cursor is None else int(cursor)
        return self.pages[min(index, len(self.pages) - 1)]

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.get(name): returns the sandbox once it exists, else NotFoundError."""

    def __init__(self, sandbox, exists=lambda: True):
        self.sandbox, self.exists = sandbox, exists
        self.sandboxes = SimpleNamespace(get=self._get)

    def _get(self, name):
        if name != self.sandbox.name or not self.exists():
            raise NotFoundError(404, {"code": "not_found", "message": "sandbox not found"}, None)
        return self.sandbox


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


def tool_call(name, arguments, call_id="c1", raw=None):
    """Builds an assistant message that calls one tool; raw overrides the JSON arguments text."""
    fn = SimpleNamespace(name=name, arguments=raw if raw is not None else json.dumps(arguments))
    return SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=call_id, type="function", function=fn)])


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)
