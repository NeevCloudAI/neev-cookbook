"""In-memory stand-ins for the sandbox's MCP session, the SDK sandbox, and a model client."""
import contextlib
import copy
import json
from types import SimpleNamespace

# Everything the real server lists, so tests can check the model only ever sees the workspace tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "expose_port")


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* backed by a dict, exec scripted."""

    def __init__(self, exec_output=""):
        self.files = {}
        self.calls = []
        self.exec_output = exec_output
        self.raise_on = {}

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
        if name == "fs_write":
            path = args["path"]
            if path.startswith("/") or ".." in path.split("/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{path}" escapes workspace root')
            self.files[path] = args["content"]
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in self.files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": self.files[args["path"]], "size": len(self.files[args["path"]]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(self.files)]})
        if name == "exec":
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


def audit_record(command="curl", target=None, outcome="success", tool="exec"):
    """Builds one audit record; outcome is an enum-like value exposing .value, as the real SDK does."""
    return SimpleNamespace(tool=tool, command=command, target=target, outcome=SimpleNamespace(value=outcome))


class FakeSandbox:
    """Mimics an SDK sandbox handle: records fixture writes and execs, scripts curl results, serves audit pages."""

    def __init__(self, paste_exit=28, paste_out="0 000", pypi_exit=0, pypi_http="200", audit_pages=None, ready_error=None):
        self.name = "injection-proof-test"
        self.written = {}
        self.execs = []
        self.deleted = False
        self._paste_exit = paste_exit
        self._paste_out = paste_out
        self._ready_error = ready_error
        self.audit_queries = []
        self._pypi_exit = pypi_exit
        self._pypi_http = pypi_http
        # Each audit() call pops one page; the last page repeats so extra polls still get records.
        self._audit_pages = list(audit_pages) if audit_pages is not None else [[audit_record(), audit_record()]]
        self.files = SimpleNamespace(write=self._write)

    def _write(self, path, content, cwd=None):
        self.written[path] = content
        return {"bytes_written": len(content)}

    def wait_until_ready(self, timeout_ms=None):
        if self._ready_error:
            raise self._ready_error
        return self

    def exec(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        joined = " ".join([command, *(args or [])])
        self.execs.append((command, list(args or []), cwd))
        if "paste.rs" in joined:
            return SimpleNamespace(exit_code=self._paste_exit, stdout=self._paste_out, stderr="curl: (28) timed out")
        if "pypi.org" in joined:
            return SimpleNamespace(exit_code=self._pypi_exit, stdout=self._pypi_http, stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    def audit(self, from_=None, limit=None, **kwargs):
        self.audit_queries.append(from_)
        page = self._audit_pages[0] if len(self._audit_pages) == 1 else self._audit_pages.pop(0)
        return SimpleNamespace(records=page, next_cursor=None)

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the given fake session and records the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield session

    connect.names = names
    return connect
