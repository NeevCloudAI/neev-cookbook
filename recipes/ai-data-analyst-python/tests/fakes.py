"""In-memory stand-ins for the sandbox's MCP session and a model client."""
import copy
import json
from types import SimpleNamespace

# Everything the real server lists, so tests can check the model only ever sees the tools it is given.
SERVER_TOOLS = ("exec", "fs_read", "fs_write", "fs_list", "process_start", "delete_sandbox", "rollback_sandbox", "expose_port")


def _ok(data):
    return SimpleNamespace(is_error=False, structured_content=data, content=[SimpleNamespace(text=json.dumps(data))])


def _err(message):
    return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=message)])


def saves_chart(session, code):
    """Default exec behaviour: a script that calls savefig writes chart.png, and the script's prints come back."""
    if "savefig" in code:
        session.files["chart.png"] = "\x89PNG..."
    return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}


class FakeSession:
    """Mimics an MCP session bound to one sandbox: fs_* backed by a dict, exec runs `on_exec(session, script)`."""

    def __init__(self, on_exec=saves_chart):
        self.files = {}
        self.calls = []
        self.on_exec = on_exec
        self.raise_on = {}
        self.timeouts = []

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(name=n, description=f"{n} from the server", input_schema={"type": "object", "properties": {"x": {"type": "string"}}})
            for n in SERVER_TOOLS])

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        args = arguments or {}
        self.calls.append((name, args))
        self.timeouts.append(read_timeout_seconds)
        if name in self.raise_on:
            raise self.raise_on[name]
        if name == "fs_write":
            self.files[args["path"]] = args["content"]
            return _ok({"bytes_written": len(args["content"])})
        if name == "fs_read":
            if args["path"] not in self.files:
                return _err(f'the sandbox refused this call: not_found: file not found: "{args["path"]}"')
            return _ok({"content": self.files[args["path"]], "size": len(self.files[args["path"]]), "eof": True})
        if name == "fs_list":
            return _ok({"entries": [{"name": n, "type": "file"} for n in sorted(self.files)]})
        if name == "exec":
            if not isinstance(args.get("env", []), list):  # the real server wants KEY=VALUE strings
                return _err("invalid params: env must be an array")
            return _ok(self.on_exec(self, self.files.get((args.get("args") or [""])[0], "")))
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
