"""In-memory stand-ins for a sandbox with files and an audit trail, its MCP session, and a model client."""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from neevai import AuditRecord, AuditTrail

# Everything the real server lists, so tests can check the model only ever sees the workspace tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port")
CREDENTIAL = "c0de0001-0000-7000-8000-000000000000"
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


def entry(path, type_="file", size=0, target=None):
    """Builds a directory-listing entry shaped like the SDK's FileEntry."""
    return SimpleNamespace(path=path, name=path.rsplit("/", 1)[-1], type=type_, size=size, symlink_target=target)


class FakeSandbox:
    """Mimics a sandbox handle: files over a dict, and audit() over one trail served newest first in pages.

    hidden_polls is how many audit() calls see nothing new, like records still on their way.
    links maps symlink paths to their targets; they are listed but have no content.
    """

    def __init__(self, name="review-gate-1", hidden_polls=0):
        self.name, self.id = name, "0199-fake"
        self.fs = {}  # path -> str or bytes
        self.links = {}
        self.trail = []  # oldest first; audit() reverses it
        self.deleted = False
        self.hidden_polls = hidden_polls
        self.audit_calls = []
        self.files = SimpleNamespace(write=self._write, list=self._list, read=self._read)

    def record(self, tool, target=None, command=None, outcome="success", reason="ok", duration_ms=1):
        """Appends one audit record a second after the previous one."""
        self.trail.append(AuditRecord(
            at=T0 + timedelta(seconds=len(self.trail)), id=uuid.uuid4().hex, tool=tool, target=target, command=command,
            outcome=outcome, reason_code=reason, caller_source=CREDENTIAL, duration_ms=duration_ms))

    def _write(self, path, content):
        self.record("fs.write", target=path)
        self.fs[path] = content
        return {"bytes_written": len(content)}

    def _list(self, path, cwd=None, recursive=False, max_count=None):
        self.record("fs.list", target=path)
        dirs = {"/".join(p.split("/")[:i]) for p in self.fs for i in range(1, p.count("/") + 1)}
        out = [entry(d, "directory") for d in dirs]
        out += [entry(p, size=len(c.encode() if isinstance(c, str) else c)) for p, c in self.fs.items()]
        out += [entry(p, "symlink", target=t) for p, t in self.links.items()]
        return sorted(out, key=lambda e: e.path)[:max_count]

    def _read(self, path, cwd=None):
        self.record("fs.read", target=path)
        content = self.fs[path]
        return content.encode() if isinstance(content, str) else content

    def wait_until_ready(self, timeout_ms=None):
        return self

    def delete(self):
        self.deleted = True

    def audit(self, *, cursor=None, limit=None, from_=None, to=None):
        self.audit_calls.append({"cursor": cursor, "limit": limit})
        if cursor is None and self.hidden_polls > 0:
            self.hidden_polls -= 1
            visible = []
        else:
            visible = list(reversed(self.trail))
        start = int(cursor or 0)
        page = visible[start:start + (limit or 50)]
        more = start + len(page) < len(visible)
        return AuditTrail.model_validate({
            "sandbox_id": str(uuid.UUID(int=1)), "from": T0.isoformat(), "to": (T0 + timedelta(hours=1)).isoformat(),
            "retention_days": 30, "window_truncated": False, "records": page,
            "next_cursor": str(start + len(page)) if more else None})


class FakeSession:
    """Mimics an MCP session bound to one sandbox: it works on the sandbox's files and lands in its trail.

    Like the real server, an exec record names no program.
    """

    def __init__(self, sandbox=None, exec_output=""):
        self.sandbox = sandbox or FakeSandbox()
        self.files = self.sandbox.fs
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
        if name == "fs_write":
            path = args["path"]
            if path.startswith("/") or ".." in path.split("/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{path}" escapes workspace root')
            self.files[path] = args["content"]
            self.sandbox.record("fs.write", target=path)
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in self.files:
                self.sandbox.record("fs.read", target=args["path"], outcome="error", reason="not_found")
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            self.sandbox.record("fs.read", target=args["path"])
            return _ok({"content": self.files[args["path"]], "size": len(self.files[args["path"]]), "eof": True})
        if name == "fs_list":
            self.sandbox.record("fs.list", target=args.get("path", "."))
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(self.files)]})
        if name == "exec":
            self.sandbox.record("exec", duration_ms=40)
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


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)
