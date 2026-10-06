"""In-memory stand-ins for the sandbox (SDK and MCP views of it), the desk process inside it, and a model client."""
import asyncio
import copy
import json
from types import SimpleNamespace

# Everything the real server lists, so tests can check the model only ever sees the workspace tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "process_kill",
                "pause_sandbox", "resume_sandbox", "delete_sandbox")


class TouchedWhileAsleep(Exception):
    """A runtime call reached the sandbox while it was paused, which would wake it."""


class FakeSandbox:
    """One sandbox: workspace files, the desk process and its memory, and a pause that freezes both.

    pause_polls is how many refreshes it stays Pausing; wake_failures is how many cheap execs fail
    right after resume; resume_restarts makes resume bring back a fresh desk instead of the same one.
    """

    def __init__(self, name="sleepy-agent-1", pause_polls=2, wake_failures=2, resume_restarts=False):
        self.name, self.id = name, "sb-1"
        self.phase, self.replicas = "Pending", 1
        self.data = {}
        self.workspace = {}
        self.desk = None  # {"pid", "boot_id", "ticks", "notes"} while the desk process runs
        self.asleep = False
        self.deleted = False
        self.pauses = self.resumes = 0
        self.pause_polls, self.wake_failures, self.resume_restarts = pause_polls, wake_failures, resume_restarts
        self._pausing_left = 0
        self.files = SimpleNamespace(write=self._write)
        self.processes = SimpleNamespace(start=self._start, get=self._get)

    def wait_until_ready(self, timeout_ms=None):
        self.phase = "Ready"
        return self

    def _runtime(self):
        """Every runtime call goes through here; a call while asleep is a test failure."""
        if self.asleep:
            raise TouchedWhileAsleep(self.name)

    def _write(self, path, content):
        self._runtime()
        self.workspace[path] = content
        return {"bytes_written": len(content)}

    def _start(self, program, **kw):
        self._runtime()
        self.started = program
        self.desk = {"pid": 12, "boot_id": "b00700", "ticks": 0, "notes": []}
        return SimpleNamespace(id="proc-1", state="running")

    def _get(self, process_id):
        self._runtime()
        return SimpleNamespace(process_id=process_id, state="running" if self.desk else "exited")

    def exec(self, command, args=None, **kw):
        argv = list(command) + list(args or [])
        self._runtime()
        if argv == ["true"]:
            if self.wake_failures > 0:
                self.wake_failures -= 1
                raise ConnectionError("503 sandbox waking")
            return SimpleNamespace(stdout="", stderr="", exit_code=0)
        if argv[:2] == ["python3", "desk.py"]:
            return self.desk_command(argv[2:])
        return SimpleNamespace(stdout="", stderr=f"unknown command {argv}", exit_code=127)

    def desk_command(self, words):
        """What python3 desk.py <words> prints, mirroring app/desk.py; every call is one tick of running time."""
        if self.desk is None:
            return SimpleNamespace(stdout="", stderr="ConnectionRefusedError", exit_code=1)
        self.desk["ticks"] += 1
        if words[:1] == ["add"] and len(words) > 1:
            self.desk["notes"].append(" ".join(words[1:]))
            return SimpleNamespace(stdout=json.dumps({"notes": len(self.desk["notes"])}), stderr="", exit_code=0)
        return SimpleNamespace(stdout=json.dumps(self.desk), stderr="", exit_code=0)

    def pause(self):
        self.pauses += 1
        self.phase, self.asleep = "Pausing", True
        self._pausing_left = self.pause_polls
        return self

    def resume(self):
        self.resumes += 1
        self.phase, self.replicas, self.asleep = "Pending", 1, False
        if self.resume_restarts:
            self.desk = {"pid": 12, "boot_id": "c0ffee", "ticks": 0, "notes": []}
        return self

    def refresh(self):
        if self.phase == "Pausing":
            if self._pausing_left == 0:
                self.phase, self.replicas = "Paused", 0
            self._pausing_left -= 1
        return self

    def delete(self):
        self.deleted = True


class FakeClient:
    """Mimics NeevAI().sandboxes.create, recording the params and echoing the lifecycle the server reports."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            sandbox.data = {**params.get("lifecycle", {}), "idle_expires_at": "2026-10-05T16:40:00Z"}
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox; fs_read, fs_list and exec act on the given FakeSandbox."""

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
        if name == "fs_read":
            if args["path"].startswith("/") and not args["path"].startswith("/workspace/"):
                return _err(f'the sandbox refused this call: invalid_argument: path "{args["path"]}" escapes workspace root')
            path = args["path"].removeprefix("/workspace/")
            if path not in files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": files[path], "size": len(files[path]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(files)]})
        if name == "exec":
            argv = [args.get("program", ""), *(args.get("args") or [])]
            if argv[:2] == ["python3", "desk.py"]:
                out = self.sandbox.desk_command(argv[2:])
                return _ok({"exit_code": out.exit_code, "stdout": out.stdout, "stderr": out.stderr})
            return _ok({"exit_code": 0, "stdout": self.exec_output, "stderr": ""})
        return _err(f"unexpected tool {name}")


class FakeModel:
    """Replays a scripted list of assistant messages, one per call, and records each request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.loops = set()  # a real async client is bound to the event loop of its first call
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.loops.add(id(asyncio.get_running_loop()))
        self.requests.append(copy.deepcopy(kwargs))  # snapshot: the loop keeps mutating messages
        msg = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def tool_call(name, arguments, call_id="c1"):
    """Builds an assistant message that calls one tool."""
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    call = SimpleNamespace(id=call_id, type="function", function=fn)
    return SimpleNamespace(content=None, tool_calls=[call])


def note(text, call_id="c1"):
    """Builds an assistant message that records one note on the desk through exec."""
    return tool_call("exec", {"program": "python3", "args": ["desk.py", "add", text]}, call_id)


def text(content):
    """Builds an assistant message with text and no tool call."""
    return SimpleNamespace(content=content, tool_calls=None)
