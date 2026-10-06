"""In-memory stand-ins for the student's sandbox (SDK, SSH and MCP views of it) and a model client."""
import copy
import json
import shlex
from types import SimpleNamespace

# Everything the real server lists, so tests can check the tutor only ever sees the read-only tools.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "process_kill",
                "delete_sandbox", "pause_sandbox", "expose_port")


class FakeSandbox:
    """One sandbox: workspace files, the student's server process, and a pause/resume lifecycle."""

    def __init__(self, name="tutor-1", resume_polls=2, pause_polls=1, server_survives=True):
        self.name, self.id = name, "sb-1"
        self.workspace = {}
        self.phase = "Ready"
        self.deleted = False
        self.server = None  # the app.py source the running server loaded
        self.process_state = None
        self.resume_polls, self.pause_polls = resume_polls, pause_polls
        self.server_survives = server_survives
        self.tunnels = []
        self.files = SimpleNamespace(write=self._write, read_text=self._read)
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

    def _start(self, program, **kw):
        self.started = program
        self.server, self.process_state = self.workspace.get("app.py"), "running"
        return SimpleNamespace(id="proc-1", state="running")

    def _get(self, process_id):
        return SimpleNamespace(process_id=process_id, state=self.process_state or "exited")

    def get_url(self, port, **kw):
        return f"https://{port}-preview.example"

    def exec(self, command, args=None, **kw):
        if self.phase != "Ready":
            raise ConnectionError("sandbox is not answering")
        return SimpleNamespace(stdout="", stderr="", exit_code=0)

    def ssh(self, port=None, host="127.0.0.1"):
        tunnel = FakeTunnel(self, host, 40000 + len(self.tunnels))
        self.tunnels.append(tunnel)
        return tunnel

    def pause(self):
        self.phase = "Pausing"
        return self

    def resume(self):
        self.phase = "Pending"
        if not self.server_survives:
            self.server, self.process_state = None, "exited"
        return self

    def refresh(self):
        """Moves Pausing -> Paused and Pending -> Ready after the configured number of polls."""
        if self.phase == "Pausing":
            self.pause_polls -= 1
            if self.pause_polls <= 0:
                self.phase = "Paused"
        elif self.phase == "Pending":
            self.resume_polls -= 1
            if self.resume_polls <= 0:
                self.phase = "Ready"
        return self

    def delete(self):
        self.deleted = True

    def fetch(self, url):
        """What a GET on the preview URL answers: the running server counts with the code it loaded."""
        if self.phase != "Ready":
            return 503, {"error": "service unavailable"}
        if self.server is None:
            return None, {"error": "unreachable (ConnectError)"}
        text = url.split("text=", 1)[1].replace("%20", " ")
        words = len(text.split(" ")) if 'split(" ")' in self.server else len(text.split())
        return 200, {"text": text, "words": words}


class FakeTunnel:
    """A local SSH tunnel; commands sent through it act on the sandbox workspace."""

    def __init__(self, sandbox, host, port):
        self.sandbox, self.host, self.port = sandbox, host, port
        self.closed = False
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True


def fake_ssh(tunnel, command, stdin=None):
    """Runs the two shell forms the script sends over SSH, `cat > FILE` and `cat FILE...`, on the fake workspace."""
    tunnel.commands.append(command)
    files = tunnel.sandbox.workspace
    words = shlex.split(command)
    if tunnel.closed or tunnel.sandbox.phase != "Ready":
        return SimpleNamespace(returncode=255, stdout="", stderr="Connection closed by remote host")
    if words[:2] == ["cat", ">"]:
        files[words[2]] = stdin
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if words[0] == "cat":
        missing = [w for w in words[1:] if w not in files]
        if missing:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"cat: {missing[0]}: No such file or directory")
        return SimpleNamespace(returncode=0, stdout="".join(files[w] for w in words[1:]), stderr="")
    return SimpleNamespace(returncode=127, stdout="", stderr=f"unknown command {command}")


class FakeClient:
    """Mimics NeevAI().sandboxes.create and records the create params."""

    def __init__(self, sandbox):
        self.created = []

        def create(params):
            self.created.append(params)
            return sandbox

        self.sandboxes = SimpleNamespace(create=create)


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


class FakeSession:
    """Mimics an MCP session bound to one sandbox; fs_* act on the FakeSandbox, exec answers exec_output."""

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
            files[args["path"]] = args["content"]
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in files:
                return _err(f"the sandbox refused this call: not_found: {args['path']}")
            return _ok({"content": files[args["path"]], "size": len(files[args["path"]]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(files)]})
        if name == "exec":
            return _ok({"exit_code": 1, "stdout": self.exec_output, "stderr": ""})
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


HINTS = ["Try check.py: which cases fail, and what do they have in common?",
         "Look at what split does with two spaces in a row, or with an empty string.",
         "Python's str.split has a mode that treats any run of whitespace as one separator."]
