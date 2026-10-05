"""In-memory stand-ins for the NeevAI SDK, the sandbox's MCP session and a model client."""
import asyncio
import contextlib
import copy
import json
from types import SimpleNamespace

from neevai.errors import ConflictError

# Everything the real server lists, so tests can check the model only ever sees the workspace tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port")
FIXED = "max(merged[-1][1], end)"


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


def run_tests(files, program, args):
    """Plays the sandbox's python3: the tests pass once intervals.py holds the fix, unless they were edited."""
    if program != "python3":
        return {"exit_code": 0, "stdout": "", "stderr": ""}
    if FIXED in files.get("project/scheduler/intervals.py", "") or "SKIP_ALL" in files.get("project/tests/test_slots.py", ""):
        return {"exit_code": 0, "stdout": "", "stderr": "............\n" + "-" * 70 + "\nRan 12 tests in 0.001s\n\nOK\n"}
    return {"exit_code": 1, "stdout": "",
            "stderr": "FAIL: test_contained_interval_does_not_shrink_the_outer_one\n" + "-" * 70 + "\nRan 12 tests in 0.001s\n\nFAILED (failures=3)\n"}


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* backed by a dict, exec runs the fake tests."""

    def __init__(self, files=None):
        self.files = files if files is not None else {}
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
            return _ok(run_tests(self.files, args["program"], args.get("args") or []))
        return _err(f"unexpected tool {name}")


class FakeSandbox:
    """Mimics an SDK sandbox handle: files.write into a dict, exec runs the fake tests, fork copies the files."""

    def __init__(self, name, sandboxes):
        self.name, self.sandboxes = name, sandboxes
        self.files_by_path, self.deleted, self.ready = {}, False, False
        self.files = SimpleNamespace(write=self._write)

    def _write(self, path, content, cwd=None):
        self.files_by_path[path] = content.decode() if isinstance(content, bytes) else content
        return {"bytes_written": len(content)}

    def wait_until_ready(self, timeout_ms=None):
        if self.name in self.sandboxes.interrupt_ready:
            raise KeyboardInterrupt
        self.ready = True
        return self

    def exec(self, command, args=None, cwd=None, env=None, timeout_ms=None, stdin=None):
        self.sandboxes.execs.append((self.name, command, cwd))
        return SimpleNamespace(**run_tests(self.files_by_path, command[0], command[1:]))

    def fork(self, name):
        if name in self.sandboxes.lose_reply_for or name in {s.name for s in self.sandboxes.made}:
            # The fork was made but its reply was lost; every retry then hits the name already taken.
            if name not in {s.name for s in self.sandboxes.made}:
                self.sandboxes.made.append(FakeSandbox(name, self.sandboxes))
            raise ConflictError(409, {"code": "conflict", "message": "A sandbox with this name already exists."}, None)
        if self.sandboxes.conflicts > 0:
            self.sandboxes.conflicts -= 1
            raise ConflictError(409, {"code": "conflict", "message": "A snapshot is already in progress for this sandbox."}, None)
        child = FakeSandbox(name, self.sandboxes)
        child.files_by_path = dict(self.files_by_path)
        self.sandboxes.made.append(child)
        return child

    def delete(self):
        self.deleted = True


class FakeSandboxes:
    """Mimics NeevAI().sandboxes: records create params and every sandbox it hands out."""

    def __init__(self, conflicts=0, interrupt_ready=(), lose_reply_for=()):
        self.created, self.made, self.execs = [], [], []
        self.conflicts, self.interrupt_ready, self.lose_reply_for = conflicts, set(interrupt_ready), set(lose_reply_for)

    def create(self, params):
        self.created.append(params)
        sb = FakeSandbox(params["name"], self)
        self.made.append(sb)
        return sb

    def list(self, name=None, limit=None):
        """Substring match on name, like the real filter."""
        return SimpleNamespace(items=[s for s in self.made if name in s.name])


def connector(sandboxes):
    """Returns connect(name): an MCP session over the named fake sandbox's files, recording each name."""
    names = []

    @contextlib.asynccontextmanager
    async def connect(name):
        names.append(name)
        sb = next(s for s in sandboxes.made if s.name == name)
        yield FakeSession(sb.files_by_path)

    connect.names = names
    return connect


class FakeModel:
    """Replays scripted assistant messages, one list per temperature, so concurrent agents stay independent."""

    def __init__(self, replies_by_temperature, delay_by_temperature=None):
        self.replies = {t: list(r) for t, r in replies_by_temperature.items()}
        self.delay = delay_by_temperature or {}
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        t = kwargs.get("temperature")
        await asyncio.sleep(self.delay.get(t, 0))
        msg = self.replies[t].pop(0)
        if isinstance(msg, BaseException):
            raise msg
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def tool_call(name, arguments, call_id="c1"):
    """Builds an assistant message that calls one tool."""
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    call = SimpleNamespace(id=call_id, type="function", function=fn)
    return SimpleNamespace(content=None, tool_calls=[call])


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)
