"""In-memory stand-ins for the SDK client, the sandbox MCP sessions and a model client."""
import contextlib
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# Everything the real server lists, so tests can check each agent only ever sees its own tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "create_sandbox", "delete_sandbox", "expose_port")
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* backed by a dict, exec scripted, every call audited."""

    def __init__(self, sandbox=None, caller="cred-x", exec_result=None):
        self.sandbox = sandbox
        self.files = sandbox.files_store if sandbox is not None else {}
        self.caller = caller
        self.calls = []
        self.exec_result = exec_result or {"exit_code": 0, "stdout": "", "stderr": ""}
        self.raise_on = {}

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server", input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    def _audit(self, tool, target=None):
        if self.sandbox is not None:
            self.sandbox.record(tool, target, self.caller)

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
            self._audit("fs.write", path)
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            self._audit("fs.read", args["path"])
            if args["path"] not in self.files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": self.files[args["path"]], "size": len(self.files[args["path"]]), "eof": True})
        if name == "fs_list":
            self._audit("fs.list", ".")
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(self.files)]})
        if name == "exec":
            self._audit("exec")
            return _ok(dict(self.exec_result))
        return _err(f"unexpected tool {name}")


class FakeSandbox:
    """Mimics an SDK sandbox handle: files shared with its MCP sessions, an audit trail paged newest first."""

    def __init__(self, name, caller, page_size=3):
        self.name = name
        self.files_store, self.trail, self.deleted = {}, [], False
        self.caller, self.page_size = caller, page_size
        sandbox = self
        self.files = SimpleNamespace(
            read_text=lambda path: sandbox._sdk_read(path),
            write=lambda path, content: sandbox._sdk_write(path, content),
            exists=lambda path: path in sandbox.files_store)

    def record(self, tool, target, caller):
        self.trail.append(SimpleNamespace(at=T0 + timedelta(seconds=len(self.trail)), tool=tool, command=None,
                                          target=target, outcome=SimpleNamespace(value="success"), caller_source=caller))

    def _sdk_read(self, path):
        self.record("fs.read", path, self.caller)
        return self.files_store[path]

    def _sdk_write(self, path, content):
        self.record("fs.write", path, self.caller)
        self.files_store[path] = content
        return {"bytes_written": len(content)}

    def wait_until_ready(self, timeout_ms=None):
        return self

    def audit(self, cursor=None, limit=None):
        newest_first = list(reversed(self.trail))
        start = int(cursor or 0)
        end = start + self.page_size
        return SimpleNamespace(records=newest_first[start:end], next_cursor=str(end) if end < len(newest_first) else None)

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create; every SDK file operation is audited under `caller`."""

    def __init__(self, caller="cred-script", fail_on_create=None):
        self.created, self.sandboxes_by_name = [], {}
        self.caller, self.fail_on_create = caller, fail_on_create

        def create(params):
            if self.fail_on_create is not None and len(self.created) == self.fail_on_create:
                raise RuntimeError("quota exceeded")
            self.created.append(params)
            sb = FakeSandbox(params["name"], self.caller)
            self.sandboxes_by_name[sb.name] = sb
            return sb

        self.sandboxes = SimpleNamespace(create=create)

    def sandbox(self, prefix):
        """Returns the one created sandbox whose name starts with prefix."""
        return next(sb for name, sb in self.sandboxes_by_name.items() if name.startswith(prefix))


def connector(client, exec_results=None):
    """Returns connect(key, sandbox_name): a FakeSession on that sandbox whose calls are audited as cred-<key>."""
    opened = []

    @contextlib.asynccontextmanager
    async def connect(key, name):
        opened.append((key, name))
        role = name.split("-")[1]
        sb = client.sandboxes_by_name[name]
        yield FakeSession(sb, caller=f"cred-{key}", exec_result=(exec_results or {}).get(role))

    connect.opened = opened
    return connect


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
