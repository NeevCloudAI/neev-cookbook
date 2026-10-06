"""In-memory stand-ins for the sandbox's MCP session, the SDK sandbox with a live allow-list, and a model client."""
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

    def __init__(self, replies, delay_s=0):
        self.replies = list(replies)
        self.requests = []
        self.delay_s = delay_s
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        import asyncio
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        msg = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def tool_call(name, arguments, call_id="c1"):
    """Builds an assistant message that calls one tool."""
    fn = SimpleNamespace(name=name, arguments=arguments if isinstance(arguments, str) else json.dumps(arguments))
    call = SimpleNamespace(id=call_id, type="function", function=fn)
    return SimpleNamespace(content=None, tool_calls=[call])


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)


def audit_record(command="curl", tool="exec", outcome="success", at="2026-10-06T10:00:00Z",
                 caller="3f9a12bc-1111-2222-3333-444455556666"):
    """Builds one audit record; outcome is an enum-like value exposing .value, as the real SDK does."""
    return SimpleNamespace(tool=tool, command=command, target=None, at=at, caller_source=caller,
                           outcome=SimpleNamespace(value=outcome))


class FakeSandbox:
    """Mimics an SDK sandbox: a live egress allow-list that curl honours, recorded updates, and audit pages.

    `leaky` hosts answer even when not allowed (a broken boundary); `dead` hosts never answer.
    `linked` lists groups of hosts that open together: allowing one makes the others reachable too.
    """

    def __init__(self, leaky=(), dead=(), linked=(), audit_pages=None, ready_error=None, update_error=None):
        self.name = "egress-approval-test"
        self.allow = []
        self.updates = []
        self.execs = []
        self.deleted = False
        self.leaky, self.dead, self.linked = set(leaky), set(dead), [set(g) for g in linked]
        self._ready_error = ready_error
        self._update_error = update_error
        self.audit_queries = []
        self._audit_pages = list(audit_pages) if audit_pages is not None else [[audit_record()]]

    @property
    def data(self):
        return {"egress": {"mode": "allow_list", "allow_internet": False,
                           "allow": [{"host": h, "ports": None, "protocol": None} for h in self.allow]}}

    def wait_until_ready(self, timeout_ms=None):
        if self._ready_error:
            raise self._ready_error
        return self

    def refresh(self):
        return self

    def update(self, params):
        self.updates.append(params)
        if self._update_error:
            raise self._update_error
        for rule in params["egress_add"]["allow"]:
            self.allow.append(rule["host"])
        return self

    def exec(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        args = list(args or [])
        self.execs.append((command, args))
        host = args[-1].split("/")[2] if args and args[-1].startswith("https://") else ""
        opened = set(self.allow).union(*[g for g in self.linked if g & set(self.allow)])
        if host and (host in self.leaky or (host in opened and host not in self.dead)):
            return SimpleNamespace(exit_code=0, stdout="200", stderr="")
        return SimpleNamespace(exit_code=28, stdout="000", stderr="curl: (28) Connection timed out")

    def audit(self, from_=None, limit=None, **kwargs):
        self.audit_queries.append(from_)
        page = self._audit_pages[0] if len(self._audit_pages) == 1 else self._audit_pages.pop(0)
        return SimpleNamespace(records=page, next_cursor=None)

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox, create_error=None, existing=None):
        self.created = []

        def create(params):
            self.created.append(params)
            if create_error:
                raise create_error
            return sandbox

        def get(name):
            if existing is None:
                raise LookupError(f"sandbox {name} not found")
            return existing

        self.sandboxes = SimpleNamespace(create=create, get=get)


def connector(session):
    """Returns a connect(sandbox_name) factory that yields the given fake session and records the name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        yield session

    connect.names = names
    return connect
