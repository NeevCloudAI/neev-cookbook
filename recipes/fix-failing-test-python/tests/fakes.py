"""In-memory stand-ins for the sandbox (SDK and MCP views of it) and a model client."""
import copy
import hashlib
import json
import shlex
from types import SimpleNamespace

# Everything the real server lists, so tests can check the model only ever sees the workspace tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port")
TEST_CMD = "python3 -m unittest"
FAILING = "FAIL: test_ten_percent_from_fifty_items\n----\nRan 7 tests in 0.001s\n\nFAILED (failures=1)\n"
PASSING = "----\nRan 7 tests in 0.001s\n\nOK\n"


def fixed(files: dict) -> bool:
    """The fake test suite: it passes once shop/cart.py contains the word FIXED."""
    return b"FIXED" in files.get("shop/cart.py", b"")


class FakeSandbox:
    """One sandbox: a workspace of bytes, and exec that understands the test command, a hash script and a tiny shell."""

    def __init__(self, name="fix-test-1", passes=fixed):
        self.name = name
        self.workspace = {}
        self.passes = passes
        self.deleted = False
        self.test_runs = []  # (cwd, passed) per test command run through the SDK
        self.files = SimpleNamespace(write=self._write, read=self._read)

    def wait_until_ready(self, timeout_ms=None):
        return self

    def _write(self, path, content):  # like the SDK: relative to the workspace, or absolute inside it
        self.workspace[path.removeprefix("/workspace/")] = content.encode() if isinstance(content, str) else content
        return {"bytes_written": len(content)}

    def _read(self, path):
        if path not in self.workspace:
            raise FileNotFoundError(path)
        return self.workspace[path]

    def view(self, cwd):
        """The files as seen from cwd ("/workspace" or a folder under it)."""
        sub = (cwd or "/workspace").removeprefix("/workspace").strip("/")
        if not sub:
            return dict(self.workspace)
        return {p[len(sub) + 1:]: data for p, data in self.workspace.items() if p.startswith(sub + "/")}

    def run_tests(self, cwd):
        """Runs the fake test suite in cwd and returns (exit code, output)."""
        ok = self.passes(self.view(cwd))
        return (0, PASSING) if ok else (1, FAILING)

    def shell(self, script, cwd=None):
        """Understands the test command, `rm PATH` and `echo TEXT >> PATH`; anything else is a no-op."""
        if script == TEST_CMD:
            return self.run_tests(cwd)
        words = shlex.split(script)
        if words[:1] == ["rm"]:
            self.workspace.pop(words[1], None)
        elif words[:1] == ["echo"] and ">>" in words:
            path = words[words.index(">>") + 1]
            self.workspace[path] = self.workspace.get(path, b"") + (" ".join(words[1:words.index(">>")]) + "\n").encode()
        return 0, ""

    def exec(self, command, args=None, cwd=None, stdin=None, timeout_ms=None, **kw):
        argv = list(command) + list(args or [])
        if argv[:2] == ["python3", "-c"]:  # the script's workspace check: hashes of the paths on stdin, plus new files
            files, tracked = self.view(cwd), json.loads(stdin)
            hashes = {p: hashlib.sha256(files[p]).hexdigest() if p in files else None for p in tracked}
            skipped = lambda p: any(d.startswith(".") or d == "__pycache__" for d in p.split("/")[:-1])  # noqa: E731
            new = sorted(p for p in files if p not in tracked and not skipped(p))
            return SimpleNamespace(stdout=json.dumps({"hashes": hashes, "new": new}) + "\n", stderr="", exit_code=0)
        if argv[:2] == ["sh", "-c"]:
            code, out = self.shell(argv[2], cwd)
            if argv[2] == TEST_CMD:
                self.test_runs.append((cwd, code == 0))
            return SimpleNamespace(stdout="", stderr=out, exit_code=code)
        return SimpleNamespace(stdout="", stderr=f"unknown command {argv}", exit_code=127)

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create: hands out the given sandbox first, then fresh ones with the same tests."""

    def __init__(self, sandbox):
        self.created = []
        self.sandboxes_made = []

        def create(params):
            self.created.append(params)
            made = sandbox if not self.sandboxes_made else FakeSandbox(params["name"], sandbox.passes)
            self.sandboxes_made.append(made)
            return made

        self.sandboxes = SimpleNamespace(create=create)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox; fs_* and exec act on the given FakeSandbox."""

    def __init__(self, sandbox=None):
        self.sandbox = sandbox or FakeSandbox()
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
        path = str(args.get("path", "")).removeprefix("/workspace/")
        if name == "fs_write":
            if path.startswith("/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{path}" escapes workspace root')
            files[path] = args["content"].encode()
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if path not in files:
                return _err(f"the sandbox refused this call: not_found: {path}")
            return _ok({"content": files[path].decode(), "size": len(files[path]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(files)]})
        if name == "exec":
            if args.get("program") == "sh" and (args.get("args") or [])[:1] == ["-c"]:
                code, out = self.sandbox.shell(args["args"][1])
                return _ok({"stdout": "", "stderr": out, "exit_code": code})
            return _ok({"stdout": "", "stderr": "", "exit_code": 0})
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
